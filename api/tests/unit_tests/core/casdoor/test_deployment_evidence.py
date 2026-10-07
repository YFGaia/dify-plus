"""Synthetic audit/signing fixtures only; these never establish actual G0 evidence."""

import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CasdoorCrypto
from core.casdoor.deployment_evidence import (
    DEPLOYMENT_CAPABILITIES,
    MAX_EVIDENCE_BYTES,
    DeploymentEvidenceError,
    accept_deployment_evidence,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from models.account import Tenant
from models.base import Base
from models.casdoor_extend import CasdoorConfigRevisionExtend
from pydantic import SecretStr
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationRepository
from sqlalchemy.orm import Session

NOW = datetime(2026, 10, 5, 1, tzinfo=UTC)
NAMESPACE = UUID("10000000-0000-0000-0000-000000000001")
REVISION = UUID("20000000-0000-0000-0000-000000000001")


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def canonical(data):
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def synthetic_artifact():
    configuration = CasdoorConfiguration(
        browser_frontend_url="https://browser.synthetic.test",
        backend_api_url="https://api.synthetic.test",
        expected_issuer="https://issuer.synthetic.test",
        organization="synthetic-org",
        application="synthetic-app",
        client_id="synthetic-client",
        default_workspace_id=uuid4(),
    )
    binding = {
        "namespace_id": str(NAMESPACE),
        "revision_id": str(REVISION),
        "config_digest": configuration.config_digest(),
        "configuration_digest": configuration.config_digest(),
        "rbac_mode": "off",
        "deployment_edition": "COMMUNITY",
        **{
            name: getattr(configuration, name)
            for name in (
                "browser_frontend_url",
                "backend_api_url",
                "expected_issuer",
                "organization",
                "application",
                "client_id",
            )
        },
    }
    # Deliberately synthetic despite modeling the real-review wire shape.
    manifest = {
        "schema_version": 1,
        "authority_id": "synthetic-authority",
        "reviewed_source": "actual_deployment",
        "binding": binding,
        "issued_at": "2026-10-05T00:59:00Z",
        "expires_at": "2026-10-05T01:10:00Z",
        "casdoor_release": "synthetic-v1",
        "image_digest": "sha256:" + "a" * 64,
        "native_token_schema": "flat_user_v1",
        "directory_schema": "flat_directory_v1",
        "directory_authentication": "basic_header",
        "role_effects_profile": "community_local_v1",
        "evidence": {
            name: {"record_id": "synthetic/" + name, "sha256": hashlib.sha256(name.encode()).hexdigest()}
            for name in (
                "release",
                "image",
                "non_dcr_creation",
                "organization_admin_scope",
                "directory_visibility",
                "directory_schema",
                "native_token_layout",
                "directory_authentication",
                "local_role_effects",
                "pkce_s256_enforcement",
                "id_token_contract",
                "nonce_contract",
                "userinfo_subject_contract",
            )
        },
    }
    return configuration, Ed25519PrivateKey.generate(), manifest


def documents(key, manifest):
    payload = canonical(manifest)
    authority = {
        "schema_version": 1,
        "authority_id": "synthetic-authority",
        "public_key": b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
        "accepted_manifest_sha256": hashlib.sha256(payload).hexdigest(),
    }
    envelope = {"manifest": manifest, "signature": b64(key.sign(payload))}
    return canonical(authority), canonical(envelope)


def accepted(configuration, authority, envelope, **overrides):
    values = {
        "authority_document": authority,
        "envelope_document": envelope,
        "configuration": configuration,
        "namespace_id": NAMESPACE,
        "revision_id": REVISION,
        "config_digest": configuration.config_digest(),
        "rbac_mode": "off",
        "deployment_edition": "COMMUNITY",
        "now": NOW,
    }
    values.update(overrides)
    return accept_deployment_evidence(**values)


def assert_invalid(callback):
    with pytest.raises(DeploymentEvidenceError, match="^deployment_proof_invalid$") as error:
        callback()
    assert error.value.reason == "deployment_proof_invalid"
    assert error.value.code.value == "config_conflict"
    assert error.value.__cause__ is None


def test_signed_review_produces_existing_contracts_and_no_other_validation_kinds():
    config, key, manifest = synthetic_artifact()
    authority, envelope = documents(key, manifest)
    policy = accepted(config, authority, envelope)
    assert policy.directory_deployment_proof.matches(config)
    assert policy.directory_snapshot_contract.deployment_proof is policy.directory_deployment_proof
    assert policy.native_token_contract.schema.value == "flat_user_v1"
    assert (
        policy.native_token_contract.schema_proof_fingerprint == manifest["evidence"]["native_token_layout"]["sha256"]
    )
    assert policy.deployment_capabilities == DEPLOYMENT_CAPABILITIES
    assert "pkce_s256" not in policy.deployment_capabilities
    assert "role_graph_complete" not in policy.deployment_capabilities
    assert policy.evidence_references.pkce_s256_enforcement.record_id == "synthetic/pkce_s256_enforcement"
    assert repr(policy) == "AcceptedDeploymentPolicy(<redacted>)"
    with pytest.raises(FrozenInstanceError):
        policy.proof_fingerprint = "b" * 64
    with pytest.raises(ValueError):
        policy.evidence_references.release.sha256 = "b" * 64


@pytest.mark.parametrize(
    "change",
    [
        {"namespace_id": uuid4()},
        {"revision_id": uuid4()},
        {"config_digest": "0" * 64},
        {"rbac_mode": "on"},
        {"deployment_edition": "ENTERPRISE"},
        {"deployment_edition": "CLOUD"},
        {"now": NOW + timedelta(minutes=10)},
        {"now": NOW - timedelta(minutes=2)},
        {"now": NOW.replace(tzinfo=None)},
    ],
)
def test_exact_binding_and_half_open_window(change):
    config, key, manifest = synthetic_artifact()
    authority, envelope = documents(key, manifest)
    assert_invalid(lambda: accepted(config, authority, envelope, **change))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend_api_url", "https://elsewhere.synthetic.test"),
        ("browser_frontend_url", "https://elsewhere.synthetic.test"),
        ("expected_issuer", "https://elsewhere.synthetic.test"),
        ("organization", "other-org"),
        ("application", "other-app"),
        ("client_id", "other-client"),
        ("name_sync", "managed"),
    ],
)
def test_configuration_change_cannot_reuse_a_signed_revision(field, value):
    config, key, manifest = synthetic_artifact()
    authority, envelope = documents(key, manifest)
    changed = CasdoorConfiguration.model_validate({**config.model_dump(), field: value})
    assert_invalid(lambda: accepted(changed, authority, envelope))


def test_repin_alone_does_not_authenticate_a_forged_review():
    config, key, manifest = synthetic_artifact()
    authority, envelope = documents(key, manifest)
    tampered = json.loads(envelope)
    tampered["manifest"]["casdoor_release"] = "forged-release"
    repinned = json.loads(authority)
    repinned["accepted_manifest_sha256"] = hashlib.sha256(canonical(tampered["manifest"])).hexdigest()
    assert_invalid(lambda: accepted(config, canonical(repinned), canonical(tampered)))


def test_different_authority_and_removed_pin_reject_old_signed_review():
    config, key, manifest = synthetic_artifact()
    authority, envelope = documents(key, manifest)
    other_authority, _ = documents(Ed25519PrivateKey.generate(), manifest)
    assert_invalid(lambda: accepted(config, other_authority, envelope))
    revoked = json.loads(authority)
    del revoked["accepted_manifest_sha256"]
    assert_invalid(lambda: accepted(config, canonical(revoked), envelope))
    revoked["accepted_manifest_sha256"] = "0" * 64
    assert_invalid(lambda: accepted(config, canonical(revoked), envelope))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewed_source", "offline_fixture"),
        ("native_token_schema", "unknown"),
        ("directory_authentication", "query"),
        ("directory_schema", "unknown"),
        ("role_effects_profile", "enterprise_remote"),
        ("schema_version", True),
        ("expires_at", "2026-10-05T00:58:00Z"),
        ("issued_at", 1791161940),
        ("client_secret", "synthetic-private-canary"),
        ("access_token", "synthetic-private-canary"),
        ("code", "synthetic-private-canary"),
        ("passed", True),
        ("directory_users", []),
    ],
)
def test_even_validly_signed_unknown_or_secret_bearing_profiles_are_rejected(field, value):
    config, key, manifest = synthetic_artifact()
    manifest[field] = value
    authority, envelope = documents(key, manifest)
    assert_invalid(lambda: accepted(config, authority, envelope))


def test_every_review_reference_is_required_and_references_never_contain_raw_secret_or_pii():
    config, key, original = synthetic_artifact()
    for name in original["evidence"]:
        manifest = deepcopy(original)
        del manifest["evidence"][name]
        authority, envelope = documents(key, manifest)
        assert_invalid(lambda authority=authority, envelope=envelope: accepted(config, authority, envelope))
    manifest = deepcopy(original)
    manifest["evidence"]["release"]["record_id"] = "https://example.test/?secret=synthetic-private-canary"
    authority, envelope = documents(key, manifest)
    assert_invalid(lambda: accepted(config, authority, envelope))
    manifest = deepcopy(original)
    manifest["evidence"]["native_token_layout"]["email"] = "person@example.test"
    authority, envelope = documents(key, manifest)
    assert_invalid(lambda: accepted(config, authority, envelope))


def test_duplicate_unknown_and_oversized_documents_have_only_safe_errors():
    config, key, manifest = synthetic_artifact()
    authority, envelope = documents(key, manifest)
    duplicate = authority[:-1] + b',"authority_id":"synthetic-private-canary"}'
    for document in (duplicate, b"{", b"{}", b"[[]]", b"x" * (MAX_EVIDENCE_BYTES + 1), b"\xff"):
        assert_invalid(lambda document=document: accepted(config, document, envelope))
    extra = json.loads(envelope)
    extra["client_secret"] = "synthetic-private-canary"
    assert_invalid(lambda: accepted(config, authority, canonical(extra)))


def test_strategy_uses_short_lived_gateway_secret_and_retains_none():
    config, key, manifest = synthetic_artifact()
    authority, envelope = documents(key, manifest)
    strategy = accepted(config, authority, envelope).credential_strategy
    header = strategy.authorization(config.client_id, "synthetic-secret")
    assert base64.b64decode(header.removeprefix("Basic ")) == b"synthetic-client:synthetic-secret"
    assert "synthetic-secret" not in repr(strategy)
    assert set(vars(strategy)) == {"proof"}
    assert_invalid(lambda: strategy.authorization("wrong-client", "synthetic-secret"))
    assert_invalid(lambda: strategy.authorization(config.client_id, "bad\r\nvalue"))


def test_actual_saved_revision_digest_is_bound_separately_from_public_configuration_digest():
    config, key, manifest = synthetic_artifact()
    engine = sa.create_engine("sqlite://")
    tables = [Tenant.__table__] + [table for table in Base.metadata.sorted_tables if table.name.startswith("casdoor_")]
    Base.metadata.create_all(engine, tables=tables)
    try:
        with Session(engine) as session, session.begin():
            workspace = Tenant(name="Synthetic deployment policy workspace")
            session.add(workspace)
            session.flush()
            config = CasdoorConfiguration.model_validate({**config.model_dump(), "default_workspace_id": workspace.id})
            owner = CasdoorConfigurationRepository(
                session, crypto=CasdoorCrypto(secret_key="synthetic-server-key", key_version="v1"), rbac_enabled=False
            )
            saved = owner.save_draft(
                config, etag=0, actor_account_id=uuid4(), secret=SecretStr("synthetic-revision-secret")
            )
            revision = session.get(CasdoorConfigRevisionExtend, str(saved.draft_revision_id))
            assert revision is not None
            assert revision.config_digest != config.config_digest()
            manifest["binding"].update(
                namespace_id=revision.namespace_id,
                revision_id=revision.id,
                config_digest=revision.config_digest,
                configuration_digest=config.config_digest(),
            )
            authority, envelope = documents(key, manifest)
            policy = accepted(
                config,
                authority,
                envelope,
                namespace_id=UUID(revision.namespace_id),
                revision_id=UUID(revision.id),
                config_digest=revision.config_digest,
            )
            assert policy.binding.config_digest == revision.config_digest
            assert policy.binding.configuration_digest == config.config_digest()
            second = owner.save_draft(config, etag=1, actor_account_id=uuid4())
            next_revision = session.get(CasdoorConfigRevisionExtend, str(second.draft_revision_id))
            assert next_revision.config_digest != revision.config_digest
            assert_invalid(
                lambda: accepted(
                    config,
                    authority,
                    envelope,
                    namespace_id=UUID(next_revision.namespace_id),
                    revision_id=UUID(next_revision.id),
                    config_digest=next_revision.config_digest,
                )
            )
    finally:
        engine.dispose()


def test_authority_controls_explicit_longer_review_window():
    config, key, manifest = synthetic_artifact()
    manifest["expires_at"] = "2026-11-05T01:10:00Z"
    authority, envelope = documents(key, manifest)
    policy = accepted(config, authority, envelope, now=NOW + timedelta(days=2))
    assert policy.expires_at == datetime(2026, 11, 5, 1, 10, tzinfo=UTC)
