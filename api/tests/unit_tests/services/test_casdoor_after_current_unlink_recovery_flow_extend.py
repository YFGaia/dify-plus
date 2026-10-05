"""Read-only next-package RED after actual current unlink; no successful ledger seed."""

from test_casdoor_identity_action_flow_extend import begin, complete
from test_casdoor_local_lifecycle_flow_extend import review, send
from test_casdoor_namespace_reset_flow_extend import RESET, ROOT, reset_review
from test_casdoor_repeated_namespace_recovery_flow_extend import (
    observed_failure as observed_failure,
    test_current_recent_auth_unlink_after_actual_archive_release as produce_current_unlink,
)
from test_casdoor_cross_namespace_adoption_flow_extend import ordinary
from models.casdoor_extend import CasdoorIdentityExtend as Identity
import sqlalchemy as sa

pytest_plugins = ("test_casdoor_local_lifecycle_flow_extend",)


def test_same_namespace_fresh_link_adopt_ordinary_after_current_unlink(lifecycle, monkeypatch):
    d = lifecycle
    produce_current_unlink(d, monkeypatch)
    state, _ = begin(d)
    response = complete(d, state)
    assert response.location.endswith("=linked"), (response.location, response.json)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    with d.f.service._session_factory() as session:
        identity = session.scalar(sa.select(Identity).where(
            Identity.account_id == d.actor.id, Identity.namespace_id == str(snapshot.active.namespace_id),
        ))
        assert identity is not None
        from uuid import UUID

        target = {"identity_id": identity.id, "workspace_id": str(UUID(int=200))}
    proof = review(d, target, "adopt")
    response = send(d, ROOT + "/local-membership/adopt", method="POST", json=proof)
    assert response.status_code == 200, response.json
    ordinary(d, 1)


def test_disable_reset_without_relink_after_current_unlink(lifecycle, monkeypatch):
    d = lifecycle
    produce_current_unlink(d, monkeypatch)
    snapshot = d.services.casdoor_configuration.get(d.actor)
    disabled = send(d, ROOT + "/disable", method="POST", json={"etag": snapshot.etag})
    assert disabled.status_code == 200, disabled.json
    d.reset_input = {
        "namespace_id": str(snapshot.active.namespace_id),
        "etag": disabled.json["configuration"]["etag"],
        "confirm_management_review": True,
    }
    response = send(d, RESET, method="POST", json=reset_review(d))
    assert response.status_code == 200, response.json
