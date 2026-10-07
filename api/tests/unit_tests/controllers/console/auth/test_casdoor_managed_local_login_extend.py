"""Fresh registered invitation then ordinary authentication through real owners."""

import traceback
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session
from test_casdoor_invited_local_http_extend import (
    test_registered_invite_auto_session_original_init_native_and_sql as original_invite_success,
)
from test_casdoor_local_http_extend import begin, cookie, send
from test_gateway import response as wire_response

from core.casdoor.configuration import RoleRef, WorkspaceRoleMapping
from core.helper import ssrf_proxy
from libs.token import _real_cookie_name
from models.account import Account, AccountIntegrate, AccountStatus, Tenant, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.agent import Agent
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorIdentityExtend,
    CasdoorManagedMembershipExtend,
    CasdoorSyncIntentExtend,
)
from models.dataset import Dataset
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend
from models.model import App
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeRepository
from services.account_service import TenantService
from services.casdoor_local_http_service_extend import CasdoorLocalHttpService

pytest_plugins = ("test_casdoor_invited_local_http_extend",)


def historical_rows(case):
    with Session(case.m.f.local.engine) as session:
        return tuple(
            tuple(
                tuple(row)
                for row in session.execute(
                    sa.select(*model.__table__.columns).order_by(*model.__table__.primary_key.columns)
                )
            )
            for model in (CasdoorSyncIntentExtend, InvitationAuthorityIssuanceExtend, CasdoorAuditExtend)
        )


def retained_login_facts(case):
    with Session(case.m.f.local.engine) as session:
        account = session.get(Account, case.account)
        return (
            (
                account.initialized_at,
                account.password,
                account.password_salt,
                account.email,
                account.interface_language,
                account.timezone,
            ),
            tuple(
                tuple(row)
                for row in session.execute(
                    sa.select(*AccountMoneyExtend.__table__.columns).where(
                        AccountMoneyExtend.account_id == case.account
                    )
                )
            ),
            tuple(
                tuple(row)
                for row in session.execute(
                    sa.select(*AccountIntegrate.__table__.columns).where(AccountIntegrate.account_id == case.account)
                )
            ),
            tuple(
                session.scalars(
                    sa.select(CasdoorIdentityExtend.id).where(CasdoorIdentityExtend.account_id == case.account)
                )
            ),
        )


def ordinary(case, number):
    retained = retained_login_facts(case)
    init_records = len(case.m.f.init.records)
    state = begin(case.m, return_path="/apps/ordinary")
    result = send(case.m, "/callback", query_string={"state": state, "code": "synthetic-fresh-code-" + str(number)})
    assert result.status_code == 302, (result.json, case.m.f.control.errors)
    assert result.location == case.m.f.settings.CONSOLE_WEB_URL + "/apps/ordinary", (
        case.m.f.control.errors,
        len(case.m.f.control.created),
        len(case.m.f.control.consumed),
        len(case.m.f.control.completed),
        case.m.f.control.completed[-1],
    )
    consumed = case.m.f.control.consumed[-1]
    assert consumed.context.invite is None
    assert consumed.nonce == case.m.f.control.created[-1].nonce
    completed = case.m.f.control.completed[-1]
    assert completed.phases.local_outcome == completed.phases.finalization_outcome == "committed"
    assert completed.phases.token_outcome == "issued"
    assert completed.phases.cleanup_released is True
    assert completed.provenance is not None
    assert str(completed.provenance.account_id) == case.account
    assert completed.provenance.namespace_id == consumed.context.namespace_id
    assert completed.provenance.revision_id == consumed.context.revision_id
    assert completed.provenance.id_token_expires_at > datetime.now().timestamp()
    cookies = [cookie(case.m, _real_cookie_name(name)) for name in ("access_token", "refresh_token", "csrf_token")]
    assert all(cookies)
    access, refresh, csrf = cookies
    assert access.value == completed.tokens.access_token
    assert refresh.value == completed.tokens.refresh_token
    assert csrf.value == completed.tokens.csrf_token
    assert access.http_only
    assert refresh.http_only
    assert not csrf.http_only
    assert len(result.headers.getlist("Set-Cookie")) == 4
    assert len(case.m.f.init.records) == init_records
    assert retained_login_facts(case) == retained
    with Session(case.m.f.local.engine) as session:
        assert session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == number + 1
    return consumed


def test_registered_invitation_then_two_fresh_ordinary_logins(invited_mounted, monkeypatch):
    case = invited_mounted
    original_invite_success(case, AccountStatus.PENDING, False, False)
    original_error = case.m.f.service._public_error

    def observed_error(error, *args):
        case.m.f.control.errors.append([frame.name for frame in traceback.extract_tb(error.__traceback__)])
        return original_error(error, *args)

    monkeypatch.setattr(case.m.f.service, "_public_error", observed_error)
    original_base_error = CasdoorLocalHttpService._public_error

    def observed_base_error(error, *args):
        case.m.f.control.errors.append([frame.name for frame in traceback.extract_tb(error.__traceback__)])
        return original_base_error(error, *args)

    monkeypatch.setattr(CasdoorLocalHttpService, "_public_error", staticmethod(observed_base_error))
    original_barrier = CasdoorLoginScopeRepository._intent_barrier

    def observed_barrier(self, *args, **kwargs):
        try:
            return original_barrier(self, *args, **kwargs)
        except Exception as error:
            case.m.f.control.errors.append([frame.name for frame in traceback.extract_tb(error.__traceback__)])
            raise

    monkeypatch.setattr(CasdoorLoginScopeRepository, "_intent_barrier", observed_barrier)
    before = historical_rows(case)
    first = ordinary(case, 1)
    second = ordinary(case, 2)
    assert case.m.f.control.created[-2].state != case.m.f.control.created[-1].state
    assert first.nonce != second.nonce
    # New ordinary membership audits are separate; historical invitation rows
    # and its D/F evidence remain byte-for-byte SQL facts.
    after = historical_rows(case)
    assert after[:2] == before[:2]
    assert all(row in after[2] for row in before[2])


def managed_row(case):
    with Session(case.m.f.local.engine) as session:
        return session.execute(
            sa.select(*CasdoorManagedMembershipExtend.__table__.columns).where(
                CasdoorManagedMembershipExtend.workspace_id == str(UUID(int=200))
            )
        ).one()


def test_current_roles_withdraw_nohit_regrant_keep_historical_terminal_facts(invited_mounted, monkeypatch):
    case = invited_mounted
    original_invite_success(case, AccountStatus.PENDING, False, False)
    historical = historical_rows(case)
    ordinary(case, 1)
    ordinary(case, 2)
    before = managed_row(case)
    f = case.m.f
    with Session(f.local.engine) as session, session.begin():
        # A real original current-owner role effect also advances the old
        # invitation lifecycle; historical closure must survive that change.
        TenantService.persist_tenant_member(
            session.get(Tenant, str(f.local.config.default_workspace_id)),
            session.get(Account, case.account),
            session,
            "normal",
        )
    role = RoleRef(organization="Org", name="operators")
    config = f.local.config.model_copy(
        update={"workspace_mappings": (WorkspaceRoleMapping(workspace_id=UUID(int=200), editor=role),)}
    )
    # Use the existing real configuration producer; the offline active pointer
    # is the same explicit fixture seam as the original mounted suite.
    with Session(f.local.engine) as session, session.begin():
        owner = f.coordinator._configuration_service._repository(session)
        integration = owner._integration()
        draft = owner.save_draft(config, etag=integration.etag, actor_account_id=UUID(int=700))
        integration.active_revision_id = str(draft.draft_revision_id)
        session.flush()
    ordinary(case, 3)
    lowered = managed_row(case)
    assert lowered.join_id == before.join_id
    assert lowered.source == before.source
    with Session(f.local.engine) as session:
        assert session.get(TenantAccountJoin, lowered.join_id).role == "editor"
    for model in (App, Dataset, Agent):
        model.__table__.create(f.local.engine, checkfirst=True)
    with Session(f.local.engine) as session, session.begin():
        recipient = Account(
            name="Original owner",
            email="owner@example.test",
            status=AccountStatus.ACTIVE,
            initialized_at=datetime(2026, 1, 1),
        )
        session.add(recipient)
        session.flush()
        session.add(TenantAccountJoin(tenant_id=str(UUID(int=200)), account_id=recipient.id, role="owner"))
    original_transport = ssrf_proxy.make_request_with_deadline
    hit = [False]

    def transport(method, url, **kwargs):
        result = original_transport(method, url, **kwargs)
        if urlsplit(url).path == "/api/get-roles":
            data = result.json()
            data["data"][0]["users"] = ["Org/person"] if hit[0] else []
            return wire_response(data)
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", transport)
    ordinary(case, 4)
    absent = managed_row(case)
    assert absent.join_id == lowered.join_id
    assert absent.ownership_epoch == lowered.ownership_epoch + 1
    assert not absent.tombstone
    assert absent.finalization.value == "finalized"
    with Session(f.local.engine) as session:
        assert session.get(TenantAccountJoin, absent.join_id) is None
    ordinary(case, 5)
    assert managed_row(case) == absent
    hit[0] = True
    ordinary(case, 6)
    rejoined = managed_row(case)
    assert rejoined.id == before.id
    assert rejoined.join_id != before.join_id
    assert rejoined.ownership_epoch == before.ownership_epoch + 2
    assert rejoined.source == before.source
    assert rejoined.baseline_json == before.baseline_json
    ordinary(case, 7)
    assert managed_row(case).join_id == rejoined.join_id
    after = historical_rows(case)
    assert after[:2] == historical[:2]
    assert all(row in after[2] for row in historical[2])
