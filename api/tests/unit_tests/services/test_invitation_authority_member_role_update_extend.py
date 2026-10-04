"""Sequential shared membership role updates; caller owns SQL and external effects."""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, sentinel
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories import invitation_authority_repository_extend as authority_module
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from services import account_service
from services.account_service import TenantService


@pytest.fixture(autouse=True)
def local_configuration(config_overrides):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)


def seed(session, prior=None, *, role="normal", existing=True, epoch=7):
    tenant, other = Tenant(name="Role update"), Tenant(name="Protected")
    account = Account(name="Member", email="role-update@example.test", status=AccountStatus.ACTIVE)
    session.add_all([tenant, other, account])
    session.flush()
    if existing:
        session.add(TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=TenantAccountRole(role)))
    authority = InvitationAuthorityRepository()
    if prior:
        record = authority.set_lifecycle_state(session, account_id=account.id, workspace_id=tenant.id, state=prior)
        row = session.get(Lifecycle, record.lifecycle_id)
        row.epoch = epoch
        row.updated_at = datetime(2020, 1, 1)
    protected = authority.set_lifecycle_state(session, account_id=account.id, workspace_id=other.id, state="withdrawn")
    session.commit()
    return SimpleNamespace(
        tenant=tenant, account=account, account_id=account.id, workspace_id=tenant.id, protected=protected
    )


def lifecycle(session, s):
    return InvitationAuthorityRepository().get_lifecycle(session, account_id=s.account_id, workspace_id=s.workspace_id)


def join(session, s):
    return session.scalar(
        sa.select(TenantAccountJoin).where(
            TenantAccountJoin.account_id == s.account_id, TenantAccountJoin.tenant_id == s.workspace_id
        )
    )


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("old,new", [("normal", "admin"), ("admin", "editor"), ("editor", "normal")])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_existing_role_change_after_flush_uses_caller_root_and_preserves_lifecycle_state(
    sqlite_session_factory, monkeypatch, prior, old, new, finish
):
    with sqlite_session_factory() as session:
        s = seed(session, prior, role=old)
        before = lifecycle(session, s)
        original_join = join(session, s)
        join_id = original_join.id
        root = session.get_transaction()
        event = InvitationAuthorityRepository.record_membership_role_change
        calls = []

        def observed(repo, supplied, **kwargs):
            assert supplied is session
            assert kwargs == dict(account_id=s.account_id, workspace_id=s.workspace_id)
            assert supplied.get_transaction() is root
            assert not supplied.new and not supplied.dirty and not supplied.deleted
            assert supplied.scalar(sa.select(TenantAccountJoin.role).where(TenantAccountJoin.id == join_id)) == new
            calls.append(1)
            return event(repo, supplied, **kwargs)

        creation = Mock(side_effect=AssertionError("existing Join must not emit creation"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", creation)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", observed)
        with monkeypatch.context() as guard:
            for name in ("begin", "begin_nested", "commit", "rollback", "close"):
                guard.setattr(session, name, Mock(side_effect=AssertionError(name)))
            result = TenantService.persist_tenant_member(s.tenant, s.account, session, new)
            assert not result.membership_created and result.join is original_join
            assert result.join.id == join_id and result.join.role == new and calls == [1]
            after = lifecycle(session, s)
            assert after.epoch == (8 if before else 1) and after.state == (prior or "active")
            assert UUID(after.lifecycle_id).version == 4
            if before:
                assert (after.lifecycle_id, after.created_at, after.state) == (
                    before.lifecycle_id,
                    before.created_at,
                    before.state,
                )
                assert after.updated_at > before.updated_at
            assert session.get_transaction() is root
            with sqlite_session_factory() as observer:
                assert join(observer, s).role == old and lifecycle(observer, s) == before
            assert not TenantService.persist_tenant_member(s.tenant, s.account, session, new).membership_created
            assert calls == [1] and lifecycle(session, s) == after
        getattr(session, finish)()
        with sqlite_session_factory() as observer:
            assert join(observer, s).id == join_id
            assert join(observer, s).role == (new if finish == "commit" else old)
            assert lifecycle(observer, s) == (after if finish == "commit" else before)
            assert (
                InvitationAuthorityRepository().get_lifecycle(
                    observer, account_id=s.protected.account_id, workspace_id=s.protected.workspace_id
                )
                == s.protected
            )
        creation.assert_not_called()


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
@pytest.mark.parametrize("role", ["normal", "editor", "admin"])
def test_same_role_reentry_never_emits_any_event_even_at_max_epoch(sqlite_session_factory, monkeypatch, prior, role):
    with sqlite_session_factory() as session:
        s = seed(session, prior, role=role, epoch=MAX_LIFECYCLE_EPOCH)
        before = lifecycle(session, s)
        event = Mock(side_effect=AssertionError("same role emits no event"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", event)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        result = TenantService.persist_tenant_member(s.tenant, s.account, session, role)
        assert not result.membership_created and result.join.role == role
        session.commit()
        with sqlite_session_factory() as observer:
            assert lifecycle(observer, s) == before
        event.assert_not_called()


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
def test_new_join_emits_only_creation_and_reactivates_by_existing_contract(sqlite_session_factory, monkeypatch, prior):
    with sqlite_session_factory() as session:
        s = seed(session, prior, existing=False)
        before = lifecycle(session, s)
        original = InvitationAuthorityRepository.record_membership_creation
        calls = []

        def creation(repo, supplied, **kwargs):
            calls.append(1)
            assert supplied is session and not session.new
            assert join(session, s).role is TenantAccountRole.ADMIN
            return original(repo, supplied, **kwargs)

        role_event = Mock(side_effect=AssertionError("new Join must not emit role change"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", creation)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", role_event)
        result = TenantService.persist_tenant_member(s.tenant, s.account, session, "admin")
        assert result.membership_created and calls == [1]
        session.commit()
        with sqlite_session_factory() as observer:
            after = lifecycle(observer, s)
            assert after.state == "active" and after.epoch == (8 if before else 1)
        role_event.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["epoch_active", "epoch_withdrawn", "unique", "insert_after_write", "update_after_write", "schema"]
)
def test_lifecycle_failure_after_role_flush_escapes_and_caller_rolls_back(sqlite_session_factory, monkeypatch, failure):
    with sqlite_session_factory() as session:
        prior = (
            "withdrawn"
            if failure == "epoch_withdrawn"
            else "active"
            if failure in ("epoch_active", "update_after_write")
            else None
        )
        s = seed(session, prior, epoch=MAX_LIFECYCLE_EPOCH if failure.startswith("epoch") else 7)
        before = lifecycle(session, s)
        session.commit()
        if failure == "unique":
            monkeypatch.setattr(authority_module, "uuid4", lambda: UUID(s.protected.lifecycle_id))
        if failure == "schema":
            Lifecycle.__table__.drop(session.get_bind())
        writes, commits, calls = [], [], []
        original = InvitationAuthorityRepository.record_membership_role_change

        def event(repo, supplied, **kwargs):
            calls.append(1)
            assert supplied is session
            assert (
                supplied.scalar(sa.select(TenantAccountJoin.role).where(TenantAccountJoin.account_id == s.account_id))
                is TenantAccountRole.ADMIN
            )
            return original(repo, supplied, **kwargs)

        def observed(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith(
                ("UPDATE tenant_account_joins", "INSERT INTO invitation_authority", "UPDATE invitation_authority")
            ):
                writes.append(statement)
                if failure.endswith("after_write") and "invitation_authority" in statement:
                    raise RuntimeError("lifecycle_write_failed")

        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        sa.event.listen(session, "after_commit", lambda _: commits.append(1))
        engine = session.get_bind()
        sa.event.listen(engine, "after_cursor_execute", observed)
        expected = (
            InvitationAuthorityConflict
            if failure.startswith("epoch")
            else IntegrityError
            if failure == "unique"
            else OperationalError
            if failure == "schema"
            else RuntimeError
        )
        try:
            with pytest.raises(expected):
                TenantService.persist_tenant_member(s.tenant, s.account, session, "admin")
            assert calls == [1] and commits == []
            assert writes[0].startswith("UPDATE tenant_account_joins")
        finally:
            sa.event.remove(engine, "after_cursor_execute", observed)
            session.rollback()
        with sqlite_session_factory() as observer:
            assert join(observer, s).role is TenantAccountRole.NORMAL
            if failure != "schema":
                assert lifecycle(observer, s) == before
                assert (
                    InvitationAuthorityRepository().get_lifecycle(
                        observer, account_id=s.protected.account_id, workspace_id=s.protected.workspace_id
                    )
                    == s.protected
                )


def test_join_flush_failure_never_reaches_role_event(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        s = seed(session, "active")
        before = lifecycle(session, s)
        event = Mock(side_effect=AssertionError("event before Join flush"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        real_flush = session.flush

        def fail_role_flush(objects=None):
            if any(
                isinstance(row, TenantAccountJoin) and sa.inspect(row).attrs.role.history.has_changes()
                for row in session.dirty
            ):
                raise RuntimeError("join_flush_failed")
            return real_flush(objects)

        monkeypatch.setattr(session, "flush", fail_role_flush)
        with pytest.raises(RuntimeError, match="join_flush_failed"):
            TenantService.persist_tenant_member(s.tenant, s.account, session, "admin")
        session.rollback()
        event.assert_not_called()
        assert join(session, s).role is TenantAccountRole.NORMAL and lifecycle(session, s) == before


@pytest.mark.parametrize("consumed", [False, True])
def test_actual_role_update_fences_prior_issuance_and_consumption_replay(sqlite_session_factory, consumed):
    with sqlite_session_factory() as session:
        s = seed(session, "active")
        authority = InvitationAuthorityRepository()
        before = lifecycle(session, s)
        facts = dict(
            schema_version=1,
            issuance_id=str(uuid4()),
            lifecycle_id=before.lifecycle_id,
            lifecycle_epoch=before.epoch,
            token_digest="a" * 64,
            join_id_at_issue=join(session, s).id,
        )
        issued = authority.record_issuance(
            session,
            payload_json=json.dumps(
                dict(
                    account_id=s.account_id,
                    workspace_id=s.workspace_id,
                    email=s.account.email,
                    role="normal",
                    requires_setup=False,
                    invitation_authority=facts,
                )
            ),
        )
        receipt = json.dumps(
            dict(
                **facts,
                status="consumed",
                operation_id=str(uuid4()),
                account_id=s.account_id,
                workspace_id=s.workspace_id,
                payload_digest=issued.payload_digest,
                key_digest="b" * 64,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        if consumed:
            issued = authority.record_consumption(session, receipt_json=receipt)
        session.commit()
        TenantService.persist_tenant_member(s.tenant, s.account, session, "admin")
        session.commit()
        with pytest.raises(InvitationAuthorityConflict, match="invitation_lifecycle_conflict"):
            authority.record_consumption(session, receipt_json=receipt)
        session.rollback()
        assert lifecycle(session, s).epoch == before.epoch + 1
        assert authority.get_issuance(session, issuance_id=issued.issuance_id) == issued


def test_p3e_local_seam_delegates_exact_session_without_changing_cas_contract(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        event = Mock(return_value=sentinel.lifecycle)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_role_change", event)
        account_id, workspace_id = str(uuid4()), str(uuid4())
        result = InvitationAuthorityRepository().record_local_role_change(
            session, account_id=account_id, workspace_id=workspace_id
        )
        assert result is sentinel.lifecycle
        event.assert_called_once_with(session, account_id=account_id, workspace_id=workspace_id)


@pytest.mark.parametrize("fail_cache", [False, True])
def test_public_update_commits_before_cache_and_never_queues_creation_task(
    sqlite_session_factory, monkeypatch, config_overrides, fail_cache
):
    import tasks.initialize_created_app_rbac_access_task as tasks

    config_overrides(DEPLOYMENT_EDITION=DeploymentEdition.CLOUD, RBAC_ENABLED=True)
    with sqlite_session_factory() as session:
        s = seed(session, "active")
        order = []
        sa.event.listen(session, "after_commit", lambda _: order.append("commit"))

        def cache(*_args):
            assert order == ["commit"]
            with sqlite_session_factory() as observer:
                assert join(observer, s).role is TenantAccountRole.ADMIN
                assert lifecycle(observer, s).epoch == 8
            order.append("cache")
            if fail_cache:
                raise RuntimeError("cache_failed_after_commit")

        monkeypatch.setattr(account_service.BillingService, "clean_billing_info_cache", Mock(side_effect=cache))
        task = Mock(side_effect=AssertionError("existing member must not queue creation task"))
        monkeypatch.setattr(tasks.sync_joined_workspace_member_rbac_access_task, "delay", task)
        if fail_cache:
            with pytest.raises(RuntimeError, match="cache_failed_after_commit"):
                TenantService.create_tenant_member(s.tenant, s.account, session, "admin")
            session.rollback()
        else:
            result = TenantService.create_tenant_member(s.tenant, s.account, session, "admin")
            assert result.role is TenantAccountRole.ADMIN
        assert order == ["commit", "cache"]
        task.assert_not_called()
        with sqlite_session_factory() as observer:
            assert lifecycle(observer, s).epoch == 8 and join(observer, s).role is TenantAccountRole.ADMIN
