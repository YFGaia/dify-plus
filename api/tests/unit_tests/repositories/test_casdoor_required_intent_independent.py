"""Independent actual-SQL checks for mixed required-intent associations."""

from uuid import UUID, uuid4

import sqlalchemy as sa
from models.casdoor_extend import CasdoorIntentKind, CasdoorOperationState, CasdoorTerminationState
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_required_intent_repository_extend import CasdoorRequiredIntentRepository
from test_casdoor_local_role_repository_extend import storage as storage_fixture

storage = storage_fixture


def _namespace(session, template, identifier):
    values = dict(
        session.execute(sa.select(*Namespace.__table__.columns).where(Namespace.id == template.id)).one()._mapping
    )
    values.update(id=identifier, core_fingerprint=uuid4().hex * 2)
    session.execute(sa.insert(Namespace).values(**values))
    return identifier


def _intent(storage, **overrides):
    values = dict(
        namespace_id=storage.namespace.id,
        identity_id=str(uuid4()),
        account_id=storage.account.id,
        workspace_id=storage.workspace.id,
        revision_id=storage.revision.id,
        generation=93,
        ownership_epoch=7,
        fence_epoch=0,
        kind=CasdoorIntentKind.ROLE_REPLACE,
        scope_digest="m" * 64,
        idempotency_key=uuid4().hex * 2,
        desired_json="{}",
        operation_state=CasdoorOperationState.APPLIED,
        termination_state=CasdoorTerminationState.CONFIRMED,
    )
    values.update(overrides)
    row = Intent(**values)
    storage.session.add(row)
    return row


def _read(storage):
    return CasdoorRequiredIntentRepository(storage.session).read_locked(
        UUID(storage.account.id), UUID(storage.workspace.id)
    )


def test_mixed_third_history_membership_still_blocks_amid_avatar_and_unrelated_rows(storage):
    """Membership linkage survives first-two discovery and unrelated mixed rows."""
    s = storage
    with s.session.begin():
        base = dict(s.session.execute(sa.select(*History.__table__.columns)).one()._mapping)
        third_namespace = _namespace(s.session, s.namespace, "ffffffff-ffff-ffff-ffff-ffffffffffff")
        s.session.execute(sa.update(History).values(namespace_id=third_namespace))
        for number in (1, 2):
            values = dict(base)
            values.update(
                id=str(uuid4()),
                namespace_id=_namespace(s.session, s.namespace, str(UUID(int=number))),
                baseline_json="unread-history-" * 4000,
            )
            s.session.execute(sa.insert(History).values(**values))
        first_two = s.session.scalars(sa.select(History.id).order_by(History.namespace_id, History.id).limit(2)).all()
        assert s.history.id not in first_two

        unrelated = _intent(s, account_id=str(uuid4()), workspace_id=str(uuid4()), scope_digest="a" * 64)
        avatar = _intent(
            s,
            account_id=str(uuid4()),
            workspace_id=str(uuid4()),
            membership_id=s.history.id,
            kind=CasdoorIntentKind.PROFILE_AVATAR,
            scope_digest="b" * 64,
        )
        associated = _intent(
            s,
            account_id=str(uuid4()),
            workspace_id=str(uuid4()),
            membership_id=s.history.id,
            scope_digest="c" * 64,
            operation_state=CasdoorOperationState.APPLIED,
            termination_state=CasdoorTerminationState.CONFIRMED,
        )
    with s.session.begin():
        result = _read(s)
        assert result == ((associated.id,),)
        assert unrelated.id != associated.id and avatar.id != associated.id


def test_foreign_account_global_intents_do_not_block_target(storage):
    s = storage
    with s.session.begin():
        foreign_global = _intent(s, account_id=str(uuid4()), workspace_id=None)
    with s.session.begin():
        assert _read(s) == ()
        assert foreign_global.workspace_id is None


def test_mixed_candidates_keep_deterministic_scalar_order_without_false_negatives(storage):
    s = storage
    with s.session.begin():
        namespace_ids = [_namespace(s.session, s.namespace, str(UUID(int=n))) for n in (11, 12, 13)]
        # The first ordered candidate is account-global; the later workspace
        # candidate remains discoverable when the first row is removed.
        first = _intent(s, namespace_id=namespace_ids[1], scope_digest="b" * 64, workspace_id=None)
        later = _intent(s, namespace_id=namespace_ids[2], scope_digest="a" * 64)
        avatar = _intent(
            s,
            namespace_id=namespace_ids[0],
            scope_digest="a" * 64,
            kind=CasdoorIntentKind.PROFILE_AVATAR,
        )
    with s.session.begin():
        assert _read(s) == ((first.id,),)
        s.session.execute(sa.delete(Intent).where(Intent.id == first.id))
    with s.session.begin():
        assert _read(s) == ((later.id,),)
        assert avatar.id != later.id


def test_read_only_blocking_observation_preserves_prior_caller_write(storage):
    s = storage
    marker = "caller-write-survives-read"
    with s.session.begin():
        _intent(s)
    with s.session.begin():
        s.session.execute(sa.update(type(s.account)).where(type(s.account).id == s.account.id).values(name=marker))
        blocking = _read(s)
        assert len(blocking) == 1
        assert s.session.scalar(sa.select(type(s.account).name).where(type(s.account).id == s.account.id)) == marker
        assert not s.session.new and not s.session.dirty and not s.session.deleted
