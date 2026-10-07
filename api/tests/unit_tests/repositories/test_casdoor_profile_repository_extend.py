"""Real SQLite profile/configuration/setup/audit UoW; no authentication proof."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.casdoor.admission import (
    AdmissionAction,
    AdmissionContext,
    AdmissionPlan,
    resolve_initial_setup,
)
from core.casdoor.auth_transactions import AuthMode
from core.casdoor.claims import VerifiedProfile
from core.casdoor.mapping import MappingIdentityContext
from core.casdoor.request_safety import ProfileAuditResult, ProfileNameReason
from models.account import Account, AccountStatus, Tenant
from models.account_money_extend import AccountMoneyExtend
from models.casdoor_extend import (
    CasdoorAuditExtend,
    CasdoorConfigRevisionExtend,
    CasdoorIdentityExtend,
    CasdoorIntegrationExtend,
    CasdoorNamespaceExtend,
    CasdoorNamespaceLifecycle,
    CasdoorValidationExtend,
)
from repositories.account_activation_repository import (
    SQLAlchemyAccountActivationRepository,
)
from repositories.casdoor_profile_repository_extend import (
    CasdoorProfileConflict,
    CasdoorProfileRepository,
)
from tests.unit_tests.core.casdoor import (
    test_configuration_repository_extend as configuration,
)

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


@pytest.fixture
def storage():
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def connect(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @sa.event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    metadata = sa.MetaData()
    for model in (
        Account,
        Tenant,
        AccountMoneyExtend,
        CasdoorIntegrationExtend,
        CasdoorNamespaceExtend,
        CasdoorConfigRevisionExtend,
        CasdoorIdentityExtend,
        CasdoorAuditExtend,
        CasdoorValidationExtend,
    ):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if column.server_default is not None and str(column.server_default.arg) == "CURRENT_TIMESTAMP(0)":
                column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            tenant = Tenant(name="Synthetic workspace")
            session.add(tenant)
        from core.casdoor.permissions import CasdoorManagementPolicy
        from services.casdoor_configuration_service_extend import CasdoorConfigurationService

        config_owner = CasdoorConfigurationService(
            session_factory=None,
            management_policy=CasdoorManagementPolicy(),
            secret_key="synthetic-profile-key",
            rbac_enabled=False,
        )._repository(session)
        saved = configuration.save(
            session,
            configuration.config(tenant.id, name_sync="managed"),
            repository=config_owner,
        )
        with session.begin():
            revision = session.get(CasdoorConfigRevisionExtend, str(saved.draft_revision_id))
            integration = session.get(CasdoorIntegrationExtend, revision.integration_id)
            integration.enabled = True
            integration.active_revision_id = revision.id
            account = Account(
                name="",
                email="local@example.test",
                normalized_email="local@example.test",
                status=AccountStatus.ACTIVE,
                initialized_at=NOW.replace(tzinfo=None),
                interface_language="ja-JP",
                timezone="Asia/Tokyo",
                interface_theme="dark",
                avatar="local-avatar",
                password="synthetic-hash",
                password_salt="synthetic-salt",
            )
            session.add(account)
            session.flush()
            session.add(AccountMoneyExtend(account_id=account.id, total_quota=91, used_quota=4))
            identity = CasdoorIdentityExtend(
                namespace_id=revision.namespace_id,
                account_id=account.id,
                issuer=revision.expected_issuer,
                organization=revision.organization,
                subject="SyntheticSubject",
                last_applied_json="{}",
                profile_sync_json="{}",
                sync_generation=1,
            )
            session.add(identity)
            session.flush()
            context = MappingIdentityContext(
                UUID(integration.id),
                UUID(revision.id),
                UUID(revision.namespace_id),
                UUID(identity.id),
                UUID(account.id),
                revision.config_digest,
                revision.expected_issuer,
                revision.organization,
                revision.application,
                revision.client_id,
                identity.subject,
            )
            ac = AdmissionContext(
                context.integration_id,
                context.revision_id,
                context.revision_id,
                context.namespace_id,
                CasdoorNamespaceLifecycle.ACTIVE,
                0,
                context.config_digest,
                context.issuer,
                context.organization,
                context.application,
                context.client_id,
                context.subject,
            )
            admission = AdmissionPlan(
                ac,
                AuthMode.LOGIN,
                AdmissionAction.USE_BOUND,
                account_id=context.account_id,
            )
            # Config save's audit is outside profile assertions.
            session.execute(sa.delete(CasdoorAuditExtend))
        yield SimpleNamespace(
            session=session,
            owner=CasdoorProfileRepository(session, configuration_repository=config_owner),
            config_owner=config_owner,
            context=context,
            admission=admission,
            account=account,
            identity=identity,
            revision=revision,
            correlation=uuid4(),
        )
    engine.dispose()


def profile(s, **changes):
    return replace(
        VerifiedProfile(s.context.subject, "remote@example.test", True, " Remote 汉 ", None, None),
        **changes,
    )


def persist(s, **changes):
    arguments = {
        "expected_generation": 1,
        "expected_fence_epoch": 0,
        "auth_started_at": NOW,
        "admission": s.admission,
        "correlation_id": s.correlation,
        "now": NOW,
    }
    p = changes.pop("profile", profile(s))
    arguments.update(changes)
    return s.owner.persist(s.context, p, **arguments)


def rows(s):
    return (
        tuple(s.session.execute(sa.select(*Account.__table__.columns)).one()),
        tuple(s.session.execute(sa.select(*CasdoorIdentityExtend.__table__.columns)).one()),
        tuple(s.session.execute(sa.select(*AccountMoneyExtend.__table__.columns)).one()),
    )


def trace(s):
    statements = []
    sa.event.listen(
        s.session.bind,
        "before_cursor_execute",
        lambda _c, _cu, st, _p, _ct, _m: statements.append(st),
    )
    return statements


def dml(statements):
    return [s for s in statements if s.startswith(("UPDATE", "INSERT", "DELETE"))]


def mode(s, name_sync):
    config = s.config_owner._configuration(s.revision).model_copy(update={"name_sync": name_sync})
    integration = s.session.get(CasdoorIntegrationExtend, str(s.context.integration_id))
    saved = s.config_owner.save_draft(config, etag=integration.etag, actor_account_id=configuration.ACTOR, secret=None)
    s.revision = s.session.get(CasdoorConfigRevisionExtend, str(saved.draft_revision_id))
    integration.active_revision_id = s.revision.id
    s.session.flush()
    s.context = replace(s.context, config_digest=s.revision.config_digest, revision_id=UUID(s.revision.id))
    s.admission = replace(
        s.admission,
        context=replace(
            s.admission.context,
            config_digest=s.revision.config_digest,
            revision_id=UUID(s.revision.id),
            active_revision_id=UUID(s.revision.id),
        ),
    )
    s.session.execute(sa.delete(CasdoorAuditExtend))


@pytest.mark.parametrize("policy", ["off", "fill_empty", "managed"])
@pytest.mark.parametrize("local", ["", "   ", "Existing", " Remote 汉 "])
def test_policy_exact_name_no_adoption_and_private_fields(storage, policy, local):
    s = storage
    with s.session.begin():
        mode(s, policy)
        s.session.execute(sa.update(Account).values(name=local, updated_at=NOW.replace(tzinfo=None)))
        before = rows(s)
        out = persist(s)
        after = rows(s)
        assert out.snapshot_changed
        assert after[2] == before[2]
        for column, old, new in zip(Account.__table__.columns, before[0], after[0], strict=True):
            if column.name not in ("name", "updated_at"):
                assert old == new, column.name
        applied = policy != "off" and not local.strip()
        assert s.session.scalar(sa.select(Account.name)) == (profile(s).name if applied else local)
        baseline = json.loads(s.session.scalar(sa.select(CasdoorIdentityExtend.last_applied_json)))
        assert ("name" in baseline) == applied
        updated_index = list(Account.__table__.columns.keys()).index("updated_at")
        if not applied:
            assert after[0][updated_index] == before[0][updated_index]
        else:
            assert after[0][updated_index] >= before[0][updated_index]
        assert not s.session.new
        assert not s.session.dirty
        assert not s.session.deleted
        assert s.session.in_transaction()
        audit = s.session.scalar(sa.select(CasdoorAuditExtend))
        assert audit.action == "profile_sync"
        assert audit.result_code == out.name_status.value
        raw = audit.summary_json + repr(out)
        for private in [
            profile(s).name,
            profile(s).email,
            s.context.subject,
            s.context.config_digest,
        ]:
            assert private not in raw


def test_managed_repeat_manual_edit_and_newer_watermark(storage):
    s = storage
    with s.session.begin():
        persist(s)
    statements = trace(s)
    with s.session.begin():
        assert not persist(s, now=NOW + timedelta(seconds=1)).snapshot_changed
    assert not dml(statements)
    with s.session.begin():
        s.session.execute(sa.update(Account).values(name="Manual"))
    for remote in [profile(s), profile(s, name="Changed remote")]:
        with s.session.begin():
            out = persist(
                s,
                profile=remote,
                auth_started_at=NOW + timedelta(seconds=2),
                now=NOW + timedelta(seconds=3),
                correlation_id=uuid4(),
            )
            # first is newer; second same start+foreign correlation safely ambiguous
            assert out.name_reason in (
                ProfileNameReason.LOCAL_OVERRIDE,
                ProfileNameReason.AMBIGUOUS_PROFILE_ATTEMPT,
            )
            assert s.session.scalar(sa.select(Account.name)) == "Manual"
    with s.session.begin():
        s.session.execute(sa.update(Account).values(name=profile(s).name))
        out = persist(
            s,
            profile=profile(s, name="Next"),
            auth_started_at=NOW + timedelta(seconds=4),
            now=NOW + timedelta(seconds=4),
        )
        assert out.name_status is ProfileAuditResult.APPLIED
        assert s.session.scalar(sa.select(Account.name)) == "Next"
    statements.clear()
    with s.session.begin():
        s.session.execute(sa.update(CasdoorIdentityExtend).values(sync_generation=2))
    statements.clear()
    with s.session.begin():
        persist(
            s,
            expected_generation=2,
            profile=profile(s, name="Next"),
            auth_started_at=NOW + timedelta(seconds=5),
            now=NOW + timedelta(seconds=5),
        )
    assert not any(st.startswith("UPDATE accounts") for st in statements)
    with s.session.begin():
        before = rows(s)
        out = persist(
            s,
            expected_generation=2,
            profile=profile(s, name="Too late"),
            auth_started_at=NOW,
            now=NOW + timedelta(seconds=6),
        )
        assert out.name_reason is ProfileNameReason.STALE_PROFILE_ATTEMPT
        assert rows(s) == before


@pytest.mark.parametrize(
    ("remote", "verified", "status"),
    [
        (None, True, "unavailable"),
        ("bad", False, "invalid"),
        ("local@example.test", None, "same"),
        ("other@example.test", False, "different"),
    ],
)
def test_remote_email_snapshot_pairs_and_never_changes_local(storage, remote, verified, status):
    s = storage
    with s.session.begin():
        s.session.execute(
            sa.update(CasdoorIdentityExtend).values(remote_email="prior@example.test", email_verified=True)
        )
        out = persist(s, profile=profile(s, email=remote, email_verified=verified, name=None))
        row = s.session.execute(
            sa.select(CasdoorIdentityExtend.remote_email, CasdoorIdentityExtend.email_verified)
        ).one()
        assert out.remote_email_status.value == status
        assert row == (("prior@example.test", None) if status in ("invalid", "unavailable") else (remote, verified))
        assert s.session.scalar(sa.select(Account.email)) == "local@example.test"


@pytest.mark.parametrize("policy", ["off", "fill_empty", "managed"])
@pytest.mark.parametrize("kind", ["create", "fallback", "initialize_bound", "initialize_invited"])
def test_original_setup_baseline_only_actual_create_remote_name(storage, policy, kind):
    s = storage
    with s.session.begin():
        mode(s, policy)
        remote = profile(s, name=None) if kind == "fallback" else profile(s)
        setup = resolve_initial_setup(
            remote,
            local_email=s.account.email,
            request_language="en-US",
            request_timezone="UTC",
        )
        SQLAlchemyAccountActivationRepository.persist_account_setup(s.account, setup)
        s.session.flush()
        action = {
            "create": AdmissionAction.CREATE_INITIALIZED,
            "fallback": AdmissionAction.CREATE_INITIALIZED,
            "initialize_bound": AdmissionAction.INITIALIZE_BOUND,
            "initialize_invited": AdmissionAction.INITIALIZE_INVITED,
        }[kind]
        admission = replace(
            s.admission,
            action=action,
            setup=setup,
            account_id=None if "create" in action.value else s.context.account_id,
            creation_email=s.account.email if "create" in action.value else None,
        )
        before = rows(s)
        statements = trace(s)
        persist(s, profile=remote, admission=admission)
        assert not any(st.startswith("UPDATE accounts") for st in statements)
        assert rows(s)[0] == before[0]
        assert rows(s)[2] == before[2]
        baseline = json.loads(s.session.scalar(sa.select(CasdoorIdentityExtend.last_applied_json)))
        assert ("name" in baseline) == (kind == "create" and policy != "off")


@pytest.mark.parametrize("field", ["last_applied_json", "profile_sync_json"])
@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"avatar":{}}',
        '{"schema_version":1,"schema_version":1}',
        "[]",
        "null",
        '{"schema_version":true}',
        '{"schema_version":2}',
        '{"nested":[]}',
        " " * 4097,
        '{"schema_version":1,"name":"Name","name_generation":true}',
        "{",
    ],
)
def test_closed_bounded_snapshot_no_overwrite(storage, field, raw):
    s = storage
    with s.session.begin():
        s.session.execute(sa.update(CasdoorIdentityExtend).values(**{field: raw}))
        before = rows(s)
        statements = trace(s)
        with pytest.raises(CasdoorProfileConflict, match="^config_conflict$"):
            persist(s)
        assert not dml(statements)
        assert rows(s) == before


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (CasdoorIntegrationExtend, "enabled", False),
        (CasdoorIntegrationExtend, "active_revision_id", None),
        (CasdoorNamespaceExtend, "lifecycle", "fencing"),
        (CasdoorNamespaceExtend, "fence_epoch", 1),
        (CasdoorNamespaceExtend, "core_fingerprint", "a" * 64),
        (CasdoorConfigRevisionExtend, "policy_json", "{}"),
        (CasdoorConfigRevisionExtend, "mappings_json", "{}"),
        (CasdoorConfigRevisionExtend, "certificates_json", "[{}]"),
        (CasdoorConfigRevisionExtend, "encrypted_secret", "tampered"),
        (CasdoorIdentityExtend, "subject", "other"),
        (CasdoorIdentityExtend, "subject_digest", "b" * 64),
        (CasdoorIdentityExtend, "sync_generation", 2),
        (Account, "status", "banned"),
        (Account, "status", "closed"),
        (Account, "initialized_at", None),
    ],
)
def test_actual_parent_and_original_config_owner_fresh_rejection(storage, model, field, value):
    s = storage
    with s.session.begin():
        s.session.execute(sa.update(model.__table__).values(**{field: value}))
        before = rows(s)
        statements = trace(s)
        with pytest.raises(CasdoorProfileConflict):
            persist(s)
        assert not dml(statements)
        assert rows(s) == before


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted", "nested"])
def test_reject_pending_or_nested_before_sql(storage, pending):
    s = storage
    with s.session.begin():
        nested = None
        if pending == "new":
            s.session.add(Account(name="Pending", email="pending@example.test"))
        elif pending == "dirty":
            s.account.name = "Pending"
        elif pending == "deleted":
            s.session.delete(s.account)
        else:
            nested = s.session.begin_nested()
        statements = trace(s)
        with pytest.raises(RuntimeError if nested else CasdoorProfileConflict):
            persist(s)
        assert not statements
        s.session.rollback()


@pytest.mark.parametrize("failure", ["profile", "audit", "caller"])
def test_real_trigger_and_caller_rollback_restore_prior_setup_quota_identity(storage, failure):
    s = storage
    with s.session.begin():
        before = rows(s)
        if failure != "caller":
            table = "casdoor_identity_extend" if failure == "profile" else "casdoor_audit_extend"
            operation = "UPDATE" if failure == "profile" else "INSERT"
            s.session.execute(
                sa.text(
                    f"CREATE TRIGGER reject_profile BEFORE {operation} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic fail'); END"
                )
            )
    if failure == "caller":
        s.session.begin()
        SQLAlchemyAccountActivationRepository.persist_account_setup(
            s.account, resolve_initial_setup(profile(s), local_email=s.account.email)
        )
        s.session.flush()
        persist(s)
        s.session.rollback()
    else:
        with pytest.raises(IntegrityError, match="synthetic fail"), s.session.begin():  # noqa: PT012
            SQLAlchemyAccountActivationRepository.persist_account_setup(
                s.account,
                resolve_initial_setup(profile(s), local_email=s.account.email),
            )
            s.session.flush()
            s.session.execute(sa.update(AccountMoneyExtend).values(total_quota=999))
            persist(s)
    with s.session.begin():
        assert rows(s) == before
        assert s.session.scalar(sa.select(sa.func.count()).select_from(CasdoorAuditExtend)) == 0


@pytest.mark.parametrize("name", [True, 42, [], "\t", "\n", " " * 256, "\ud800", "汉" * 86])
def test_hostile_profile_name_no_sql(storage, name):
    s = storage
    with s.session.begin():
        statements = trace(s)
        with pytest.raises(CasdoorProfileConflict, match="^config_conflict$"):
            persist(s, profile=profile(s, name=name))
        assert not statements


def test_same_time_foreign_or_changed_content_and_stale_generation(storage):
    s = storage
    with s.session.begin():
        persist(s)
    for change in [
        {"correlation_id": uuid4()},
        {"profile": profile(s, name="Other")},
    ]:
        statements = trace(s)
        with s.session.begin():
            assert persist(s, **change).name_reason is ProfileNameReason.AMBIGUOUS_PROFILE_ATTEMPT
        assert not dml(statements)
    with s.session.begin():
        with pytest.raises(CasdoorProfileConflict):
            persist(s, expected_generation=0)


def test_draft_etag_irrelevant_and_whole_root_success_rollback(storage):
    s = storage
    with s.session.begin():
        before = rows(s)
    s.session.begin()
    s.session.execute(sa.update(CasdoorIntegrationExtend).values(etag=777, draft_revision_id=str(uuid4())))
    persist(s)
    s.session.rollback()
    with s.session.begin():
        assert rows(s) == before


@pytest.mark.parametrize("failure", [None, "profile", "audit"])
def test_actual_b2_creation_quota_binding_setup_generation_and_root_rollback(storage, monkeypatch, failure):
    """Real original B2 effects + profile; only pre-UoW eligibility is synthetic."""
    from decimal import Decimal
    from time import monotonic
    from unittest.mock import Mock

    from core.casdoor.admission import SharedOwnerRequirement
    from core.casdoor.claims import StructuredUserRef
    from core.casdoor.mapping import (
        BuiltinResolution,
        ServerWorkspaceAvailability,
        WorkspaceAvailability,
        WorkspaceState,
        resolve_workspace_plan,
    )
    from core.casdoor.role_graph import EffectiveRoleSnapshot
    from enums import DeploymentEdition
    from repositories.casdoor_account_preflight_repository_extend import CasdoorAccountPreflightRepository
    from repositories.casdoor_generation_repository_extend import CasdoorGenerationRepository
    from repositories.casdoor_identity_repository_extend import VerifiedIdentityKey
    from services import account_service
    from services.account_activation_service import AccountActivationService
    from services.casdoor_login_account_service_extend import CasdoorLoginAccountService

    s = storage
    with s.session.begin():
        s.session.execute(sa.delete(CasdoorIdentityExtend))
        s.session.execute(sa.delete(AccountMoneyExtend))
        s.session.execute(sa.delete(Account))
        config = s.config_owner._configuration(s.revision)
        if failure:
            table, operation = (
                ("casdoor_identity_extend", "UPDATE") if failure == "profile" else ("casdoor_audit_extend", "INSERT")
            )
            condition = (
                "WHEN NEW.remote_profile_version IS NOT OLD.remote_profile_version " if failure == "profile" else ""
            )
            s.session.execute(
                sa.text(
                    f"CREATE TRIGGER fail_b2_profile BEFORE {operation} ON {table} {condition}"
                    "BEGIN SELECT RAISE(ABORT, 'profile uow fail'); END"
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
    key = VerifiedIdentityKey(s.context.namespace_id, s.context.issuer, s.context.organization, s.context.subject)
    remote = profile(s, email="new@example.test", name=" Signed Name ")
    setup = resolve_initial_setup(remote, local_email=remote.email, request_language="en-US", request_timezone="UTC")
    plan = AdmissionPlan(
        s.admission.context,
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
            context = replace(s.context, account_id=result.account_id, identity_id=result.identity_id)
            desired = resolve_workspace_plan(
                configuration=config,
                context=context,
                snapshot=EffectiveRoleSnapshot(context.subject, StructuredUserRef(context.organization, "u"), ()),
                availability=ServerWorkspaceAvailability(
                    (
                        WorkspaceAvailability(
                            config.default_workspace_id,
                            WorkspaceState.NORMAL,
                            (BuiltinResolution("normal", "local-normal"),),
                        ),
                    )
                ),
            )
            # B3 normally owns this one allocation; no allocation inside profile.
            generation = CasdoorGenerationRepository(s.session).allocate(
                desired, expected_fence_epoch=0, expected_generation=0
            )
            before = rows(s)
            statements = trace(s)
            out = s.owner.persist(
                context,
                remote,
                expected_generation=generation.generation,
                expected_fence_epoch=generation.fence_epoch,
                auth_started_at=NOW,
                admission=plan,
                correlation_id=s.correlation,
                now=NOW,
            )
            assert out.name_reason is ProfileNameReason.CREATED_BASELINE
            assert not any(sql.startswith("UPDATE accounts") for sql in statements)
            assert rows(s)[0] == before[0]
            assert s.session.scalar(sa.select(AccountMoneyExtend.total_quota)) == Decimal(15)
            assert s.session.scalar(sa.select(CasdoorIdentityExtend.sync_generation)) == 1
            assert s.session.scalar(sa.select(CasdoorAuditExtend.action)) == "profile_sync"
            s.session.rollback()

    if failure:
        with pytest.raises(IntegrityError, match="profile uow fail"):
            write_uow()
    else:
        write_uow()
    with s.session.begin():
        for model in (Account, AccountMoneyExtend, CasdoorIdentityExtend, CasdoorAuditExtend):
            assert s.session.scalar(sa.select(sa.func.count()).select_from(model)) == 0


def test_real_local_profile_owner_manual_edit_detected_despite_cached_account(storage):
    from sqlalchemy.orm import sessionmaker

    from repositories.account_repository import SQLAlchemyAccountRepository
    from services.entities.account_entities import AccountProfileChanges

    s = storage
    with s.session.begin():
        persist(s)
    owner = SQLAlchemyAccountRepository(sessionmaker(s.session.bind))
    owner.update_profile(str(s.context.account_id), AccountProfileChanges(name="Actual local owner edit"))
    with s.session.begin():
        out = persist(s)
        assert out.name_reason is ProfileNameReason.LOCAL_OVERRIDE
        assert s.session.scalar(sa.select(Account.name)) == "Actual local owner edit"
        assert (
            json.loads(s.session.scalar(sa.select(CasdoorIdentityExtend.last_applied_json)))["name"] == profile(s).name
        )


@pytest.mark.parametrize("target", ["identity", "account"])
def test_actual_unlink_or_deleted_account_no_profile_dml(storage, target):
    s = storage
    with s.session.begin():
        s.session.execute(sa.delete(CasdoorIdentityExtend))
        if target == "account":
            s.session.execute(sa.delete(AccountMoneyExtend))
            s.session.execute(sa.delete(Account))
        statements = trace(s)
        with pytest.raises(CasdoorProfileConflict):
            persist(s)
        assert not dml(statements)


@pytest.mark.parametrize("field", ["name", "profile_sync_json"])
def test_real_compare_and_swap_loser_requires_caller_rollback(storage, monkeypatch, field):
    s = storage
    with s.session.begin():
        before = rows(s)
    original = s.session.execute
    changed = False

    def competing_write(statement, *args, **kwargs):
        nonlocal changed
        if isinstance(statement, sa.sql.dml.Update) and not changed:
            if field == "name" and statement.table.name == Account.__tablename__:
                changed = True
                original(sa.update(Account.__table__).values(name="Competing local change"))
            elif field == "profile_sync_json" and statement.table.name == CasdoorIdentityExtend.__tablename__:
                changed = True
                original(sa.update(CasdoorIdentityExtend.__table__).values(profile_sync_json='{"schema_version":2}'))
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(s.session, "execute", competing_write)
    with pytest.raises(CasdoorProfileConflict), s.session.begin():
        persist(s)
    assert changed
    with s.session.begin():
        assert rows(s) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_generation": True},
        {"expected_fence_epoch": True},
        {"expected_generation": -1},
        {"expected_generation": 2**63},
        {"now": datetime(2026, 1, 1)},
        {"auth_started_at": NOW + timedelta(seconds=1)},
        {"correlation_id": "bad"},
    ],
)
def test_invalid_shape_no_sql(storage, changes):
    s = storage
    with s.session.begin():
        statements = trace(s)
        with pytest.raises(CasdoorProfileConflict):
            persist(s, **changes)
        assert statements == []


def test_off_after_applied_reentry_retains_baseline_and_reports_disabled(storage):
    s = storage
    with s.session.begin():
        persist(s)
        baseline = s.session.scalar(sa.select(CasdoorIdentityExtend.last_applied_json))
    with s.session.begin():
        mode(s, "off")
        out = persist(s)
        assert out.name_status is ProfileAuditResult.DISABLED
        assert s.session.scalar(sa.select(CasdoorIdentityExtend.last_applied_json)) == baseline
        assert s.session.scalar(sa.select(Account.name)) == profile(s).name


def test_profile_audit_failure_occurs_after_account_and_identity_update(storage):
    s = storage
    with s.session.begin():
        s.session.execute(
            sa.text(
                "CREATE TRIGGER fail_profile_audit BEFORE INSERT ON casdoor_audit_extend "
                "BEGIN SELECT RAISE(ABORT, 'audit after business'); END"
            )
        )
        before = rows(s)
    statements = trace(s)
    with pytest.raises(IntegrityError, match="audit after business"), s.session.begin():
        persist(s)
    writes = dml(statements)
    assert writes[0].startswith("UPDATE accounts")
    assert writes[1].startswith("UPDATE casdoor_identity_extend")
    assert writes[2].startswith("INSERT INTO casdoor_audit_extend")
    with s.session.begin():
        assert rows(s) == before
