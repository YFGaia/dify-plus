"""Offline management UoW tests; all keys/certificates are synthetic fixtures."""

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from core.casdoor.crypto import CasdoorCrypto, CryptoError, EncryptionContext, EncryptionPurpose
from core.casdoor.permissions import CasdoorManagementForbiddenError, CasdoorManagementPolicy
from models.account import Account, AccountStatus, Tenant, TenantAccountRole, TenantStatus
from models.base import Base
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorConfigRevisionExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
    CasdoorValidationExtend,
    CasdoorValidationKind,
    CasdoorValidationStatus,
)
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError
from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from tests.unit_tests.core.casdoor import test_configuration_repository_extend as foundation

NOW = foundation.NOW
ACTOR = foundation.ACTOR


@pytest.fixture
def database():
    engine = sa.create_engine("sqlite://")
    tables = [Tenant.__table__] + [table for table in Base.metadata.sorted_tables if table.name.startswith("casdoor_")]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        workspace = Tenant(name="Synthetic existing workspace")
        workspace.created_at = NOW.replace(tzinfo=None)
        session.add(workspace)
    yield factory, workspace.id
    engine.dispose()


@pytest.fixture
def actor():
    account = Account(name="Synthetic normal account", email="synthetic@example.test")
    account.id = str(ACTOR)
    account.status = AccountStatus.ACTIVE
    account.role = TenantAccountRole.NORMAL
    return account


@pytest.fixture(scope="module")
def certificate():
    return foundation.pin.__wrapped__()


def service(factory, *, allowlist=str(ACTOR), key="synthetic-i24a-deployment-key"):
    return CasdoorConfigurationService(
        session_factory=factory,
        management_policy=CasdoorManagementPolicy.from_deployment(allowlist),
        secret_key=key,
        rbac_enabled=False,
    )


def save(owner, actor, workspace_id, certificate=None, **kwargs):
    return owner.save(
        actor,
        configuration=foundation.config(workspace_id, certificate),
        etag=kwargs.get("etag", 0),
        secret=kwargs.get("secret", SecretStr("synthetic-i24a-client-secret")),
    )


def test_unconfigured_get_and_permission_never_insert(database, actor):
    factory, _ = database
    owner = service(factory)
    assert owner.can_manage(actor)
    assert owner.get(actor).etag == 0
    with factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIntegrationExtend)) == 0


@pytest.mark.parametrize("allowlist", ["", "not-a-uuid", str(ACTOR) + ",", "20000000-0000-4000-8000-000000000002"])
@pytest.mark.parametrize("role", [TenantAccountRole.OWNER, TenantAccountRole.ADMIN, TenantAccountRole.NORMAL])
def test_allowlist_only_ignores_workspace_roles(database, actor, allowlist, role):
    factory, workspace_id = database
    actor.role = role
    owner = service(factory, allowlist=allowlist)
    assert not owner.can_manage(actor)
    with pytest.raises(CasdoorManagementForbiddenError):
        owner.get(actor)
    with pytest.raises(CasdoorManagementForbiddenError):
        save(owner, actor, workspace_id)
    with factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIntegrationExtend)) == 0


def test_non_active_account_denied(database, actor):
    factory, _ = database
    actor.status = AccountStatus.BANNED
    assert not service(factory).can_manage(actor)


@pytest.mark.parametrize("blank", [None, SecretStr("")])
def test_service_cas_immutable_keep_new_aad_clear_audit(database, actor, certificate, blank):
    factory, workspace_id = database
    owner = service(factory)
    first = save(owner, actor, workspace_id, certificate)
    second = save(owner, actor, workspace_id, certificate, etag=1, secret=blank)
    assert second.etag == 2
    assert first.draft_revision_id != second.draft_revision_id
    assert second.draft.secret_configured
    with factory() as session:
        old = session.get(CasdoorConfigRevisionExtend, str(first.draft_revision_id))
        new = session.get(CasdoorConfigRevisionExtend, str(second.draft_revision_id))
        assert old.encrypted_secret != new.encrypted_secret
        assert json.loads(new.encrypted_secret)["key_version"] == "v1"
        crypto = CasdoorCrypto(secret_key="synthetic-i24a-deployment-key", key_version="v1")
        context = EncryptionContext(EncryptionPurpose.CONFIG_SECRET, UUID(new.namespace_id), UUID(new.id))
        assert crypto.decrypt(new.encrypted_secret, context=context) == "synthetic-i24a-client-secret"
        with pytest.raises(CryptoError):
            crypto.decrypt(old.encrypted_secret, context=context)
    with pytest.raises(CasdoorConfigurationError):
        save(owner, actor, workspace_id, certificate, etag=1)
    with pytest.raises(CasdoorConfigurationError):
        owner.clear_secret(actor, etag=2, revision_id=first.draft_revision_id)
    third = owner.clear_secret(actor, etag=2, revision_id=second.draft_revision_id)
    assert third.etag == 3
    assert not third.draft.secret_configured
    with factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorConfigRevisionExtend)) == 3
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 3
        assert session.get(CasdoorConfigRevisionExtend, str(first.draft_revision_id)).encrypted_secret is not None


def test_save_crypto_failure_rolls_back_cas_and_every_write(database, actor, certificate):
    factory, workspace_id = database
    owner = service(factory)
    first = save(owner, actor, workspace_id, certificate)
    with patch.object(CasdoorCrypto, "encrypt", side_effect=CryptoError()):
        with pytest.raises(CryptoError):
            save(owner, actor, workspace_id, certificate, etag=1)
    assert owner.get(actor).etag == 1
    assert owner.get(actor).draft_revision_id == first.draft_revision_id
    with factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorConfigRevisionExtend)) == 1


def test_static_exact_draft_local_checks_no_proof_or_activation(database, actor, certificate):
    factory, workspace_id = database
    owner = service(factory)
    saved = save(owner, actor, workspace_id, certificate)
    result = owner.validate_static(actor, etag=1, revision_id=saved.draft_revision_id, now=NOW)
    assert result.revision_id == saved.draft_revision_id
    assert result.etag == 1
    assert len(result.certificates) == 1
    assert not owner.get(actor).enabled
    with factory() as session:
        snapshot = owner._repository(session).get(now=NOW)
        assert snapshot.draft.validation[0].kind is CasdoorValidationKind.STATIC
        assert snapshot.draft.validation[0].status == CasdoorValidationStatus.PASSED.value
        row = session.scalar(sa.select(CasdoorValidationExtend))
        assert row.revision_id == str(saved.draft_revision_id)
        revision = session.get(CasdoorConfigRevisionExtend, str(saved.draft_revision_id))
        assert row.config_digest == revision.config_digest
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 1
        repository = owner._repository(session)
        assert repository.deployment_proof_fingerprint is None
        with pytest.raises(CasdoorConfigurationError):
            repository.activate(etag=1, revision_id=saved.draft_revision_id, actor_account_id=ACTOR, now=NOW)
    newer = save(owner, actor, workspace_id, certificate, etag=1, secret=None)
    with pytest.raises(CasdoorConfigurationError):
        owner.validate_static(actor, etag=2, revision_id=saved.draft_revision_id, now=NOW)
    with pytest.raises(CasdoorConfigurationError):
        owner.validate_static(actor, etag=1, revision_id=newer.draft_revision_id, now=NOW)


@pytest.mark.parametrize("failure", ["secret", "certificate", "expired", "workspace", "key", "tamper"])
def test_static_rejects_unready_config(database, actor, certificate, failure):
    factory, workspace_id = database
    owner = service(factory)
    saved = save(
        owner,
        actor,
        workspace_id,
        certificate if failure != "certificate" else None,
        secret=None if failure == "secret" else SecretStr("synthetic-i24a-client-secret"),
    )
    now = NOW + timedelta(hours=2) if failure == "expired" else NOW
    if failure in {"workspace", "tamper"}:
        with factory.begin() as session:
            if failure == "workspace":
                session.get(Tenant, workspace_id).status = TenantStatus.ARCHIVE
            else:
                session.execute(
                    sa.update(CasdoorConfigRevisionExtend)
                    .where(CasdoorConfigRevisionExtend.id == str(saved.draft_revision_id))
                    .values(encrypted_secret="invalid")
                )
    if failure == "key":
        owner = service(factory, key="different-synthetic-deployment-key")
    with pytest.raises((CasdoorConfigurationError, CryptoError)):
        owner.validate_static(actor, etag=1, revision_id=saved.draft_revision_id, now=now)
    with factory() as session:
        row = session.scalar(sa.select(CasdoorValidationExtend))
        # Corrupting the encrypted secret invalidates the revision's opaque digest
        # before a trusted validation binding can be constructed.
        if failure == "tamper":
            assert row is None
            return
        assert row is not None
        assert row.revision_id == str(saved.draft_revision_id)
        assert row.kind is CasdoorValidationKind.STATIC
        assert row.status is CasdoorValidationStatus.FAILED


def test_global_workspace_created_order_pagination_earliest_unavailable_ambiguity(database, actor):
    factory, workspace_id = database
    with factory.begin() as session:
        session.get(Tenant, workspace_id).created_at = NOW.replace(tzinfo=None) + timedelta(days=1)
        for index in range(103):
            workspace = Tenant(name=f"Synthetic workspace {index}")
            workspace.id = f"20000000-0000-4000-8000-{index:012d}"
            workspace.created_at = NOW.replace(tzinfo=None)
            workspace.status = TenantStatus.ARCHIVE if index == 0 else TenantStatus.NORMAL
            session.add(workspace)
    owner = service(factory)
    first = owner.workspaces(actor, page=1, limit=100)
    second = owner.workspaces(actor, page=2, limit=100)
    assert first.total == second.total == 104
    assert len(first.workspaces) == 100
    assert first.has_more
    assert len(second.workspaces) == 4
    assert not second.has_more
    assert first.earliest_created_ambiguous
    assert second.earliest_created_ambiguous
    assert second.earliest_created_workspace == first.workspaces[0]
    assert not first.earliest_created_workspace.available
    assert str(first.workspaces[0].workspace_id).endswith("000000000000")
    assert str(second.workspaces[-1].workspace_id) == workspace_id


def test_service_disable_fences_namespace_and_reports_no_unresolved_remote_work(database, actor):
    factory, workspace_id = database
    owner = service(factory)
    saved = save(owner, actor, workspace_id)

    result = owner.disable(actor, etag=saved.etag)

    assert result.configuration.enabled is False
    assert result.configuration.etag == saved.etag + 1
    assert result.reconciliation_required is False
    with factory() as session:
        integration = session.scalar(sa.select(CasdoorIntegrationExtend))
        namespace = session.get(CasdoorNamespaceExtend, str(saved.draft.namespace_id))
        assert integration is not None and integration.enabled is False
        assert namespace is not None
        assert namespace.lifecycle is CasdoorNamespaceLifecycle.FENCING
        assert namespace.fence_epoch == 1


def test_activation_requires_revision_diagnostic_not_deployment_proof(database, actor, certificate):
    factory, workspace_id = database
    owner = service(factory)
    saved = save(owner, actor, workspace_id, certificate)
    owner.validate_static(actor, etag=saved.etag, revision_id=saved.draft_revision_id, now=NOW)

    with pytest.raises(CasdoorConfigurationError) as activate_error:
        owner.activate(actor, etag=saved.etag, revision_id=saved.draft_revision_id, now=NOW)
    assert activate_error.value.reason == "validation_required"

    assert owner.test_login(actor, etag=saved.etag, revision_id=saved.draft_revision_id) == "live_test_not_wired"
    assert owner.get(actor).enabled is False


def test_repository_reader_pending_rows_no_flush(database, actor, certificate):
    factory, workspace_id = database
    owner = service(factory)
    saved = save(owner, actor, workspace_id, certificate)
    with factory() as session:
        pending = Tenant(name="Must remain pending")
        session.add(pending)
        repository = owner._repository(session)
        with patch.object(session, "flush", wraps=session.flush) as flush:
            repository.get()
            page = repository.list_workspaces(page=1, limit=100)
            repository.validate_static(etag=1, revision_id=saved.draft_revision_id, now=NOW)
            flush.assert_not_called()
            assert pending in session.new
            assert page.total == 1


@pytest.mark.parametrize("case", ["invalid-pem", "duplicate-der", "non-overlap"])
def test_static_delegates_real_x509_and_rotation_window_checks(database, actor, certificate, case):
    factory, workspace_id = database
    owner = service(factory)
    first = dict(certificate)
    if case == "invalid-pem":
        first["pem"] = "-----BEGIN CERTIFICATE-----\nSYNTHETIC\n-----END CERTIFICATE-----"
        pins = [first]
    else:
        second = foundation.pin.__wrapped__() if case == "non-overlap" else dict(certificate)
        second["kid"] = "synthetic-second-pin"
        if case == "non-overlap":
            first["accept_until"] = NOW
            second["not_before"] = NOW
        pins = [first, second]
    configuration = foundation.config(workspace_id, certificates=pins)
    saved = owner.save(actor, configuration=configuration, etag=0, secret=SecretStr("synthetic-static-client-secret"))
    with pytest.raises(CryptoError):
        owner.validate_static(actor, etag=1, revision_id=saved.draft_revision_id, now=NOW)


def test_empty_workspace_selection_has_no_automatic_default(database, actor):
    factory, _ = database
    with factory.begin() as session:
        session.execute(sa.delete(Tenant))
    page = service(factory).workspaces(actor, page=1, limit=100)
    assert page.workspaces == ()
    assert page.total == 0
    assert page.earliest_created_workspace is None
    assert not page.has_more
    assert not page.earliest_created_ambiguous
