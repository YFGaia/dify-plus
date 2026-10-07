"""Independent SQLite counterexamples for the bounded preflight owner."""

from uuid import UUID

import pytest
import sqlalchemy as sa
from models.casdoor_extend import CasdoorIdentityExtend
from repositories.casdoor_account_preflight_repository_extend import (
    AccountPreflightConflict,
    CasdoorAccountPreflightRepository,
)
from sqlalchemy.orm import Session

from test_casdoor_account_preflight_repository_extend import db as _author_db
from test_casdoor_account_preflight_repository_extend import run


@pytest.fixture
def db(tmp_path):
    # Reuse the author's actual mapped SQLite fixture without importing its tests
    # as collected cases.
    yield from _author_db.__wrapped__(tmp_path)


def test_external_active_pointer_change_overrides_stale_identity_map(db):
    session, _, _, _, _ = db
    from models.casdoor_extend import CasdoorIntegrationExtend

    stale = session.get(CasdoorIntegrationExtend, str(db[1].integration_id))
    assert stale is not None
    with Session(session.bind) as writer:
        writer.execute(sa.update(CasdoorIntegrationExtend).values(active_revision_id=None))
        writer.commit()
    assert stale.active_revision_id is not None
    with session.begin(), pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
        run(db)


def test_final_reciprocal_identity_rebind_drift_denies(db, monkeypatch):
    session, _, _, account, _ = db
    original = CasdoorAccountPreflightRepository._subject

    def rebound(self, key, *, lock):
        row = original(self, key, lock=lock)
        if lock and row is not None:
            # Model a rebind visible on the final locked reread; the snapshot is
            # deliberately changed only at the discovery/final boundary.
            return tuple(
                "other-account" if column == "account_id" else value
                for column, value in zip(row._fields, row)
            )
        return row

    monkeypatch.setattr(CasdoorAccountPreflightRepository, "_subject", rebound)
    with session.begin(), pytest.raises(AccountPreflightConflict, match="^identity_conflict$"):
        run(db)
    assert account.email == "local@example.com"


def test_invalid_persisted_account_uuid_does_not_return_success_or_echo_row(db):
    session, _, _, account, _ = db
    session.execute(sa.update(CasdoorIdentityExtend).values(account_id="private-malformed-account"))
    session.commit()
    with session.begin():
        with pytest.raises(AccountPreflightConflict, match="^identity_conflict$") as caught:
            run(db)
    assert "private-malformed-account" not in str(caught.value)
