"""Independent sequential checks for invitation activation lifecycle writes."""

import json
import socket
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
from repositories import invitation_authority_repository_extend as authority_module
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from repositories.invitation_authority_repository_extend import (
    MAX_LIFECYCLE_EPOCH,
    InvitationAuthorityConflict,
    InvitationAuthorityRepository,
)
from services.entities.account_activation_entities import AccountInvitation, AccountSetup


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    attempts = []

    def reject(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("network access denied in offline activation verification")

    for method in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, method, reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)
    yield
    assert attempts == []


def prepare(session, *, lifecycle_state=None, epoch=5, existing=False):
    account = Account(
        name="Waiting for setup",
        email="independent-activation@example.test",
        status=AccountStatus.PENDING,
    )
    target = Tenant(name="Invite destination")
    previous = Tenant(name="Previous current workspace")
    session.add_all([account, target, previous])
    session.flush()
    current = TenantAccountJoin(
        account_id=account.id,
        tenant_id=previous.id,
        role=TenantAccountRole.NORMAL,
        current=True,
    )
    session.add(current)
    if existing:
        session.add(
            TenantAccountJoin(
                account_id=account.id,
                tenant_id=target.id,
                role=TenantAccountRole.EDITOR,
                current=False,
            )
        )
    authority = InvitationAuthorityRepository()
    if lifecycle_state is not None:
        record = authority.set_lifecycle_state(
            session,
            account_id=account.id,
            workspace_id=target.id,
            state=lifecycle_state,
        )
        lifecycle_row = session.get(InvitationAuthorityLifecycleExtend, record.lifecycle_id)
        lifecycle_row.epoch = epoch
        lifecycle_row.updated_at = datetime(2022, 1, 1)
    protected = authority.set_lifecycle_state(
        session,
        account_id=account.id,
        workspace_id=previous.id,
        state="active",
    )
    session.commit()
    invitation = AccountInvitation(
        account_id=account.id,
        account_email=account.email,
        account_status=AccountStatus.PENDING.value,
        workspace_id=target.id,
        workspace_name=target.name,
        role="admin",
        requires_setup=True,
    )
    return SimpleNamespace(
        account_id=account.id,
        target_id=target.id,
        previous_id=previous.id,
        invitation=invitation,
        protected=protected,
        authority=authority,
    )


def get_join(session, state, workspace_id=None):
    return session.scalar(
        sa.select(TenantAccountJoin).where(
            TenantAccountJoin.account_id == state.account_id,
            TenantAccountJoin.tenant_id == (workspace_id or state.target_id),
        )
    )


def get_lifecycle(session, state):
    return state.authority.get_lifecycle(session, account_id=state.account_id, workspace_id=state.target_id)


def setup_payload():
    return AccountSetup(name="Finished profile", interface_language="en-GB", timezone="Europe/London")


def assert_database_before(session, state, old_lifecycle):
    assert get_join(session, state) is None
    assert get_join(session, state, state.previous_id).current
    person = session.get(Account, state.account_id)
    assert person.name == "Waiting for setup" and person.status == AccountStatus.PENDING
    assert person.initialized_at is None
    assert get_lifecycle(session, state) == old_lifecycle
    assert (
        state.authority.get_lifecycle(session, account_id=state.account_id, workspace_id=state.previous_id)
        == state.protected
    )


@pytest.mark.parametrize("old_state", [None, "active", "withdrawn"])
def test_new_membership_records_one_lifecycle_in_the_callers_transaction(
    sqlite_session_factory, monkeypatch, old_state
):
    with sqlite_session_factory() as session:
        state = prepare(session, lifecycle_state=old_state)
        old = get_lifecycle(session, state)
        root_transaction = session.get_transaction()
        explicit_flushes = []
        original_flush = session.flush
        original_record = InvitationAuthorityRepository.record_membership_creation
        event_scopes = []

        def record_flush(objects=None):
            if objects is not None:
                explicit_flushes.append(list(objects))
            return original_flush(objects)

        def observe_record(repository, supplied, **scope):
            assert supplied is session
            assert scope == {"account_id": state.account_id, "workspace_id": state.target_id}
            created_join = get_join(supplied, state)
            assert explicit_flushes[-1] == [created_join]
            assert sa.inspect(created_join).persistent
            assert created_join.role == TenantAccountRole.ADMIN and created_join.current
            assert created_join.last_opened_at is not None
            assert supplied.get(Account, state.account_id).name == "Finished profile"
            with sqlite_session_factory() as outside:
                assert get_join(outside, state) is None
                assert get_lifecycle(outside, state) == old
            event_scopes.append(scope)
            return original_record(repository, supplied, **scope)

        monkeypatch.setattr(session, "flush", record_flush)
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", observe_record)
        with monkeypatch.context() as guard:
            for method in ("begin", "begin_nested", "commit", "rollback", "close"):
                guard.setattr(session, method, Mock(side_effect=AssertionError(f"helper invoked {method}")))
            outcome = SQLAlchemyAccountActivationRepository(sqlite_session_factory).persist_activation(
                state.invitation,
                role="admin",
                setup=setup_payload(),
                session=session,
            )
            assert outcome.membership_created and event_scopes == [
                {"account_id": state.account_id, "workspace_id": state.target_id}
            ]
            assert session.get_transaction() is root_transaction
            current = get_lifecycle(session, state)
            assert current.state == "active"
            assert current.epoch == (old.epoch + 1 if old else 1)
            if old:
                assert (current.lifecycle_id, current.created_at) == (old.lifecycle_id, old.created_at)
                assert current.updated_at > old.updated_at
            with sqlite_session_factory() as outside:
                assert_database_before(outside, state, old)

        session.rollback()
        with sqlite_session_factory() as outside:
            assert_database_before(outside, state, old)


def test_existing_membership_preserves_role_and_skips_lifecycle_event(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = prepare(
            session,
            lifecycle_state="active",
            epoch=MAX_LIFECYCLE_EPOCH,
            existing=True,
        )
        before = get_lifecycle(session, state)
        original_join = get_join(session, state)
        forbidden = Mock(side_effect=AssertionError("existing membership emitted event"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", forbidden)
        outcome = SQLAlchemyAccountActivationRepository(sqlite_session_factory).persist_activation(
            state.invitation, role="admin", setup=None, session=session
        )
        assert not outcome.membership_created
        assert get_join(session, state) is original_join
        assert original_join.role == TenantAccountRole.EDITOR
        assert original_join.current and original_join.last_opened_at is not None
        assert get_join(session, state, state.previous_id).current is False
        assert get_lifecycle(session, state) == before
        forbidden.assert_not_called()
        session.rollback()


@pytest.mark.parametrize("problem", ["event_write", "epoch_limit", "unique_key"])
def test_lifecycle_errors_propagate_for_caller_rollback(sqlite_session_factory, monkeypatch, problem):
    with sqlite_session_factory() as session:
        old_state = "withdrawn" if problem == "event_write" else "active" if problem == "epoch_limit" else None
        old_epoch = MAX_LIFECYCLE_EPOCH if problem == "epoch_limit" else 5
        state = prepare(session, lifecycle_state=old_state, epoch=old_epoch)
        before = get_lifecycle(session, state)
        session.rollback()
        if problem == "unique_key":
            monkeypatch.setattr(authority_module, "uuid4", lambda: UUID(state.protected.lifecycle_id))
            expected = IntegrityError
        elif problem == "epoch_limit":
            expected = InvitationAuthorityConflict
        else:
            expected = RuntimeError

        original_record = InvitationAuthorityRepository.record_membership_creation
        commits = []
        sa.event.listen(session, "after_commit", lambda _session: commits.append(True))

        def fail_after_event_write(repository, supplied, **scope):
            assert supplied is session
            original_record(repository, supplied, **scope)
            raise RuntimeError("injected lifecycle write error")

        if problem == "event_write":
            monkeypatch.setattr(
                InvitationAuthorityRepository,
                "record_membership_creation",
                fail_after_event_write,
            )
        with pytest.raises(expected):
            SQLAlchemyAccountActivationRepository(sqlite_session_factory).persist_activation(
                state.invitation, role="admin", setup=setup_payload(), session=session
            )
        assert commits == []
        session.rollback()
        with sqlite_session_factory() as outside:
            assert_database_before(outside, state, before)


def test_flush_error_prevents_lifecycle_call_and_outer_rollback_restores_rows(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        state = prepare(session, lifecycle_state="withdrawn")
        before = get_lifecycle(session, state)
        original_flush = session.flush
        event = Mock(side_effect=AssertionError("event called before Join flush"))
        monkeypatch.setattr(InvitationAuthorityRepository, "record_membership_creation", event)

        def reject_join_flush(objects=None):
            if objects and isinstance(objects[0], TenantAccountJoin):
                raise RuntimeError("join flush rejected")
            return original_flush(objects)

        monkeypatch.setattr(session, "flush", reject_join_flush)
        with pytest.raises(RuntimeError, match="join flush rejected"):
            SQLAlchemyAccountActivationRepository(sqlite_session_factory).persist_activation(
                state.invitation, role="admin", setup=setup_payload(), session=session
            )
        event.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as outside:
            assert_database_before(outside, state, before)


def test_activate_keeps_session_factory_begin_as_commit_owner(sqlite_session_factory, monkeypatch):
    state_holder = {}
    original_persist = SQLAlchemyAccountActivationRepository.persist_activation
    repository = SQLAlchemyAccountActivationRepository(sqlite_session_factory)

    with sqlite_session_factory() as setup_session:
        state_holder["state"] = prepare(setup_session)
        setup_session.rollback()

    observed = []

    def inspect_activation(instance, invitation, *, role, setup, session):
        assert session.get_transaction() is not None
        observed.append(session)
        return original_persist(instance, invitation, role=role, setup=setup, session=session)

    monkeypatch.setattr(repository, "persist_activation", inspect_activation.__get__(repository))
    state = state_holder["state"]
    result = repository.activate(state.invitation, role="admin", setup=setup_payload())
    assert result.membership_created and len(observed) == 1
    with sqlite_session_factory() as reader:
        assert get_join(reader, state) is not None
        event = get_lifecycle(reader, state)
        assert event.state == "active" and event.epoch == 1
        assert reader.get(Account, state.account_id).status == AccountStatus.ACTIVE


def test_old_issuance_receipt_is_fenced_after_activation_advances_epoch(sqlite_session_factory):
    with sqlite_session_factory() as session:
        state = prepare(session, lifecycle_state="active")
        before = get_lifecycle(session, state)
        issuance_facts = {
            "schema_version": 1,
            "issuance_id": str(uuid4()),
            "lifecycle_id": before.lifecycle_id,
            "lifecycle_epoch": before.epoch,
            "token_digest": "c" * 64,
            "join_id_at_issue": None,
        }
        issuance = state.authority.record_issuance(
            session,
            payload_json=json.dumps(
                {
                    "account_id": state.account_id,
                    "workspace_id": state.target_id,
                    "email": state.invitation.account_email,
                    "role": "admin",
                    "requires_setup": True,
                    "invitation_authority": issuance_facts,
                }
            ),
        )
        stale_receipt = json.dumps(
            {
                **issuance_facts,
                "status": "consumed",
                "operation_id": str(uuid4()),
                "account_id": state.account_id,
                "workspace_id": state.target_id,
                "payload_digest": issuance.payload_digest,
                "key_digest": "d" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()

        result = SQLAlchemyAccountActivationRepository(sqlite_session_factory).activate(
            state.invitation, role="admin", setup=None
        )
        assert result.membership_created
        with pytest.raises(InvitationAuthorityConflict, match="invitation_lifecycle_conflict"):
            state.authority.record_consumption(session, receipt_json=stale_receipt)
        session.rollback()
        assert state.authority.get_issuance(session, issuance_id=issuance.issuance_id) == issuance
        assert get_lifecycle(session, state).epoch == before.epoch + 1
