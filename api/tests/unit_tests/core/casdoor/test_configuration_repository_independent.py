"""Independent adversarial checks for the Casdoor configuration repository.

All database rows, credentials, certificates, and validation summaries here are
synthetic algorithm fixtures. They are not deployment or G0-B evidence.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CasdoorCrypto, CryptoError, EncryptionContext, EncryptionPurpose
from core.casdoor.permissions import CasdoorManagementPolicy
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from models.account import Tenant
from models.base import Base
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
    CasdoorValidationExtend,
)
from pydantic import SecretStr, ValidationError
from repositories.casdoor_configuration_repository_extend import (
    REQUIRED_CAPABILITIES,
    CasdoorConfigurationError,
    CasdoorConfigurationRepository,
)
from sqlalchemy.orm import Session

NOW = datetime(2026, 9, 30, 9, tzinfo=UTC)
ACTOR = UUID("20000000-0000-4000-8000-000000000002")
FINGERPRINT = "c" * 64
SECRET_KEY = "independent-synthetic-key-only"


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite://")
    tables = [Tenant.__table__, *[t for t in Base.metadata.sorted_tables if t.name.startswith("casdoor_")]]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        with session.begin():
            workspace = Tenant(name="Independent synthetic workspace")
            session.add(workspace)
        workspace_id = workspace.id
        session.rollback()
        yield session, workspace_id
    engine.dispose()


def repository(session, *, proof=FINGERPRINT, rbac=False):
    return CasdoorConfigurationRepository(
        session,
        crypto=CasdoorCrypto(secret_key=SECRET_KEY, key_version="test-only"),
        rbac_enabled=rbac,
        deployment_proof_fingerprint=proof,
    )


def configuration(workspace_id, **changes):
    values = {
        "browser_frontend_url": "https://idp.synthetic.invalid",
        "backend_api_url": "https://idp.synthetic.invalid/api",
        "expected_issuer": "https://idp.synthetic.invalid/issuer",
        "organization": "FixtureOrg",
        "application": "FixtureApp",
        "client_id": "fixture-client",
        "default_workspace_id": workspace_id,
        "certificates": [],
    }
    values.update(changes)
    return CasdoorConfiguration.model_validate(values)


def synthetic_pin():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.synthetic.invalid")])
    cert = (
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
        "pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
        "kid": "independent-fixture",
        "not_before": NOW - timedelta(hours=1),
        "accept_until": NOW + timedelta(hours=1),
    }


def write(session, value, *, etag=0, secret="fixture-secret", repo=None):
    with session.begin():
        return (repo or repository(session)).save_draft(
            value,
            etag=etag,
            actor_account_id=ACTOR,
            secret=SecretStr(secret) if secret is not None else None,
        )


def test_untrusted_constructed_model_is_revalidated_before_any_write(db):
    session, workspace = db
    valid = configuration(workspace)
    forged = CasdoorConfiguration.model_construct(
        **{
            **valid.model_dump(),
            "scope": "openid password",
            "backend_api_url": "https://operator:synthetic-url-secret@idp.synthetic.invalid/api",
        }
    )

    with pytest.raises(ValidationError) as failure:
        write(session, forged)
    safe_errors = json.dumps(failure.value.errors(include_input=False, include_context=False))

    assert "synthetic-url-secret" not in safe_errors
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIntegrationExtend)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorConfigRevisionExtend)) == 0


def test_empty_get_does_not_flush_pending_configuration(db):
    session, _ = db
    pending = CasdoorIntegrationExtend(id=str(uuid4()))
    session.add(pending)

    snapshot = repository(session).get()

    assert not snapshot.enabled
    assert snapshot.etag == 0
    assert snapshot.draft is None
    assert pending in session.new
    session.rollback()
    assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIntegrationExtend)) == 0


def test_blank_secret_creates_new_aad_envelope_without_copying_ciphertext(db):
    session, workspace = db
    cfg = configuration(workspace)
    first = write(session, cfg, secret="kept-synthetic-secret")
    first_row = session.get(CasdoorConfigRevisionExtend, str(first.draft_revision_id))
    first_envelope = first_row.encrypted_secret
    first_context = EncryptionContext(EncryptionPurpose.CONFIG_SECRET, UUID(first_row.namespace_id), UUID(first_row.id))
    session.rollback()

    second = write(session, cfg, etag=first.etag, secret=None)
    second_row = session.get(CasdoorConfigRevisionExtend, str(second.draft_revision_id))
    second_envelope = second_row.encrypted_secret
    second_context = EncryptionContext(
        EncryptionPurpose.CONFIG_SECRET, UUID(second_row.namespace_id), UUID(second_row.id)
    )
    crypto = repository(session).crypto

    assert first_envelope != second_envelope
    assert crypto.decrypt(second_envelope, context=second_context) == "kept-synthetic-secret"
    with pytest.raises(CryptoError):
        crypto.decrypt(second_envelope, context=first_context)


def test_mapping_ref_cannot_fill_two_slots_and_default_fallback_is_fixed(db):
    _, workspace = db
    workspace_id = str(uuid4())
    ref = {"organization": "FixtureOrg", "name": "SameExactRole"}

    with pytest.raises(ValidationError):
        configuration(
            workspace,
            workspace_mappings=[{"workspace_id": workspace_id, "admin": ref, "normal": ref}],
        )

    assert configuration(workspace).default_normal_fallback is True


def test_secret_rotation_changes_storage_digest_even_when_policy_is_identical(db):
    session, workspace = db
    cfg = configuration(workspace, certificates=[synthetic_pin()])
    first = write(session, cfg, secret="first-synthetic-secret")
    first_row = session.get(CasdoorConfigRevisionExtend, str(first.draft_revision_id))
    first_storage_digest = first_row.config_digest
    first_envelope = first_row.encrypted_secret
    session.rollback()
    first_policy_digest = cfg.config_digest()

    second = write(session, cfg, etag=first.etag, secret="second-synthetic-secret")
    second_row = session.get(CasdoorConfigRevisionExtend, str(second.draft_revision_id))

    assert cfg.config_digest() == first_policy_digest
    assert first_storage_digest != second_row.config_digest
    assert first_envelope != second_row.encrypted_secret
    assert first.draft_revision_id != second.draft_revision_id


def test_old_server_records_cannot_be_rebound_to_secret_changed_revision(db):
    session, workspace = db
    cfg = configuration(workspace, certificates=[synthetic_pin()])
    original = write(session, cfg, secret="before-rotation")
    original_row = session.get(CasdoorConfigRevisionExtend, str(original.draft_revision_id))
    original_digest = original_row.config_digest
    session.rollback()
    next_snapshot = write(session, cfg, etag=original.etag, secret="after-rotation")
    changed = session.get(CasdoorConfigRevisionExtend, str(next_snapshot.draft_revision_id))
    changed_id = changed.id
    changed_namespace_id = changed.namespace_id
    session.rollback()

    # Simulate an accidental copy of prior validator rows to the new revision.
    # The old digest is retained, which must make the server-owned gate reject.
    with session.begin():
        for kind, capabilities in REQUIRED_CAPABILITIES.items():
            session.add(
                CasdoorValidationExtend(
                    revision_id=changed_id,
                    config_digest=original_digest,
                    kind=kind,
                    status="passed",
                    rbac_mode="off",
                    proof_fingerprint=FINGERPRINT,
                    summary_json=json.dumps(
                        {
                            "schema_version": 1,
                            "namespace_id": changed_namespace_id,
                            "evidence_source": "real",
                            "capabilities": dict.fromkeys(capabilities, "passed"),
                        }
                    ),
                    correlation_id=str(uuid4()),
                    checked_at=(NOW - timedelta(minutes=1)).replace(tzinfo=None),
                    expires_at=(NOW + timedelta(minutes=10)).replace(tzinfo=None),
                )
            )

    with pytest.raises(CasdoorConfigurationError):
        with session.begin():
            repository(session).activate(
                etag=next_snapshot.etag,
                revision_id=next_snapshot.draft_revision_id,
                actor_account_id=ACTOR,
                now=NOW,
            )
    session.rollback()
    assert not repository(session).get().enabled


@pytest.mark.parametrize(
    ("checked_at", "expires_at", "expected"),
    [
        (NOW + timedelta(seconds=1), NOW + timedelta(minutes=5), False),
        (NOW - timedelta(minutes=5), NOW, False),
        (NOW - timedelta(minutes=1), NOW + timedelta(minutes=15, seconds=1), False),
        (NOW - timedelta(minutes=1), NOW + timedelta(minutes=14), True),
        (None, NOW + timedelta(minutes=5), False),
    ],
)
def test_validation_freshness_requires_nonfuture_check_and_bounded_expiry(checked_at, expires_at, expected):
    record = SimpleNamespace(
        checked_at=checked_at.replace(tzinfo=None) if checked_at else None,
        expires_at=expires_at.replace(tzinfo=None) if expires_at else None,
    )

    assert CasdoorConfigurationRepository._fresh(record, NOW.replace(tzinfo=None)) is expected


def test_disabled_bound_namespace_cannot_be_reopened_by_saving_same_core(db):
    session, workspace = db
    saved = write(session, configuration(workspace))
    session.add(
        CasdoorIdentityExtend(
            namespace_id=str(saved.draft.namespace_id),
            account_id=str(uuid4()),
            issuer=saved.draft.configuration.expected_issuer,
            organization="FixtureOrg",
            subject="synthetic-subject",
            last_applied_json="{}",
            profile_sync_json="{}",
        )
    )
    session.commit()

    with session.begin():
        disabled = repository(session).disable(etag=saved.etag, actor_account_id=ACTOR)
    fenced_id = str(saved.draft.namespace_id)
    assert session.get(CasdoorNamespaceExtend, fenced_id).lifecycle == CasdoorNamespaceLifecycle.FENCING
    assert not disabled.configuration.enabled
    session.rollback()

    with pytest.raises(CasdoorConfigurationError):
        write(session, configuration(workspace), etag=disabled.configuration.etag)
    session.rollback()
    assert session.get(CasdoorNamespaceExtend, fenced_id).lifecycle == CasdoorNamespaceLifecycle.FENCING
    assert not repository(session).get().enabled


def test_independent_sessions_reject_a_lost_update_after_both_read_same_etag(db):
    session, workspace = db
    initial = write(session, configuration(workspace))
    competing = Session(session.get_bind())
    try:
        # Keep an independent identity map holding the old etag while the first
        # writer commits a new immutable revision.
        seen = competing.scalar(sa.select(CasdoorIntegrationExtend))
        assert seen.etag == initial.etag
        winner = write(session, configuration(workspace, button_text="winner"), etag=initial.etag)
        with pytest.raises(CasdoorConfigurationError):
            repository(competing).save_draft(
                configuration(workspace, button_text="stale writer"),
                etag=initial.etag,
                actor_account_id=ACTOR,
                secret=SecretStr("another-fixture-secret"),
            )
        competing.rollback()
        assert repository(session).get().draft_revision_id == winner.draft_revision_id
    finally:
        competing.close()


def test_allowlist_is_exact_active_local_account_boundary():
    permitted = "20000000-0000-4000-8000-000000000002"
    policy = CasdoorManagementPolicy.from_deployment(f" {permitted} ")
    local_admin = SimpleNamespace(id=permitted, status="active", is_admin=True)
    workspace_owner = SimpleNamespace(id="20000000-0000-4000-8000-000000000003", status="active", is_admin=True)
    suspended = SimpleNamespace(id=permitted, status="banned", is_admin=True)

    assert policy.can_manage_casdoor(local_admin)
    assert not policy.can_manage_casdoor(workspace_owner)
    assert not policy.can_manage_casdoor(suspended)
    assert not CasdoorManagementPolicy.from_deployment(f"{permitted},bad").can_manage_casdoor(local_admin)
