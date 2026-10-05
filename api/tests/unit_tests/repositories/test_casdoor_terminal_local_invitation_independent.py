"""Independent D20-A historical terminal observation boundary check."""

import sqlalchemy as sa

from models.account import TenantAccountJoin as Join
from models.account import TenantAccountRole
from models.casdoor_extend import CasdoorIdentityExtend as Identity
from models.casdoor_extend import CasdoorNamespaceExtend as Namespace
from models.invitation_authority_extend import InvitationAuthorityLifecycleExtend as Lifecycle
from repositories.casdoor_terminal_local_invitation_repository_extend import (
    CasdoorTerminalLocalInvitationRepository,
)

from tests.unit_tests.repositories.test_casdoor_terminal_local_invitation_extend import (
    advance_current_revision,
    denied,
    observe,
    prepare,
    invited_scope_case,
    writer_case,
)

__all__ = ["invited_scope_case"]


def test_historical_closure_survives_combined_current_generation_role_revision_and_lifecycle_changes(writer_case):
    case = writer_case(absent=True)
    prepare(case)
    original = observe(case)

    # Advance current configuration/fence and then apply legal ordinary-owner
    # changes. The D/F closure remains tied to its original historical facts.
    advance_current_revision(case)
    with case.db() as session, session.begin():
        session.execute(sa.update(Identity).values(sync_generation=original.current_generation + 1))
        session.execute(
            sa.update(Join)
            .where(Join.id == case.completed.facts.join_id)
            .values(role=TenantAccountRole.EDITOR.value)
        )
        session.execute(
            sa.update(Lifecycle)
            .where(Lifecycle.lifecycle_id == case.ids["lifecycle"])
            .values(epoch=original.result_epoch + 2)
        )

    result = observe(case)
    assert result.current_generation == original.current_generation + 1
    assert result.result_epoch == original.result_epoch
    assert result.write_summary_sha256 == original.write_summary_sha256
    assert result.finalization_summary_sha256 == original.finalization_summary_sha256


def test_namespace_growth_after_header_read_is_rejected_before_orm_materialization(writer_case, monkeypatch):
    case = writer_case()
    prepare(case)
    advance_current_revision(case)
    calls = []
    original = CasdoorTerminalLocalInvitationRepository._bounded_orm

    def grow(owner, model, row):
        if model is Namespace:
            owner.session.execute(
                sa.update(Namespace).where(Namespace.id == row.id).values(expected_issuer="x" * 16385)
            )
            calls.append(True)
        return original(owner, model, row)

    monkeypatch.setattr(CasdoorTerminalLocalInvitationRepository, "_bounded_orm", grow)
    denied(case)
    assert calls == [True]
