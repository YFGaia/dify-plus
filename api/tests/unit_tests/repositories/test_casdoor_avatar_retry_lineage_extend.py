"""Actual v2 terminal producer and strict bounded retry lineage, no capability fixtures."""

import json
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from models.casdoor_extend import CasdoorAuditExtend as Audit
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorManagedMembershipExtend as History
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_avatar_repository_extend import (
    CasdoorAvatarConflict,
    CasdoorAvatarRepository,
)
from sqlalchemy.orm import Session
from test_casdoor_avatar_consumer_extend import (
    avatar_fixture as original_avatar_fixture,
)
from test_casdoor_avatar_consumer_extend import consumer as original_consumer
from test_casdoor_avatar_consumer_extend import row
from test_casdoor_avatar_consumer_extend import (
    storage_fixture as original_storage_fixture,
)
from test_casdoor_avatar_pre_storage_recovery_extend import produce

consumer = original_consumer
avatar_fixture = original_avatar_fixture
storage_fixture = original_storage_fixture


def repo(s, session):
    return CasdoorAvatarRepository(
        session,
        configuration_repository=s.consumer._configuration_service._repository(session),
    )


def prepare(s):
    with s.maker() as session, session.begin():
        return repo(s, session).prepare_confirmed_pre_storage_retry(
            s.intent_id, actor_account_id=UUID(s.account.id), now=s.utc
        )


@pytest.mark.parametrize(
    "field", ["updated_at", "proof_ref", "reason", "digest", "duplicate", "oversize"]
)
def test_original_v2_exact_facts_cannot_be_replaced_by_retry(field, consumer):
    s = consumer
    produce(s)
    with Session(s.session.get_bind()) as session, session.begin():
        audit = session.scalar(
            sa.select(Audit).where(Audit.action == "avatar_pre_storage")
        )
        if field == "updated_at":
            session.execute(
                sa.update(Intent).values(
                    updated_at=sa.func.datetime(Intent.updated_at, "+1 second")
                )
            )
        elif field == "proof_ref":
            session.execute(sa.update(Intent).values(proof_ref=str(uuid4())))
        elif field == "duplicate":
            values = {
                column.name: getattr(audit, column.name)
                for column in Audit.__table__.columns
            }
            values["id"] = str(uuid4())
            session.add(Audit(**values))
        elif field == "oversize":
            audit.summary_json = "x" * 8193
        else:
            value = json.loads(audit.summary_json)
            value["reason" if field == "reason" else "terminal_intent_sha256"] = (
                "image_rejected" if field == "reason" else "0" * 64
            )
            audit.summary_json = json.dumps(
                value, sort_keys=True, separators=(",", ":")
            )
    with pytest.raises(CasdoorAvatarConflict):
        prepare(s)
    assert not s.data


@pytest.mark.parametrize("stage", ["pending", "claim"])
@pytest.mark.parametrize("damage", ["missing", "duplicate", "hash"])
def test_every_retry_stage_requires_unique_actual_transition_audit(
    consumer, stage, damage
):
    s = consumer
    produce(s)
    prepare(s)
    if stage == "claim":
        with s.maker() as session, session.begin():
            claim = repo(s, session).claim_and_reserve(s.intent_id, now=s.utc)
            assert claim.code == "reserved"
    with Session(s.session.get_bind()) as session, session.begin():
        audit = session.scalar(
            sa.select(Audit).where(
                Audit.action
                == ("avatar_retry" if stage == "pending" else "avatar_retry_claim")
            )
        )
        if damage == "missing":
            session.delete(audit)
        elif damage == "duplicate":
            values = {
                column.name: getattr(audit, column.name)
                for column in Audit.__table__.columns
            }
            values["id"] = str(uuid4())
            session.add(Audit(**values))
        else:
            value = json.loads(audit.summary_json)
            value["pending_sha256"] = "0" * 64
            audit.summary_json = json.dumps(
                value, sort_keys=True, separators=(",", ":")
            )
    with s.maker() as session, session.begin(), pytest.raises(CasdoorAvatarConflict):
        repo(s, session)._worker_root(s.intent_id, lock=False)
    assert not s.data


@pytest.mark.parametrize(
    "gap",
    [
        "terminal_intent",
        "fake_applied_avatar",
        "foreign_identity",
        "bad_subject",
        "foreign_history",
    ],
)
def test_full_related_scope_is_proved_not_just_locked_or_labelled(consumer, gap):
    s = consumer
    produce(s)
    with Session(s.session.get_bind()) as session, session.begin():
        original = dict(row(s))
        if gap in ("terminal_intent", "fake_applied_avatar"):
            original.update(
                id=str(uuid4()),
                generation=0,
                idempotency_key="negative-" + str(uuid4()),
                scope_digest="1" * 64,
                operation_state="applied",
                termination_state="confirmed",
            )
            if gap == "terminal_intent":
                original.update(kind="role_replace", desired_json="{}")
            session.add(Intent(**original))
        elif gap == "bad_subject":
            session.execute(sa.update(Identity).values(subject_digest="0" * 64))
        elif gap == "foreign_identity":
            session.execute(sa.update(Identity).values(account_id=str(uuid4())))
        else:
            identity = session.scalar(sa.select(Identity))
            foreign = str(uuid4())
            values = {
                column.name: getattr(identity, column.name)
                for column in Identity.__table__.columns
            }
            values.update(
                id=foreign,
                account_id=str(uuid4()),
                subject="different",
                subject_digest=__import__("hashlib").sha256(b"different").hexdigest(),
            )
            session.add(Identity(**values))
            session.add(
                History(
                    namespace_id=identity.namespace_id,
                    identity_id=foreign,
                    account_id=s.account.id,
                    workspace_id=s.revision.default_workspace_id,
                    revision_id=s.revision.id,
                    ownership="managed",
                    source="mapping",
                    ownership_epoch=0,
                    desired_generation=1,
                    finalization="finalized",
                    tombstone=False,
                    last_applied_roles_json="[]",
                    desired_roles_json="[]",
                    baseline_json="{}",
                )
            )
    with s.maker() as session, session.begin(), pytest.raises(CasdoorAvatarConflict):
        repo(s, session).inspect_retry_target(s.intent_id, now=s.utc)
    with pytest.raises(CasdoorAvatarConflict):
        prepare(s)
    assert not s.data


def test_retry_original_audit_and_first_reservation_are_retained(consumer):
    s = consumer
    produce(s)
    old = row(s)
    with Session(s.session.get_bind()) as session:
        old_audit = dict(
            session.execute(
                sa.select(Audit.__table__).where(Audit.action == "avatar_pre_storage")
            )
            .mappings()
            .one()
        )
    prepare(s)
    with s.maker() as session, session.begin():
        claim = repo(s, session).claim_and_reserve(s.intent_id, now=s.utc)
        assert claim.code == "reserved"
    new = row(s)
    assert (
        json.loads(new["desired_json"])["reservations"][0]
        == json.loads(old["desired_json"])["reservations"][0]
    )
    with Session(s.session.get_bind()) as session:
        assert (
            dict(
                session.execute(
                    sa.select(Audit.__table__).where(Audit.id == old_audit["id"])
                )
                .mappings()
                .one()
            )
            == old_audit
        )
        summaries = session.scalars(
            sa.select(Audit.summary_json).where(
                Audit.action.in_(["avatar_retry", "avatar_retry_claim"])
            )
        ).all()
        assert len(summaries) == 2
        for text in summaries:
            assert (
                "url_ciphertext" not in text
                and "desired_json" not in text
                and "images.example" not in text
            )
