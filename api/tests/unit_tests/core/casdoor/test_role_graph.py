"""Offline graph contracts; synthetic proofs are NOT real Casdoor/G0-B evidence."""

import json
import time
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import pytest
from core.casdoor.claims import ClaimsValidator, StructuredUserRef, VerifiedIDToken, VerifiedOnlineUser
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CertificateTrustStore, TrustedCertificate
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import (
    JSON_LIMIT,
    ROLES_LIMIT,
    CasdoorBasicDirectoryCredentialStrategy,
    DirectoryDeploymentProof,
    GatewayError,
    GatewayOperation,
)
from core.casdoor.role_graph import (
    DirectorySnapshotContract,
    OnlineRoleSnapshotLoader,
    RoleSnapshotError,
    compute_effective_roles,
)
from core.helper import ssrf_proxy
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

ORG = "business-org"
SUBJECT = "stable-subject"


def deployment() -> DirectoryDeploymentProof:
    return DirectoryDeploymentProof(
        "https://issuer.example.test", ORG, "dedicated-app", "test-client", "1" * 64, "2" * 64, "3" * 64
    )


def proof() -> DirectorySnapshotContract:
    return DirectorySnapshotContract(deployment(), "4" * 64, "5" * 64)


def user(**changes: Any) -> dict[str, Any]:
    return dict(owner=ORG, id=SUBJECT, name="directory-name", isForbidden=False, isDeleted=False, groups=[], **changes)


def role(name: str, *, enabled: bool = True, users=None, groups=None, children=None) -> dict[str, Any]:
    return {
        "owner": ORG,
        "name": name,
        "isEnabled": enabled,
        "users": users or [],
        "groups": groups or [],
        "roles": children or [],
    }


def organization(items=None) -> dict[str, Any]:
    return {"owner": "admin", "name": ORG, "accountItems": items}


def item(name: str, view_rule: str = "Admin", visible: bool = True) -> dict[str, Any]:
    return {"name": name, "visible": visible, "viewRule": view_rule}


def compute(roles=None, *, raw_user=None, raw_org=None, contract=None):
    raw_user = user() if raw_user is None else raw_user
    return compute_effective_roles(
        organization=ORG,
        verified_subject=SUBJECT,
        online_user=VerifiedOnlineUser(SUBJECT, StructuredUserRef(ORG, raw_user.get("name", "directory-name"))),
        raw_user=raw_user,
        raw_roles=[] if roles is None else roles,
        raw_organization=organization() if raw_org is None else raw_org,
        contract=proof() if contract is None else contract,
    )


def names(snapshot) -> set[tuple[str, str]]:
    return {(ref.organization, ref.name) for ref in snapshot.effective_roles}


def test_direct_group_parent_direction_and_disabled_cutoff():
    raw_user = user()
    raw_user.update(groups=[ORG + "/staff"], roles=[ORG + "/native-admin"], isAdmin=True)
    roles = [
        role("direct", users=[ORG + "/directory-name"]),
        role("group", groups=[ORG + "/staff"]),
        role("group-admin", children=[ORG + "/group"]),
        role("direct-child"),
        role("parent", children=[ORG + "/direct", ORG + "/direct-child"]),
        role("disabled", enabled=False, children=[ORG + "/parent"]),
        role("above-disabled", children=[ORG + "/disabled"]),
        role("disabled-seed", enabled=False, users=[ORG + "/directory-name"]),
        role("native-admin"),
    ]
    assert names(compute(roles, raw_user=raw_user)) == {
        (ORG, "direct"),
        (ORG, "group"),
        (ORG, "group-admin"),
        (ORG, "parent"),
    }
    # Stable sub is not a directory relationship name, and JWT/native roles have
    # no authority. The child of an effective parent receives no grant.
    assert names(compute([role("r", users=[ORG + "/" + SUBJECT])], raw_user=raw_user)) == set()


def test_same_basename_cross_organization_and_bare_user_ref_rejected():
    for relation in ("elsewhere/directory-name", "directory-name"):
        with pytest.raises(RoleSnapshotError, match="relation_boundary"):
            compute([role("admin", users=[relation])])


@pytest.mark.parametrize("key", ["users", "groups", "roles"])
def test_cross_organization_relation_is_unknown_even_when_disabled(key):
    raw = role("r", enabled=False)
    raw[key] = ["elsewhere/same-basename"]
    with pytest.raises(RoleSnapshotError, match="relation_boundary"):
        compute([raw])


def test_user_groups_exact_no_aliases_or_group_inheritance():
    raw_user = user()
    raw_user["groups"] = [ORG + "/staff"]
    assert names(compute([role("r", groups=[ORG + "/STAFF"])], raw_user=raw_user)) == set()
    raw_user["groups"] = ["staff"]
    with pytest.raises(RoleSnapshotError, match="relation_boundary"):
        compute([role("r", groups=[ORG + "/staff"])], raw_user=raw_user)


def test_unknown_contract_does_not_convert_known_empty_to_complete():
    with pytest.raises(RoleSnapshotError, match="contract_unknown"):
        compute_effective_roles(
            organization=ORG,
            verified_subject=SUBJECT,
            raw_user=user(),
            raw_roles=[],
            online_user=VerifiedOnlineUser(SUBJECT, StructuredUserRef(ORG, "directory-name")),
            raw_organization=organization(),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "new_schema"},
        {"schema": "flat_directory_v1"},
        {"schema_proof_fingerprint": ""},
        {"visibility_proof_fingerprint": "X" * 64},
        {"schema_proof_fingerprint": True},
        {"deployment_proof": None},
    ],
)
def test_unknown_or_malformed_contract_rejected(change):
    with pytest.raises(RoleSnapshotError, match="contract_unknown"):
        compute(contract=replace(proof(), **change))


@pytest.mark.parametrize("field", ["release_fingerprint", "creator_proof_fingerprint", "credential_proof_fingerprint"])
def test_deployment_evidence_required(field):
    contract = replace(proof(), deployment_proof=replace(deployment(), **{field: ""}))
    with pytest.raises(RoleSnapshotError, match="contract_unknown"):
        compute(contract=contract)


@pytest.mark.parametrize("items", [None, [], [item("Email", "UnknownFutureRule")]])
def test_account_items_null_empty_unmatched_are_no_filter_only_with_trusted_proof(items):
    assert names(compute(raw_org=organization(items))) == set()


@pytest.mark.parametrize("name", ["Owner", "Id", "Name", "IsForbidden", "IsDeleted", "Groups", "Roles"])
@pytest.mark.parametrize("view_rule", ["Public", "Self", "Admin"])
def test_required_fields_visible_to_proven_org_admin(name, view_rule):
    # visible is presentation-only in this attested candidate filter algorithm.
    assert names(compute(raw_org=organization([item(name, view_rule, visible=False)]))) == set()


@pytest.mark.parametrize(
    "name", ["Groups", "groups", "GROUPS", "G r o u p s", " IsForbidden ", "isdeleted", "R o l e s"]
)
@pytest.mark.parametrize("view_rule", ["Unknown", "Hidden", "", "admin", None, False])
def test_matching_unknown_or_hidden_rule_never_passes_via_case_or_space(name, view_rule):
    with pytest.raises(RoleSnapshotError):
        compute(raw_org=organization([item(name, view_rule)]))


@pytest.mark.parametrize("key", ["name", "visible", "viewRule"])
def test_account_item_required_keys(key):
    value = item("Groups")
    del value[key]
    with pytest.raises(RoleSnapshotError, match="visibility_schema"):
        compute(raw_org=organization([value]))


@pytest.mark.parametrize("value", [False, {}, "", [None], [item("Gröups")], [item("Groups", visible=1)]])
def test_account_items_malformed_or_unsupported_unicode_unknown(value):
    with pytest.raises(RoleSnapshotError):
        compute(raw_org=organization(value))


def test_missing_account_items_or_wrong_organization_owner_unknown():
    for value in (
        {"owner": "admin", "name": ORG},
        {"owner": ORG, "name": ORG, "accountItems": []},
        {"owner": "admin", "name": "elsewhere", "accountItems": []},
    ):
        with pytest.raises(RoleSnapshotError):
            compute(raw_org=value)


@pytest.mark.parametrize("key", ["owner", "id", "name", "isForbidden", "isDeleted", "groups"])
def test_user_required_raw_keys_no_sdk_defaults(key):
    value = user()
    del value[key]
    with pytest.raises(RoleSnapshotError):
        compute(raw_user=value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("owner", "elsewhere"),
        ("id", "another-sub"),
        ("name", ""),
        ("name", "x" * 256),
        ("isForbidden", None),
        ("isDeleted", 0),
        ("groups", {}),
        ("groups", "staff"),
    ],
)
def test_user_malformed_unknown(key, value):
    raw_user = user()
    raw_user[key] = value
    with pytest.raises(RoleSnapshotError):
        compute(raw_user=raw_user)


@pytest.mark.parametrize("key", ["isForbidden", "isDeleted"])
def test_online_disabled_user_rejected(key):
    raw_user = user()
    raw_user[key] = True
    with pytest.raises(RoleSnapshotError, match="user_schema"):
        compute(raw_user=raw_user)


@pytest.mark.parametrize("key", ["owner", "name", "isEnabled", "users", "groups", "roles"])
def test_role_all_required_keys_present(key):
    raw_role = role("r")
    del raw_role[key]
    with pytest.raises(RoleSnapshotError):
        compute([raw_role])


@pytest.mark.parametrize(
    "key,value",
    [
        ("owner", "other-org"),
        ("name", ""),
        ("name", "x" * 256),
        ("isEnabled", None),
        ("isEnabled", 1),
        ("users", {}),
        ("groups", ""),
        ("roles", [None]),
    ],
)
def test_role_unknown_schema_or_wrong_type(key, value):
    raw_role = role("r")
    raw_role[key] = value
    with pytest.raises(RoleSnapshotError):
        compute([raw_role])


def test_present_null_relations_known_empty_with_contract():
    raw_role = role("r")
    raw_role.update(users=None, groups=None, roles=None)
    raw_user = user()
    raw_user["groups"] = None
    assert names(compute([raw_role], raw_user=raw_user)) == set()


def test_duplicate_roles_and_relations_rejected():
    with pytest.raises(RoleSnapshotError, match="role_duplicate"):
        compute([role("r"), role("r")])
    with pytest.raises(RoleSnapshotError, match="relations_duplicate"):
        compute([role("r", users=[ORG + "/u", ORG + "/u"])])


@pytest.mark.parametrize("enabled", [True, False])
def test_dangling_and_cycle_rejected_even_outside_user_closure(enabled):
    with pytest.raises(RoleSnapshotError, match="role_dangling"):
        compute([role("unused", enabled=enabled, children=[ORG + "/absent"])])
    with pytest.raises(RoleSnapshotError, match="role_cycle"):
        compute([role("a", enabled=enabled, children=[ORG + "/b"]), role("b", children=[ORG + "/a"])])
    with pytest.raises(RoleSnapshotError, match="role_cycle"):
        compute([role("self", children=[ORG + "/self"])])


def chain(depth: int, *, seed: bool = True):
    return [role("r0", users=[ORG + "/directory-name"] if seed else [])] + [
        role(f"r{i}", children=[ORG + f"/r{i - 1}"]) for i in range(1, depth + 1)
    ]


def test_depth_exact_32_and_disconnected_depth_33_rejected():
    assert len(compute(chain(32)).effective_roles) == 33
    for seed in (True, False):
        with pytest.raises(RoleSnapshotError, match="depth_limit"):
            compute(chain(33, seed=seed))
    roles = chain(40)
    roles[20]["isEnabled"] = False
    assert len(compute(roles).effective_roles) == 20


def test_longest_path_checked_even_with_shortcut_seed():
    roles = chain(33)
    roles[-1]["roles"].append(ORG + "/r0")
    with pytest.raises(RoleSnapshotError, match="depth_limit"):
        compute(roles)


def test_nodes_exact_2000_and_2001_rejected():
    assert names(compute([role(f"r{i}") for i in range(2000)])) == set()
    with pytest.raises(RoleSnapshotError, match="nodes_limit"):
        compute([role(f"r{i}") for i in range(2001)])


def test_edges_exact_20000_and_20001_rejected():
    roles = [role(f"c{i}") for i in range(1000)]
    roles += [role(f"p{i}", children=[ORG + f"/c{j}" for j in range(1000)]) for i in range(20)]
    assert names(compute(roles)) == set()
    roles.append(role("extra", children=[ORG + "/c0"]))
    with pytest.raises(RoleSnapshotError, match="edges_limit"):
        compute(roles)


@pytest.mark.parametrize("key", ["users", "groups", "roles", "user_groups"])
def test_relation_lists_exact_2000_and_2001_rejected(key):
    roles = [role(f"c{i}") for i in range(1999)] + [role("r")]
    values = [ORG + f"/c{i}" for i in range(1999)] + [ORG + "/r"]
    raw_user = user()
    if key == "roles":
        # 2000 refs can be valid if this role has 2000 existing child nodes; node
        # limit then includes itself, so its own ref would correctly be a cycle.
        # Demonstrate per-list exact acceptance via User/Group refs and separately
        # inspect the role list bound before graph cycle validation.
        roles[-1][key] = values
        expected = "role_cycle"
    elif key == "user_groups":
        raw_user["groups"] = values
        expected = None
    else:
        roles[-1][key] = values
        expected = None
    if expected:
        with pytest.raises(RoleSnapshotError, match=expected):
            compute(roles, raw_user=raw_user)
    else:
        compute(roles, raw_user=raw_user)
    values.append(ORG + "/overflow")
    with pytest.raises(RoleSnapshotError, match="relations_schema"):
        compute(roles, raw_user=raw_user)


def test_full_reference_exact_511_utf8_bytes_and_512_rejected():
    org = "o" * 255
    raw_user = user()
    raw_user.update(owner=org, name="u" * 255)
    raw_role = role("r")
    raw_role.update(owner=org, users=[org + "/" + raw_user["name"]])
    contract = replace(proof(), deployment_proof=replace(deployment(), organization=org))
    args = dict(
        organization=org,
        verified_subject=SUBJECT,
        raw_user=raw_user,
        raw_roles=[raw_role],
        online_user=VerifiedOnlineUser(SUBJECT, StructuredUserRef(org, raw_user["name"])),
        raw_organization={"owner": "admin", "name": org, "accountItems": []},
        contract=contract,
    )
    assert len(compute_effective_roles(**args).effective_roles) == 1
    raw_role["users"][0] += "x"
    with pytest.raises(RoleSnapshotError, match="relation_boundary"):
        compute_effective_roles(**args)


def test_utf8_bytes_not_characters_and_no_normalization():
    raw_user = user()
    raw_user.update(name="界" * 85)
    assert len(compute([role("r", users=[ORG + "/" + raw_user["name"]])], raw_user=raw_user).effective_roles) == 1
    raw_user["name"] += "界"
    with pytest.raises(RoleSnapshotError, match="user_schema"):
        compute(raw_user=raw_user)
    raw_user = user()
    raw_user["name"] = " Directory-Name "
    assert names(compute([role("r", users=[ORG + "/directory-name"])], raw_user=raw_user)) == set()


def test_snapshot_minimal_immutable_redacted_and_input_not_mutated():
    raw_user = user()
    raw_user.update(password="synthetic-private", roles=[ORG + "/native"], isAdmin=True)
    raw_org = organization()
    raw_org.update(ldap="synthetic-private", masterPassword="synthetic-private")
    roles = [role("r", users=[ORG + "/directory-name"])]
    original = deepcopy((raw_user, raw_org, roles))
    snapshot = compute(roles, raw_user=raw_user, raw_org=raw_org)
    assert (raw_user, raw_org, roles) == original
    assert set(vars(snapshot)) == {"subject", "user_ref", "effective_roles"}
    assert "directory-name" not in repr(snapshot)
    assert "synthetic-private" not in repr(snapshot)
    assert "business-org" not in repr(proof())
    with pytest.raises(FrozenInstanceError):
        snapshot.subject = "changed"
    with pytest.raises(FrozenInstanceError):
        snapshot.user_ref.name = "changed"
    with pytest.raises(ValidationError):
        snapshot.effective_roles[0].name = "changed"


@pytest.mark.parametrize(
    "online_user",
    [
        None,
        "unverified",
        VerifiedIDToken("i", SUBJECT, "c", 0, 1),
        VerifiedOnlineUser("other-subject", StructuredUserRef(ORG, "directory-name")),
        VerifiedOnlineUser(SUBJECT, StructuredUserRef("elsewhere", "directory-name")),
        VerifiedOnlineUser(SUBJECT, StructuredUserRef(ORG, "changed-name")),
    ],
)
def test_pure_graph_requires_i06_projection_and_exact_raw_consistency(online_user):
    with pytest.raises(RoleSnapshotError, match="user_schema"):
        compute_effective_roles(
            organization=ORG,
            verified_subject=SUBJECT,
            online_user=online_user,
            raw_user=user(),
            raw_roles=[],
            raw_organization=organization(),
            contract=proof(),
        )


@pytest.mark.parametrize("raw_roles", [None, {}, "", [None]])
def test_unknown_roles_response_never_known_empty(raw_roles):
    with pytest.raises(RoleSnapshotError):
        compute_effective_roles(
            organization=ORG,
            verified_subject=SUBJECT,
            online_user=VerifiedOnlineUser(SUBJECT, StructuredUserRef(ORG, "directory-name")),
            raw_user=user(),
            raw_roles=raw_roles,
            raw_organization=organization(),
            contract=proof(),
        )


def test_account_items_exact_bound_and_overflow():
    assert names(compute(raw_org=organization([item("Email")] * 2000))) == set()
    with pytest.raises(RoleSnapshotError, match="visibility_schema"):
        compute(raw_org=organization([item("Email")] * 2001))


def test_noncurrent_user_group_refs_are_not_falsely_declared_directory_verified():
    # No get-users/get-groups is permitted. Only role children have a complete
    # catalog; unread same-org entities do not seed the current user's grants.
    roles = [role("r", users=[ORG + "/unread-user"], groups=[ORG + "/unread-group"])]
    assert names(compute(roles)) == set()


class SyntheticCredential:
    """Only tests use this unrecognized header protocol and synthetic proof."""

    proof = deployment()

    def authorization(self, client_id: str, client_secret: str) -> str:
        assert client_id == "test-client"
        assert client_secret == "synthetic-only-placeholder"
        return "Synthetic test-only protocol"


class FakeLease:
    def __init__(self, *, lose_after: int | None = None):
        self.checks = 0
        self.lose_after = lose_after

    def ensure_owned(self, *, renew=False):
        assert renew is False
        self.checks += 1
        if self.lose_after is not None and self.checks > self.lose_after:
            raise RuntimeError("synthetic-private-lease-detail")


def operation() -> GatewayOperation:
    config = CasdoorConfiguration(
        browser_frontend_url="https://front.example.test",
        backend_api_url="https://back.example.test",
        expected_issuer="https://issuer.example.test",
        organization=ORG,
        application="dedicated-app",
        client_id="test-client",
        default_workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
    )
    return GatewayOperation(config, "synthetic-only-placeholder", "https://console.example.test/callback")


@lru_cache
def claims_validator() -> ClaimsValidator:
    # A genuine initialized owner, with a synthetic in-memory pin; these online
    # tests do not invoke signature verification or mint verified ID Tokens.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC).replace(microsecond=0)
    start, end = now - timedelta(days=1), now + timedelta(days=1)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.example.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .sign(key, hashes.SHA256())
    )
    pin = TrustedCertificate(
        pem=cert.public_bytes(serialization.Encoding.PEM).decode(), not_before=start, accept_until=end
    )
    return ClaimsValidator(
        trust_store=CertificateTrustStore([pin]),
        expected_issuer=deployment().expected_issuer,
        organization=ORG,
        application=deployment().application,
        client_id=deployment().client_id,
    )


def loader(op, *, leases=None, contract=None, strategy=None, identity=None):
    return OnlineRoleSnapshotLoader(
        op,
        identity=identity or VerifiedIDToken(op.config.expected_issuer, SUBJECT, op.config.client_id, 0, 9999999999),
        claims_validator=claims_validator(),
        leases=leases or FakeLease(),
        credential_strategy=SyntheticCredential() if strategy is None else strategy,
        contract=proof() if contract is None else contract,
    )


def response(data, *, padding_to=0, metadata=None):
    payload = {"status": "ok", "data": data}
    payload.update(metadata or {})
    body = json.dumps(payload).encode()
    body += b" " * max(0, padding_to - len(body))
    return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})


@pytest.fixture
def transport(monkeypatch):
    calls = []
    replies = []

    def send(method, url, **kwargs):
        calls.append((method, url, kwargs))
        result = replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", send)
    return calls, replies


def test_online_actual_gateway_fixed_paths_shared_deadline_and_fresh_reads(transport):
    calls, replies = transport
    op = operation()
    leases = FakeLease()
    coordinator = loader(op, leases=leases)
    replies.extend([response(organization()), response(user()), response([role("r", users=[ORG + "/directory-name"])])])
    assert names(coordinator.load()) == {(ORG, "r")}
    # A new fresh read sees revoked roles; no cached snapshot/group/visibility.
    replies.extend([response(organization([])), response(user()), response([])])
    assert names(coordinator.load()) == set()
    for offset in (0, 3):
        assert [
            (method, url.removeprefix("https://back.example.test"), kwargs["params"])
            for method, url, kwargs in calls[offset : offset + 3]
        ] == [
            ("GET", "/api/get-organization", {"id": "admin/" + ORG}),
            ("GET", "/api/get-user", {"owner": ORG, "userId": SUBJECT}),
            ("GET", "/api/get-roles", {"owner": ORG}),
        ]
    assert leases.checks >= 12
    for _, _, kwargs in calls:
        assert kwargs["deadline"] == op.deadline
        assert kwargs["max_retries"] == 0
        assert kwargs["follow_redirects"] is False
        assert kwargs["ssl_verify"] is True
        assert kwargs["request_timeout"] == 15.0
    assert [entry[2]["max_response_bytes"] for entry in calls[:3]] == [JSON_LIMIT, JSON_LIMIT, ROLES_LIMIT]


def test_builtin_profile_without_manifest_still_reads_and_validates_complete_roles(transport):
    calls, replies = transport
    op = operation()
    replies.extend(
        [response(organization()), response(user()), response([role("r", users=[ORG + "/directory-name"])])]
    )
    contract = DirectorySnapshotContract(organization=ORG)
    coordinator = OnlineRoleSnapshotLoader(
        op,
        identity=VerifiedIDToken(op.config.expected_issuer, SUBJECT, op.config.client_id, 0, 9999999999),
        claims_validator=claims_validator(),
        leases=FakeLease(),
        credential_strategy=CasdoorBasicDirectoryCredentialStrategy(op.config.client_id),
        contract=contract,
    )

    assert names(coordinator.load()) == {(ORG, "r")}
    assert [urlsplit(url).path for _, url, _ in calls] == [
        "/api/get-organization",
        "/api/get-user",
        "/api/get-roles",
    ]
    assert all(call[2]["headers"]["Authorization"].startswith("Basic ") for call in calls)


def test_default_contract_and_default_credentials_disabled_before_dispatch(transport):
    calls, _ = transport
    for include_contract in (False, True):
        op = operation()
        coordinator = OnlineRoleSnapshotLoader(
            op,
            identity=VerifiedIDToken(op.config.expected_issuer, SUBJECT, op.config.client_id, 0, 9999999999),
            claims_validator=claims_validator(),
            leases=FakeLease(),
            contract=proof() if include_contract else None,
        )
        with pytest.raises(GatewayError) as error:
            coordinator.load()
        assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
        assert error.value.retry_allowed is False
    assert calls == []


@pytest.mark.parametrize(
    "field", ["expected_issuer", "organization", "application", "client_id", "release_fingerprint"]
)
def test_online_proof_binding_mismatch_before_network(transport, field):
    calls, _ = transport
    contract = replace(proof(), deployment_proof=replace(deployment(), **{field: "x" * 64}))
    with pytest.raises(GatewayError):
        loader(operation(), contract=contract).load()
    assert calls == []


def test_online_identity_must_match_trusted_operation_config(transport):
    calls, _ = transport
    op = operation()
    identity = VerifiedIDToken("https://elsewhere.example.test", SUBJECT, op.config.client_id, 0, 9999999999)
    with pytest.raises(GatewayError) as error:
        loader(op, identity=identity).load()
    assert error.value.reason == "identity_schema"
    assert calls == []


@pytest.mark.parametrize("lose_after,dispatches", [(0, 0), (2, 1), (4, 2), (6, 3), (7, 3)])
def test_lease_loss_before_after_reads_latches_shared_operation_no_further_dispatch(transport, lose_after, dispatches):
    calls, replies = transport
    replies.extend([response(organization()), response(user()), response([])])
    op = operation()
    coordinator = loader(op, leases=FakeLease(lose_after=lose_after))
    with pytest.raises(GatewayError) as error:
        coordinator.load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert "synthetic-private" not in str(error.value)
    assert len(calls) == dispatches
    with pytest.raises(GatewayError):
        coordinator.load()
    with pytest.raises(GatewayError):
        op.userinfo("synthetic-token")
    assert len(calls) == dispatches


def test_shared_deadline_expired_during_first_read_prevents_later_dispatch(transport, monkeypatch):
    calls, replies = transport
    op = operation()
    clock = [op.deadline - 1]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    old_send = ssrf_proxy.make_request_with_deadline

    def send(*args, **kwargs):
        result = old_send(*args, **kwargs)
        clock[0] = op.deadline
        return result

    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", send)
    replies.append(response(organization()))
    with pytest.raises(GatewayError) as error:
        loader(op).load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert len(calls) == 1
    with pytest.raises(GatewayError):
        op.discover()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "ok", "data": [], "page": 1},
        {"status": "ok", "data": [], "data2": 1},
        {"status": "error", "data": []},
        {"status": "ok"},
    ],
)
def test_partial_or_unknown_role_envelope_never_fallback(transport, bad):
    calls, replies = transport
    replies.extend(
        [
            response(organization()),
            response(user()),
            httpx.Response(200, json=bad, headers={"Content-Type": "application/json"}),
        ]
    )
    with pytest.raises(GatewayError) as error:
        loader(operation()).load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert len(calls) == 3


@pytest.mark.parametrize("limit,endpoint", [(JSON_LIMIT, "org"), (JSON_LIMIT, "user"), (ROLES_LIMIT, "roles")])
@pytest.mark.parametrize("overflow", [0, 1])
def test_actual_gateway_raw_byte_limits_exact_and_overflow(transport, limit, endpoint, overflow):
    _, replies = transport
    replies.extend(
        [
            response(organization(), padding_to=limit + overflow if endpoint == "org" else 0),
            response(user(), padding_to=limit + overflow if endpoint == "user" else 0),
            response([], padding_to=limit + overflow if endpoint == "roles" else 0),
        ]
    )
    if overflow:
        with pytest.raises(GatewayError) as error:
            loader(operation()).load()
        assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    else:
        assert names(loader(operation()).load()) == set()


def test_online_visibility_drift_rejected_before_user_roles_dispatch(transport):
    calls, replies = transport
    op = operation()
    coordinator = loader(op)
    replies.extend([response(organization()), response(user()), response([])])
    coordinator.load()
    replies.append(response(organization([item("G r o u p s", "Unknown")])))
    with pytest.raises(GatewayError) as error:
        coordinator.load()
    assert error.value.reason == "visibility_hidden"
    assert len(calls) == 4


@pytest.mark.parametrize("flag", ["isForbidden", "isDeleted"])
def test_online_i06_disabled_result_preserved_before_roles_dispatch(transport, flag):
    calls, replies = transport
    raw_user = user()
    raw_user[flag] = True
    replies.extend([response(organization()), response(raw_user)])
    op = operation()
    with pytest.raises(GatewayError) as error:
        loader(op).load()
    assert error.value.code is CasdoorErrorCode.REMOTE_ACCOUNT_DISABLED
    assert error.value.reason == "online_disabled"
    assert len(calls) == 2
    with pytest.raises(GatewayError):
        op.discover()
    assert len(calls) == 2


def test_online_missing_groups_unknown_before_roles_dispatch(transport):
    calls, replies = transport
    raw_user = user()
    del raw_user["groups"]
    replies.extend([response(organization()), response(raw_user)])
    with pytest.raises(GatewayError) as error:
        loader(operation()).load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert error.value.reason == "relations_missing"
    assert len(calls) == 2


def test_unconfirmed_transport_termination_preserved_and_stops_other_facades(transport):
    calls, replies = transport
    replies.append(ssrf_proxy.RequestTerminationUnconfirmedError())
    op = operation()
    with pytest.raises(GatewayError) as error:
        loader(op).load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert error.value.termination_confirmed is False
    assert error.value.retry_allowed is False
    with pytest.raises(GatewayError):
        op.userinfo("synthetic-token")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [ssrf_proxy.RequestDeadlineExceededError(), RuntimeError("synthetic-private")])
def test_transport_failure_unknown_no_fallback_no_retry(transport, failure):
    calls, replies = transport
    replies.append(failure)
    op = operation()
    with pytest.raises(GatewayError) as error:
        loader(op).load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert "synthetic-private" not in str(error.value)
    with pytest.raises(GatewayError):
        op.discover()
    assert len(calls) == 1
