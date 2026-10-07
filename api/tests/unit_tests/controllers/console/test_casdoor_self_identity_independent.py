"""Independent adversarial cases using the original synthetic reader fixtures."""

import sqlalchemy as sa
import test_casdoor_self_identity_extend as reader_fixtures

from models.casdoor_extend import (
    CasdoorConfigRevisionExtend as Revision,
)
from models.casdoor_extend import (
    CasdoorIdentityExtend as Identity,
)
from models.casdoor_extend import (
    CasdoorNamespaceExtend as Namespace,
)

pytest_plugins = ["test_casdoor_self_identity_extend"]


def test_historical_namespace_with_its_own_core_stays_inactive(storage):
    """A different historical namespace core must not invalidate the active binding."""
    reader_fixtures.add_binding(storage, 100)
    historical_core = {
        "expected_issuer": "https://historical.synthetic.invalid",
        "organization": "historical-organization",
        "application": "historical-application",
        "client_id": "historical-client",
    }
    with storage.factory.begin() as session:
        session.execute(sa.update(Namespace).where(Namespace.id == reader_fixtures.uid(104)).values(**historical_core))
        session.execute(sa.update(Revision).where(Revision.id == reader_fixtures.uid(105)).values(**historical_core))
        session.execute(
            sa.update(Identity)
            .where(Identity.id == reader_fixtures.uid(106))
            .values(issuer=historical_core["expected_issuer"], organization=historical_core["organization"])
        )

    result = reader_fixtures.read(storage, limit=10)
    identities = {row["id"]: row for row in result["identities"]}
    memberships = {row["id"]: row for row in result["memberships"]}

    assert result["binding"] == "linked"
    assert identities[reader_fixtures.uid(6)]["activity"] == "active"
    assert identities[reader_fixtures.uid(106)]["activity"] == "inactive"
    assert identities[reader_fixtures.uid(106)]["lifecycle"] == "active"
    assert memberships[reader_fixtures.uid(8)]["state"] == "recorded_managed"
    assert memberships[reader_fixtures.uid(108)]["state"] == "historical"
    assert all(value is False for value in result["actions"].values())
    assert all(row["remote_actual_state"] == "unknown" for row in memberships.values())
