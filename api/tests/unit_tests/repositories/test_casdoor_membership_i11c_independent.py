"""Independent SQLite probes for bounded Casdoor membership history reads."""

import pytest
import sqlalchemy as sa
from core.casdoor.ownership import MAX_SNAPSHOT_BYTES, OwnershipDecision
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from repositories.casdoor_membership_repository_extend import CasdoorMembershipConflict
from test_casdoor_membership_i11c_bounded_history import old_history
from test_casdoor_membership_repository_extend import inspect, register
from test_casdoor_membership_repository_extend import storage as storage_fixture

storage = storage_fixture


def _history_selects(statements):
    return [
        statement
        for statement in statements
        if statement.is_select and str(statement.get_final_froms()[0]) == "casdoor_managed_membership_extend"
    ]


def test_current_after_two_large_historical_rows_reads_only_current_text(storage):
    s = storage
    with s.session.begin():
        snapshot = register(s)
        current = s.session.get(History, str(snapshot.membership_id))
        old_one = old_history(s, 41, oversized=True)
        old_two = old_history(s, 42, oversized=True)
        statements = []
        sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))

        result = inspect(s)

        assert result.decision is OwnershipDecision.AUTHORIZATION_PENDING
        assert result.managed.membership_id == snapshot.membership_id
        reads = _history_selects(statements)
        assert reads
        for statement in reads:
            selected = {str(column).split(".")[-1] for column in statement.selected_columns}
            assert not selected.intersection(
                {"last_applied_roles_json", "desired_roles_json", "baseline_json"}
            ) or "namespace_id =" in str(statement)
        text_reads = [
            statement
            for statement in reads
            if {"last_applied_roles_json", "desired_roles_json", "baseline_json"}.intersection(
                {str(column).split(".")[-1] for column in statement.selected_columns}
            )
        ]
        assert len(text_reads) == 1
        assert f"namespace_id = '{s.namespace.id}'" in str(
            text_reads[0].compile(compile_kwargs={"literal_binds": True})
        )
        assert old_one.namespace_id != current.namespace_id
        assert old_two.namespace_id != current.namespace_id


def test_byte_overflow_between_precheck_and_materialization_is_never_loaded(storage):
    s = storage
    with s.session.begin():
        snapshot = register(s)
        row = s.session.get(History, str(snapshot.membership_id))
        original = row.baseline_json
        statements = []
        sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))
        changed = False

        @sa.event.listens_for(s.session, "do_orm_execute")
        def enlarge_before_materialization(state):
            nonlocal changed
            statement = state.statement
            if not changed and statement.is_select and "baseline_json" in statement.selected_columns.keys():
                changed = True
                s.session.connection().execute(
                    sa.update(History)
                    .where(History.id == row.id)
                    .values(baseline_json="中" * (MAX_SNAPSHOT_BYTES // 3 + 1))
                )

        with pytest.raises(CasdoorMembershipConflict, match="^config_conflict$"):
            inspect(s)

        assert changed
        assert row.baseline_json == original
        reads = _history_selects(statements)
        materialized = [statement for statement in reads if "baseline_json" in statement.selected_columns.keys()]
        assert len(materialized) == 1
        assert "BETWEEN" in str(materialized[0])
        s.session.rollback()
