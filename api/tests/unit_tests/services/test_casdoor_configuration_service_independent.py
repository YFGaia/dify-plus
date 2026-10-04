"""Independent SQLite checks for Casdoor management transaction boundaries."""

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.crypto import CasdoorCrypto, CryptoError, EncryptionContext, EncryptionPurpose
from core.casdoor.permissions import CasdoorManagementPolicy
from models.account import Account, AccountStatus, Tenant, TenantAccountRole, TenantStatus
from models.base import Base
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorConfigRevisionExtend,
    CasdoorIntegrationExtend,
    CasdoorValidationExtend,
)
from pydantic import SecretStr
from repositories.casdoor_configuration_repository_extend import CasdoorConfigurationError
from services.casdoor_configuration_service_extend import CasdoorConfigurationService
from sqlalchemy.orm import sessionmaker

from tests.unit_tests.core.casdoor import test_configuration_repository_extend as synthetic

ACTOR_ID = UUID("10000000-0000-4000-8000-000000000021")
DEPLOYMENT_KEY = "independent-synthetic-deployment-key"


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")
    tables = [Tenant.__table__, *[table for table in Base.metadata.sorted_tables if table.name.startswith("casdoor_")]]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        workspace = Tenant(name="Independent synthetic initial workspace")
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
    yield factory, workspace_id
    engine.dispose()


@pytest.fixture
def actor():
    account = Account(name="Independent synthetic account", email="independent@example.test")
    account.id = str(ACTOR_ID)
    account.status = AccountStatus.ACTIVE
    account.role = TenantAccountRole.NORMAL
    return account


@pytest.fixture(scope="module")
def cert_pin():
    return synthetic.pin.__wrapped__()


def service(factory, *, allowlist=str(ACTOR_ID), key=DEPLOYMENT_KEY):
    return CasdoorConfigurationService(
        session_factory=factory,
        management_policy=CasdoorManagementPolicy.from_deployment(allowlist),
        secret_key=key,
        rbac_enabled=False,
    )


def save(owner, actor, workspace_id, cert_pin, *, etag, secret):
    return owner.save(
        actor,
        configuration=synthetic.config(workspace_id, cert_pin),
        etag=etag,
        secret=secret,
    )


def test_secret_keep_creates_new_aad_bound_revision_and_clear_is_explicit(storage, actor, cert_pin):
    factory, workspace_id = storage
    owner = service(factory)
    first = save(owner, actor, workspace_id, cert_pin, etag=0, secret=SecretStr("synthetic-secret-alpha"))
    second = save(owner, actor, workspace_id, cert_pin, etag=1, secret=SecretStr(""))
    assert second.etag == 2
    assert second.draft_revision_id != first.draft_revision_id
    assert second.draft.secret_configured

    with factory() as session:
        old = session.get(CasdoorConfigRevisionExtend, str(first.draft_revision_id))
        new = session.get(CasdoorConfigRevisionExtend, str(second.draft_revision_id))
        assert old.encrypted_secret
        assert new.encrypted_secret
        assert old.encrypted_secret != new.encrypted_secret
        assert json.loads(new.encrypted_secret)["key_version"] == "v1"
        crypto = CasdoorCrypto(secret_key=DEPLOYMENT_KEY, key_version="v1")
        aad = EncryptionContext(EncryptionPurpose.CONFIG_SECRET, UUID(new.namespace_id), UUID(new.id))
        assert crypto.decrypt(new.encrypted_secret, context=aad) == "synthetic-secret-alpha"
        with pytest.raises(CryptoError):
            crypto.decrypt(old.encrypted_secret, context=aad)

    with pytest.raises(CasdoorConfigurationError):
        save(owner, actor, workspace_id, cert_pin, etag=1, secret=SecretStr("synthetic-stale"))
    cleared = owner.clear_secret(actor, etag=2, revision_id=second.draft_revision_id)
    assert cleared.etag == 3
    assert not cleared.draft.secret_configured
    with factory() as session:
        assert session.get(CasdoorConfigRevisionExtend, str(first.draft_revision_id)).encrypted_secret is not None
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorConfigRevisionExtend)) == 3
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 3


def test_crypto_write_failure_rolls_back_revision_pointer_and_audit(storage, actor, cert_pin):
    factory, workspace_id = storage
    owner = service(factory)
    saved = save(owner, actor, workspace_id, cert_pin, etag=0, secret=SecretStr("synthetic-secret-alpha"))
    with patch.object(CasdoorCrypto, "encrypt", side_effect=CryptoError()):
        with pytest.raises(CryptoError):
            save(owner, actor, workspace_id, cert_pin, etag=1, secret=SecretStr("synthetic-secret-beta"))
    current = owner.get(actor)
    assert current.etag == 1
    assert current.draft_revision_id == saved.draft_revision_id
    with factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorConfigRevisionExtend)) == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 1


def test_static_validation_is_read_only_and_does_not_supply_activation_proof(storage, actor, cert_pin):
    factory, workspace_id = storage
    owner = service(factory)
    saved = save(owner, actor, workspace_id, cert_pin, etag=0, secret=SecretStr("synthetic-secret-alpha"))
    checked = owner.validate_static(actor, etag=1, revision_id=saved.draft_revision_id, now=synthetic.NOW)
    assert checked.revision_id == saved.draft_revision_id
    assert checked.etag == 1
    with factory() as session:
        repo = owner._repository(session)
        assert repo.deployment_proof_fingerprint is None
        with session.begin():
            with pytest.raises(CasdoorConfigurationError):
                repo.activate(etag=1, revision_id=saved.draft_revision_id, actor_account_id=ACTOR_ID, now=synthetic.NOW)
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorValidationExtend)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 1


def test_readonly_repository_seams_do_not_flush_caller_pending_rows(storage, actor, cert_pin):
    factory, workspace_id = storage
    owner = service(factory)
    saved = save(owner, actor, workspace_id, cert_pin, etag=0, secret=SecretStr("synthetic-secret-alpha"))
    with factory() as session:
        pending = Tenant(name="Pending synthetic caller row")
        session.add(pending)
        repo = owner._repository(session)
        with patch.object(session, "flush", wraps=session.flush) as flush:
            repo.get()
            page = repo.list_workspaces(page=1, limit=50)
            repo.validate_static(etag=1, revision_id=saved.draft_revision_id, now=synthetic.NOW)
            flush.assert_not_called()
        assert pending in session.new
        assert page.total == 1


def test_global_workspace_page_metadata_is_stable_and_never_skips_unavailable_earliest(storage, actor):
    factory, initial_workspace_id = storage
    start = synthetic.NOW.replace(tzinfo=None)
    with factory.begin() as session:
        initial = session.get(Tenant, initial_workspace_id)
        initial.created_at = start + timedelta(days=2)
        initial.status = TenantStatus.NORMAL
        for number in range(102):
            workspace = Tenant(name=f"Independent synthetic workspace {number}")
            workspace.id = f"30000000-0000-4000-8000-{number:012d}"
            workspace.created_at = start
            workspace.status = TenantStatus.ARCHIVE if number == 0 else TenantStatus.NORMAL
            session.add(workspace)
    owner = service(factory)
    page_one = owner.workspaces(actor, page=1, limit=100)
    page_two = owner.workspaces(actor, page=2, limit=100)
    assert page_one.total == page_two.total == 103
    assert len(page_one.workspaces) == 100
    assert page_one.has_more
    assert len(page_two.workspaces) == 3
    assert not page_two.has_more
    assert page_one.earliest_created_ambiguous
    assert page_two.earliest_created_ambiguous
    assert page_one.workspaces[0].workspace_id == page_two.earliest_created_workspace.workspace_id
    assert not page_two.earliest_created_workspace.available
    assert str(page_one.workspaces[0].workspace_id).endswith("000000000000")
    assert page_two.workspaces[-1].workspace_id == UUID(initial_workspace_id)
    with factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CasdoorIntegrationExtend)) == 0
