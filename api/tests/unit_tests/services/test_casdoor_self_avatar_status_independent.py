"""Independent SQL projection negatives for private avatar observations."""

import sqlalchemy as sa
from machinery.context import RequestContext
from models.account import Account
from models.model import UploadFile
from repositories.casdoor_self_identity_repository_extend import CasdoorSelfReadConflict
from services.casdoor_self_identity_service_extend import CasdoorSelfIdentityService
from sqlalchemy.orm import sessionmaker
from test_casdoor_avatar_attachment_extend import attach
from test_casdoor_profile_repository_extend import NOW

pytest_plugins = ("test_casdoor_avatar_attachment_extend",)


def project(s):
    service = CasdoorSelfIdentityService(session_factory=sessionmaker(s.session.get_bind()), rbac_enabled=False)
    context = RequestContext("independent-avatar-observer", None, s.account.id, s.revision.default_workspace_id)
    return service.get_avatar_observations(context, now=NOW)


def test_attachment_storage_key_drift_is_unknown_after_actual_sql_readback(attachment):
    s = attachment
    attach(s)
    with s.session.begin():
        file_id = s.session.scalar(sa.select(Account.avatar).where(Account.id == s.account.id))
    with s.session.begin():
        s.session.execute(sa.update(UploadFile).where(UploadFile.id == file_id).values(key="drifted-key"))

    value = project(s)["identities"][0]
    assert value["avatar_status"] == "unknown"
    assert value["avatar_recorded_at"] is None
    assert value["avatar_last_reason"] is None


def test_oversized_local_avatar_reference_fails_closed_before_projection(attachment):
    s = attachment
    attach(s)
    with s.session.begin():
        s.session.execute(sa.update(Account).where(Account.id == s.account.id).values(avatar="x" * 256))

    try:
        project(s)
    except CasdoorSelfReadConflict:
        pass
    else:
        raise AssertionError("oversized authenticated-account avatar value was projected")
