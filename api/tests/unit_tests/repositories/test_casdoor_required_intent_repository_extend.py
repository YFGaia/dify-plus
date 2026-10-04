"""Actual SQLite coverage for the thin all-history intent observation."""

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from models.account import Account
from models.casdoor_extend import CasdoorIntentKind, CasdoorOperationState, CasdoorTerminationState
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_required_intent_repository_extend import (
    CasdoorRequiredIntentConflict,
    CasdoorRequiredIntentRepository,
)
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import IntegrityError
from test_casdoor_local_role_repository_extend import storage as storage_fixture

storage = storage_fixture


def read(s, **kwargs):
    return CasdoorRequiredIntentRepository(s.session).read_locked(
        kwargs.get("account_id", s.version.plan.context.account_id), kwargs.get("workspace_id", s.target.workspace_id)
    )


def namespace(s, identifier=None):
    identifier = identifier or str(uuid4())
    base = dict(
        s.session.execute(sa.select(*Namespace.__table__.columns).where(Namespace.id == s.namespace.id)).one()._mapping
    )
    s.session.execute(sa.insert(Namespace).values(**dict(base, id=identifier, core_fingerprint=uuid4().hex * 2)))
    return identifier


def intent(s, **kwargs):
    values = dict(
        namespace_id=kwargs.pop("namespace_id", None) or namespace(s),
        identity_id=str(uuid4()),
        account_id=s.account.id,
        workspace_id=s.workspace.id,
        revision_id=s.revision.id,
        generation=93,
        ownership_epoch=7,
        fence_epoch=0,
        kind=CasdoorIntentKind.ROLE_REPLACE,
        scope_digest="a" * 64,
        idempotency_key=uuid4().hex * 2,
        desired_json="{}",
        operation_state=CasdoorOperationState.APPLIED,
        termination_state=CasdoorTerminationState.CONFIRMED,
    )
    values.update(kwargs)
    row = Intent(**values)
    s.session.add(row)
    return row


@pytest.mark.parametrize("kind", list(CasdoorIntentKind))
@pytest.mark.parametrize("association", ["scope", "global", "third_history"])
def test_all_kinds_cross_namespace_and_third_history(storage, kind, association):
    s = storage
    with s.session.begin():
        values = {}
        if association == "global":
            values["workspace_id"] = None
        if association == "third_history":
            # IDs sort before the existing current namespace; third is outside discovery2.
            base = dict(s.session.execute(sa.select(*History.__table__.columns)).one()._mapping)
            s.session.execute(
                sa.update(History).values(namespace_id=namespace(s, "ffffffff-ffff-ffff-ffff-ffffffffffff"))
            )
            for number in (1, 2):
                copy = dict(
                    base, id=str(uuid4()), namespace_id=namespace(s, str(UUID(int=number))), baseline_json="汉" * 30000
                )
                s.session.execute(sa.insert(History).values(**copy))
            refs = s.session.scalars(sa.select(History.id).order_by(History.namespace_id, History.id).limit(2)).all()
            assert s.history.id not in refs
            values.update(account_id=str(uuid4()), workspace_id=str(uuid4()), membership_id=s.history.id)
        row = intent(s, kind=kind, **values)
    with s.session.begin():
        result = read(s)
        assert result == (() if kind is CasdoorIntentKind.PROFILE_AVATAR else ((row.id,),))


@pytest.mark.parametrize("operation", list(CasdoorOperationState))
@pytest.mark.parametrize("termination", list(CasdoorTerminationState))
def test_every_state_remains_required(storage, operation, termination):
    with storage.session.begin():
        intent(storage, operation_state=operation, termination_state=termination)
    with storage.session.begin():
        assert read(storage)


@pytest.mark.parametrize(
    "case",
    ["foreign_account", "foreign_workspace", "foreign_history_account", "foreign_history_workspace", "no_history"],
)
def test_exact_scope_negatives(storage, case):
    s = storage
    with s.session.begin():
        if case.startswith("foreign_history"):
            field = "account_id" if case.endswith("account") else "workspace_id"
            s.session.execute(sa.update(History).values(**{field: str(uuid4())}))
        values = dict(account_id=str(uuid4()), workspace_id=str(uuid4()))
        if case == "foreign_account":
            values["workspace_id"] = s.workspace.id
        elif case == "foreign_workspace":
            values["account_id"] = s.account.id
        else:
            values["membership_id"] = None if case == "no_history" else s.history.id
        intent(s, **values)
    with s.session.begin():
        assert read(s) == ()


@pytest.mark.parametrize(
    "bad", ["new", "dirty", "deleted", "nested", "no_root", "failed", "account_string", "workspace_string"]
)
def test_clean_root_and_canonical_input_reject_without_sql(storage, bad):
    s = storage
    if bad != "no_root":
        s.session.begin()
    if bad == "new":
        s.session.add(Account(name="Pending", email="pending@example.test"))
    elif bad == "dirty":
        s.account.name = "Dirty"
    elif bad == "deleted":
        s.session.delete(s.account)
    elif bad == "nested":
        s.session.begin_nested()
    elif bad == "failed":
        identifier = s.account.id
        s.session.expunge(s.account)
        duplicate = Account(name="Duplicate", email="duplicate@example.test")
        duplicate.id = identifier
        s.session.add(duplicate)
        with pytest.raises(IntegrityError):
            s.session.flush()
    statements = []
    sa.event.listen(s.engine, "before_cursor_execute", lambda _c, _u, stmt, _p, _x, _m: statements.append(stmt))
    kwargs = {bad.split("_")[0] + "_id": str(uuid4())} if bad.endswith("_string") else {}
    with pytest.raises(CasdoorRequiredIntentConflict, match="^authorization_pending$"):
        read(s, **kwargs)
    assert statements == []
    s.session.rollback()


def test_invalid_session_stable_error():
    with pytest.raises(CasdoorRequiredIntentConflict, match="^authorization_pending$"):
        CasdoorRequiredIntentRepository(None).read_locked(uuid4(), uuid4())


def test_single_scalar_query_order_limit_lock_and_no_text(storage):
    s = storage
    with s.session.begin():
        namespace_id = namespace(s)
        rows = [intent(s, scope_digest=char * 64, namespace_id=namespace_id) for char in ("c", "a", "b")]
    statements = []
    sa.event.listen(s.session, "do_orm_execute", lambda state: statements.append(state.statement))
    with s.session.begin():
        assert read(s) == ((rows[1].id,),)
    assert len(statements) == 1
    stmt = statements[0]
    assert list(stmt.selected_columns) == [Intent.id.property.columns[0]]
    assert stmt._limit_clause.value == 1 and stmt._for_update_arg is not None
    for dialect in (postgresql.dialect(), mysql.dialect()):
        sql = str(stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        assert "LIMIT 1 FOR UPDATE" in sql
        assert (
            "ORDER BY casdoor_sync_intent_extend.namespace_id, "
            "casdoor_sync_intent_extend.scope_digest, casdoor_sync_intent_extend.id" in sql
        )
        assert "IN (SELECT casdoor_managed_membership_extend.id" in sql
        assert "casdoor_managed_membership_extend.account_id =" in sql
        assert "casdoor_managed_membership_extend.workspace_id =" in sql
        assert not any(
            name in sql for name in ("baseline_json", "desired_json", "last_applied_roles_json", "desired_roles_json")
        )
    assert not s.session.new and not s.session.dirty and not s.session.deleted


@pytest.mark.parametrize("has_history", [False, True])
@pytest.mark.parametrize("association", ["scope", "global", "membership", "unrelated"])
def test_original_inline_query_exact_row_equivalence_for_local_admitted_scopes(storage, has_history, association):
    s = storage
    with s.session.begin():
        if not has_history:
            s.session.delete(s.history)
        values = {}
        if association == "global":
            values["workspace_id"] = None
        elif association in ("membership", "unrelated"):
            values.update(account_id=str(uuid4()), workspace_id=str(uuid4()))
            if has_history and association == "membership":
                values["membership_id"] = s.history.id
        intent(s, **values)
    with s.session.begin():
        refs = s.session.execute(
            sa.select(History.id).where(History.account_id == s.account.id, History.workspace_id == s.workspace.id)
        ).all()
        original = tuple(
            s.session.execute(
                sa.select(Intent.id)
                .where(
                    Intent.kind != CasdoorIntentKind.PROFILE_AVATAR,
                    sa.or_(
                        sa.and_(
                            Intent.account_id == s.account.id,
                            sa.or_(Intent.workspace_id == s.workspace.id, Intent.workspace_id.is_(None)),
                        ),
                        Intent.membership_id.in_([ref.id for ref in refs]),
                    ),
                )
                .order_by(Intent.namespace_id, Intent.scope_digest, Intent.id)
                .limit(1)
                .with_for_update()
            )
        )
        current = read(s)
        assert current == original
        assert not current or type(current[0]) is type(original[0])
