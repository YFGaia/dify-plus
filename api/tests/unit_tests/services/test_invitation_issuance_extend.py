"""Real SQLite issuance snapshots and existing invite orchestration, offline publication."""

import json
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from enums import DeploymentEdition
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.invitation_authority_repository_extend import MAX_LIFECYCLE_EPOCH, InvitationAuthorityRepository
from services import account_service as account_module
from services import invitation_issuance_service_extend as svc
from services.account_adapters import _parse_versioned_invitation
from services.account_service import RegisterService
from services.errors.account import AccountAlreadyInTenantError, NoPermissionError


@pytest.fixture(autouse=True)
def local_settings(config_overrides, monkeypatch):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY, INVITE_EXPIRY_HOURS=24)
    monkeypatch.setattr("libs.workspace_permission.check_workspace_member_invite_permission", Mock())
    monkeypatch.setattr(account_module.send_invite_member_mail_task, "delay", Mock())
    monkeypatch.setattr(
        RegisterService, "generate_invite_token", Mock(side_effect=AssertionError("main path cannot publish legacy"))
    )


def seed(session, *, prior="active", joined=True, status=AccountStatus.PENDING, epoch=7):
    tenant, other_tenant = Tenant(name="Invite workspace"), Tenant(name="Other workspace")
    owner = Account(name="Owner", email="owner-issuer@example.test", status=AccountStatus.ACTIVE)
    target = Account(name="Target", email="target-issuer@example.test", status=status, interface_language="en-US")
    session.add_all([tenant, other_tenant, owner, target])
    session.flush()
    session.add(TenantAccountJoin(tenant_id=tenant.id, account_id=owner.id, role=TenantAccountRole.OWNER))
    target_join = None
    if joined:
        target_join = TenantAccountJoin(tenant_id=tenant.id, account_id=target.id, role=TenantAccountRole.NORMAL)
        session.add(target_join)
    repo = InvitationAuthorityRepository()
    if prior:
        life = repo.set_lifecycle_state(session, account_id=target.id, workspace_id=tenant.id, state=prior)
        session.get(Lifecycle, life.lifecycle_id).epoch = epoch
    protected = repo.set_lifecycle_state(session, account_id=target.id, workspace_id=other_tenant.id, state="withdrawn")
    session.commit()
    return SimpleNamespace(
        tenant=tenant,
        owner=owner,
        target=target,
        workspace_id=tenant.id,
        account_id=target.id,
        join_id=target_join.id if target_join else None,
        other_id=other_tenant.id,
        protected=protected,
    )


def life(session, s):
    return InvitationAuthorityRepository().get_lifecycle(session, account_id=s.account_id, workspace_id=s.workspace_id)


def issue(session, s, *, role="editor", requires_setup=True):
    return svc.InvitationIssuer.issue(
        s.tenant,
        s.target,
        role,
        requires_setup=requires_setup,
        actor_id=s.owner.id,
        session=session,
        redis=account_module.redis_client,
    )


def rows(session):
    return list(session.scalars(sa.select(Issuance).order_by(Issuance.issuance_id)))


@pytest.mark.parametrize(
    "prior,joined", [(None, False), (None, True), ("active", False), ("active", True), ("withdrawn", False)]
)
def test_exact_sql_snapshot_committed_before_redis_without_extra_epoch(
    sqlite_session_factory, monkeypatch, prior, joined
):
    with sqlite_session_factory() as session:
        s = seed(session, prior=prior, joined=joined)
        before = life(session, s)
        events = []
        sa.event.listen(session, "after_commit", lambda _: events.append("commit"))

        def publish(self, *, token, payload, ttl):
            assert events == ["commit"]
            assert UUID(token).version == 4 and str(UUID(token)) == token
            assert ttl == 24 * 3600
            parsed, _ = _parse_versioned_invitation(payload, token)
            assert parsed == "observed"
            expected = json.loads(payload)
            with sqlite_session_factory() as observer:
                stored = rows(observer)
                assert len(stored) == 1
                record = stored[0]
                assert record.payload_json.encode() == payload
                assert record.payload_digest == sha256(payload).hexdigest()
                assert record.actor_id == s.owner.id and record.state == "issued"
                assert record.consumption_receipt_json is None
                current = life(observer, s)
                assert expected == {
                    "account_id": s.account_id,
                    "email": s.target.email,
                    "workspace_id": s.workspace_id,
                    "role": "editor",
                    "requires_setup": True,
                    "invitation_authority": {
                        "schema_version": 1,
                        "issuance_id": record.issuance_id,
                        "lifecycle_id": current.lifecycle_id,
                        "lifecycle_epoch": current.epoch,
                        "token_digest": sha256(token.encode()).hexdigest(),
                        "join_id_at_issue": s.join_id,
                    },
                }
                assert UUID(record.issuance_id).version == 4
                assert current.state == "active"
                assert current.epoch == (1 if prior is None else 8 if prior == "withdrawn" else 7)
                if before:
                    assert current.lifecycle_id == before.lifecycle_id and current.created_at == before.created_at
                assert (
                    InvitationAuthorityRepository().get_lifecycle(
                        observer, account_id=s.account_id, workspace_id=s.other_id
                    )
                    == s.protected
                )
            events.append("publish")
            return svc.PublicationOutcome.PUBLISHED

        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        assert UUID(issue(session, s)).version == 4
        assert events == ["commit", "publish"]


def test_active_resend_issues_fresh_snapshots_without_advancing_epoch(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        s = seed(session)
        before = life(session, s)
        publish = Mock(return_value=svc.PublicationOutcome.PUBLISHED)
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        tokens = [issue(session, s), issue(session, s)]
        assert tokens[0] != tokens[1] and publish.call_count == 2
        records = rows(session)
        assert len(records) == 2 and records[0].issuance_id != records[1].issuance_id
        assert life(session, s) == before


def test_withdrawn_join_is_not_reopened(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        s = seed(session, prior="withdrawn", joined=True)
        before = life(session, s)
        publish = Mock()
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        with pytest.raises(svc.InvitationIssuanceError, match="invitation_issuance_unconfirmed") as caught:
            issue(session, s)
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        publish.assert_not_called()
        session.rollback()
        assert life(session, s) == before and rows(session) == []


@pytest.mark.parametrize("failure", ["before_commit", "after_commit", "flush", "mismatch", "regrant_overflow"])
def test_sql_failure_never_publishes_or_retries(sqlite_session_factory, monkeypatch, failure):
    with sqlite_session_factory() as session:
        s = seed(
            session,
            prior="withdrawn" if failure == "regrant_overflow" else "active",
            joined=failure != "regrant_overflow",
            epoch=MAX_LIFECYCLE_EPOCH if failure == "regrant_overflow" else 7,
        )
        before = life(session, s)
        publish = Mock()
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        original_commit = session.commit
        original_get = InvitationAuthorityRepository.get_lifecycle
        if failure in ("before_commit", "after_commit"):

            def fail_commit():
                if failure == "after_commit":
                    original_commit()
                raise RuntimeError("private raw database failure")

            monkeypatch.setattr(session, "commit", fail_commit)
        elif failure == "flush":
            original_flush = session.flush

            def fail_flush(objects=None):
                if objects and any(isinstance(row, Issuance) for row in objects):
                    raise RuntimeError("private raw snapshot failure")
                return original_flush(objects)

            monkeypatch.setattr(session, "flush", fail_flush)
        elif failure == "mismatch":

            def mismatch(repo, supplied, **kwargs):
                return replace(original_get(repo, supplied, **kwargs), lifecycle_id=str(uuid4()))

            monkeypatch.setattr(InvitationAuthorityRepository, "get_lifecycle", mismatch)
        ids = Mock(side_effect=[uuid4(), uuid4()])
        monkeypatch.setattr(svc, "uuid4", ids)
        with pytest.raises(svc.InvitationIssuanceError, match="invitation_issuance_unconfirmed") as caught:
            issue(session, s)
        assert ids.call_count == 2 and caught.value.__context__ is None
        publish.assert_not_called()
        session.rollback()
        monkeypatch.setattr(InvitationAuthorityRepository, "get_lifecycle", original_get)
        with sqlite_session_factory() as observer:
            assert len(rows(observer)) == (1 if failure == "after_commit" else 0)
            assert life(observer, s) == before


@pytest.mark.parametrize("duplicate", ["issuance", "token"])
def test_duplicate_identity_is_not_replayed_to_redis(sqlite_session_factory, monkeypatch, duplicate):
    with sqlite_session_factory() as session:
        s = seed(session)
        ids = [uuid4(), uuid4()]
        more = [uuid4() if duplicate == "issuance" else ids[0], ids[1] if duplicate == "issuance" else uuid4()]
        generate = Mock(side_effect=ids + more)
        monkeypatch.setattr(svc, "uuid4", generate)
        publish = Mock(return_value=svc.PublicationOutcome.PUBLISHED)
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        assert issue(session, s) == str(ids[0])
        with pytest.raises(svc.InvitationIssuanceError):
            issue(session, s)
        session.rollback()
        assert len(rows(session)) == 1 and generate.call_count == 4
        publish.assert_called_once()


@pytest.mark.parametrize("hours", [0, -1, True, 1.5, svc.MAX_INVITATION_TTL_SECONDS // 3600 + 1])
def test_invalid_expiry_has_no_sql_mutation_id_or_publication(sqlite_session_factory, monkeypatch, hours):
    with sqlite_session_factory() as session:
        s = seed(session, prior=None)
        monkeypatch.setattr(svc.dify_config, "INVITE_EXPIRY_HOURS", hours)
        ids, publish = Mock(), Mock()
        monkeypatch.setattr(svc, "uuid4", ids)
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        with pytest.raises(svc.InvitationIssuanceError, match="invitation_expiry_invalid"):
            issue(session, s)
        assert life(session, s) is None and rows(session) == []
        ids.assert_not_called()
        publish.assert_not_called()


@pytest.mark.parametrize("hours", [1, svc.MAX_INVITATION_TTL_SECONDS // 3600])
def test_expiry_boundaries_are_exact_seconds(sqlite_session_factory, monkeypatch, hours):
    with sqlite_session_factory() as session:
        s = seed(session)
        monkeypatch.setattr(svc.dify_config, "INVITE_EXPIRY_HOURS", hours)
        publish = Mock(return_value=svc.PublicationOutcome.PUBLISHED)
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        issue(session, s)
        assert publish.call_args.kwargs["ttl"] == hours * 3600


@pytest.mark.parametrize("outcome", list(svc.PublicationOutcome))
def test_main_path_mails_only_after_positive_confirmation(sqlite_session_factory, monkeypatch, outcome):
    with sqlite_session_factory() as session:
        s = seed(session, joined=False, status=AccountStatus.ACTIVE, prior="withdrawn")
        mail = account_module.send_invite_member_mail_task.delay
        order = []

        def publish(self, *, token, payload, ttl):
            mail.assert_not_called()
            with sqlite_session_factory() as observer:
                assert len(rows(observer)) == 1 and life(observer, s).epoch == 8
                assert json.loads(payload)["invitation_authority"]["join_id_at_issue"] is None
                assert json.loads(payload)["requires_setup"] is False
            order.append("confirmed" if outcome is svc.PublicationOutcome.PUBLISHED else "unconfirmed")
            return outcome

        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        args = dict(
            tenant=s.tenant, email=s.target.email, language="en-US", role="admin", inviter=s.owner, session=session
        )
        if outcome is svc.PublicationOutcome.PUBLISHED:
            token = RegisterService.invite_new_member(**args)
            mail.assert_called_once_with(
                language="en-US",
                to=s.target.email,
                token=token,
                inviter_name=s.owner.name,
                workspace_name=s.tenant.name,
            )
            assert order == ["confirmed"]
        else:
            with pytest.raises(svc.InvitationIssuanceError, match=f"invitation_publication_{outcome.value}"):
                RegisterService.invite_new_member(**args)
            mail.assert_not_called()
            assert order == ["unconfirmed"]
        assert len(rows(session)) == 1
        assert (
            session.scalar(
                sa.select(TenantAccountJoin).where(
                    TenantAccountJoin.account_id == s.account_id, TenantAccountJoin.tenant_id == s.workspace_id
                )
            )
            is None
        )


@pytest.mark.parametrize("prior", [None, "active", "withdrawn"])
def test_pending_new_join_uses_existing_role_writer_without_double_advance(sqlite_session_factory, monkeypatch, prior):
    with sqlite_session_factory() as session:
        s = seed(session, joined=False, status=AccountStatus.PENDING, prior=prior)
        publish = Mock(return_value=svc.PublicationOutcome.PUBLISHED)
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        token = RegisterService.invite_new_member(
            s.tenant, s.target.email, "en-US", role="editor", inviter=s.owner, session=session
        )
        join = session.scalar(
            sa.select(TenantAccountJoin).where(
                TenantAccountJoin.account_id == s.account_id, TenantAccountJoin.tenant_id == s.workspace_id
            )
        )
        assert join.role is TenantAccountRole.EDITOR
        assert life(session, s).epoch == (1 if prior is None else 8)
        snapshot = json.loads(rows(session)[0].payload_json)
        assert snapshot["role"] == "editor" and snapshot["requires_setup"] is True
        assert snapshot["invitation_authority"]["join_id_at_issue"] == join.id
        assert account_module.send_invite_member_mail_task.delay.call_args.kwargs["token"] == token


@pytest.mark.parametrize("failure", ["missing_inviter", "member_permission", "workspace_permission", "already_member"])
def test_existing_policy_failures_precede_issuance_and_mail(sqlite_session_factory, monkeypatch, failure):
    with sqlite_session_factory() as session:
        s = seed(session, status=AccountStatus.ACTIVE if failure == "already_member" else AccountStatus.PENDING)
        if failure == "member_permission":
            session.execute(
                sa.update(TenantAccountJoin).where(TenantAccountJoin.account_id == s.owner.id).values(role="normal")
            )
            session.commit()
        if failure == "workspace_permission":
            monkeypatch.setattr(
                "libs.workspace_permission.check_workspace_member_invite_permission",
                Mock(side_effect=NoPermissionError()),
            )
        issuer = Mock(side_effect=AssertionError("policy must precede issuer"))
        monkeypatch.setattr(svc.InvitationIssuer, "issue", issuer)
        expected = (
            ValueError
            if failure == "missing_inviter"
            else AccountAlreadyInTenantError
            if failure == "already_member"
            else NoPermissionError
        )
        with pytest.raises(expected):
            RegisterService.invite_new_member(
                s.tenant,
                s.target.email,
                "en-US",
                inviter=None if failure == "missing_inviter" else s.owner,
                session=session,
            )
        issuer.assert_not_called()
        account_module.send_invite_member_mail_task.delay.assert_not_called()
        assert rows(session) == []


@pytest.mark.parametrize("committed", [False, True])
def test_main_sql_commit_failure_never_publishes_or_mails(sqlite_session_factory, monkeypatch, committed):
    with sqlite_session_factory() as session:
        s = seed(session, joined=False, status=AccountStatus.ACTIVE)
        original = session.commit
        publish = Mock()

        def fail_commit():
            if committed:
                original()
            raise RuntimeError("private database outcome")

        monkeypatch.setattr(session, "commit", fail_commit)
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        with pytest.raises(svc.InvitationIssuanceError, match="invitation_issuance_unconfirmed"):
            RegisterService.invite_new_member(s.tenant, s.target.email, "en-US", inviter=s.owner, session=session)
        publish.assert_not_called()
        account_module.send_invite_member_mail_task.delay.assert_not_called()
        session.rollback()
        with sqlite_session_factory() as observer:
            assert len(rows(observer)) == int(committed)


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_main_precommit_interrupt_propagates_without_publication_or_mail(sqlite_session_factory, monkeypatch, control):
    with sqlite_session_factory() as session:
        s = seed(session, joined=False, status=AccountStatus.ACTIVE)
        publish = Mock()
        monkeypatch.setattr(session, "commit", Mock(side_effect=control("private interruption")))
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        with pytest.raises(control) as caught:
            RegisterService.invite_new_member(s.tenant, s.target.email, "en-US", inviter=s.owner, session=session)
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert str(caught.value) == ("invitation issuance interrupted" if control is KeyboardInterrupt else "1")
        publish.assert_not_called()
        account_module.send_invite_member_mail_task.delay.assert_not_called()
        session.rollback()
        assert rows(session) == []


def test_existing_token_reader_accepts_exact_versioned_issuer_payload(sqlite_session_factory, monkeypatch):
    with sqlite_session_factory() as session:
        s = seed(session)
        publish = Mock(return_value=svc.PublicationOutcome.PUBLISHED)
        monkeypatch.setattr(svc.InvitationPublisher, "publish", publish)
        token = issue(session, s, role="editor", requires_setup=True)
        emitted = publish.call_args.kwargs["payload"]
        assert emitted == rows(session)[0].payload_json.encode("utf-8")
        assert json.loads(emitted)["invitation_authority"]["schema_version"] == 1
        redis = Mock()
        redis.get.return_value = emitted
        monkeypatch.setattr(account_module, "redis_client", redis)

        actual = RegisterService.get_invitation_by_token(token)

        assert actual == {
            "account_id": s.account_id,
            "email": s.target.email,
            "workspace_id": s.workspace_id,
            "role": "editor",
            "requires_setup": True,
        }
        redis.get.assert_called_once_with(f"member_invite:token:{token}")
