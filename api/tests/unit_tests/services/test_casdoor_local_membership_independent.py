"""Independent failure-boundary checks for LOCAL membership composition."""

from dataclasses import replace
from uuid import UUID

import pytest
import sqlalchemy as sa
from models.account import Tenant, TenantAccountJoin
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from services.casdoor_local_membership_service_extend import CasdoorLocalMembershipService
from sqlalchemy.exc import IntegrityError
from test_casdoor_local_membership_service_extend import (
    counts,
    local_storage,  # noqa: F401
    login_env,  # noqa: F401
    new_plan,
    persist,
    prelock,
    prepare,
    run,
)
from test_casdoor_local_membership_service_extend import joined_env as _joined_env_fixture  # noqa: F401
from test_casdoor_local_membership_service_extend import ready as _ready_fixture  # noqa: F401


def test_history_registration_failure_rolls_back_b2a_and_earlier_space(request):
    env, spaces = request.getfixturevalue("_joined_env_fixture")
    session = env[0]
    prepared = prepare(env)
    with session.begin():
        session.execute(
            sa.text(
                f"CREATE TRIGGER fail_second_history BEFORE INSERT ON {History.__tablename__} "
                f"WHEN NEW.workspace_id = '{spaces[1].id}' "
                "BEGIN SELECT RAISE(ABORT, 'second history rejected'); END"
            )
        )

    with pytest.raises(IntegrityError), session.begin():
        prelock(env, spaces, prepared)
        account = persist(env, prepared)
        session.flush()
        run(session, new_plan(env, account, spaces))

    assert counts(session.get_bind()) == (0, 0, 0)
    with session.begin():
        assert session.scalar(sa.select(sa.func.count()).select_from(TenantAccountJoin)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(History)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(Identity)) == 0


def test_second_managed_metadata_cas_failure_restores_first_target(request):
    s = request.getfixturevalue("_ready_fixture")
    with s.session.begin():
        second = Tenant(name="Second managed target")
        second.id = str(UUID(int=2**128 - 1))
        s.session.add(second)
    second_target = replace(s.target, workspace_id=UUID(second.id), target_role="normal", builtin_id="normal")
    with s.session.begin():
        run(s.session, replace(s.version.plan, targets=(second_target,)), generation=1)

    first_target = replace(s.target, target_role="admin", builtin_id="admin")
    second_target = replace(second_target, target_role="editor", builtin_id="editor")
    plan = replace(s.version.plan, targets=(second_target, first_target))
    with s.session.begin():
        s.session.execute(
            sa.text(
                f"CREATE TRIGGER fail_second_metadata BEFORE UPDATE ON {History.__tablename__} "
                f"WHEN NEW.workspace_id = '{second.id}' "
                "BEGIN SELECT RAISE(ABORT, 'second metadata rejected'); END"
            )
        )
        before_joins = tuple(
            s.session.execute(sa.select(*TenantAccountJoin.__table__.columns).order_by(TenantAccountJoin.tenant_id))
        )
        before_history = tuple(s.session.execute(sa.select(*History.__table__.columns).order_by(History.workspace_id)))
        before_generation = s.session.scalar(sa.select(Identity.sync_generation))

    with pytest.raises(IntegrityError), s.session.begin():
        CasdoorLocalMembershipService(s.session).persist_local_memberships(
            plan, expected_fence_epoch=0, expected_generation=2
        )

    with s.session.begin():
        after_joins = tuple(
            s.session.execute(sa.select(*TenantAccountJoin.__table__.columns).order_by(TenantAccountJoin.tenant_id))
        )
        after_history = tuple(s.session.execute(sa.select(*History.__table__.columns).order_by(History.workspace_id)))
        assert after_joins == before_joins
        assert after_history == before_history
        assert s.session.scalar(sa.select(Identity.sync_generation)) == before_generation == 2
