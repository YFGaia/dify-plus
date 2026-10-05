"""Ordinary producer -> original InvitationIssuer -> registered invitation callback.

Synthetic signed provider and offline redis-py sockets are source evidence only.
No successful intent, receipt, managed history or audit is fabricated here.
"""

import json
import traceback
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, SessionTransaction
from configs import dify_config
from enums import DeploymentEdition

from models.account import Account, AccountIntegrate, AccountStatus, Tenant, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend,
    CasdoorSyncIntentExtend,
    CasdoorIdentityExtend,
    CasdoorAuditExtend,
)
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
from repositories.casdoor_invited_login_scope_repository_extend import CasdoorInvitedLoginScopeRepository
from services import account_adapters as adapters
from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from services.invitation_issuance_service_extend import InvitationIssuer
from services.account_service import RegisterService, TenantService
from test_casdoor_invited_local_http_extend import initialization_wire
from test_casdoor_invited_local_recovery_http_extend import new_attempt, finish
from test_casdoor_local_http_extend import mounted as original_mounted, begin, callback
from test_invitation_publication_transport_extend import Wire, client_for, resp
from test_invitation_token_consumption_extend import FakeRedis as InvitationRedis

pytest_plugins = ("test_casdoor_local_http_service_extend",)
mounted = original_mounted


def retained(case):
    with Session(case.m.f.local.engine) as session:
        account = session.get(Account, case.account)
        fields = tuple(
            getattr(account, key)
            for key in (
                "id",
                "initialized_at",
                "password",
                "password_salt",
                "email",
                "normalized_email",
                "interface_language",
                "timezone",
                "interface_theme",
            )
        )
        return fields, tuple(
            tuple(
                tuple(row)
                for row in session.execute(
                    sa.select(*model.__table__.columns).order_by(*model.__table__.primary_key.columns)
                )
            )
            for model in (AccountMoneyExtend, AccountIntegrate)
        )


@pytest.fixture
def ordinary_then_invited(mounted, monkeypatch, request):
    m, f = mounted, mounted.f
    monkeypatch.setattr(dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.COMMUNITY)
    for model in (Issuance, AccountIntegrate):
        model.__table__.create(f.local.engine, checkfirst=True)
    errors = []
    original_error = f.service._public_error

    def error(error, *args):
        errors.append(
            (
                type(error).__name__,
                str(error),
                [(frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)],
            )
        )
        return original_error(error, *args)

    monkeypatch.setattr(f.service, "_public_error", error)
    original_discover = CasdoorInvitedLoginScopeRepository.discover_unconsumed_invitation

    def discover(owner, *args, **kwargs):
        try:
            return original_discover(owner, *args, **kwargs)
        except Exception as caught:
            errors.append(
                (
                    type(caught).__name__,
                    [(frame.name, frame.lineno) for frame in traceback.extract_tb(caught.__traceback__)],
                )
            )
            raise

    monkeypatch.setattr(CasdoorInvitedLoginScopeRepository, "discover_unconsumed_invitation", discover)
    first = begin(m, return_path="/apps/ordinary")
    assert callback(m, first).location.endswith("/apps/ordinary"), errors
    with Session(f.local.engine) as session:
        account_id = session.scalar(sa.select(Account.id))
        old = tuple(
            dict(row._mapping) for row in session.execute(sa.select(*CasdoorManagedMembershipExtend.__table__.columns))
        )
        assert len(old) == 2
        assert not session.scalar(sa.select(CasdoorSyncIntentExtend.id))

    invitation = InvitationRedis(prefix=f.redis._get_prefix())
    invitation.entries.clear()

    class PublicationAndReadbackWire(Wire):
        def answer(self, sock, frame):
            if frame[0].upper() == b"EVAL" and frame[1] == adapters._INVITATION_READBACK.encode():
                self.frames.append(frame)
                return resp(invitation.eval(adapters._INVITATION_READBACK, 2, *frame[3:]))
            return super().answer(sock, frame)

    publication = PublicationAndReadbackWire()
    wrapper, raw, pool = client_for(publication, monkeypatch)
    monkeypatch.setattr(wrapper, "_get_prefix", f.redis._get_prefix)
    public = getattr(request, "param", "existing") == "public-new-workspace"
    if public:
        with Session(f.local.engine) as session, session.begin():
            tenant = Tenant(name="Public invitation target")
            tenant.id = str(UUID(int=300))
            inviter = Account(
                name="Original invitation owner",
                email="inviter@example.test",
                status=AccountStatus.ACTIVE,
                initialized_at=datetime(2024, 1, 1),
            )
            session.add_all((tenant, inviter))
            session.flush()
            TenantService.persist_tenant_member(tenant, inviter, session, "owner")
            inviter_id = inviter.id
        from services import account_service

        monkeypatch.setattr(account_service, "redis_client", wrapper)
        delivered = []
        monkeypatch.setattr(
            account_service.send_invite_member_mail_task, "delay", lambda **kwargs: delivered.append(kwargs)
        )
    with Session(f.local.engine) as session:
        if public:
            token = RegisterService.invite_new_member(
                session.get(Tenant, str(UUID(int=300))),
                "new@example.test",
                "en-US",
                "editor",
                inviter=session.get(Account, inviter_id),
                session=session,
            )
            assert len(delivered) == 1 and delivered[0]["token"] == token
        else:
            token = InvitationIssuer.issue(
                session.get(Tenant, str(UUID(int=100))),
                session.get(Account, account_id),
                "editor",
                requires_setup=False,
                actor_id=str(UUID(int=700)),
                session=session,
                redis=wrapper,
            )
    assert publication.value is not None and publication.eval_responses == 1
    frame = next(frame for frame in publication.frames if frame[0] == b"EVAL")
    invitation.token_key = frame[3]
    invitation.entries[frame[3]] = ("string", publication.value, publication.ttl)
    effects = [Mock() for _ in range(4)]
    effects[1].get_freeze_type.return_value = None
    activation = AccountActivationService(
        tokens=RedisInvitationTokenStore(redis=f.redis),
        accounts=SQLAlchemyAccountActivationRepository(f.coordinator._session_factory),
        workspace_policy=effects[0],
        eligibility=effects[1],
        membership_cache=effects[2],
        member_access_sync=effects[3],
    )
    monkeypatch.setattr(f.service, "_account_activation", activation)
    original_eval = f.redis.eval

    def evaluate(script, numkeys, *args):
        assert not f.opened and not f.local.session.in_transaction()
        if script in (adapters._INVITATION_OBSERVE, adapters._INVITATION_CONSUME, adapters._INVITATION_READBACK):
            return invitation.eval(script, numkeys, *args)
        return original_eval(script, numkeys, *args)

    monkeypatch.setattr(f.redis, "eval", evaluate)
    monkeypatch.setattr(f.redis, "get", invitation.get, raising=False)
    monkeypatch.setattr(f.redis, "_require_client", wrapper._require_client, raising=False)
    init_eval = f.init.eval
    monkeypatch.setattr(f.init, "eval", lambda *args: initialization_wire(f.init, init_eval, *args))
    assert activation._tokens.observe_versioned_invitation(token).status == "observed"
    return SimpleNamespace(m=m, account=account_id, token=token, wire=invitation, errors=errors, old=old)


@pytest.mark.parametrize("ordinary_then_invited", ["existing", "public-new-workspace"], indirect=True)
@pytest.mark.parametrize("bootstrap", [False, True])
def test_real_ordinary_history_original_issuer_then_registered_invitation(ordinary_then_invited, bootstrap):
    case = ordinary_then_invited
    before = retained(case)
    tokens = len(case.m.f.control.tokens)
    result = finish(case, new_attempt(case, token=case.token, bootstrap=bootstrap))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), case.errors
    assert len(case.m.f.control.tokens) == tokens + 2
    assert retained(case) == before
    assert len([call for call in case.wire.calls if call[0] == adapters._INVITATION_CONSUME]) == 1
    with Session(case.m.f.local.engine) as session:
        assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == 2
        histories = tuple(session.execute(sa.select(*CasdoorManagedMembershipExtend.__table__.columns)))
        assert len(histories) == len(case.old)
        by_id = {row.id: row for row in histories}
        for old in case.old:
            row = by_id[old["id"]]
            assert row.finalization.value == "finalized" and row.desired_generation == 2
            for key in (
                "id",
                "namespace_id",
                "identity_id",
                "account_id",
                "workspace_id",
                "join_id",
                "ownership",
                "ownership_epoch",
                "source",
                "baseline_json",
                "tombstone",
                "created_at",
            ):
                assert getattr(row, key) == old[key]
        operation = session.scalar(sa.select(CasdoorSyncIntentExtend))
        assert operation.operation_state.value == "applied" and operation.termination_state.value == "confirmed"
        assert session.get(Issuance, json.loads(operation.desired_json)["issuance_id"]).state == "consumed"
        assert set(
            session.scalars(
                sa.select(CasdoorAuditExtend.action).where(CasdoorAuditExtend.correlation_id == operation.id)
            )
        ) == {"invited_local_membership_write", "invited_local_membership_finalization"}


@pytest.mark.parametrize("fault", ["before-consume", "p3l-ack", "d-ack", "f-ack"])
def test_existing_history_original_commits_resume_only_missing_owners(ordinary_then_invited, monkeypatch, fault):
    from services.casdoor_invitation_finalization_service_extend import CasdoorInvitationFinalizationService

    case = ordinary_then_invited
    f = case.m.f
    before = retained(case)
    issued = len(f.control.tokens)
    lost = []
    if fault == "before-consume":
        original = CasdoorInvitationFinalizationService._finalize_invited_login

        def stopped(*args, **kwargs):
            raise TimeoutError("synthetic before original invitation consume")

        monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", stopped)
    else:
        flush, commit = Session.flush, SessionTransaction.commit

        def observed_flush(session, *args, **kwargs):
            selected = any(
                (
                    fault == "p3l-ack"
                    and isinstance(row, CasdoorSyncIntentExtend)
                    and row.operation_state.value == "applied"
                )
                or (
                    fault == "d-ack"
                    and isinstance(row, CasdoorAuditExtend)
                    and row.action == "invited_local_membership_write"
                )
                or (
                    fault == "f-ack"
                    and isinstance(row, CasdoorAuditExtend)
                    and row.action == "invited_local_membership_finalization"
                )
                for row in tuple(session.new) + tuple(session.dirty)
            )
            result = flush(session, *args, **kwargs)
            if selected:
                session.info["d33_actual_commit"] = True
            return result

        def observed_commit(transaction, *args, **kwargs):
            selected = transaction._parent is None and not lost and transaction.session.info.get("d33_actual_commit")
            result = commit(transaction, *args, **kwargs)
            if selected:
                lost.append(True)
                raise TimeoutError("synthetic actual committed phase acknowledgement lost")
            return result

        monkeypatch.setattr(Session, "flush", observed_flush)
        monkeypatch.setattr(SessionTransaction, "commit", observed_commit)
    first = new_attempt(case, token=case.token)
    finish(case, first)
    assert len(f.control.tokens) == issued
    if fault == "before-consume":
        monkeypatch.setattr(CasdoorInvitationFinalizationService, "_finalize_invited_login", original)
    else:
        assert lost == [True]
    second = new_attempt(case, token=case.token)
    result = finish(case, second)
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), case.errors
    assert first != second and f.control.created[-1].nonce != f.control.created[-2].nonce
    assert len(f.control.tokens) == issued + 2 and retained(case) == before
    assert len([call for call in case.wire.calls if call[0] == adapters._INVITATION_CONSUME]) == 1
    with Session(f.local.engine) as session:
        assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == 2
        assert len(tuple(session.scalars(sa.select(CasdoorSyncIntentExtend)))) == 1


@pytest.mark.parametrize("new_role", ["editor", "owner"])
def test_actual_manual_writer_override_or_owner_is_preserved(ordinary_then_invited, new_role):
    case = ordinary_then_invited
    engine = case.m.f.local.engine
    with Session(engine) as session, session.begin():
        tenant = session.get(Tenant, str(UUID(int=200)))
        operator = Account(
            name="Original workspace owner",
            email="manual-owner@example.test",
            status=AccountStatus.ACTIVE,
            initialized_at=datetime(2024, 1, 1),
        )
        session.add(operator)
        session.flush()
        TenantService.persist_tenant_member(tenant, operator, session, "owner")
        operator_id = operator.id
    with Session(engine) as session:
        TenantService.update_member_role(
            session.get(Tenant, str(UUID(int=200))),
            session.get(Account, case.account),
            new_role,
            session.get(Account, operator_id),
            session=session,
        )
    with Session(engine) as session:
        prior = session.execute(
            sa.select(*CasdoorManagedMembershipExtend.__table__.columns).where(
                CasdoorManagedMembershipExtend.workspace_id == str(UUID(int=200))
            )
        ).one()
        assert prior.ownership.value == "local_override"
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), case.errors
    with Session(engine) as session:
        assert (
            session.execute(
                sa.select(*CasdoorManagedMembershipExtend.__table__.columns).where(
                    CasdoorManagedMembershipExtend.id == prior.id
                )
            ).one()
            == prior
        )
        assert session.get(TenantAccountJoin, prior.join_id).role.value == new_role


@pytest.mark.parametrize("blocked", ["pending-history", "banned", "unknown-intent"])
def test_existing_history_unknown_or_ineligible_scope_has_no_consume_or_issue(ordinary_then_invited, blocked):
    from models.casdoor_extend import CasdoorIntentKind, CasdoorFinalizationState
    from hashlib import sha256

    case = ordinary_then_invited
    f = case.m.f
    with Session(f.local.engine) as session, session.begin():
        if blocked == "banned":
            session.get(Account, case.account).status = AccountStatus.BANNED
        elif blocked == "pending-history":
            session.scalar(sa.select(CasdoorManagedMembershipExtend)).finalization = CasdoorFinalizationState.PENDING
        else:
            identity = session.scalar(sa.select(CasdoorIdentityExtend))
            history = session.scalar(sa.select(CasdoorManagedMembershipExtend))
            session.add(
                CasdoorSyncIntentExtend(
                    namespace_id=identity.namespace_id,
                    identity_id=identity.id,
                    account_id=case.account,
                    workspace_id=None,
                    membership_id=history.id,
                    revision_id=history.revision_id,
                    generation=history.desired_generation,
                    ownership_epoch=history.ownership_epoch,
                    fence_epoch=0,
                    kind=CasdoorIntentKind.RESOURCE_REVOKE,
                    scope_digest="d" * 64,
                    idempotency_key=sha256(b"d33-negative-unknown-intent").hexdigest(),
                    desired_json="{}",
                )
            )
    issued = len(f.control.tokens)
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code in (302, 409) and not (result.location or "").endswith("/apps/invited")
    assert len(f.control.tokens) == issued
    assert not any(call[0] == adapters._INVITATION_CONSUME for call in case.wire.calls)
