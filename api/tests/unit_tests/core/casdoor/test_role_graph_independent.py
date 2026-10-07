"""Independent loader checks using a signed identity and the real HTTP gateway.

All credentials, certificates, deployment fingerprints and HTTP responses here
are synthetic. Passing these tests is not evidence about a deployed Casdoor.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from core.casdoor.claims import ClaimsValidator, StructuredUserRef, VerifiedOnlineUser
from core.casdoor.configuration import CasdoorConfiguration
from core.casdoor.crypto import CertificateTrustStore, TrustedCertificate
from core.casdoor.errors import CasdoorErrorCode
from core.casdoor.gateway import DirectoryDeploymentProof, GatewayError, GatewayOperation
from core.casdoor.role_graph import (
    MAX_DEPTH,
    MAX_NODES,
    DirectorySnapshotContract,
    OnlineRoleSnapshotLoader,
    RoleSnapshotError,
    compute_effective_roles,
)
from core.helper import ssrf_proxy
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

ORG = "independent-business"
SUBJECT = "signed-stable-subject"
ISSUER = "https://issuer.example.test"
APP = "dedicated-org-app"
CLIENT = "independent-client"
SECRET = "synthetic-only-secret"
REDIRECT = "https://console.example.test/callback"
_AUTO_CONTRACT = object()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def identity_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC).replace(microsecond=0)
    start, end = now - timedelta(days=1), now + timedelta(days=1)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.example.test")])
    cert = (
        x509
        .CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start.replace(tzinfo=None))
        .not_valid_after(end.replace(tzinfo=None))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    pin = TrustedCertificate(pem=pem, kid="synthetic-kid", not_before=start, accept_until=end)
    validator = ClaimsValidator(
        trust_store=CertificateTrustStore([pin]),
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
    )
    return key, validator, now


def _signed_identity(key, validator, now: datetime, **extra_claims):
    nonce = "callback-nonce-bound-to-this-test"
    claims = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": CLIENT,
        "exp": now.timestamp() + 600,
        "iat": now.timestamp() - 1,
        "nonce": nonce,
        **extra_claims,
    }
    header = {"alg": "RS256", "typ": "JWT", "kid": "synthetic-kid"}
    encoded_header = _b64(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}"
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    token = signing_input + "." + _b64(signature)
    return validator.verify_id_token(
        token,
        expected_nonce=nonce,
        auth_started_at=now - timedelta(seconds=2),
        now=now,
    )


def _operation() -> GatewayOperation:
    config = CasdoorConfiguration(
        browser_frontend_url="https://front.example.test",
        backend_api_url="https://back.example.test",
        expected_issuer=ISSUER,
        organization=ORG,
        application=APP,
        client_id=CLIENT,
        default_workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
    )
    return GatewayOperation(config, SECRET, REDIRECT)


def _proof(operation: GatewayOperation) -> DirectoryDeploymentProof:
    config = operation.config
    return DirectoryDeploymentProof(
        config.expected_issuer,
        config.organization,
        config.application,
        config.client_id,
        "1" * 64,
        "2" * 64,
        "3" * 64,
    )


class _SyntheticCredential:
    def __init__(self, proof: DirectoryDeploymentProof):
        self.proof = proof

    def authorization(self, client_id: str, client_secret: str) -> str:
        assert (client_id, client_secret) == (CLIENT, SECRET)
        return "Synthetic app credential; not a real provider protocol"


class _Lease:
    def __init__(self, fail_at: int | None = None):
        self.checks = 0
        self.fail_at = fail_at

    def ensure_owned(self, *, renew: bool = False) -> None:
        assert renew is False
        self.checks += 1
        if self.checks == self.fail_at:
            raise RuntimeError("private lease detail")


class _HttpFixture:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, data: list[Any]):
        self.calls: list[dict[str, Any]] = []
        self.data = list(data)

        def send(method: str, url: str, **kwargs: Any) -> httpx.Response:
            self.calls.append({"method": method, "url": url, **kwargs})
            response = self.data.pop(0)
            if isinstance(response, Exception):
                raise response
            return httpx.Response(
                200,
                content=json.dumps({"status": "ok", "data": response}, separators=(",", ":")).encode(),
                headers={"Content-Type": "application/json"},
            )

        monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", send)


def _user(**changes: Any) -> dict[str, Any]:
    return {
        "owner": ORG,
        "id": SUBJECT,
        "name": "alice",
        "isForbidden": False,
        "isDeleted": False,
        "groups": [ORG + "/staff"],
        # These untrusted convenience fields must not grant permissions.
        "roles": [ORG + "/jwt-admin"],
        "isAdmin": True,
        **changes,
    }


def _role(name: str, *, enabled: bool = True, users=(), groups=(), parents=()):
    return {
        "owner": ORG,
        "name": name,
        "isEnabled": enabled,
        "users": list(users),
        "groups": list(groups),
        "roles": [ORG + "/" + child for child in parents],
    }


def _loader(operation, identity, validator, lease=None, *, proof=True, strategy=True):
    linked_proof = _proof(operation)
    return OnlineRoleSnapshotLoader(
        operation,
        identity=identity,
        claims_validator=validator,
        leases=lease or _Lease(),
        credential_strategy=_SyntheticCredential(linked_proof) if strategy else None,
        contract=DirectorySnapshotContract(linked_proof, "4" * 64, "5" * 64) if proof else None,
    )


def _pure_snapshot(raw_user=None, raw_roles=None, raw_org=None, *, contract=_AUTO_CONTRACT):
    raw_user = _user() if raw_user is None else raw_user
    return compute_effective_roles(
        organization=ORG,
        verified_subject=SUBJECT,
        online_user=VerifiedOnlineUser(SUBJECT, StructuredUserRef(ORG, "alice")),
        raw_user=raw_user,
        raw_roles=[] if raw_roles is None else raw_roles,
        raw_organization=({"owner": "admin", "name": ORG, "accountItems": []} if raw_org is None else raw_org),
        contract=(
            DirectorySnapshotContract(_proof(_operation()), "4" * 64, "5" * 64)
            if contract is _AUTO_CONTRACT
            else contract
        ),
    )


def _run(monkeypatch, identity_material, payloads, *, lease=None, proof=True, strategy=True):
    key, validator, now = identity_material
    identity = _signed_identity(key, validator, now, roles=[ORG + "/claim-admin"], isAdmin=True)
    operation = _operation()
    transport = _HttpFixture(monkeypatch, payloads)
    loader = _loader(operation, identity, validator, lease, proof=proof, strategy=strategy)
    return loader.load(), operation, transport


def test_signed_identity_uses_group_seed_and_child_to_parent_closure(monkeypatch, identity_material):
    org = {
        "owner": "admin",
        "name": ORG,
        "accountItems": [
            {"name": "G r o u p s", "visible": False, "viewRule": "Admin"},
            {"name": "Roles", "visible": True, "viewRule": "Public"},
        ],
    }
    roles = [
        _role("group-child", groups=[ORG + "/staff"]),
        _role("group-parent", parents=("group-child",)),
        _role("disabled-parent", enabled=False, parents=("group-child",)),
        _role("beyond-disabled", parents=("disabled-parent",)),
        _role("seed-parent", groups=[ORG + "/staff"], parents=("unseeded-child",)),
        _role("unseeded-child"),
        _role("jwt-admin"),
        _role("claim-admin"),
    ]
    snapshot, operation, transport = _run(monkeypatch, identity_material, [org, _user(), roles])
    assert {(ref.organization, ref.name) for ref in snapshot.effective_roles} == {
        (ORG, "group-child"),
        (ORG, "group-parent"),
        (ORG, "seed-parent"),
    }
    assert [call["url"].removeprefix("https://back.example.test") for call in transport.calls] == [
        "/api/get-organization",
        "/api/get-user",
        "/api/get-roles",
    ]
    assert [call["params"] for call in transport.calls] == [
        {"id": "admin/" + ORG},
        {"owner": ORG, "userId": SUBJECT},
        {"owner": ORG},
    ]
    assert all(call["deadline"] == operation.deadline for call in transport.calls)
    assert all(call["max_retries"] == 0 and call["follow_redirects"] is False for call in transport.calls)


@pytest.mark.parametrize("flag", ["isForbidden", "isDeleted"])
def test_signed_identity_remote_disabled_state_stops_before_role_read(monkeypatch, identity_material, flag):
    raw = _user(**{flag: True})
    operation = _operation()
    key, validator, now = identity_material
    identity = _signed_identity(key, validator, now)
    transport = _HttpFixture(monkeypatch, [{"owner": "admin", "name": ORG, "accountItems": []}, raw])
    loader = _loader(operation, identity, validator)
    with pytest.raises(GatewayError) as error:
        loader.load()
    assert error.value.code is CasdoorErrorCode.REMOTE_ACCOUNT_DISABLED
    assert len(transport.calls) == 2
    with pytest.raises(GatewayError):
        operation.discover()
    assert len(transport.calls) == 2


def test_account_item_candidate_hidden_rule_stops_before_user_roles(monkeypatch, identity_material):
    operation = _operation()
    key, validator, now = identity_material
    identity = _signed_identity(key, validator, now)
    transport = _HttpFixture(
        monkeypatch,
        [
            {
                "owner": "admin",
                "name": ORG,
                "accountItems": [{"name": "R o l e s", "visible": True, "viewRule": "FutureRule"}],
            }
        ],
    )
    with pytest.raises(GatewayError) as error:
        _loader(operation, identity, validator).load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert len(transport.calls) == 1


def test_missing_current_groups_is_unknown_before_complete_roles(monkeypatch, identity_material):
    user = _user()
    del user["groups"]
    operation = _operation()
    key, validator, now = identity_material
    identity = _signed_identity(key, validator, now)
    transport = _HttpFixture(monkeypatch, [{"owner": "admin", "name": ORG, "accountItems": []}, user])
    with pytest.raises(GatewayError) as error:
        _loader(operation, identity, validator).load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert len(transport.calls) == 2


@pytest.mark.parametrize("missing", ["proof", "strategy"])
def test_unproven_loader_defaults_reject_before_any_http_dispatch(monkeypatch, identity_material, missing):
    operation = _operation()
    key, validator, now = identity_material
    identity = _signed_identity(key, validator, now)
    transport = _HttpFixture(monkeypatch, [])
    loader = _loader(
        operation,
        identity,
        validator,
        proof=missing != "proof",
        strategy=missing != "strategy",
    )
    with pytest.raises(GatewayError) as error:
        loader.load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert error.value.retry_allowed is False
    assert transport.calls == []


def test_lease_loss_after_visibility_prevents_any_user_dispatch(monkeypatch, identity_material):
    operation = _operation()
    key, validator, now = identity_material
    identity = _signed_identity(key, validator, now)
    transport = _HttpFixture(monkeypatch, [{"owner": "admin", "name": ORG, "accountItems": []}])
    loader = _loader(operation, identity, validator, _Lease(fail_at=3))
    with pytest.raises(GatewayError) as error:
        loader.load()
    assert error.value.code is CasdoorErrorCode.ROLE_SNAPSHOT_UNKNOWN
    assert len(transport.calls) == 1
    assert operation._halted is True
    with pytest.raises(GatewayError):
        operation.userinfo("synthetic-access-token")
    assert len(transport.calls) == 1


def test_direct_role_requires_exact_full_owner_and_directory_name():
    exact = _role("direct", users=[ORG + "/alice"])
    assert [item.name for item in _pure_snapshot(raw_roles=[exact]).effective_roles] == ["direct"]
    # The signed subject and SDK/JWT convenience role fields are not UserRef names.
    for relation in (SUBJECT, "alice", "other-business/alice"):
        with pytest.raises(RoleSnapshotError):
            _pure_snapshot(raw_roles=[_role("direct", users=[relation])])


def test_group_seed_is_an_exact_intersection_and_unread_catalog_is_not_claimed():
    raw_user = _user(groups=[ORG + "/staff"])
    assert [
        item.name for item in _pure_snapshot(raw_user, [_role("matched", groups=[ORG + "/staff"])]).effective_roles
    ] == ["matched"]
    assert _pure_snapshot(raw_user, [_role("case-mismatch", groups=[ORG + "/Staff"])]).effective_roles == ()
    # This unrelated same-org group is not in the current user's complete Groups
    # list. The three allowed reads do not prove whether that group exists.
    assert _pure_snapshot(raw_user, [_role("unread-group", groups=[ORG + "/unread-group"])]).effective_roles == ()


def test_parent_role_references_child_and_disabled_node_cuts_path():
    graph = [
        _role("seed", users=[ORG + "/alice"]),
        _role("enabled-parent", parents=("seed",)),
        _role("disabled-parent", enabled=False, parents=("enabled-parent",)),
        _role("above-disabled", parents=("disabled-parent",)),
        _role("unseeded-child"),
    ]
    assert {item.name for item in _pure_snapshot(raw_roles=graph).effective_roles} == {"seed", "enabled-parent"}
    # A seed on a parent does not flow downward into the listed child.
    inverted = [_role("parent-seed", users=[ORG + "/alice"], parents=("child",)), _role("child")]
    assert {item.name for item in _pure_snapshot(raw_roles=inverted).effective_roles} == {"parent-seed"}


@pytest.mark.parametrize(
    "roles",
    [
        [_role("a", parents=("b",)), _role("b", parents=("a",))],
        [_role("a", parents=("missing-child",))],
        [_role("a", enabled=1)],
        [{"owner": ORG, "name": "a", "isEnabled": True, "users": [], "groups": []}],
    ],
)
def test_cycle_dangling_nonboolean_enabled_and_missing_relation_key_reject_whole_snapshot(roles):
    with pytest.raises(RoleSnapshotError):
        _pure_snapshot(raw_roles=roles)


def test_known_empty_null_and_empty_relations_require_linked_contract():
    assert _pure_snapshot(raw_user=_user(groups=None), raw_roles=[]).effective_roles == ()
    assert _pure_snapshot(raw_user=_user(groups=[]), raw_roles=[]).effective_roles == ()
    with pytest.raises(RoleSnapshotError):
        _pure_snapshot(raw_user=_user(groups=None), raw_roles=[], contract=None)


def test_whole_graph_depth_and_node_bounds_are_not_silently_truncated():
    chain = [_role(f"r{i}", parents=(f"r{i - 1}",) if i else ()) for i in range(MAX_DEPTH + 2)]
    chain[0]["users"] = [ORG + "/alice"]
    with pytest.raises(RoleSnapshotError, match="depth_limit"):
        _pure_snapshot(raw_roles=chain)
    assert _pure_snapshot(raw_roles=[_role(f"n{i}") for i in range(MAX_NODES)]).effective_roles == ()
    with pytest.raises(RoleSnapshotError, match="nodes_limit"):
        _pure_snapshot(raw_roles=[_role(f"n{i}") for i in range(MAX_NODES + 1)])
    children = [_role(f"c{i}") for i in range(1001)]
    parents = [_role(f"p{i}", parents=tuple(f"c{j}" for j in range(1000))) for i in range(20)]
    assert _pure_snapshot(raw_roles=children + parents).effective_roles == ()
    parents[0]["roles"].append(ORG + "/c1000")
    with pytest.raises(RoleSnapshotError, match="edges_limit"):
        _pure_snapshot(raw_roles=children + parents)
