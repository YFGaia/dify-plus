"""Offline configuration UoW tests. All credentials/proofs are synthetic.

In-memory test rows labelled 'real' exercise rejection/selection logic only; they
are not real Casdoor evidence and must never be copied to a deployment database.
The test launcher blocks dotenv and plugin/app bootstrap (see I03-config.md).
"""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CasdoorCrypto, CryptoError
from core.casdoor.permissions import CasdoorManagementForbiddenError, CasdoorManagementPolicy
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from models.account import Tenant, TenantStatus
from models.base import Base
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
    CasdoorSyncIntentExtend,
    CasdoorValidationExtend,
    CasdoorValidationKind,
)
from pydantic import SecretStr, ValidationError
from repositories.casdoor_configuration_repository_extend import (
    REQUIRED_CAPABILITIES,
    CasdoorConfigurationError,
    CasdoorConfigurationRepository,
)
from sqlalchemy.orm import Session

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
ACTOR = UUID("10000000-0000-4000-8000-000000000001")
SYNTHETIC_SECRET = SecretStr("synthetic-client-secret")
PROOF = "a" * 64  # Synthetic fixture fingerprint; no actual G0-B proof.


@pytest.fixture(scope="module")
def pin():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.example.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=2))
        .sign(key, hashes.SHA256())
    )
    return {
        "pem": certificate.public_bytes(serialization.Encoding.PEM).decode(),
        "kid": "synthetic-pin",
        "not_before": NOW - timedelta(hours=1),
        "accept_until": NOW + timedelta(hours=1),
    }


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")
    tables = [Tenant.__table__] + [table for table in Base.metadata.sorted_tables if table.name.startswith("casdoor_")]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        with session.begin():
            workspace = Tenant(name="Synthetic existing workspace")
            session.add(workspace)
        workspace_id = workspace.id
        session.rollback()
        yield session, workspace_id
    engine.dispose()


def repo(session, *, proof=PROOF, rbac=False):
    return CasdoorConfigurationRepository(
        session,
        crypto=CasdoorCrypto(secret_key="synthetic-deployment-key", key_version="1"),
        rbac_enabled=rbac,
        deployment_proof_fingerprint=proof,
    )


def config(workspace_id, pin=None, **updates):
    return CasdoorConfiguration.model_validate(
        {
            "browser_frontend_url": "https://synthetic.example.test",
            "backend_api_url": "https://synthetic.example.test/api",
            "expected_issuer": "https://synthetic.example.test/issuer",
            "organization": "SyntheticOrg",
            "application": "SyntheticApp",
            "client_id": "SyntheticClient",
            "default_workspace_id": workspace_id,
            "certificates": [pin] if pin else [],
            **updates,
        }
    )


def save(session, configuration, *, etag=0, secret=SYNTHETIC_SECRET, repository=None):
    with session.begin():
        result = (repository or repo(session)).save_draft(
            configuration, etag=etag, actor_account_id=ACTOR, secret=secret
        )
    return result


def proofs(session, revision_id, *, rbac=False, optional=()):
    """Inject test-only records through Session, never a production writer API."""
    revision = session.get(CasdoorConfigRevisionExtend, str(revision_id))
    for kind, required in REQUIRED_CAPABILITIES.items():
        capabilities = dict.fromkeys(required, "passed")
        if kind == CasdoorValidationKind.DEPLOYMENT and rbac:
            capabilities["role_and_resource_termination"] = "passed"
        capabilities.update(dict.fromkeys(optional, "passed"))
        session.add(
            CasdoorValidationExtend(
                revision_id=revision.id,
                config_digest=revision.config_digest,
                kind=kind,
                status="passed",
                rbac_mode="on" if rbac else "off",
                proof_fingerprint=PROOF,
                summary_json=json.dumps(
                    {
                        "schema_version": 1,
                        "namespace_id": revision.namespace_id,
                        "evidence_source": "real",
                        "capabilities": capabilities,
                    }
                ),
                correlation_id=str(uuid4()),
                checked_at=NOW.replace(tzinfo=None),
                expires_at=(NOW + timedelta(minutes=15)).replace(tzinfo=None),
            )
        )
    session.flush()


def activate(session, revision_id, *, etag=1, repository=None):
    with session.begin():
        proofs(session, revision_id, rbac=(repository.rbac_mode == "on") if repository else False)
        return (repository or repo(session)).activate(
            etag=etag, revision_id=revision_id, actor_account_id=ACTOR, now=NOW
        )


def test_unconfigured_get_never_inserts_or_autoflushes(storage):
    session, _ = storage
    session.add(CasdoorIntegrationExtend())
    with patch.object(session, "flush", wraps=session.flush) as flush:
        snapshot = repo(session).get()
        assert snapshot.etag == 0
        assert snapshot.draft is None
        assert not snapshot.enabled
        flush.assert_not_called()
    session.rollback()
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIntegrationExtend)) == 0


def test_write_requires_caller_uow_and_does_not_commit_or_rollback(storage):
    session, workspace = storage
    repository = repo(session)
    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        repository.save_draft(config(workspace), etag=0, actor_account_id=ACTOR)
    with session.begin():
        with patch.object(session, "commit", side_effect=AssertionError("hidden commit")), patch.object(
            session, "rollback", side_effect=AssertionError("hidden rollback")
        ):
            repository.save_draft(config(workspace), etag=0, actor_account_id=ACTOR)
        session.rollback()
    assert repository.get().draft is None


def test_whole_revision_roundtrip_no_secret_in_read_and_storage_digest(storage, pin):
    session, workspace = storage
    configuration = config(
        workspace,
        pin,
        button_text="S" * 120,
        workspace_mappings=[{"workspace_id": workspace, "admin": {"organization": "SyntheticOrg", "name": "Admins"}}],
    )
    snapshot = save(session, configuration)
    assert snapshot.draft.configuration == configuration
    assert snapshot.etag == 1
    assert not snapshot.enabled
    assert snapshot.active is None
    assert snapshot.draft.secret_configured
    assert "synthetic-client-secret" not in repr(asdict(snapshot))
    revision = session.get(CasdoorConfigRevisionExtend, str(snapshot.draft_revision_id))
    assert revision.config_digest != configuration.config_digest()
    assert "secret" not in revision.policy_json
    assert revision.revision_number == 1


@pytest.mark.parametrize("secret", [None, SecretStr("")])
def test_keep_reencrypts_secret_new_aad_nonce_and_new_validation_digest(storage, secret):
    session, workspace = storage
    repository = repo(session)
    first = save(session, config(workspace))
    second = save(session, config(workspace, button_text="Changed"), etag=1, secret=secret)
    old = session.get(CasdoorConfigRevisionExtend, str(first.draft_revision_id))
    new = session.get(CasdoorConfigRevisionExtend, str(second.draft_revision_id))
    assert old.namespace_id == new.namespace_id
    assert old.encrypted_secret != new.encrypted_secret
    assert old.config_digest != new.config_digest
    assert new.revision_number == 2
    assert (
        repository.crypto.decrypt(new.encrypted_secret, context=repository._secret_context(new.namespace_id, new.id))
        == "synthetic-client-secret"
    )
    assert json.loads(old.encrypted_secret)["nonce"] != json.loads(new.encrypted_secret)["nonce"]
    with pytest.raises(CryptoError):
        repository.crypto.decrypt(new.encrypted_secret, context=repository._secret_context(old.namespace_id, old.id))


def test_draft_and_clear_do_not_touch_active_or_mutate_prior_revision(storage, pin):
    session, workspace = storage
    first = save(session, config(workspace, pin))
    active = activate(session, first.draft_revision_id)
    second = save(session, config(workspace, pin, button_text="Draft"), etag=active.etag)
    with session.begin():
        cleared = repo(session).clear_draft_secret(
            etag=second.etag, revision_id=second.draft_revision_id, actor_account_id=ACTOR
        )
    assert cleared.enabled
    assert cleared.active_revision_id == first.draft_revision_id
    assert cleared.active.secret_configured
    assert not cleared.draft.secret_configured
    assert cleared.draft_revision_id != second.draft_revision_id
    with pytest.raises(CasdoorConfigurationError, match="config_conflict"), session.begin():
        repo(session).activate(
            etag=cleared.etag, revision_id=cleared.draft_revision_id, actor_account_id=ACTOR, now=NOW
        )
    assert repo(session).get().active_revision_id == first.draft_revision_id


def test_clear_only_current_draft_pointer(storage, pin):
    session, workspace = storage
    first = save(session, config(workspace, pin))
    active = activate(session, first.draft_revision_id)
    second = save(session, config(workspace, pin), etag=active.etag)
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session).clear_draft_secret(etag=second.etag, revision_id=first.draft_revision_id, actor_account_id=ACTOR)


@pytest.mark.parametrize("target", ["missing_default", "archived_default", "missing_mapping", "archived_mapping"])
def test_requires_existing_normal_fixed_workspace_uuids(storage, target):
    session, workspace = storage
    if target.startswith("archived"):
        with session.begin():
            session.get(Tenant, workspace).status = TenantStatus.ARCHIVE
    unavailable = workspace if target.startswith("archived") else str(uuid4())
    configuration = (
        config(unavailable)
        if target.endswith("default")
        else config(workspace, workspace_mappings=[{"workspace_id": unavailable}])
    )
    with pytest.raises(CasdoorConfigurationError, match="workspace_unavailable"), session.begin():
        repo(session).save_draft(configuration, etag=0, actor_account_id=ACTOR)
    assert repo(session).get().draft is None


def test_workspace_selection_is_never_first_list_or_new_owner_space(storage):
    session, workspace = storage
    with session.begin():
        another = Tenant(name="Another existing workspace")
        session.add(another)
        session.flush()
        selected = another.id
    snapshot = save(session, config(selected))
    assert str(snapshot.draft.configuration.default_workspace_id) == selected != workspace
    assert session.scalar(sa.select(sa.func.count()).select_from(Tenant)) == 2


def test_repository_revalidates_model_construct_bypass(storage):
    session, workspace = storage
    invalid = CasdoorConfiguration.model_construct(**{**config(workspace).model_dump(), "scope": "password"})
    with pytest.raises(ValidationError), session.begin():
        repo(session).save_draft(invalid, etag=0, actor_account_id=ACTOR)


def test_stale_etag_does_not_overwrite_new_draft(storage):
    session, workspace = storage
    first = save(session, config(workspace))
    latest = save(session, config(workspace, button_text="new"), etag=first.etag)
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session).save_draft(config(workspace, button_text="lost"), etag=first.etag, actor_account_id=ACTOR)
    assert repo(session).get().draft_revision_id == latest.draft_revision_id


def test_database_cas_rejects_second_session_with_stale_identity_map(storage):
    session, workspace = storage
    first = save(session, config(workspace))
    with Session(session.get_bind()) as another:
        stale_row = another.scalar(sa.select(CasdoorIntegrationExtend))
        assert stale_row.etag == first.etag
        latest = save(session, config(workspace, button_text="winner"), etag=first.etag)
        with pytest.raises(CasdoorConfigurationError):
            repo(another).save_draft(config(workspace, button_text="loser"), etag=first.etag, actor_account_id=ACTOR)
        another.rollback()
    assert repo(session).get().draft_revision_id == latest.draft_revision_id


@pytest.mark.parametrize("etag", [-1, True, 2**63, "1"])
def test_invalid_etag_fail_closed(storage, etag):
    session, workspace = storage
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session).save_draft(config(workspace), etag=etag, actor_account_id=ACTOR)


def add_identity(session, snapshot):
    session.add(
        CasdoorIdentityExtend(
            namespace_id=str(snapshot.draft.namespace_id),
            account_id=str(uuid4()),
            issuer=snapshot.draft.configuration.expected_issuer,
            organization="SyntheticOrg",
            subject="synthetic-subject",
            last_applied_json="{}",
            profile_sync_json="{}",
        )
    )
    session.flush()


@pytest.mark.parametrize("field", ["expected_issuer", "organization", "application", "client_id"])
def test_bound_core_change_conflict_no_archive_or_reuse(storage, field):
    session, workspace = storage
    first = save(session, config(workspace))
    with session.begin():
        add_identity(session, first)
    changed = "https://other.example.test" if field == "expected_issuer" else "Changed"
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session).save_draft(config(workspace, **{field: changed}), etag=first.etag, actor_account_id=ACTOR)
    assert repo(session).get().draft_revision_id == first.draft_revision_id
    assert (
        session.get(CasdoorNamespaceExtend, str(first.draft.namespace_id)).lifecycle == CasdoorNamespaceLifecycle.ACTIVE
    )


def test_unbound_core_change_uses_new_namespace_without_disturbing_active(storage, pin):
    session, workspace = storage
    first = save(session, config(workspace, pin))
    current = activate(session, first.draft_revision_id)
    changed = save(session, config(workspace, pin, client_id="Changed"), etag=current.etag)
    assert changed.active_revision_id == current.active_revision_id
    assert changed.enabled
    assert changed.draft.namespace_id != current.active.namespace_id
    restored = save(session, config(workspace, pin), etag=changed.etag)
    assert restored.draft.namespace_id not in {changed.draft.namespace_id, current.active.namespace_id}
    assert (
        session.get(CasdoorNamespaceExtend, str(current.active.namespace_id)).lifecycle
        == CasdoorNamespaceLifecycle.ACTIVE
    )


def test_default_missing_real_deployment_proof_blocks_activation(storage, pin):
    session, workspace = storage
    snapshot = save(session, config(workspace, pin))
    with session.begin():
        proofs(session, snapshot.draft_revision_id)
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session, proof=None).activate(
            etag=1, revision_id=snapshot.draft_revision_id, actor_account_id=ACTOR, now=NOW
        )
    assert not repo(session).get().enabled


@pytest.mark.parametrize(
    "fault",
    [
        "no_records",
        "unknown",
        "failed",
        "digest",
        "namespace",
        "proof",
        "rbac",
        "expired",
        "too_long",
        "future",
        "synthetic",
        "missing_capability",
    ],
)
def test_activation_rejects_incomplete_stale_or_mismatched_server_records(storage, pin, fault):
    session, workspace = storage
    snapshot = save(session, config(workspace, pin))
    with session.begin():
        if fault != "no_records":
            proofs(session, snapshot.draft_revision_id)
            row = session.scalar(sa.select(CasdoorValidationExtend).where(CasdoorValidationExtend.kind == "protocol"))
            if fault in {"unknown", "failed"}:
                row.status = fault
            elif fault in {"digest", "proof", "rbac"}:
                setattr(
                    row,
                    {"digest": "config_digest", "proof": "proof_fingerprint", "rbac": "rbac_mode"}[fault],
                    "b" * 64 if fault != "rbac" else "on",
                )
            elif fault == "expired":
                row.expires_at = NOW.replace(tzinfo=None)
            elif fault == "too_long":
                row.expires_at = (NOW + timedelta(minutes=16)).replace(tzinfo=None)
            elif fault == "future":
                row.checked_at = (NOW + timedelta(seconds=1)).replace(tzinfo=None)
            else:
                summary = json.loads(row.summary_json)
                if fault == "namespace":
                    summary["namespace_id"] = str(uuid4())
                elif fault == "synthetic":
                    summary["evidence_source"] = "synthetic"
                else:
                    summary["capabilities"].pop("standard_id_token")
                row.summary_json = json.dumps(summary)
            session.flush()
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session).activate(etag=1, revision_id=snapshot.draft_revision_id, actor_account_id=ACTOR, now=NOW)


@pytest.mark.parametrize("rbac", [False, True])
def test_exact_fresh_server_record_gate_algorithm_accepts_synthetic_fixture(storage, pin, rbac):
    session, workspace = storage
    repository = repo(session, rbac=rbac)
    snapshot = save(session, config(workspace, pin), repository=repository)
    active = activate(session, snapshot.draft_revision_id, repository=repository)
    assert active.enabled
    assert active.active_revision_id == snapshot.draft_revision_id
    assert active.etag == 2


def test_rbac_on_requires_role_and_resource_termination_proof(storage, pin):
    session, workspace = storage
    snapshot = save(session, config(workspace, pin))
    with session.begin():
        proofs(session, snapshot.draft_revision_id, rbac=True)
        row = session.scalar(sa.select(CasdoorValidationExtend).where(CasdoorValidationExtend.kind == "deployment"))
        summary = json.loads(row.summary_json)
        summary["capabilities"].pop("role_and_resource_termination")
        row.summary_json = json.dumps(summary)
        session.flush()
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session, rbac=True).activate(
            etag=1, revision_id=snapshot.draft_revision_id, actor_account_id=ACTOR, now=NOW
        )


@pytest.mark.parametrize("option", ["avatar_sync", "rp_logout", "self_unlink"])
def test_optional_flags_require_their_own_passed_capability(storage, pin, option):
    session, workspace = storage
    snapshot = save(session, config(workspace, pin, **{option: True}))
    with session.begin():
        proofs(session, snapshot.draft_revision_id)
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session).activate(etag=1, revision_id=snapshot.draft_revision_id, actor_account_id=ACTOR, now=NOW)


def test_new_revision_cannot_reuse_prior_validation(storage, pin):
    session, workspace = storage
    first = save(session, config(workspace, pin))
    with session.begin():
        proofs(session, first.draft_revision_id)
    second = save(session, config(workspace, pin), etag=1)
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session).activate(etag=2, revision_id=second.draft_revision_id, actor_account_id=ACTOR, now=NOW)


@pytest.mark.parametrize("fault", ["fake", "duplicate", "expired", "disjoint"])
def test_activation_uses_real_certificate_parser_and_window(storage, pin, fault):
    session, workspace = storage
    pin = dict(pin)
    if fault == "fake":
        pin["pem"] = "-----BEGIN CERTIFICATE-----\nSYNTHETIC\n-----END CERTIFICATE-----"
    elif fault == "expired":
        pin["accept_until"] = NOW
    configuration = config(workspace, pin)
    if fault == "duplicate":
        configuration = config(workspace, certificates=[pin, {**pin, "kid": "other"}])
    if fault == "disjoint":
        # Distinct DER fingerprints with explicitly nonoverlapping windows.
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-rotation.example.test")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - timedelta(days=1))
            .not_valid_after(NOW + timedelta(days=2))
            .sign(key, hashes.SHA256())
        )
        next_pin = {
            "pem": certificate.public_bytes(serialization.Encoding.PEM).decode(),
            "kid": "next",
            "not_before": NOW + timedelta(hours=2),
            "accept_until": NOW + timedelta(hours=3),
        }
        configuration = config(workspace, certificates=[pin, next_pin])
    snapshot = save(session, configuration)
    with session.begin():
        proofs(session, snapshot.draft_revision_id)
    with pytest.raises(CryptoError), session.begin():
        repo(session).activate(etag=1, revision_id=snapshot.draft_revision_id, actor_account_id=ACTOR, now=NOW)


def test_read_summary_does_not_report_synthetic_or_wrong_proof_as_passed(storage, pin):
    session, workspace = storage
    snapshot = save(session, config(workspace, pin))
    with session.begin():
        proofs(session, snapshot.draft_revision_id)
        row = session.scalar(sa.select(CasdoorValidationExtend).where(CasdoorValidationExtend.kind == "protocol"))
        row.proof_fingerprint = "b" * 64
    result = repo(session).get(now=NOW)
    assert (
        next(item for item in result.draft.validation if item.kind == CasdoorValidationKind.PROTOCOL).status
        == "unknown"
    )


@pytest.mark.parametrize("ambiguous", [False, True])
def test_latest_checked_validation_and_ambiguous_time_fail_closed(storage, pin, ambiguous):
    session, workspace = storage
    snapshot = save(session, config(workspace, pin))
    with session.begin():
        proofs(session, snapshot.draft_revision_id)
        passed = session.scalar(sa.select(CasdoorValidationExtend).where(CasdoorValidationExtend.kind == "protocol"))
        checked = passed.checked_at if ambiguous else passed.checked_at + timedelta(seconds=1)
        session.add(
            CasdoorValidationExtend(
                revision_id=passed.revision_id,
                config_digest=passed.config_digest,
                kind=passed.kind,
                status="failed",
                rbac_mode=passed.rbac_mode,
                proof_fingerprint=passed.proof_fingerprint,
                summary_json=passed.summary_json,
                correlation_id=str(uuid4()),
                checked_at=checked,
                expires_at=passed.expires_at,
                created_at=passed.created_at if ambiguous else passed.created_at - timedelta(seconds=1),
            )
        )
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session).activate(
            etag=1, revision_id=snapshot.draft_revision_id, actor_account_id=ACTOR, now=NOW + timedelta(seconds=2)
        )


@pytest.mark.parametrize(
    ("operation", "termination", "sent"),
    [
        ("in_flight", "unconfirmed", True),
        ("unknown", "unconfirmed", True),
        ("applied", "unconfirmed", True),
        ("pending", "not_started", False),
    ],
)
def test_disable_fences_without_falsely_terminating_intents(storage, pin, operation, termination, sent):
    session, workspace = storage
    snapshot = save(session, config(workspace, pin))
    active = activate(session, snapshot.draft_revision_id)
    with session.begin():
        intent = CasdoorSyncIntentExtend(
            namespace_id=str(snapshot.draft.namespace_id),
            identity_id=str(uuid4()),
            account_id=str(uuid4()),
            revision_id=str(snapshot.draft_revision_id),
            generation=1,
            ownership_epoch=0,
            fence_epoch=0,
            kind="role_replace",
            scope_digest="b" * 64,
            idempotency_key="c" * 64,
            desired_json="{}",
            operation_state=operation,
            termination_state=termination,
            sent_at=NOW.replace(tzinfo=None) if sent else None,
        )
        session.add(intent)
        session.flush()
        result = repo(session).disable(etag=active.etag, actor_account_id=ACTOR)
        assert not result.configuration.enabled
        assert result.reconciliation_required == sent
        assert intent.operation_state == operation
        assert intent.termination_state == termination
        namespace = session.get(CasdoorNamespaceExtend, str(snapshot.draft.namespace_id))
        assert namespace.fence_epoch == 1
        assert namespace.lifecycle == CasdoorNamespaceLifecycle.FENCING
    with pytest.raises(CasdoorConfigurationError), session.begin():
        repo(session).activate(
            etag=result.configuration.etag, revision_id=snapshot.draft_revision_id, actor_account_id=ACTOR, now=NOW
        )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "not-a-uuid",
        str(ACTOR) + ",bad",
        str(ACTOR) + ",",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        "{" + str(ACTOR) + "}",
    ],
)
def test_management_empty_invalid_deployment_allowlist_denies_everyone(raw):
    policy = CasdoorManagementPolicy.from_deployment(raw)
    account = SimpleNamespace(id=str(ACTOR), status="active", is_admin_or_owner=True, is_admin=True)
    assert not policy.can_manage_casdoor(account)
    with pytest.raises(CasdoorManagementForbiddenError):
        policy.require_management(account)


@pytest.mark.parametrize("status", ["pending", "uninitialized", "banned", "closed"])
def test_management_requires_active_local_account(status):
    policy = CasdoorManagementPolicy.from_deployment(str(ACTOR))
    assert not policy.can_manage_casdoor(SimpleNamespace(id=str(ACTOR), status=status))


@pytest.mark.parametrize(("rbac", "role"), [(False, "owner"), (False, "admin"), (True, "owner"), (True, "admin")])
def test_management_workspace_owner_rbac_admin_cannot_bypass_exact_account_id(rbac, role):
    policy = CasdoorManagementPolicy.from_deployment(str(ACTOR))
    assert not policy.can_manage_casdoor(
        SimpleNamespace(
            id=str(uuid4()),
            status="active",
            current_role=role,
            is_admin=True,
            is_admin_or_owner=True,
            rbac_enabled=rbac,
        )
    )
    ordinary = SimpleNamespace(id=str(ACTOR), status="active", current_role="normal", is_admin=False)
    assert policy.can_manage_casdoor(ordinary)
    policy.require_management(ordinary)
    assert not policy.can_manage_casdoor(None)
