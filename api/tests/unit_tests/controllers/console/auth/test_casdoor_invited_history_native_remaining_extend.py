"""Actual LINK/manager ADOPT and native namespace producers before public invite.

Only bottom provider/Redis/socket seams are offline. Successful invitation,
adoption, consumption and D/F rows are produced by their original owners.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.helper import ssrf_proxy
from models.account import Account, AccountIntegrate, AccountStatus, Tenant
from models.casdoor_extend import CasdoorIdentityExtend, CasdoorManagedMembershipExtend
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend
from repositories.account_activation_repository import (
    SQLAlchemyAccountActivationRepository,
)
from services import account_adapters as adapters
from services import account_service
from services.account_activation_service import AccountActivationService
from services.account_adapters import RedisInvitationTokenStore
from services.account_service import RegisterService, TenantService
from sqlalchemy.orm import Session
from test_casdoor_cross_namespace_adoption_flow_extend import create_adoption, ordinary
from test_casdoor_identity_action_flow_extend import begin as link_begin
from test_casdoor_identity_action_flow_extend import complete as link_complete
from test_casdoor_invited_local_http_extend import initialization_wire
from test_casdoor_invited_local_recovery_http_extend import finish, new_attempt
from test_casdoor_local_lifecycle_flow_extend import lifecycle as original_lifecycle
from test_casdoor_local_lifecycle_flow_extend import review, send
from test_gateway import response
from test_invitation_publication_transport_extend import Wire, client_for, resp
from test_invitation_token_consumption_extend import FakeRedis as InvitationRedis

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)
lifecycle = original_lifecycle


def public_invitation(d, monkeypatch, *, account=None):
    f = d.f
    account = d.actor if account is None else account
    for model in (InvitationAuthorityIssuanceExtend, AccountIntegrate):
        model.__table__.create(f.local.engine, checkfirst=True)
    with Session(f.local.engine) as session, session.begin():
        owner = Account(
            name="Native public invitation owner",
            email="native-inviter@example.test",
            status=AccountStatus.ACTIVE,
            initialized_at=datetime(2024, 1, 1),
        )
        tenant = Tenant(name="Native invitation new target")
        tenant.id = str(UUID(int=300))
        session.add_all((owner, tenant))
        session.flush()
        TenantService.persist_tenant_member(tenant, owner, session, "owner")
        owner_id = owner.id
    invitation = InvitationRedis(prefix=f.redis._get_prefix())
    invitation.entries.clear()

    class NativeWire(Wire):
        def answer(self, sock, frame):
            if frame[0].upper() == b"EVAL" and frame[1] == adapters._INVITATION_READBACK.encode():
                return resp(invitation.eval(adapters._INVITATION_READBACK, 2, *frame[3:]))
            return super().answer(sock, frame)

    wire = NativeWire()
    wrapper, raw, pool = client_for(wire, monkeypatch)
    monkeypatch.setattr(wrapper, "_get_prefix", f.redis._get_prefix)
    monkeypatch.setattr(account_service, "redis_client", wrapper)
    mail = []
    monkeypatch.setattr(account_service.send_invite_member_mail_task, "delay", lambda **kwargs: mail.append(kwargs))
    with Session(f.local.engine) as session:
        token = RegisterService.invite_new_member(
            session.get(Tenant, str(UUID(int=300))),
            account.email,
            "en-US",
            "editor",
            inviter=session.get(Account, owner_id),
            session=session,
        )
    assert len(mail) == 1 and mail[0]["token"] == token
    frame = next(frame for frame in wire.frames if frame[0] == b"EVAL")
    invitation.entries[frame[3]] = ("string", wire.value, wire.ttl)
    original_eval, original_get = f.redis.eval, f.redis.get

    def evaluate(script, numkeys, *args):
        if script in (adapters._INVITATION_OBSERVE, adapters._INVITATION_CONSUME, adapters._INVITATION_READBACK):
            return invitation.eval(script, numkeys, *args)
        return original_eval(script, numkeys, *args)

    def get(key):
        return invitation.get(key) if "member_invite:token:" in key else original_get(key)

    monkeypatch.setattr(f.redis, "eval", evaluate)
    monkeypatch.setattr(f.redis, "get", get)
    monkeypatch.setattr(f.redis, "_require_client", wrapper._require_client, raising=False)
    effects = [Mock() for _ in range(4)]
    effects[1].get_freeze_type.return_value = None
    activation = AccountActivationService(
        tokens=RedisInvitationTokenStore(redis=f.redis),
        accounts=SQLAlchemyAccountActivationRepository(f.service._session_factory),
        workspace_policy=effects[0],
        eligibility=effects[1],
        membership_cache=effects[2],
        member_access_sync=effects[3],
    )
    monkeypatch.setattr(f.service, "_account_activation", activation)
    init_eval = f.init.eval
    monkeypatch.setattr(f.init, "eval", lambda *args: initialization_wire(f.init, init_eval, *args))
    original_transport = ssrf_proxy.make_request_with_deadline

    def invited_profile(method, url, **kwargs):
        result = original_transport(method, url, **kwargs)
        if urlsplit(url).path.endswith("userinfo"):
            data = result.json()
            data["email"] = account.email
            return response(data)
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", invited_profile)
    return SimpleNamespace(
        m=SimpleNamespace(f=f, client=d.client, app=d.app), account=account.id, token=token, wire=invitation
    )


def test_actual_generation_zero_link_then_manager_adopt_before_public_invitation(lifecycle, monkeypatch):
    d = lifecycle
    state, _ = link_begin(d)
    assert link_complete(d, state).location.endswith("=linked")
    with Session(d.f.local.engine) as session:
        identity = session.scalar(
            sa.select(CasdoorIdentityExtend).where(CasdoorIdentityExtend.account_id == d.actor.id)
        )
        assert identity.sync_generation == 0
        target = {"identity_id": identity.id, "workspace_id": str(UUID(int=100))}
    proof = review(d, target, "adopt")
    assert (
        send(
            d, "/console/api/system-manage-extend/integration/casdoor/local-membership/adopt", method="POST", json=proof
        ).status_code
        == 200
    )
    with Session(d.f.local.engine) as session:
        history = session.scalar(sa.select(CasdoorManagedMembershipExtend))
        assert history.desired_generation == 0 and history.source.value == "adopt"
    case = public_invitation(d, monkeypatch)
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), getattr(d, "errors", ())


@pytest.mark.parametrize("writer", ["local-role", "withdraw", "regrant", "new-member"])
def test_actual_generation_zero_read_does_not_open_original_writer(lifecycle, monkeypatch, writer):
    from core.casdoor.ownership import MembershipBackend
    from repositories.casdoor_local_role_repository_extend import (
        CasdoorLocalRoleConflict,
        CasdoorLocalRoleRepository,
    )
    from repositories.casdoor_membership_repository_extend import (
        CasdoorMembershipConflict,
        CasdoorMembershipRepository,
    )

    d = lifecycle
    state, _ = link_begin(d)
    assert link_complete(d, state).location.endswith("=linked")
    with Session(d.f.local.engine) as session:
        identity = session.scalar(
            sa.select(CasdoorIdentityExtend).where(CasdoorIdentityExtend.account_id == d.actor.id)
        )
        target = {"identity_id": identity.id, "workspace_id": str(UUID(int=100))}
    proof = review(d, target, "adopt")
    assert (
        send(
            d, "/console/api/system-manage-extend/integration/casdoor/local-membership/adopt", method="POST", json=proof
        ).status_code
        == 200
    )
    original = CasdoorMembershipRepository.inspect
    observed = []

    def checked(owner, version, workspace_id, **kwargs):
        view = original(owner, version, workspace_id, **kwargs)
        if version.generation == 0 and not observed:
            observed.append(writer)
            signed_target = next(t for t in version.plan.targets if t.workspace_id == workspace_id)
            before = tuple(owner._session.execute(sa.select(*CasdoorManagedMembershipExtend.__table__.columns)))
            if writer == "local-role":
                with pytest.raises(CasdoorLocalRoleConflict):
                    CasdoorLocalRoleRepository(owner._session).prepare(version, signed_target)
            elif writer == "withdraw":
                with pytest.raises(CasdoorMembershipConflict):
                    owner.prepare_local_withdrawal(version, workspace_id)
            elif writer == "regrant":
                with pytest.raises(CasdoorMembershipConflict):
                    owner.prepare_local_regrant(version, signed_target)
            else:
                with pytest.raises(CasdoorMembershipConflict):
                    owner.prepare_new(version, signed_target, backend=MembershipBackend.LOCAL)
            assert tuple(owner._session.execute(sa.select(*CasdoorManagedMembershipExtend.__table__.columns))) == before
        return view

    monkeypatch.setattr(CasdoorMembershipRepository, "inspect", checked)
    case = public_invitation(d, monkeypatch)
    result = finish(case, new_attempt(case, token=case.token))
    assert result.location.endswith("/apps/invited") and observed == [writer]


def test_actual_namespace_reset_link_adopt_ordinary_then_public_invitation(lifecycle, monkeypatch):
    d = lifecycle
    create_adoption(d, monkeypatch)
    ordinary(d, 1)
    with Session(d.f.local.engine) as session:
        assert len(tuple(session.scalars(sa.select(CasdoorIdentityExtend)))) == 2
    case = public_invitation(d, monkeypatch)
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), getattr(d, "errors", ())


@pytest.mark.parametrize("default_role", ["normal", "admin", "editor"])
def test_actual_save_diagnostic_activate_default_mapped_role_then_public_invitation(
    lifecycle, monkeypatch, default_role
):
    import json

    from core.casdoor.configuration import RoleRef, WorkspaceRoleMapping
    from test_casdoor_diagnostic_flow_extend import begin as diagnostic_begin
    from test_casdoor_diagnostic_flow_extend import complete as diagnostic_complete
    from test_casdoor_identity_action_flow_extend import reviewed_reauth
    from test_casdoor_local_http_extend import begin as ordinary_begin

    d = lifecycle
    snapshot = d.services.casdoor_configuration.get(d.actor)
    ref = RoleRef(organization="Org", name="operators")
    config = snapshot.draft.configuration.model_copy(
        update={
            "workspace_mappings": (
                WorkspaceRoleMapping(workspace_id=UUID(int=100), **{default_role: ref}),
                WorkspaceRoleMapping(workspace_id=UUID(int=200), admin=ref),
            )
        }
    )
    d.services.casdoor_configuration.save(d.actor, configuration=config, etag=snapshot.etag, secret=None)
    reviewed_reauth(d)
    snapshot, state = diagnostic_begin(d)
    assert diagnostic_complete(d, state).status_code == 302
    activated = send(
        d,
        "/console/api/system-manage-extend/integration/casdoor/activate",
        method="POST",
        json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
    )
    assert activated.status_code == 200, activated.json
    m = SimpleNamespace(f=d.f, client=d.client, app=d.app)
    result = link_complete(d, ordinary_begin(m, return_path="/apps/default-mapped"))
    assert result.status_code == 302 and result.location.endswith("/apps/default-mapped")
    with Session(d.f.local.engine) as session:
        row = session.scalar(
            sa.select(CasdoorManagedMembershipExtend).where(
                CasdoorManagedMembershipExtend.workspace_id == str(UUID(int=100))
            )
        )
        assert row.desired_generation == 1 and json.loads(row.desired_roles_json)["reason"] == "role_mapping"
        account = session.get(Account, row.account_id)
        assert account.email == "new@example.test" and account.initialized_at is not None
    case = public_invitation(d, monkeypatch, account=account)
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), getattr(d, "errors", ())


@pytest.mark.parametrize("initial_mapping", [False, True])
@pytest.mark.parametrize("fault", [None, "d-ack", "f-ack"])
def test_actual_native_same_role_reason_change_then_public_invitation(lifecycle, monkeypatch, initial_mapping, fault):
    """Original normal Join remains normal while the sealed desired reason changes."""
    import json

    from core.casdoor.configuration import RoleRef, WorkspaceRoleMapping
    from libs.token import _real_cookie_name
    from test_casdoor_diagnostic_flow_extend import begin as diagnostic_begin
    from test_casdoor_diagnostic_flow_extend import complete as diagnostic_complete
    from test_casdoor_identity_action_flow_extend import reviewed_reauth
    from test_casdoor_local_http_extend import begin as ordinary_begin

    d = lifecycle
    ref = RoleRef(organization="Org", name="operators")

    def activate(mapped):
        # Resume the original authenticated management source from its original
        # issuer cookies; ordinary SSO does not revoke this independent source.
        for name, value in d.original_cookies.items():
            d.client.set_cookie(_real_cookie_name(name), value, domain="console.example.test", path="/")
        snapshot = d.services.casdoor_configuration.get(d.actor)
        mappings = (WorkspaceRoleMapping(workspace_id=UUID(int=200), admin=ref),)
        if mapped:
            mappings += (WorkspaceRoleMapping(workspace_id=UUID(int=100), normal=ref),)
        config = snapshot.draft.configuration.model_copy(update={"workspace_mappings": mappings})
        d.services.casdoor_configuration.save(d.actor, configuration=config, etag=snapshot.etag, secret=None)
        reviewed_reauth(d)
        snapshot, state = diagnostic_begin(d)
        assert diagnostic_complete(d, state).status_code == 302
        response = send(
            d,
            "/console/api/system-manage-extend/integration/casdoor/activate",
            method="POST",
            json={"etag": snapshot.etag, "revision_id": str(snapshot.draft_revision_id)},
        )
        assert response.status_code == 200, response.json

    activate(initial_mapping)
    m = SimpleNamespace(f=d.f, client=d.client, app=d.app)
    first = link_complete(d, ordinary_begin(m, return_path="/apps/native-initial-role"))
    assert first.location.endswith("/apps/native-initial-role")
    with Session(d.f.local.engine) as session:
        row = session.scalar(
            sa.select(CasdoorManagedMembershipExtend).where(
                CasdoorManagedMembershipExtend.workspace_id == str(UUID(int=100))
            )
        )
        prior_reason = "role_mapping" if initial_mapping else "default_normal_fallback"
        assert json.loads(row.desired_roles_json)["reason"] == prior_reason
        account = session.get(Account, row.account_id)
    activate(not initial_mapping)
    case = public_invitation(d, monkeypatch, account=account)
    if fault is not None:
        from models.casdoor_extend import CasdoorAuditExtend
        from sqlalchemy.orm import SessionTransaction

        flush, commit, lost = Session.flush, SessionTransaction.commit, []

        def selected_flush(session, *args, **kwargs):
            selected = any(
                isinstance(row, CasdoorAuditExtend)
                and row.action
                == ("invited_local_membership_write" if fault == "d-ack" else "invited_local_membership_finalization")
                for row in tuple(session.new) + tuple(session.dirty)
            )
            actual = flush(session, *args, **kwargs)
            if selected:
                session.info["d33_reason_original_commit"] = True
            return actual

        def lost_commit(transaction, *args, **kwargs):
            selected = (
                transaction._parent is None and not lost and transaction.session.info.get("d33_reason_original_commit")
            )
            actual = commit(transaction, *args, **kwargs)
            if selected:
                lost.append(True)
                raise TimeoutError("offline original reason-change commit acknowledgement")
            return actual

        monkeypatch.setattr(Session, "flush", selected_flush)
        monkeypatch.setattr(SessionTransaction, "commit", lost_commit)
        issued = len(d.f.control.tokens)
        finish(case, new_attempt(case, token=case.token))
        assert lost == [True] and len(d.f.control.tokens) == issued
    result = finish(case, new_attempt(case, token=case.token))
    assert result.status_code == 302 and result.location.endswith("/apps/invited"), getattr(d, "errors", ())
    with Session(d.f.local.engine) as session:
        row = session.scalar(
            sa.select(CasdoorManagedMembershipExtend).where(
                CasdoorManagedMembershipExtend.workspace_id == str(UUID(int=100))
            )
        )
        assert json.loads(row.desired_roles_json)["reason"] != prior_reason
