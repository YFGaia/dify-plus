"""Invoke unchanged original profile assertions with a finite isolated SQLite fixture."""

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from test_account_repository import (
    test_update_profile_persists_multiple_fields as test_update_profile_persists_multiple_fields,
)
from test_account_repository import test_update_profile_rolls_back_on_error as test_update_profile_rolls_back_on_error

from models.account import Account


@pytest.fixture
def sqlite_session_factory(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "legacy.sqlite"}')
    Account.__table__.create(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def sqlite_session(sqlite_session_factory):
    with sqlite_session_factory() as session:
        yield session
