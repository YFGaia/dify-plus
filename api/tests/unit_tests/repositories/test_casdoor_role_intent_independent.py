"""Independent SQLite counterexamples for role-intent persistence boundaries."""

import json
from uuid import uuid4

import pytest
import sqlalchemy as sa
from core.casdoor.ownership import parse_role_baseline_json
from models.account import Account, TenantAccountJoin
from models.casdoor_extend import CasdoorIntentKind, CasdoorOperationState, CasdoorTerminationState
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from sqlalchemy.exc import IntegrityError

from tests.unit_tests.repositories import test_casdoor_role_intent_repository_extend as author

pytest_plugins = ("tests.unit_tests.repositories.test_casdoor_role_intent_repository_extend",)


def test_sqlite_insert_trigger_failure_requires_whole_caller_rollback(storage):
    s = storage
    original_baseline = s.history.baseline_json
    changed = json.loads(original_baseline)
    changed["join_role"] = "editor"
    changed_baseline = json.dumps(changed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    parse_role_baseline_json(changed_baseline)
    assert changed_baseline != original_baseline
    with s.session.begin():
        s.session.execute(
            sa.text(
                "CREATE TRIGGER reject_role_intent_insert "
                "BEFORE INSERT ON casdoor_sync_intent_extend "
                "BEGIN SELECT RAISE(ABORT, 'offline trigger rejection'); END"
            )
        )

    s.session.begin()
    s.session.execute(
        sa.update(author.History).where(author.History.id == s.history.id).values(baseline_json=changed_baseline)
    )
    s.session.execute(sa.update(Account).where(Account.id == s.account.id).values(name="caller change"))
    with pytest.raises(IntegrityError, match="offline trigger rejection"):
        author.enqueue(s)

    # The repository does not own rollback. The caller must discard its entire UoW.
    s.session.rollback()
    with s.session.begin():
        assert not author.stored(s)
        assert s.session.scalar(sa.select(author.History.baseline_json)) == original_baseline
        assert s.session.scalar(sa.select(Account.name).where(Account.id == s.account.id)) == "Synthetic"
        assert s.session.scalar(sa.select(TenantAccountJoin.role)) is author.TenantAccountRole.NORMAL


@pytest.mark.parametrize("contradiction", ["mapping_slot", "saved_target_role"])
def test_saved_mapping_and_i11_id_payload_must_match_exact_target(storage, contradiction):
    s = storage
    with s.session.begin():
        if contradiction == "mapping_slot":
            s.session.execute(
                sa.update(author.Revision)
                .where(author.Revision.id == s.revision.id)
                .values(
                    mappings_json=json.dumps(
                        [{"workspace_id": s.workspace.id, "admin": {"organization": "Org", "name": "Different"}}],
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            )
        else:
            payload = json.loads(s.history.desired_roles_json)
            payload["target_role"] = "editor"
            s.session.execute(
                sa.update(author.History)
                .where(author.History.id == s.history.id)
                .values(desired_roles_json=json.dumps(payload, sort_keys=True, separators=(",", ":")))
            )

    with pytest.raises(author.CasdoorRoleIntentConflict), s.session.begin():
        author.enqueue(s)
    with s.session.begin():
        assert not author.stored(s)


def test_cross_namespace_unknown_workspace_null_intent_blocks_account_staging(storage):
    s = storage
    with s.session.begin():
        foreign = Namespace(
            integration_id=s.integration.id,
            expected_issuer=s.namespace.expected_issuer,
            organization="OtherOrg",
            application="OtherApp",
            client_id="OtherClient",
            core_fingerprint="c" * 64,
        )
        s.session.add(foreign)
        s.session.flush()
        blocker = Intent(
            namespace_id=foreign.id,
            identity_id=str(uuid4()),
            account_id=s.account.id,
            workspace_id=None,
            membership_id=None,
            revision_id=s.revision.id,
            generation=0,
            ownership_epoch=0,
            fence_epoch=0,
            kind=CasdoorIntentKind.ROLE_REPLACE,
            scope_digest="d" * 64,
            idempotency_key="e" * 64,
            desired_json="{}",
            operation_state=CasdoorOperationState.UNKNOWN,
            termination_state=CasdoorTerminationState.UNCONFIRMED,
        )
        s.session.add(blocker)

    with pytest.raises(author.CasdoorRoleIntentConflict), s.session.begin():
        author.enqueue(s)
    with s.session.begin():
        row = s.session.execute(
            sa.select(Intent.namespace_id, Intent.workspace_id, Intent.operation_state, Intent.attempt_count).where(
                Intent.idempotency_key == "e" * 64
            )
        ).one()
        assert row.namespace_id != s.namespace.id
        assert row.workspace_id is None
        assert row.operation_state is CasdoorOperationState.UNKNOWN
        assert row.attempt_count == 0
        assert len(author.stored(s)) == 1
