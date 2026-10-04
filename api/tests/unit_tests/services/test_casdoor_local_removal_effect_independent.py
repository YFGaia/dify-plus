"""Independent proof that a real later SQL failure remains caller-rollbackable."""

import pytest
from enums import DeploymentEdition
from models.account import Account, TenantAccountJoin
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import CasdoorIdentityExtend
from models.dataset import Dataset
from models.model import App
from services.account_service import TenantService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.unit_tests.services.test_casdoor_local_removal_effect_extend import seed, snapshot


def test_public_removal_dataset_trigger_failure_rolls_back_prior_app_update(sqlite_session_factory, config_overrides):
    config_overrides(RBAC_ENABLED=False, DEPLOYMENT_EDITION=DeploymentEdition.COMMUNITY)
    protected = (Account, AccountMoneyExtend, CasdoorIdentityExtend)
    observed_models = (*protected, TenantAccountJoin, App, Dataset)

    with sqlite_session_factory() as session:
        state = seed(session)
        before = {model: snapshot(session, model) for model in observed_models}
        member_id = state.member.id
        owner_id = state.owner.id
        tenant_id = state.tenant.id

        # A real SQLite trigger interrupts the public caller's second UPDATE.
        session.execute(
            text(
                "CREATE TRIGGER fail_dataset_removal BEFORE UPDATE OF maintainer ON datasets "
                "BEGIN SELECT RAISE(ABORT, 'injected dataset removal failure'); END"
            )
        )
        with pytest.raises(IntegrityError, match="injected dataset removal failure"):
            TenantService.remove_member_from_tenant(state.tenant, state.member, state.owner, session=session)

        # SQLite preserved the first UPDATE inside the still-open root transaction.
        assert (
            session.scalar(
                text(
                    "SELECT COUNT(*) FROM apps WHERE tenant_id = :tenant AND maintainer = :owner "
                    "AND created_by = :member"
                ),
                {"tenant": tenant_id, "owner": owner_id, "member": member_id},
            )
            == 2
        )
        assert (
            session.scalar(
                text("SELECT COUNT(*) FROM tenant_account_joins WHERE tenant_id = :tenant AND account_id = :member"),
                {"tenant": tenant_id, "member": member_id},
            )
            == 1
        )

        # The public service leaves transaction recovery to its caller.
        session.rollback()

    with sqlite_session_factory() as observer:
        for model in observed_models:
            assert snapshot(observer, model) == before[model]
