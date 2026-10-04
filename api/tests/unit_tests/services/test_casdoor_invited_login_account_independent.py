"""Independent counterexamples for invited LOGIN account composition."""

from dataclasses import fields
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.account import Account, Tenant, TenantAccountJoin, TenantStatus
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from repositories.casdoor_account_preflight_repository_extend import AccountPreflightConflict
from services.account_activation_service import InvalidAccountInitializationError
from tests.unit_tests.services.test_casdoor_invited_login_account_service_extend import (
    prepare,
    setup_case,
)
from tests.unit_tests.services.test_casdoor_login_account_service_extend import env as base_env  # noqa: F401


@pytest.fixture
def env(base_env):  # noqa: F811
    session, context, key, *_ = base_env
    for model in (Tenant, TenantAccountJoin):
        metadata = sa.MetaData()
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if column.server_default is not None and str(column.server_default.arg) == "CURRENT_TIMESTAMP(0)":
                column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
        metadata.create_all(session.bind)
    return session, context, key


def persist(case, prepared):
    session, context, key = case["env"]
    return case["service"].persist_invited_login_account(prepared, session=session, context=context, key=key)


def test_equal_field_forged_shared_receipt_cannot_consume_authentic_receipt(env):
    case = setup_case(env)
    authentic_wrapper = prepare(case)
    shared = authentic_wrapper._shared
    forged_shared = type(shared)(**{item.name: getattr(shared, item.name) for item in fields(shared) if item.init})
    forged_wrapper = case["service"]._prepare_invited_login(
        plan=authentic_wrapper.plan,
        preflight=authentic_wrapper.preflight,
        invitation=authentic_wrapper.invitation,
        shared=forged_shared,
        deadline=authentic_wrapper.deadline,
    )

    with env[0].begin(), pytest.raises(InvalidAccountInitializationError):
        persist(case, forged_wrapper)

    with env[0].begin():
        assert env[0].scalar(sa.select(sa.func.count()).select_from(Identity)) == 0
        assert env[0].get(Account, case["aid"]).initialized_at is None
        persist(case, authentic_wrapper)


def test_identity_integrity_failure_rolls_back_setup_and_earlier_caller_write(env):
    case = setup_case(env)
    prepared = prepare(case)
    session = env[0]
    with session.bind.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TRIGGER reject_independent_identity BEFORE INSERT ON casdoor_identity_extend "
                "BEGIN SELECT RAISE(ABORT, 'independent identity conflict'); END"
            )
        )

    try:

        def fail_transaction():
            with session.begin():
                session.connection().exec_driver_sql("BEGIN")
                session.execute(sa.update(Tenant).where(Tenant.id == case["tid"]).values(name="caller change"))
                persist(case, prepared)
                session.flush()

        with pytest.raises(IntegrityError):
            fail_transaction()
    finally:
        with session.bind.begin() as connection:
            connection.execute(sa.text("DROP TRIGGER reject_independent_identity"))

    with Session(session.bind) as reader:
        assert reader.get(Account, case["aid"]).initialized_at is None
        assert reader.get(Tenant, case["tid"]).name == "Invitation workspace"
        assert reader.scalar(sa.select(sa.func.count()).select_from(Identity)) == 0
    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(case, prepared)


def test_workspace_revoked_after_shared_preparation_rejects_without_rebinding(env):
    case = setup_case(env)
    prepared = prepare(case)
    session = env[0]
    original_identity_count = 0
    with Session(session.bind) as reader:
        original_identity_count = reader.scalar(sa.select(sa.func.count()).select_from(Identity))
    with Session(session.bind) as writer, writer.begin():
        writer.execute(sa.update(Tenant).where(Tenant.id == case["tid"]).values(status=TenantStatus.ARCHIVE))

    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(case, prepared)

    with Session(session.bind) as reader:
        assert reader.get(Account, case["aid"]).initialized_at is None
        assert reader.scalar(sa.select(sa.func.count()).select_from(Identity)) == original_identity_count
        assert reader.scalar(sa.select(Tenant.status).where(Tenant.id == case["tid"])) is TenantStatus.ARCHIVE


@pytest.mark.parametrize("drift", ["marker", "reverse_binding"])
def test_invitation_marker_or_reverse_binding_drift_rejects_after_preparation(env, drift):
    case = setup_case(env)
    prepared = prepare(case)
    session, context, _ = env
    with Session(session.bind) as writer, writer.begin():
        if drift == "marker":
            writer.execute(
                sa.update(Account).where(Account.id == case["aid"]).values(initialized_at=datetime(2021, 2, 3))
            )
        else:
            writer.add(
                Identity(
                    namespace_id=str(context.namespace_id),
                    account_id=case["aid"],
                    issuer=context.issuer,
                    organization=context.organization,
                    subject="different-subject",
                    last_applied_json="{}",
                    profile_sync_json="{}",
                )
            )

    with session.begin(), pytest.raises(AccountPreflightConflict):
        persist(case, prepared)

    with Session(session.bind) as reader:
        account = reader.get(Account, case["aid"])
        assert account.initialized_at == (datetime(2021, 2, 3) if drift == "marker" else None)
        identity_count = reader.scalar(sa.select(sa.func.count()).select_from(Identity))
        assert identity_count == (1 if drift == "reverse_binding" else 0)
