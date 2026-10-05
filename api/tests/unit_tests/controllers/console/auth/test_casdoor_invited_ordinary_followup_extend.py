"""Independent D33 producer check: complete controlled invitation then ordinary login."""
import traceback
import json, os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from core.casdoor.configuration import RoleRef, WorkspaceRoleMapping
from sqlalchemy.orm import Session
from test_casdoor_invited_history_http_extend import ordinary_then_invited as original_case
from test_casdoor_invited_local_recovery_http_extend import finish, new_attempt
from test_casdoor_local_http_extend import begin, callback, mounted as original_mounted

pytest_plugins = ("test_casdoor_local_http_service_extend",)
ordinary_then_invited = original_case
mounted = original_mounted


@pytest.mark.parametrize("ordinary_then_invited", ["public-new-workspace"], indirect=True)
def test_real_rolechange_invitation_then_next_registered_ordinary_login(ordinary_then_invited, monkeypatch, tmp_path):
    case = ordinary_then_invited
    f = case.m.f
    ref = RoleRef(organization="Org", name="operators")
    config = f.local.config.model_copy(
        update={"workspace_mappings": (WorkspaceRoleMapping(workspace_id=UUID(int=200), editor=ref),)}
    )
    with Session(f.local.engine) as session, session.begin():
        owner = f.coordinator._configuration_service._repository(session)
        integration = owner._integration()
        draft = owner.save_draft(config, etag=integration.etag, actor_account_id=UUID(int=700))
        integration.active_revision_id = str(draft.draft_revision_id)
        session.flush()

    invited = finish(case, new_attempt(case, token=case.token))
    assert invited.status_code == 302 and invited.location.endswith("/apps/invited"), case.errors

    from repositories.casdoor_terminal_local_invitation_repository_extend import (
        CasdoorTerminalLocalInvitationRepository,
    )

    verify_failures = []
    original_verify = CasdoorTerminalLocalInvitationRepository._verify

    def capture_verify_failure(repository, *args, **kwargs):
        try:
            return original_verify(repository, *args, **kwargs)
        except Exception as error:
            extracted = traceback.extract_tb(error.__traceback__)
            local_facts = {}
            cursor = error.__traceback__
            while cursor is not None:
                if cursor.tb_frame.f_code.co_name == "_verify":
                    local = cursor.tb_frame.f_locals
                    local_facts = {
                        "line": cursor.tb_lineno,
                        "results": [
                            {key: row.get(key) for key in (
                                "workspace_id", "membership_id", "membership_created", "outcome",
                                "role_changed", "metadata_changed", "ownership_decision", "current_role",
                            )}
                            for row in local.get("d", {}).get("results", [])
                        ],
                        "finalized_ids": local.get("f", {}).get("finalized_ids"),
                    }
                    break
                cursor = cursor.tb_next
            verify_failures.append(
                {
                    "type": type(error).__name__,
                    "frames": [(frame.name, frame.lineno) for frame in extracted],
                    "facts": local_facts,
                }
            )
            raise

    monkeypatch.setattr(CasdoorTerminalLocalInvitationRepository, "_verify", capture_verify_failure)
    ordinary = callback(case.m, begin(case.m, return_path="/apps/ordinary-followup"))
    location = urlsplit(ordinary.location or "")
    evidence = {
        "ordinary_response": {"status": ordinary.status_code, "path": location.path},
        "terminal_verify_failures": verify_failures,
    }
    Path(os.environ.get("D33_RUN_OUTPUT", str(tmp_path)), "ordinary-facts.json").write_text(json.dumps(evidence, indent=2) + "\n")
    assert ordinary.status_code == 302 and location.path == "/apps/ordinary-followup", evidence
