"""Independent composition checks for the C1 profile-name exception."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa
from core.casdoor.admission import AdmissionAction
from core.casdoor.claims import StructuredUserRef
from core.casdoor.configuration import CasdoorConfiguration, RoleRef, WorkspaceRoleMapping
from core.casdoor.role_graph import EffectiveRoleSnapshot
from models.account import Account, AccountStatus, Tenant
from models.account import TenantAccountJoin as Join
from models.casdoor_extend import (
    CasdoorAuditExtend as Audit,
)
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from repositories.casdoor_login_scope_repository_extend import (
    CasdoorLoginScopeConflict,
)
from repositories.casdoor_profile_repository_extend import CasdoorProfileRepository
from services.casdoor_local_login_service_extend import CasdoorLocalLoginService
from sqlalchemy.orm import Session
from test_casdoor_local_login_profile_extend import (
    NOW,
    all_business,
    arguments,
    execute,
    mode,
    ready,
)
from test_casdoor_local_login_service_extend import config_factory, seed
from test_casdoor_login_account_service_extend import env as original_env

login_env = original_env


@pytest.fixture
def local(login_env, monkeypatch):
    session, c, *_ = login_env
    engine = session.get_bind()
    for model in (Tenant, Join, History, Intent):
        model.__table__.create(engine)

    @sa.event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    from configs import dify_config

    monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
    ref = RoleRef(organization="Org", name="operators")
    configuration = CasdoorConfiguration(
        browser_frontend_url=c.issuer,
        backend_api_url=c.issuer,
        expected_issuer=c.issuer,
        organization=c.organization,
        application=c.application,
        client_id=c.client_id,
        default_workspace_id=UUID(int=100),
        workspace_mappings=(WorkspaceRoleMapping(workspace_id=UUID(int=200), admin=ref),),
    )
    with session.begin():
        for n in (100, 200):
            space = Tenant(name=f"Space {n}")
            space.id = str(UUID(int=n))
            session.add(space)
        revision = session.get(Revision, str(c.revision_id))
        data = json.loads(configuration.canonical_json())
        policy_fields = (
            "schema_version",
            "scope",
            "default_normal_fallback",
            "name_sync",
            "avatar_sync",
            "avatar_mode",
            "rp_logout",
            "self_unlink",
        )
        session.execute(
            sa.update(Revision).values(
                default_workspace_id=str(configuration.default_workspace_id),
                policy_json=json.dumps({k: data[k] for k in policy_fields}),
                mappings_json=json.dumps(data["workspace_mappings"]),
                button_text=configuration.button_text,
            )
        )
        session.expire(revision)
        digest = config_factory(session)._validation_digest(configuration, revision)
        session.execute(sa.update(Revision).values(config_digest=digest))
        session.execute(
            sa.update(Namespace).values(core_fingerprint=config_factory(session)._core_fingerprint(configuration))
        )
    env = (session, replace(c, config_digest=digest), *login_env[2:])
    roles = EffectiveRoleSnapshot(c.subject, StructuredUserRef("Org", "person"), (ref,))
    service = CasdoorLocalLoginService(session_factory=lambda: Session(engine), configuration_factory=config_factory)
    return SimpleNamespace(env=env, engine=engine, session=session, roles=roles, service=service, config=configuration)


@pytest.fixture
def profiled(local):
    Audit.__table__.create(local.engine)
    mode(local, "managed")
    return local


def test_managed_name_delta_survives_full_profile_and_account_recheck(profiled):
    local = profiled
    seed(local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))
    with local.session.begin():
        local.session.execute(sa.update(Account).values(name="Old managed", avatar="local-avatar"))
        local.session.execute(
            sa.update(Identity).values(
                last_applied_json=json.dumps({"schema_version": 1, "name": "Old managed", "name_generation": 0})
            )
        )

    bundle = ready(local, AdmissionAction.USE_BOUND)
    try:
        result = execute(
            local,
            bundle,
            **arguments(local, "Remote managed", auth_started_at=NOW - timedelta(seconds=1)),
        )
    finally:
        assert bundle[3].release()

    assert result.generation == 1
    with Session(local.engine) as reader:
        account = reader.scalar(sa.select(Account))
        assert account.name == "Remote managed"
        assert account.avatar == "local-avatar"
        assert reader.scalar(sa.select(Identity.sync_generation)) == 1


def test_bound_managed_name_update_does_not_allow_later_avatar_tamper(profiled, monkeypatch):
    local = profiled
    seed(local.env, AccountStatus.ACTIVE, datetime(2025, 1, 1))
    with local.session.begin():
        local.session.execute(sa.update(Account).values(name="Old managed", avatar="local-avatar"))
        local.session.execute(
            sa.update(Identity).values(
                last_applied_json=json.dumps({"schema_version": 1, "name": "Old managed", "name_generation": 0})
            )
        )
    before = all_business(local)
    bundle = ready(local, AdmissionAction.USE_BOUND)
    original_persist = CasdoorProfileRepository.persist

    def profile_then_tamper(repository, *args, **kwargs):
        outcome = original_persist(repository, *args, **kwargs)
        account_id = repository._session.scalar(sa.select(Account.id))
        result = repository._session.execute(
            sa.update(Account).where(Account.id == account_id).values(avatar="tampered-avatar")
        )
        assert result.rowcount == 1
        return outcome

    monkeypatch.setattr(CasdoorProfileRepository, "persist", profile_then_tamper)
    try:
        with pytest.raises(CasdoorLoginScopeConflict):
            execute(
                local,
                bundle,
                **arguments(local, "Remote managed", auth_started_at=NOW - timedelta(seconds=1)),
            )
    finally:
        assert bundle[3].release()

    assert all_business(local) == before
