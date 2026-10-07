"""Independent check of the lifecycle table in the LOCAL login SQLite fixture."""

import sqlalchemy as sa
from models.account import TenantAccountJoin as Join
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend, InvitationAuthorityLifecycleExtend
from sqlalchemy.orm import Session
from test_casdoor_local_login_service_extend import ready, run

pytest_plugins = ("test_casdoor_local_login_service_extend",)


def test_local_fixture_provisions_and_persists_lifecycle_rows(local):
    table_names = set(sa.inspect(local.engine).get_table_names())
    assert InvitationAuthorityLifecycleExtend.__tablename__ in table_names
    assert InvitationAuthorityIssuanceExtend.__tablename__ not in table_names

    bundle = ready(local)
    try:
        result = run(local, bundle)
        assert result.generation == 1
    finally:
        assert bundle[3].release()

    with Session(local.engine) as reader:
        joins = set(reader.execute(sa.select(Join.account_id, Join.tenant_id)).all())
        lifecycles = reader.execute(
            sa.select(
                InvitationAuthorityLifecycleExtend.account_id,
                InvitationAuthorityLifecycleExtend.workspace_id,
                InvitationAuthorityLifecycleExtend.epoch,
                InvitationAuthorityLifecycleExtend.state,
            )
        ).all()

    lifecycle_pairs = {(account_id, workspace_id) for account_id, workspace_id, _, _ in lifecycles}
    assert len(joins) == len(lifecycles) == 2
    assert lifecycle_pairs == joins
    assert all(epoch == 1 and state == "active" for _, _, epoch, state in lifecycles)
