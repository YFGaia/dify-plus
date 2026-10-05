"""Actual original terminal and count2 claim SQL lineage; no cleanup producer."""

import json

import sqlalchemy as sa
from sqlalchemy.orm import Session
from test_casdoor_avatar_retry_lineage_extend import (
    avatar_fixture as original_avatar_fixture,
)
from test_casdoor_avatar_retry_lineage_extend import (
    consumer as original_consumer,
)
from test_casdoor_avatar_retry_lineage_extend import (
    prepare,
    produce,
    repo,
    row,
)
from test_casdoor_avatar_retry_lineage_extend import (
    storage_fixture as original_storage_fixture,
)

from models.casdoor_avatar_file_guard_extend import CasdoorAvatarFileGuardExtend as Guard
from models.casdoor_extend import CasdoorAuditExtend as Audit

consumer = original_consumer
avatar_fixture = original_avatar_fixture
storage_fixture = original_storage_fixture


def test_actual_count2_claim_has_two_fences_and_retains_first_reservation(consumer):
    s = consumer
    produce(s)
    first = row(s)
    with Session(s.session.get_bind()) as session:
        old = dict(
            session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == "avatar_reservation_fence"))
            .mappings()
            .one()
        )
    prepare(s)
    with s.maker() as session, session.begin():
        result = repo(s, session).claim_and_reserve(s.intent_id, now=s.utc)
        assert result.code == "reserved"
    second = row(s)
    assert json.loads(second["desired_json"])["reservations"][0] == json.loads(first["desired_json"])["reservations"][0]
    with Session(s.session.get_bind()) as session:
        fences = (
            session.execute(sa.select(*Audit.__table__.columns).where(Audit.action == "avatar_reservation_fence"))
            .mappings()
            .all()
        )
        assert len(fences) == 2 and dict(next(x for x in fences if x["id"] == old["id"])) == old
        fresh = json.loads(next(x["summary_json"] for x in fences if x["id"] != old["id"]))
        assert fresh["count"] == 2 and len(fresh["lineage"]) == 5
        guards = session.scalars(sa.select(Guard)).all()
        assert len(guards) == 2 and all(x.stage == "reserved" for x in guards)
        assert not s.data
