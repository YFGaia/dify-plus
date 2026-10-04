"""Independent SQLite checks for database-enforced local-role CAS failures."""

import hashlib
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa
from configs import dify_config
from core.casdoor.errors import CasdoorDecisionReason
from core.casdoor.mapping import (
    DesiredWorkspacePlan,
    DesiredWorkspaceTarget,
    MappingIdentityContext,
)
from core.casdoor.ownership import (
    MembershipBackend,
    MembershipObservation,
    role_baseline_json,
    roles_fingerprint,
)
from models.account import (
    Account,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
)
from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorFinalizationState,
    CasdoorMembershipOwnership,
    CasdoorMembershipSource,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorIntegrationExtend as Integration,
)
from models.casdoor_extend import (
    CasdoorManagedMembershipExtend as History,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)
from models.casdoor_extend import (
    CasdoorSyncIntentExtend as Intent,
)
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend
from repositories.casdoor_generation_repository_extend import GenerationPlanVersion
from repositories.casdoor_local_role_repository_extend import (
    CasdoorLocalRoleConflict,
    CasdoorLocalRoleRepository,
)
from sqlalchemy.orm import Session


@pytest.fixture
def sqlite_owner(monkeypatch):
    monkeypatch.setattr(dify_config, "RBAC_ENABLED", False)
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def sqlite_constraints(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @sa.event.listens_for(engine, "begin")
    def explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    for model in (
        Account,
        Tenant,
        TenantAccountJoin,
        Integration,
        Namespace,
        Revision,
        Identity,
        History,
        Intent,
        InvitationAuthorityLifecycleExtend,
    ):
        model.__table__.create(engine)

    with Session(engine, expire_on_commit=False) as session:
        integration = Integration(enabled=True)
        account = Account(
            name="Independent",
            email="i13a-independent@example.test",
            status=AccountStatus.ACTIVE,
        )
        workspace = Tenant(name="Independent existing workspace")
        session.add_all((integration, account, workspace))
        session.flush()
        namespace = Namespace(
            integration_id=integration.id,
            expected_issuer="https://independent.example.test",
            organization="IndependentOrg",
            application="IndependentApp",
            client_id="IndependentClient",
            core_fingerprint="c" * 64,
        )
        session.add(namespace)
        session.flush()
        revision = Revision(
            integration_id=integration.id,
            namespace_id=namespace.id,
            revision_number=1,
            config_digest="d" * 64,
            browser_frontend_url=namespace.expected_issuer,
            backend_api_url=namespace.expected_issuer,
            expected_issuer=namespace.expected_issuer,
            organization=namespace.organization,
            application=namespace.application,
            client_id=namespace.client_id,
            button_text="Casdoor",
            default_workspace_id=workspace.id,
            certificates_json="[]",
            policy_json="{}",
            mappings_json="[]",
        )
        identity = Identity(
            namespace_id=namespace.id,
            account_id=account.id,
            issuer=namespace.expected_issuer,
            organization=namespace.organization,
            subject="independent-subject",
            sync_generation=1,
            last_applied_json="{}",
            profile_sync_json="{}",
        )
        join = TenantAccountJoin(
            account_id=account.id, tenant_id=workspace.id, role=TenantAccountRole.NORMAL
        )
        session.add_all((revision, identity, join))
        session.flush()
        integration.active_revision_id = revision.id
        context = MappingIdentityContext(
            UUID(integration.id),
            UUID(revision.id),
            UUID(namespace.id),
            UUID(identity.id),
            UUID(account.id),
            revision.config_digest,
            namespace.expected_issuer,
            namespace.organization,
            namespace.application,
            namespace.client_id,
            identity.subject,
        )
        target = DesiredWorkspaceTarget(
            UUID(workspace.id), "admin", "admin", CasdoorDecisionReason.ROLE_MAPPING, ()
        )
        version = GenerationPlanVersion(DesiredWorkspacePlan(context, (target,)), 0, 1)
        observation = MembershipObservation(
            UUID(workspace.id),
            UUID(account.id),
            UUID(join.id),
            join.role,
            MembershipBackend.LOCAL,
        )
        baseline = role_baseline_json(observation)
        history = History(
            namespace_id=namespace.id,
            identity_id=identity.id,
            account_id=account.id,
            workspace_id=workspace.id,
            join_id=join.id,
            ownership=CasdoorMembershipOwnership.MANAGED,
            ownership_epoch=11,
            source=CasdoorMembershipSource.ADOPT,
            desired_generation=1,
            revision_id=revision.id,
            last_applied_roles_json=baseline,
            last_applied_fingerprint=roles_fingerprint(observation),
            desired_roles_json='{"original":true}',
            baseline_json=baseline,
            finalization=CasdoorFinalizationState.PENDING,
            tombstone=False,
        )
        session.add(history)
        session.commit()
        yield SimpleNamespace(
            engine=engine,
            session=session,
            join_id=join.id,
            history_id=history.id,
            target=target,
            version=version,
            repository=CasdoorLocalRoleRepository(session),
        )
    engine.dispose()


def _persisted_state(owner):
    return owner.session.execute(
        sa.select(
            TenantAccountJoin.role,
            History.last_applied_roles_json,
            History.last_applied_fingerprint,
            History.desired_roles_json,
            History.baseline_json,
            History.source,
            History.ownership_epoch,
            History.finalization,
            History.desired_generation,
            History.revision_id,
        )
        .join(History, History.join_id == TenantAccountJoin.id)
        .where(TenantAccountJoin.id == owner.join_id)
    ).one()


def test_real_sqlite_metadata_cas_zero_row_restores_prior_role_and_full_history(
    sqlite_owner,
):
    owner = sqlite_owner
    with owner.session.begin():
        before = _persisted_state(owner)
        owner.session.execute(
            sa.text(
                """CREATE TRIGGER ignore_managed_role_metadata BEFORE UPDATE ON casdoor_managed_membership_extend
                BEGIN SELECT RAISE(IGNORE); END"""
            )
        )

    with pytest.raises(CasdoorLocalRoleConflict), owner.session.begin():
        token = owner.repository.prepare(owner.version, owner.target)
        owner.repository.apply(token)

    with owner.session.begin():
        after = _persisted_state(owner)
        assert after == before
        assert after.role is TenantAccountRole.NORMAL
        assert after.source is CasdoorMembershipSource.ADOPT
        assert after.ownership_epoch == 11
        assert after.finalization is CasdoorFinalizationState.PENDING


def test_real_sqlite_join_cas_zero_row_never_advances_managed_baseline(sqlite_owner):
    owner = sqlite_owner
    with owner.session.begin():
        before = _persisted_state(owner)
        owner.session.execute(
            sa.text(
                """CREATE TRIGGER ignore_local_role_change BEFORE UPDATE OF role ON tenant_account_joins
                BEGIN SELECT RAISE(IGNORE); END"""
            )
        )

    with pytest.raises(CasdoorLocalRoleConflict), owner.session.begin():
        token = owner.repository.prepare(owner.version, owner.target)
        owner.repository.apply(token)

    with owner.session.begin():
        after = _persisted_state(owner)
        assert after == before
        assert after.role is TenantAccountRole.NORMAL
        assert (
            after.last_applied_fingerprint
            == hashlib.sha256(after.last_applied_roles_json.encode()).hexdigest()
        )
        assert after.desired_roles_json == '{"original":true}'
