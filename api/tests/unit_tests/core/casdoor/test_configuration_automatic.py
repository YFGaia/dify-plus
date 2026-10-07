"""Automatic revisions preserve legacy state and require current diagnostic keys.

All proof rows and RSA material here are synthetic offline fixtures.
"""

import base64
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.signing_keys import SigningKeyTrustStore
from cryptography.hazmat.primitives.asymmetric import rsa
from models.casdoor_extend import CasdoorConfigRevisionExtend, CasdoorValidationExtend, CasdoorValidationKind
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError, required_capabilities
from tests.unit_tests.core.casdoor import test_configuration_repository_extend as foundation

storage = foundation.storage
pin = foundation.pin
NOW = foundation.NOW
ACTOR = foundation.ACTOR


def automatic(workspace):
    return foundation.config(workspace, schema_version=2, signing_key_mode="automatic")


def make_keys():
    numbers = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_numbers()

    def encoded(number):
        return base64.urlsafe_b64encode(number.to_bytes((number.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    return SigningKeyTrustStore.from_jwks({"keys": [{"kty": "RSA", "n": encoded(numbers.n), "e": encoded(numbers.e)}]})


@pytest.fixture(scope="module")
def keys():
    return make_keys()


def proof_rows(session, revision, keys):
    for kind in (CasdoorValidationKind.STATIC, CasdoorValidationKind.PROTOCOL, CasdoorValidationKind.DIAGNOSTIC):
        summary = {
            "schema_version": 1,
            "configuration_schema_version": 2,
            "namespace_id": revision.namespace_id,
            "evidence_source": "real",
            "capabilities": dict.fromkeys(required_capabilities(kind, 2), "passed"),
        }
        if kind is CasdoorValidationKind.PROTOCOL:
            summary["signing_keys"] = {
                "source": "https://synthetic.example.test/api/.well-known/jwks",
                "profile": "global",
                "fingerprints": list(keys.fingerprints),
            }
        session.add(
            CasdoorValidationExtend(
                revision_id=revision.id,
                config_digest=revision.config_digest,
                kind=kind,
                status="passed",
                rbac_mode="off",
                proof_fingerprint=foundation.PROOF,
                summary_json=json.dumps(summary),
                correlation_id=str(uuid4()),
                checked_at=NOW.replace(tzinfo=None),
                expires_at=(NOW + timedelta(minutes=15)).replace(tzinfo=None),
            )
        )
    session.flush()


def snapshot_for(revision, configuration, keys):
    from core.casdoor.signing_keys import SigningKeySnapshot

    return SigningKeySnapshot(
        trust_store=keys,
        fingerprints=keys.fingerprints,
        source_url="https://synthetic.example.test/api/.well-known/jwks",
        profile="global",
        fetched_at=NOW.timestamp(),
        namespace_id=UUID(revision.namespace_id),
        revision_id=UUID(revision.id),
        config_digest=configuration.config_digest(),
    )


def test_v1_canonical_digest_does_not_gain_automatic_mode():
    configuration = foundation.config(str(uuid4()))
    previous = configuration.model_dump(mode="json")
    previous.pop("signing_key_mode")
    serialized = json.dumps(previous, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert configuration.canonical_json() == serialized
    assert configuration.config_digest() == hashlib.sha256(serialized.encode()).hexdigest()


def test_automatic_roundtrip_static_no_certificates(storage):
    session, workspace = storage
    saved = foundation.save(session, automatic(workspace))
    revision = session.get(CasdoorConfigRevisionExtend, str(saved.draft_revision_id))
    assert revision.schema_version == 2
    assert revision.certificates_json == "[]"
    assert json.loads(revision.policy_json)["signing_key_mode"] == "automatic"
    checked = foundation.repo(session).validate_static(etag=saved.etag, revision_id=saved.draft_revision_id, now=NOW)
    assert checked.certificates == ()
    assert "certificate_trust" not in required_capabilities(CasdoorValidationKind.STATIC, 2)
    with pytest.raises(CasdoorConfigurationError) as caught:
        foundation.repo(session).activate(
            etag=saved.etag, revision_id=saved.draft_revision_id, actor_account_id=ACTOR, now=NOW
        )
    assert caught.value.reason == "validation_required"


@pytest.mark.parametrize("fault", ["missing", "source", "profile", "keys", "age", "namespace", "revision", "digest"])
def test_automatic_activation_rejects_missing_or_changed_diagnosed_keys(storage, keys, fault):
    session, workspace = storage
    saved = foundation.save(session, automatic(workspace))
    with session.begin():
        revision = session.get(CasdoorConfigRevisionExtend, str(saved.draft_revision_id))
        proof_rows(session, revision, keys)
        current = snapshot_for(revision, saved.draft.configuration, keys)
        changes = {
            "source": {"source_url": "https://different.example.test/.well-known/jwks"},
            "profile": {"profile": "application"},
            "keys": {"trust_store": make_keys()},
            "age": {"fetched_at": NOW.timestamp() - 601},
            "namespace": {"namespace_id": uuid4()},
            "revision": {"revision_id": uuid4()},
            "digest": {"config_digest": "b" * 64},
        }
        current = None if fault == "missing" else replace(current, **changes[fault])
        with pytest.raises(CasdoorConfigurationError) as caught:
            foundation.repo(session).activate(
                etag=saved.etag,
                revision_id=saved.draft_revision_id,
                actor_account_id=ACTOR,
                now=NOW,
                signing_key_snapshot=current,
            )
        assert caught.value.reason == "signing_key_diagnostic_required"


def test_automatic_activation_allows_added_keys_and_same_source(storage, keys):
    session, workspace = storage
    saved = foundation.save(session, automatic(workspace))
    with session.begin():
        revision = session.get(CasdoorConfigRevisionExtend, str(saved.draft_revision_id))
        proof_rows(session, revision, keys)
        current = snapshot_for(revision, saved.draft.configuration, keys)
        expanded_keys = SigningKeyTrustStore(keys._keys + make_keys()._keys)
        active = foundation.repo(session).activate(
            etag=saved.etag,
            revision_id=saved.draft_revision_id,
            actor_account_id=ACTOR,
            now=NOW,
            signing_key_snapshot=replace(current, trust_store=expanded_keys, fingerprints=expanded_keys.fingerprints),
        )
        assert active.enabled


def test_automatic_activation_rejects_legacy_certificate_validation_rows(storage, keys):
    session, workspace = storage
    saved = foundation.save(session, automatic(workspace))
    with session.begin():
        revision = session.get(CasdoorConfigRevisionExtend, str(saved.draft_revision_id))
        foundation.proofs(session, saved.draft_revision_id)
        with pytest.raises(CasdoorConfigurationError) as caught:
            foundation.repo(session).activate(
                etag=saved.etag,
                revision_id=saved.draft_revision_id,
                actor_account_id=ACTOR,
                now=NOW,
                signing_key_snapshot=snapshot_for(revision, saved.draft.configuration, keys),
            )
        assert caught.value.reason == "validation_required"


def test_legacy_edit_preserves_active_namespace_and_reencrypts_secret(storage, pin):
    session, workspace = storage
    legacy = foundation.save(session, foundation.config(workspace, pin))
    active = foundation.activate(session, legacy.draft_revision_id)
    automatic_draft = foundation.save(session, automatic(workspace), etag=active.etag, secret=None)
    assert automatic_draft.active_revision_id == active.active_revision_id
    assert automatic_draft.draft.namespace_id == active.active.namespace_id
    old = session.get(CasdoorConfigRevisionExtend, str(active.active_revision_id))
    new = session.get(CasdoorConfigRevisionExtend, str(automatic_draft.draft_revision_id))
    assert old.schema_version == 1
    assert new.schema_version == 2
    assert old.encrypted_secret != new.encrypted_secret
    assert old.config_digest != new.config_digest
    assert automatic_draft.draft.validation == ()
    assert automatic_draft.draft.secret_configured


def test_v2_cannot_adopt_legacy_certificate_policy(pin):
    with pytest.raises(ValueError):
        CasdoorConfiguration.model_validate(
            foundation.config(str(uuid4()), pin).model_dump() | {"schema_version": 2, "signing_key_mode": "automatic"}
        )
