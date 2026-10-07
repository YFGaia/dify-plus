"""Client UUIDs make fork inserts usable on engines without UUID RETURNING."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from models.model_extend import AppExtend, AppStatisticsExtend, EndUserAccountJoinsExtend, MessageContextExtend

MODELS = [AppStatisticsExtend, EndUserAccountJoinsExtend, AppExtend, MessageContextExtend]


def make_row(model):
    if model is EndUserAccountJoinsExtend:
        return model(end_user_id=str(uuid4()), account_id=str(uuid4()), app_id=str(uuid4()))
    if model is MessageContextExtend:
        return model(message_id=str(uuid4()), conversation_id=str(uuid4()))
    return model(app_id=str(uuid4()))


@pytest.mark.parametrize("model", MODELS)
def test_omitted_uuid_flushes_reads_back_and_rolls_back(model, sqlite_session: Session) -> None:
    column = model.__table__.c.id
    assert column.default is not None  # MySQL cannot recover the server-generated UUID identity.
    assert str(column.server_default.arg) == "uuid_generate_v4()"
    rows = [make_row(model), make_row(model)]
    assert all(row.id is None for row in rows)
    sqlite_session.add_all(rows)
    sqlite_session.flush()
    identifiers = [row.id for row in rows]
    assert len(set(identifiers)) == 2
    assert all(UUID(identifier).version == 4 for identifier in identifiers)
    sqlite_session.expunge_all()
    assert all(sqlite_session.get(model, identifier) is not None for identifier in identifiers)
    sqlite_session.rollback()
    assert sqlite_session.query(model).count() == 0


@pytest.mark.parametrize("model", MODELS)
def test_explicit_uuid_is_preserved(model, sqlite_session: Session) -> None:
    row = make_row(model)
    identifier = str(uuid4())
    row.id = identifier
    sqlite_session.add(row)
    sqlite_session.flush()
    assert row.id == identifier
    sqlite_session.expunge_all()
    assert sqlite_session.get(model, identifier) is not None
    sqlite_session.rollback()
    assert sqlite_session.query(model).count() == 0
