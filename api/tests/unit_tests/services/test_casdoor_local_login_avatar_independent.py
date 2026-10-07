"""Adversarial final-read check for actual LOCAL pending avatar persistence."""

import pytest
import sqlalchemy as sa
from test_casdoor_local_login_avatar_extend import (
    all_business,
    args,
    execute,
    policy,
    ready,
    rows,
)

from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorSyncIntentExtend as Intent
from repositories.casdoor_login_scope_repository_extend import CasdoorLoginScopeConflict

pytest_plugins = ("test_casdoor_local_login_avatar_extend",)


def test_avatar_audit_trigger_corrupting_profile_watermark_fails_final_reread_and_rolls_back(profiled):
    local = profiled
    policy(local, "managed")
    before_business = all_business(local)
    before_intents = rows(local, Intent)
    bundle = ready(local)

    with local.session.begin():
        local.session.execute(
            sa.text(
                "CREATE TRIGGER corrupt_profile_after_avatar_audit "
                "AFTER INSERT ON casdoor_audit_extend "
                "WHEN NEW.action = 'avatar_pending' "
                "BEGIN UPDATE casdoor_identity_extend SET profile_sync_json = '{}'; END"
            )
        )

    with pytest.raises(CasdoorLoginScopeConflict):
        execute(local, bundle, **args(local))

    assert all_business(local) == before_business
    assert rows(local, Intent) == before_intents
    with local.session.begin():
        assert local.session.scalar(sa.select(sa.func.count()).select_from(Identity)) == 0
        assert local.session.scalar(sa.select(sa.func.count()).select_from(Intent)) == 0
