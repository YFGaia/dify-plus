"""Signal propagation after committed navigation but before candidate work."""

from unittest.mock import Mock

import pytest
from repositories.casdoor_avatar_repository_extend import CasdoorAvatarRepository
from test_casdoor_avatar_dispatch_extend import (
    avatar_fixture as original_avatar_fixture,
)
from test_casdoor_avatar_dispatch_extend import composed as original_composed
from test_casdoor_avatar_dispatch_extend import consumer as original_consumer
from test_casdoor_avatar_dispatch_extend import dispatch as original_dispatch
from test_casdoor_avatar_dispatch_extend import registered as original_registered
from test_casdoor_avatar_dispatch_extend import row as original_row
from test_casdoor_avatar_dispatch_extend import (
    storage_fixture as original_storage_fixture,
)

avatar_fixture = original_avatar_fixture
composed = original_composed
consumer = original_consumer
dispatch = original_dispatch
registered = original_registered
row = original_row
storage_fixture = original_storage_fixture


def test_navigation_close_interrupt_propagates_sanitized_without_candidate_or_publish(
    dispatch, monkeypatch
):
    s = dispatch
    before = row(s)
    first_new_session = len(s.sessions) + 1
    commits = []
    s.commit_hook = commits.append
    s.close_hook = (
        lambda index: (_ for _ in ()).throw(
            KeyboardInterrupt("private shutdown detail")
        )
        if index == first_new_session
        else None
    )
    candidate = Mock(side_effect=AssertionError("candidate must not run"))
    monkeypatch.setattr(
        CasdoorAvatarRepository, "_initial_dispatch_candidate", candidate
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        s.recovery()

    assert commits == [first_new_session]
    assert len(s.sessions) == first_new_session
    assert s.sessions[-1].consumer_closed and not s.sessions[-1].in_transaction()
    assert str(caught.value) == "avatar dispatch interrupted"
    assert caught.value.__cause__ is caught.value.__context__ is None
    candidate.assert_not_called()
    assert s.published == []
    assert row(s) == before
