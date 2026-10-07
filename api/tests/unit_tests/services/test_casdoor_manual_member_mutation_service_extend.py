"""D24-A1 actual shared LOCAL writers; SQLite is not cross-engine lock proof."""

import socket
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from enums import DeploymentEdition
from core.casdoor.manual_ownership import ManualMemberScope, ManualMutationKind
from models.account import (
    Account,
    AccountIntegrate,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
)
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import CasdoorConfigRevisionExtend as Revision
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorIntegrationExtend as Integration
from models.casdoor_extend import (
    CasdoorIntentKind,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
    CasdoorOperationState,
    CasdoorTerminationState,
)
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import (
    InvitationAuthorityLifecycleExtend as Lifecycle,
)
from models.invitation_authority_extend import (
    InvitationAuthorityIssuanceExtend as Issuance,
)
from models.model import App
from models.dataset import Dataset
from repositories.casdoor_manual_ownership_repository_extend import (
    CasdoorManualOwnershipRepository,
)
from services.account_service import TenantService
from services.errors.account import NoPermissionError, RoleAlreadyAssignedError


@pytest.fixture(autouse=True)
def local_mode(config_overrides, monkeypatch):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    attempts = []

    def deny(*args, **kwargs):
        attempts.append(1)
        raise AssertionError("network denied")

    for name in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    yield
    assert attempts == []


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def connect(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @sa.event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    for model in (
        Account,
        AccountIntegrate,
        Tenant,
        TenantAccountJoin,
        Integration,
        Namespace,
        Revision,
        Identity,
        History,
        Intent,
        Lifecycle,
        Issuance,
        App,
        Dataset,
    ):
        model.__table__.create(engine)
    quota_table = AccountMoneyExtend.__table__.to_metadata(sa.MetaData())
    for column in quota_table.columns:
        column.server_default = None
    quota_table.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        integration = Integration(enabled=False)
        account = Account(
            name="Synthetic", email="manual@example.test", status=AccountStatus.ACTIVE
        )
        workspace = Tenant(name="Synthetic")
        session.add_all([integration, account, workspace])
        session.flush()
        namespace = Namespace(
            integration_id=integration.id,
            expected_issuer="https://example.test",
            organization="Org",
            application="App",
            client_id="Client",
            core_fingerprint="a" * 64,
        )
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="a" * 64,
            browser_frontend_url="https://example.test",
            backend_api_url="https://example.test",
            expected_issuer="https://example.test",
            organization="Org",
            application="App",
            client_id="Client",
            button_text="Casdoor",
            default_workspace_id=workspace.id,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        identity = Identity(
            namespace_id=namespace.id,
            account_id=account.id,
            issuer=namespace.expected_issuer,
            organization="Org",
            subject="Subject",
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        join = TenantAccountJoin(
            account_id=account.id, tenant_id=workspace.id, role=TenantAccountRole.NORMAL
        )
        session.add_all([revision, identity, join])
        session.flush()
        history = History(
            namespace_id=namespace.id,
            identity_id=identity.id,
            account_id=account.id,
            workspace_id=workspace.id,
            join_id=join.id,
            revision_id=revision.id,
            ownership=CasdoorMembershipOwnership.MANAGED,
            ownership_epoch=3,
            source=CasdoorMembershipSource.MAPPING,
            desired_generation=5,
            last_applied_roles_json="original-applied",
            last_applied_fingerprint="b" * 64,
            desired_roles_json="original-desired",
            baseline_json='"' + "x" * 65533 + '"',
        )
        session.add(history)
        operator = Account(
            name="Owner", email="owner@example.test", status=AccountStatus.ACTIVE
        )
        session.add(operator)
        session.flush()
        owner_join = TenantAccountJoin(
            account_id=operator.id, tenant_id=workspace.id, role=TenantAccountRole.OWNER
        )
        session.add(owner_join)
        session.commit()
        yield SimpleNamespace(
            engine=engine,
            operator=operator,
            owner_join=owner_join,
            session=session,
            integration=integration,
            namespace=namespace,
            revision=revision,
            identity=identity,
            account=account,
            workspace=workspace,
            join=join,
            history=history,
            scope=ManualMemberScope(UUID(account.id), UUID(workspace.id)),
            repo=CasdoorManualOwnershipRepository(session),
        )
    engine.dispose()


def rows(s, model):
    return tuple(s.session.execute(sa.select(*model.__table__.columns)).all())


def change_role(s, role):
    return TenantService.update_member_role(
        s.workspace, s.account, role, s.operator, session=s.session
    )


def remove(s):
    return TenantService.remove_member_from_tenant(
        s.workspace, s.account, s.operator, session=s.session
    )


@pytest.mark.parametrize(
    "initial,target", [("normal", "admin"), ("admin", "normal"), ("normal", "editor")]
)
def test_actual_shared_role_writer_marks_override_and_retains_original_fields(
    storage, initial, target
):
    s = storage
    s.join.role = initial
    s.session.commit()
    before = rows(s, History)[0]
    change_role(s, target)
    after = rows(s, History)[0]
    assert after.ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
    assert after.ownership_epoch == before.ownership_epoch + 1
    assert s.session.get(TenantAccountJoin, s.join.id).role == target
    for name in History.__table__.columns.keys():
        if name not in {"ownership", "ownership_epoch", "updated_at"}:
            assert getattr(after, name) == getattr(before, name)
    assert rows(s, Lifecycle)[0].epoch == 1


@pytest.mark.parametrize("action", ["role", "remove"])
def test_actual_writer_preserves_quota_old_provider_and_account_rows(storage, action):
    s = storage
    s.session.add(
        AccountIntegrate(
            account_id=s.account.id,
            provider="github",
            open_id="synthetic-old",
            encrypted_token="synthetic-encrypted-fixture",
        )
    )
    s.session.execute(
        sa.insert(AccountMoneyExtend).values(
            id=str(uuid4()),
            account_id=s.account.id,
            total_quota=123.456789,
            used_quota=8.7654321,
            created_at=datetime(2020, 1, 1),
            updated_at=datetime(2021, 1, 1),
        )
    )
    s.session.commit()
    before = {
        model: rows(s, model)
        for model in (Account, AccountIntegrate, AccountMoneyExtend)
    }
    change_role(s, "admin") if action == "role" else remove(s)
    assert before == {model: rows(s, model) for model in before}


def test_actual_shared_removal_marks_tombstone_and_retains_join_id(storage):
    s = storage
    old_join_id = s.join.id
    remove(s)
    after = rows(s, History)[0]
    assert after.ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
    assert (
        after.tombstone and after.join_id == old_join_id and after.ownership_epoch == 4
    )
    assert s.session.get(TenantAccountJoin, old_join_id) is None
    assert rows(s, Lifecycle)[0].state == "withdrawn"


def add_intent(s, state):
    intent = Intent(
        namespace_id=s.namespace.id,
        identity_id=s.identity.id,
        account_id=s.account.id,
        workspace_id=s.workspace.id,
        revision_id=s.revision.id,
        generation=1,
        ownership_epoch=3,
        fence_epoch=0,
        kind=CasdoorIntentKind.INVITATION_FINALIZE,
        scope_digest="c" * 64,
        idempotency_key=uuid4().hex * 2,
        desired_json="{}",
        operation_state=state,
        termination_state=CasdoorTerminationState.CONFIRMED,
    )
    s.session.add(intent)
    s.session.commit()


@pytest.mark.parametrize("state", list(CasdoorOperationState))
@pytest.mark.parametrize("action", ["role", "remove"])
def test_every_retained_required_intent_state_denies_before_business_write(
    storage, state, action
):
    s = storage
    add_intent(s, state)
    before = {
        model: rows(s, model)
        for model in (History, TenantAccountJoin, Account, Lifecycle, Intent)
    }
    with pytest.raises(NoPermissionError, match="authorization pending"):
        change_role(s, "admin") if action == "role" else remove(s)
    assert before == {model: rows(s, model) for model in before}


@pytest.mark.parametrize("action", ["role", "remove"])
@pytest.mark.parametrize("dirty", ["dirty", "new", "deleted", "nested"])
def test_dirty_entry_rejects_before_old_authorization_queries_autoflush(
    storage, action, dirty
):
    s = storage
    if dirty == "dirty":
        s.account.name = "Unflushed"
    elif dirty == "new":
        s.session.add(
            Account(name="New", email="new@example.test", status=AccountStatus.ACTIVE)
        )
    elif dirty == "deleted":
        s.session.delete(s.history)
    else:
        s.session.begin_nested()
    statements = []

    def observe(*args):
        statements.append(1)

    sa.event.listen(s.engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(NoPermissionError):
            change_role(s, "admin") if action == "role" else remove(s)
        assert statements == []
    finally:
        sa.event.remove(s.engine, "before_cursor_execute", observe)
        s.session.rollback()


def test_same_role_preserves_original_error_and_metadata(storage):
    s = storage
    before = rows(s, History)
    with pytest.raises(RoleAlreadyAssignedError):
        change_role(s, "normal")
    assert rows(s, History) == before and rows(s, Lifecycle) == ()


@pytest.mark.parametrize("action", ["role", "remove", "transfer"])
def test_unmanaged_original_outcomes_without_creating_history(storage, action):
    s = storage
    s.session.delete(s.history)
    s.session.commit()
    if action == "remove":
        remove(s)
        assert s.session.get(TenantAccountJoin, s.join.id) is None
    else:
        change_role(s, "owner" if action == "transfer" else "admin")
        assert s.join.role == ("owner" if action == "transfer" else "admin")
        if action == "transfer":
            assert s.owner_join.role == "normal"
            assert len(rows(s, Lifecycle)) == 2
    assert rows(s, History) == ()


def test_actual_transfer_prepares_both_resolved_accounts_before_any_role_write(
    storage, monkeypatch
):
    s = storage
    original = CasdoorManualOwnershipRepository.prepare
    seen = []

    def prepare(repo, scopes, **kwargs):
        assert not s.session.dirty
        assert s.owner_join.role == "owner" and s.join.role == "normal"
        seen.append((scopes, kwargs))
        return original(repo, scopes, **kwargs)

    monkeypatch.setattr(CasdoorManualOwnershipRepository, "prepare", prepare)
    change_role(s, "owner")
    assert len(seen) == 1
    assert {scope.account_id for scope in seen[0][0]} == {
        UUID(s.account.id),
        UUID(s.operator.id),
    }
    assert seen[0][1]["kind"] is ManualMutationKind.OWNER_TRANSFER
    assert s.owner_join.role == "normal" and s.join.role == "owner"
    assert rows(s, History)[0].ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE


def test_managed_standing_owner_blocks_complete_transfer_without_partial_override(
    storage,
):
    s = storage
    s.history.account_id = s.operator.id
    s.history.join_id = s.owner_join.id
    s.identity.account_id = s.operator.id
    s.session.commit()
    before = rows(s, History), rows(s, TenantAccountJoin)
    with pytest.raises(NoPermissionError):
        change_role(s, "owner")
    assert before == (rows(s, History), rows(s, TenantAccountJoin))


def test_original_admin_cannot_assign_owner_or_demote_owner(storage):
    s = storage
    s.owner_join.role = "admin"
    s.session.commit()
    before = rows(s, History), rows(s, TenantAccountJoin)
    with pytest.raises(NoPermissionError):
        change_role(s, "owner")
    assert before == (rows(s, History), rows(s, TenantAccountJoin))


def test_already_local_removal_preserves_original_foundation_metadata_limit(storage):
    s = storage
    s.history.ownership = CasdoorMembershipOwnership.LOCAL_OVERRIDE
    s.session.commit()
    before = rows(s, History)
    remove(s)
    assert rows(s, History) == before
    assert (
        not before[0].tombstone and s.session.get(TenantAccountJoin, s.join.id) is None
    )


@pytest.mark.parametrize("action", ["role", "remove"])
def test_real_authority_failure_is_rolled_back_by_caller_root(storage, action):
    s = storage
    before = {
        model: rows(s, model)
        for model in (History, TenantAccountJoin, Account, Lifecycle)
    }

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO invitation_authority_lifecycle_extend"):
            raise RuntimeError("actual authority SQL failure")

    sa.event.listen(s.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="actual authority SQL failure"):
            change_role(s, "admin") if action == "role" else remove(s)
    finally:
        sa.event.remove(s.engine, "before_cursor_execute", fail)
        s.session.rollback()
    assert before == {model: rows(s, model) for model in before}


@pytest.mark.parametrize("action", ["role", "remove"])
def test_real_committed_then_unknown_ack_is_not_claimed_rolled_back(
    storage, monkeypatch, action
):
    s = storage
    commit = s.session.commit

    def unknown():
        commit()
        raise RuntimeError("commit acknowledgement unknown")

    monkeypatch.setattr(s.session, "commit", unknown)
    with pytest.raises(RuntimeError, match="acknowledgement unknown"):
        change_role(s, "admin") if action == "role" else remove(s)
    s.session.rollback()
    assert rows(s, History)[0].ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
    if action == "remove":
        assert (
            rows(s, History)[0].tombstone
            and s.session.get(TenantAccountJoin, s.join.id) is None
        )
    else:
        assert s.session.get(TenantAccountJoin, s.join.id).role == "admin"


@pytest.mark.parametrize("other_workspace", [False, True])
def test_actual_pending_removal_keeps_original_orphan_policy(storage, other_workspace):
    s = storage
    s.account.status = AccountStatus.PENDING
    if other_workspace:
        second = Tenant(name="Second")
        s.session.add(second)
        s.session.flush()
        s.session.add(
            TenantAccountJoin(
                account_id=s.account.id, tenant_id=second.id, role="normal"
            )
        )
    s.session.commit()
    account_id = s.account.id
    remove(s)
    assert (s.session.get(Account, account_id) is not None) == other_workspace
    assert rows(s, History)[0].tombstone


@pytest.mark.parametrize("failure", [False, True])
def test_actual_removal_reassigns_only_scoped_maintainers_and_rollback_is_whole_root(
    storage, failure
):
    s = storage
    app = App(
        tenant_id=s.workspace.id,
        name="Synthetic",
        mode="chat",
        enable_site=False,
        enable_api=False,
        created_by=s.account.id,
        maintainer=s.account.id,
    )
    dataset = Dataset(
        tenant_id=s.workspace.id,
        name="Synthetic",
        created_by=s.account.id,
        maintainer=s.account.id,
        data_source_type="upload_file",
    )
    s.session.add_all([app, dataset])
    s.session.commit()
    before = {
        model: rows(s, model)
        for model in (History, TenantAccountJoin, App, Dataset, Lifecycle)
    }

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM tenant_account_joins"):
            raise RuntimeError("actual Join deletion failure")

    if failure:
        sa.event.listen(s.engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(RuntimeError, match="actual Join deletion failure"):
                remove(s)
        finally:
            sa.event.remove(s.engine, "before_cursor_execute", fail)
            s.session.rollback()
        assert before == {model: rows(s, model) for model in before}
    else:
        remove(s)
        for model in (App, Dataset):
            after = rows(s, model)[0]
            assert (
                after.maintainer == s.operator.id and after.created_by == s.account.id
            )


@pytest.mark.parametrize(
    "ownership",
    [CasdoorMembershipOwnership.RELEASED, CasdoorMembershipOwnership.MANAGED],
)
def test_actual_all_namespace_histories_mixed_refusal_or_complete_override(
    storage, ownership
):
    s = storage
    namespace = Namespace(
        **{
            column.name: getattr(s.namespace, column.name)
            for column in Namespace.__table__.columns
            if column.name not in {"id", "created_at"}
        }
    )
    namespace.id = str(uuid4())
    s.session.add(namespace)
    s.session.flush()
    revision = Revision(
        **{
            column.name: getattr(s.revision, column.name)
            for column in Revision.__table__.columns
            if column.name not in {"id", "created_at"}
        }
    )
    revision.id = str(uuid4())
    revision.namespace_id = namespace.id
    revision.revision_number = 2
    s.session.add(revision)
    s.session.flush()
    clone = History(
        **{
            column.name: getattr(s.history, column.name)
            for column in History.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        }
    )
    clone.id = str(uuid4())
    clone.namespace_id = namespace.id
    clone.revision_id = revision.id
    clone.identity_id = str(uuid4())
    clone.ownership = ownership
    s.session.add(clone)
    s.session.commit()
    before = rows(s, History), rows(s, TenantAccountJoin)
    if ownership is CasdoorMembershipOwnership.RELEASED:
        with pytest.raises(NoPermissionError):
            change_role(s, "admin")
        assert before == (rows(s, History), rows(s, TenantAccountJoin))
    else:
        change_role(s, "admin")
        assert len(rows(s, History)) == 2
        assert all(
            row.ownership == CasdoorMembershipOwnership.LOCAL_OVERRIDE
            and row.ownership_epoch == 4
            for row in rows(s, History)
        )
