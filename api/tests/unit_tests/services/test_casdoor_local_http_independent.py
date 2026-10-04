"""Independent actual-service regression for exact I19 failure phases."""

import sqlalchemy as sa
from models.casdoor_extend import CasdoorFinalizationState, CasdoorManagedMembershipExtend
from sqlalchemy.orm import Session
from test_casdoor_local_http_service_extend import begin, complete

pytest_plugins = ("test_casdoor_local_http_service_extend",)


def test_i19_known_not_committed_phase_survives_actual_http_callback(http_flow):
    flow = http_flow
    scope, _ = begin(flow)
    table = CasdoorManagedMembershipExtend.__tablename__
    with flow.local.engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TRIGGER deny_i19_finalize BEFORE UPDATE OF finalization ON {table} "
            "WHEN NEW.finalization = 'finalized' "
            "BEGIN SELECT RAISE(ABORT, 'bounded I19 finalization fault'); END"
        )

    result = complete(flow, scope)

    assert result.tokens is None and result.redirect is None and result.error
    assert result.phases.local_outcome == "committed"
    assert result.phases.token_outcome == "not_started"
    assert result.phases.cleanup_released is True
    assert len(flow.control.consumed) == 1
    assert len([path for path, _ in flow.control.requests if path.endswith("access_token")]) == 1
    assert not flow.control.tokens
    assert result.cookies == (flow.control.consumed[0].clear_cookie,)
    with Session(flow.local.engine) as reader:
        assert set(reader.scalars(sa.select(CasdoorManagedMembershipExtend.finalization))) == {
            CasdoorFinalizationState.PENDING,
        }
    assert result.phases.finalization_outcome == "not_committed"
