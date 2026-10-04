"""Independent P3M-C service-boundary verification against the frozen SQLite fixture."""

from dataclasses import replace

import pytest
import sqlalchemy as sa

from models.account import TenantAccountJoin as Join
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from models.invitation_authority_extend import InvitationAuthorityIssuanceExtend as Issuance
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict
from tests.unit_tests.repositories.test_casdoor_invited_login_scope_repository_extend import (
    invited_scope_case,  # noqa: F401
)
from tests.unit_tests.services.test_casdoor_invited_local_membership_service_extend import (
    writer_case as author_writer_case_fixture,  # noqa: F401
)


@pytest.fixture
def invited_writer_case(request):
    return request.getfixturevalue("author_writer_case_fixture")


@pytest.mark.parametrize("generation_delta", [-1, 1], ids=["stale-generation", "future-generation"])
def test_real_writer_rejects_mismatched_invitation_generation_without_any_write(invited_writer_case, generation_delta):
    case = invited_writer_case()
    tracked_models = (Identity, Join, History, Intent, Issuance, Lifecycle)

    def snapshot():
        with case.db() as session:
            return {
                model.__tablename__: tuple(
                    tuple(row)
                    for row in session.execute(
                        sa.select(*model.__table__.columns).order_by(*model.__table__.primary_key.columns)
                    )
                )
                for model in tracked_models
            }

    before = snapshot()
    with case.db() as session:
        actual_generation = session.scalar(
            sa.select(Identity.sync_generation).where(
                Identity.account_id == case.ids["account"],
                Identity.namespace_id == case.ids["namespace"],
            )
        )
    assert actual_generation == case.attempt.expected_generation

    writes = []

    def record_write(connection, cursor, statement, parameters, context, executemany):
        keyword = statement.lstrip().split(None, 1)[0].upper()
        if keyword in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(statement)

    sa.event.listen(case.engine, "before_cursor_execute", record_write)
    try:
        forged_attempt = replace(case.attempt, expected_generation=actual_generation + generation_delta)
        with pytest.raises(CasdoorLoginScopeConflict):
            case.writer.persist_invited_local_memberships(
                forged_attempt,
                roles=case.roles,
                leases=case.leases,
                deadline=case.deadline,
            )
    finally:
        sa.event.remove(case.engine, "before_cursor_execute", record_write)

    assert writes == []
    assert snapshot() == before
