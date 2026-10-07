"""Independent SQLite composition checks for the I22-A profile owner."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

pytest_plugins = ("test_casdoor_profile_repository_extend",)


@pytest.mark.parametrize("fail_audit", [False, True])
def test_b2_create_with_profile_disabled_has_no_second_name_write(storage, monkeypatch, fail_audit):
    """Compose actual B2 setup, generation allocation and profile on one SQLite root."""
    from core.casdoor.admission import (
        AdmissionAction,
        AdmissionPlan,
        SharedOwnerRequirement,
        resolve_initial_setup,
    )
    from core.casdoor.auth_transactions import AuthMode
    from core.casdoor.claims import StructuredUserRef, VerifiedProfile
    from core.casdoor.mapping import (
        BuiltinResolution,
        ServerWorkspaceAvailability,
        WorkspaceAvailability,
        WorkspaceState,
        resolve_workspace_plan,
    )
    from core.casdoor.request_safety import ProfileAuditResult
    from core.casdoor.role_graph import EffectiveRoleSnapshot
    from enums import DeploymentEdition
    from models.account import Account
    from models.account_money_extend import AccountMoneyExtend
    from models.casdoor_extend import CasdoorAuditExtend, CasdoorIdentityExtend, CasdoorIntegrationExtend
    from repositories.account_activation_repository import SQLAlchemyAccountActivationRepository
    from repositories.casdoor_account_preflight_repository_extend import CasdoorAccountPreflightRepository
    from repositories.casdoor_generation_repository_extend import CasdoorGenerationRepository
    from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
    from services import account_service
    from services.account_activation_service import AccountActivationService
    from services.casdoor_login_account_service_extend import CasdoorLoginAccountService
    from tests.unit_tests.core.casdoor import test_configuration_repository_extend as configuration

    s = storage
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    with s.session.begin():
        s.session.execute(sa.delete(CasdoorIdentityExtend))
        s.session.execute(sa.delete(AccountMoneyExtend))
        s.session.execute(sa.delete(Account))
        config = s.config_owner._configuration(s.revision)
        off_config = config.model_copy(update={"name_sync": "off"})
        integration = s.session.get(CasdoorIntegrationExtend, s.revision.integration_id)
        saved = s.config_owner.save_draft(
            off_config,
            etag=integration.etag,
            actor_account_id=configuration.ACTOR,
            secret=None,
        )
        # Resolve the saved draft through the original configuration owner and activate its revision.
        revision = s.session.get(type(s.revision), str(saved.draft_revision_id))
        config = s.config_owner._configuration(revision)
        integration.active_revision_id = revision.id
        s.session.flush()
        context = replace(s.context, revision_id=UUID(revision.id), config_digest=revision.config_digest)
        admission_context = replace(
            s.admission.context,
            revision_id=context.revision_id,
            active_revision_id=context.revision_id,
            config_digest=revision.config_digest,
        )
        old_audit_ids = set(s.session.scalars(sa.select(CasdoorAuditExtend.id)))
        if fail_audit:
            s.session.execute(
                sa.text(
                    "CREATE TRIGGER fail_off_profile_audit BEFORE INSERT ON casdoor_audit_extend "
                    "BEGIN SELECT RAISE(ABORT, 'off profile audit fail'); END"
                )
            )

    features = Mock()
    features.get_license.return_value.seats.is_available.return_value = True
    monkeypatch.setattr(account_service, "SystemFeatureService", features)
    monkeypatch.setattr(account_service.dify_config, "DEPLOYMENT_EDITION", DeploymentEdition.COMMUNITY)
    monkeypatch.setattr(account_service.dify_config, "ACCOUNT_TOTAL_QUOTA", Decimal(15))
    activation = AccountActivationService(
        tokens=Mock(),
        accounts=SQLAlchemyAccountActivationRepository(None),
        workspace_policy=Mock(),
        eligibility=Mock(),
        membership_cache=Mock(),
        member_access_sync=Mock(),
    )
    b2 = CasdoorLoginAccountService(activation=activation)
    key = VerifiedIdentityKey(context.namespace_id, context.issuer, context.organization, context.subject)
    remote = VerifiedProfile(context.subject, "off@example.test", True, " Setup Owned Name ", None, None)
    setup = resolve_initial_setup(remote, local_email=remote.email, request_language="en-US", request_timezone="UTC")
    plan = AdmissionPlan(
        admission_context,
        AuthMode.LOGIN,
        AdmissionAction.CREATE_INITIALIZED,
        creation_email=remote.email,
        setup=setup,
        required_shared_owners=(
            SharedOwnerRequirement.ACCOUNT_CREATION_PREPARE,
            SharedOwnerRequirement.ACCOUNT_SETUP_PERSIST,
        ),
    )
    with s.session.begin():
        preflight = CasdoorAccountPreflightRepository(s.session).reconstruct(
            plan.context, key, collision_email=remote.email
        )
    prepared = b2._prepare_login(plan=plan, preflight=preflight, deadline=monotonic() + 40)

    def write_uow():
        with s.session.begin():
            result = b2.persist_login_account(prepared, session=s.session, context=plan.context, key=key)
            s.session.flush()
            created_context = replace(context, account_id=result.account_id, identity_id=result.identity_id)
            desired = resolve_workspace_plan(
                configuration=config,
                context=created_context,
                snapshot=EffectiveRoleSnapshot(
                    created_context.subject, StructuredUserRef(created_context.organization, "online-user"), ()
                ),
                availability=ServerWorkspaceAvailability(
                    (
                        WorkspaceAvailability(
                            config.default_workspace_id,
                            WorkspaceState.NORMAL,
                            (BuiltinResolution("normal", "synthetic-normal"),),
                        ),
                    )
                ),
            )
            generation = CasdoorGenerationRepository(s.session).allocate(
                desired, expected_fence_epoch=0, expected_generation=0
            )
            before_account = s.session.execute(sa.select(Account.name, Account.updated_at)).one()
            statements = []
            sa.event.listen(
                s.session.bind,
                "before_cursor_execute",
                lambda _c, _cu, statement, _p, _ct, _m: statements.append(statement),
            )
            out = s.owner.persist(
                created_context,
                remote,
                expected_generation=generation.generation,
                expected_fence_epoch=generation.fence_epoch,
                auth_started_at=now,
                admission=plan,
                correlation_id=s.correlation,
                now=now,
            )
            assert out.name_status is ProfileAuditResult.DISABLED
            assert not any(statement.startswith("UPDATE accounts") for statement in statements)
            assert s.session.execute(sa.select(Account.name, Account.updated_at)).one() == before_account
            assert s.session.scalar(sa.select(CasdoorIdentityExtend.last_applied_json)) == "{}"
            assert s.session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == generation.generation
            assert s.session.scalar(sa.select(AccountMoneyExtend.total_quota)) == Decimal(15)
            assert (
                s.session.scalar(
                    sa.select(CasdoorAuditExtend.result_code).where(
                        CasdoorAuditExtend.action == "profile_sync",
                    )
                )
                == ProfileAuditResult.DISABLED.value
            )
            if not fail_audit:
                s.session.rollback()

    if fail_audit:
        with pytest.raises(IntegrityError, match="off profile audit fail"):
            write_uow()
    else:
        write_uow()
    with s.session.begin():
        assert s.session.scalar(sa.select(sa.func.count()).select_from(Account)) == 0
        assert s.session.scalar(sa.select(sa.func.count()).select_from(AccountMoneyExtend)) == 0
        assert s.session.scalar(sa.select(sa.func.count()).select_from(CasdoorIdentityExtend)) == 0
        assert set(s.session.scalars(sa.select(CasdoorAuditExtend.id))) == old_audit_ids
