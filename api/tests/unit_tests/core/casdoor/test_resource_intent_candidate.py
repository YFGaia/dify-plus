"""Synthetic candidate consistency and hash boundaries; no runtime issuance."""

import ast
import hashlib
import json
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from core.casdoor import resource_config_read as raw
from core.casdoor import resource_config_reader as reader
from core.casdoor import resource_intent_candidate as subject
from core.helper import ssrf_proxy
from extensions.ext_redis import redis_client
from repositories.casdoor_local_resource_repository_extend import (
    CasdoorLocalResourceRepository,
    LocalResourceInventory,
)
from services.enterprise.rbac_service import RBACResourceType as Kind
from services.enterprise.rbac_service import RBACService
from sqlalchemy.orm import Session
from tasks import initialize_created_app_rbac_access_task as task_owner

CONTEXT = subject.ResourceGrantCandidateContext(
    integration_id=UUID(int=10000),
    config_digest="a" * 64,
    revision_id=UUID(int=10001),
    namespace_id=UUID(int=10002),
    identity_id=UUID(int=10003),
    account_id=UUID(int=10004),
    workspace_id=UUID(int=10005),
    membership_id=UUID(int=10006),
    join_id=UUID(int=10007),
    generation=1,
    ownership_epoch=0,
    fence_epoch=0,
)
KINDS = (Kind.APP, Kind.DATASET, Kind.AGENT)
_MISSING = object()


def fixture(n=1, *, pairs=None, context=CONTEXT, false=False):
    pairs = tuple((Kind.APP, UUID(int=i + 1)) for i in range(n)) if pairs is None else pairs
    queries = 3 + sum((sum(pair[0] is kind for pair in pairs) + 499) // 500 for kind in KINDS)
    inventory = LocalResourceInventory(context.workspace_id, pairs, queries)
    batches = []
    for offset in range(0, len(pairs), 500):
        selected = pairs[offset : offset + 500]
        request = raw.ResourceConfigRequestCandidate(
            raw.ResourceConfigContract.OFFLINE_FIXTURE_V1, context.workspace_id, UUID(int=20000), selected
        )
        batches.append(
            raw.ResourceConfigReadCandidate(
                request,
                tuple(
                    (
                        pair,
                        raw.ResourceConfigObservation.RETURNED_FALSE
                        if false
                        else raw.ResourceConfigObservation.RETURNED_TRUE,
                    )
                    for pair in selected
                ),
            )
        )
    return inventory, reader.OfflineResourceConfigRead(tuple(batches))


def build(inventory=None, observations=None, *, context=CONTEXT):
    if inventory is None:
        inventory, observations = fixture(context=context)
    return subject.build_resource_grant_candidate(context=context, inventory=inventory, observations=observations)


def forge(original, **changes):
    result = object.__new__(type(original))
    for item in fields(original):
        object.__setattr__(result, item.name, changes.get(item.name, getattr(original, item.name)))
    return result


def rejected(context=CONTEXT, inventory=_MISSING, observations=None):
    if inventory is _MISSING:
        inventory, observations = fixture()
    with pytest.raises(subject.ResourceGrantCandidateError, match="^resource_grant_candidate_pending$") as caught:
        subject.build_resource_grant_candidate(context=context, inventory=inventory, observations=observations)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("n", [0, 1, 500, 501, 4096])
@pytest.mark.parametrize("false", [False, True])
def test_complete_boundaries_and_repeatability(n, false):
    inv, obs = fixture(n, false=false)
    result = build(inv, obs)
    repeated = build(inv, obs)
    assert result == repeated
    assert result.inventory == inv.resources
    assert result.observations == tuple(item for batch in obs.batches for item in batch.observations)
    assert len(result.targets) == (0 if false else n)
    assert result.schema_version == 1 and result.contract is raw.ResourceConfigContract.OFFLINE_FIXTURE_V1
    assert result.candidate_operation == "member_join_auto_include"
    if not n:
        assert result.context_id is None and result.validated_account_counts == ()
    assert not any(hasattr(result, name) for name in ("complete", "authorized", "known_absent", "receipt"))
    for item in fields(CONTEXT):
        assert getattr(result, item.name) == getattr(CONTEXT, item.name)


def test_all_kinds_same_uuid_mixed_true_false_and_exact_policy():
    pairs = tuple((kind, UUID(int=i)) for kind in KINDS for i in (1, 2))
    inv, obs = fixture(pairs=pairs)
    batch = obs.batches[0]
    vector = tuple(
        (
            pair,
            raw.ResourceConfigObservation.RETURNED_TRUE if i % 2 == 0 else raw.ResourceConfigObservation.RETURNED_FALSE,
        )
        for i, pair in enumerate(pairs)
    )
    result = build(inv, replace(obs, batches=(replace(batch, observations=vector),)))
    assert tuple((t.resource_type, t.resource_id) for t in result.targets) == pairs[::2]
    assert len(json.loads(result.canonical_plan)["observations"]) == 6
    for target in result.targets:
        value = json.loads(target.canonical_scope)
        assert value["policy"] == {
            "policy_id": task_owner.APP_RBAC_DEFAULT_ACCESS_POLICY_ID,
            "account_ids": [str(CONTEXT.account_id)],
        }
        assert set(value) == {item.name for item in fields(CONTEXT)} | {
            "schema_version",
            "contract",
            "candidate_operation",
            "resource_type",
            "resource_id",
            "policy",
        }


def test_canonical_json_and_exact_hash_domains():
    result = build()

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)

    assert result.canonical_plan == canonical(json.loads(result.canonical_plan))
    assert (
        result.plan_sha256
        == hashlib.sha256(
            ("casdoor-resource-grant-candidate-plan-v1:" + result.canonical_plan).encode("utf-8")
        ).hexdigest()
    )
    target = result.targets[0]
    assert target.canonical_scope == canonical(json.loads(target.canonical_scope))
    assert (
        target.scope_sha256
        == hashlib.sha256(
            ("casdoor-resource-grant-candidate-scope-v1:" + target.canonical_scope).encode("utf-8")
        ).hexdigest()
    )
    assert (
        target.idempotency_key
        == hashlib.sha256(
            ("casdoor-resource-grant-candidate-idempotency-v1:" + target.canonical_scope).encode("utf-8")
        ).hexdigest()
    )
    assert target.scope_sha256 != target.idempotency_key


@pytest.mark.parametrize("name", [item.name for item in fields(CONTEXT)])
def test_every_business_context_field_changes_all_scopes_and_keys(name):
    original = build(*fixture(2))
    value = getattr(CONTEXT, name)
    changed = replace(
        CONTEXT,
        **{name: UUID(int=value.int + 100) if type(value) is UUID else "b" * 64 if type(value) is str else value + 1},
    )
    result = build(*fixture(2, context=changed), context=changed)
    assert original.plan_sha256 != result.plan_sha256
    for old, new in zip(original.targets, result.targets, strict=True):
        assert old.canonical_scope != new.canonical_scope
        assert old.scope_sha256 != new.scope_sha256
        assert old.idempotency_key != new.idempotency_key


@pytest.mark.parametrize("field", ["context_id", "attempted_queries", "validated_account_count"])
def test_observation_metadata_changes_plan_only(field):
    inv, obs = fixture(501)
    old = build(inv, obs)
    if field == "attempted_queries":
        inv = replace(inv, attempted_queries=inv.attempted_queries + 1)
    elif field == "context_id":
        obs = replace(
            obs, batches=tuple(replace(b, request=replace(b.request, context_id=UUID(int=30000))) for b in obs.batches)
        )
    else:
        obs = replace(obs, batches=(replace(obs.batches[0], validated_account_count=1), obs.batches[1]))
    changed = build(inv, obs)
    assert old.plan_sha256 != changed.plan_sha256
    assert old.targets == changed.targets


def test_false_resource_and_false_observation_remain_in_plan_digest():
    inv, obs = fixture(2)
    vector = (obs.batches[0].observations[0], (inv.resources[1], raw.ResourceConfigObservation.RETURNED_FALSE))
    obs = replace(obs, batches=(replace(obs.batches[0], observations=vector),))
    old = build(inv, obs)
    changed_inv = replace(inv, resources=(inv.resources[0], (Kind.APP, UUID(int=3))))
    changed_request = replace(obs.batches[0].request, pairs=changed_inv.resources)
    changed_obs = replace(
        obs,
        batches=(
            replace(
                obs.batches[0],
                request=changed_request,
                observations=(vector[0], (changed_inv.resources[1], raw.ResourceConfigObservation.RETURNED_FALSE)),
            ),
        ),
    )
    result = build(changed_inv, changed_obs)
    assert old.targets == result.targets and old.plan_sha256 != result.plan_sha256
    all_true = build(*fixture(2))
    assert old.plan_sha256 != all_true.plan_sha256 and old.targets[0] == all_true.targets[0]


@pytest.mark.parametrize("name", [item.name for item in fields(CONTEXT) if item.name.endswith("_id")])
@pytest.mark.parametrize("value", [None, "00000000-0000-0000-0000-000000000001", True, object()])
def test_bad_context_uuid_fields(name, value):
    rejected(context=replace(CONTEXT, **{name: value}))


@pytest.mark.parametrize("value", [None, True, "A" * 64, "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n", b"a" * 64])
def test_malformed_digest(value):
    rejected(context=replace(CONTEXT, config_digest=value))


@pytest.mark.parametrize("name", ["generation", "ownership_epoch", "fence_epoch"])
@pytest.mark.parametrize("value", [True, False, -1, 2**63, 1.0, "1", None])
def test_bad_context_integers(name, value):
    rejected(context=replace(CONTEXT, **{name: value}))


def test_integer_edges_and_zero_generation():
    rejected(context=replace(CONTEXT, generation=0))
    assert (
        build(
            context=replace(CONTEXT, generation=2**63 - 1, ownership_epoch=2**63 - 1, fence_epoch=2**63 - 1)
        ).generation
        == 2**63 - 1
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": UUID(int=999)},
        {"workspace_id": str(CONTEXT.workspace_id)},
        {"status": "partial"},
        {"status": raw.ResourceConfigContract.OFFLINE_FIXTURE_V1},
        {"attempted_queries": True},
        {"attempted_queries": 2},
        {"attempted_queries": 17},
        {"attempted_queries": 3},
        {"resources": []},
        {"resources": (("app", UUID(int=1)),)},
        {"resources": ((Kind.APP, "bad"),)},
        {"resources": ((Kind.APP, UUID(int=1)),) * 2},
        {"resources": ((Kind.APP, UUID(int=2)), (Kind.APP, UUID(int=1)))},
        {"resources": ((Kind.DATASET, UUID(int=1)), (Kind.APP, UUID(int=1)))},
        {"resources": ((Kind.AGENT, UUID(int=1)), (Kind.DATASET, UUID(int=1)))},
        {"resources": ([Kind.APP, UUID(int=1)],)},
    ],
)
def test_bad_inventory(changes):
    inv, obs = fixture()
    rejected(inventory=replace(inv, **changes), observations=obs)


def test_oversized_inventory():
    inv, obs = fixture(4096)
    rejected(inventory=replace(inv, resources=inv.resources + ((Kind.APP, UUID(int=4097)),)), observations=obs)


@pytest.mark.parametrize("mode", ["none", "missing", "duplicate", "extra", "reordered", "list"])
def test_batch_coverage_rejects_atomically(mode):
    inv, obs = fixture(501)
    b = obs.batches
    changed = {
        "none": (),
        "missing": b[:1],
        "duplicate": (b[0], b[0]),
        "extra": b + b[:1],
        "reordered": b[::-1],
        "list": list(b),
    }[mode]
    rejected(inventory=inv, observations=replace(obs, batches=changed))


def test_empty_inventory_requires_zero_batches():
    inv, _ = fixture(0)
    _, obs = fixture()
    rejected(inventory=inv, observations=obs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_id", UUID(int=777)),
        ("workspace_id", "bad"),
        ("context_id", "bad"),
        ("contract", "offline_fixture_v1"),
        ("contract", None),
        ("pairs", ()),
        ("pairs", []),
        ("pairs", ((Kind.APP, UUID(int=2)),)),
        ("pairs", ((Kind.APP, UUID(int=1)),) * 2),
    ],
)
def test_request_tampering(field, value):
    inv, obs = fixture()
    batch = obs.batches[0]
    rejected(
        inventory=inv,
        observations=replace(obs, batches=(replace(batch, request=forge(batch.request, **{field: value})),)),
    )


def test_context_id_mismatch_across_batches():
    inv, obs = fixture(501)
    last = replace(obs.batches[1], request=replace(obs.batches[1].request, context_id=UUID(int=40000)))
    rejected(inventory=inv, observations=replace(obs, batches=(obs.batches[0], last)))


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "duplicate",
        "extra",
        "reordered",
        "wrong_pair",
        "unknown",
        "invalid_string",
        "invalid_bool",
        "list",
        "bad_item",
        "bad_pair",
    ],
)
def test_response_coverage_and_enum_atomic_rejection(mode):
    inv, obs = fixture(2)
    batch = obs.batches[0]
    vector = batch.observations
    changed = {
        "missing": vector[:1],
        "duplicate": (vector[0], vector[0]),
        "extra": vector + vector[:1],
        "reordered": vector[::-1],
        "wrong_pair": (vector[0], ((Kind.APP, UUID(int=3)), raw.ResourceConfigObservation.RETURNED_TRUE)),
        "unknown": (vector[0], (vector[1][0], raw.ResourceConfigObservation.OMITTED_UNKNOWN)),
        "invalid_string": (vector[0], (vector[1][0], "returned_true")),
        "invalid_bool": (vector[0], (vector[1][0], True)),
        "list": list(vector),
        "bad_item": (vector[0], list(vector[1])),
        "bad_pair": (vector[0], ([Kind.APP, UUID(int=2)], raw.ResourceConfigObservation.RETURNED_TRUE)),
    }[mode]
    rejected(inventory=inv, observations=replace(obs, batches=(replace(batch, observations=changed),)))


def test_unknown_later_batch_discards_valid_true_prefix():
    inv, obs = fixture(501)
    last = replace(obs.batches[1], observations=((inv.resources[-1], raw.ResourceConfigObservation.OMITTED_UNKNOWN),))
    rejected(inventory=inv, observations=replace(obs, batches=(obs.batches[0], last)))


@pytest.mark.parametrize("count", [True, False, -1, 4097, 0.0, "1", None])
def test_invalid_account_budget(count):
    inv, obs = fixture()
    rejected(
        inventory=inv, observations=replace(obs, batches=(replace(obs.batches[0], validated_account_count=count),))
    )


@pytest.mark.parametrize("total", [4096, 4097])
def test_aggregate_account_budget(total):
    inv, obs = fixture(501)
    changed = replace(
        obs,
        batches=(
            replace(obs.batches[0], validated_account_count=4096),
            replace(obs.batches[1], validated_account_count=total - 4096),
        ),
    )
    if total == 4097:
        rejected(inventory=inv, observations=changed)
    else:
        assert build(inv, changed).validated_account_counts == (4096, 0)


@pytest.mark.parametrize("boundary", ["context", "inventory", "read", "batch", "request", "uuid"])
def test_subclasses_rejected(boundary):
    inv, obs = fixture()
    context = CONTEXT
    original = {
        "context": context,
        "inventory": inv,
        "read": obs,
        "batch": obs.batches[0],
        "request": obs.batches[0].request,
        "uuid": CONTEXT.account_id,
    }[boundary]
    cls = type("Subclass", (type(original),), {})
    if boundary == "uuid":
        value = cls(int=original.int)
        context = replace(context, account_id=value)
    else:
        value = cls(**{item.name: getattr(original, item.name) for item in fields(original) if item.init})
        if boundary == "context":
            context = value
        elif boundary == "inventory":
            inv = value
        elif boundary == "read":
            obs = value
        elif boundary == "batch":
            obs = replace(obs, batches=(value,))
        else:
            obs = replace(obs, batches=(replace(obs.batches[0], request=value),))
    rejected(context=context, inventory=inv, observations=obs)


@pytest.mark.parametrize("position", ["context", "inventory", "read", "batch", "request"])
@pytest.mark.parametrize("value", [None, object(), {}, True])
def test_wrong_public_type_rejects(position, value):
    inv, obs = fixture()
    context = CONTEXT
    if position == "context":
        context = value
    elif position == "inventory":
        inv = value
    elif position == "read":
        obs = value
    elif position == "batch":
        obs = replace(obs, batches=(value,))
    else:
        obs = replace(obs, batches=(replace(obs.batches[0], request=value),))
    rejected(context=context, inventory=inv, observations=obs)


def test_structurally_valid_forged_dataclasses_produce_only_candidate():
    inv, obs = fixture()
    forged_obs = forge(obs, batches=(forge(obs.batches[0], request=forge(obs.batches[0].request)),))
    result = build(forge(inv), forged_obs, context=forge(CONTEXT))
    assert result == build(inv, obs)
    with pytest.raises(subject.ResourceGrantCandidateError):
        subject.prepare_resource_grant(capability=result)


def test_forged_uuid_with_noncanonical_integer_rejects():
    bad = object.__new__(UUID)
    object.__setattr__(bad, "int", True)
    rejected(context=replace(CONTEXT, account_id=bad))
    inv, obs = fixture()
    req = forge(obs.batches[0].request, pairs=((Kind.APP, bad),))
    rejected(inventory=inv, observations=replace(obs, batches=(replace(obs.batches[0], request=req),)))


def test_immutable_context_plan_targets_and_no_value_repr():
    result = build()
    for value, name, replacement in (
        (CONTEXT, "generation", 2),
        (result, "schema_version", 2),
        (result.targets[0], "idempotency_key", "bad"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, name, replacement)
        assert CONTEXT.config_digest not in repr(value) and str(CONTEXT.account_id) not in repr(value)
    assert type(result.inventory) is tuple and type(result.observations) is tuple and type(result.targets) is tuple


class AttributeTrap:
    def __getattribute__(self, name):
        raise AssertionError("input attribute read")


@pytest.mark.parametrize("capability", [None, True, False, "offline_fixture_v1", "attribute_trap", CONTEXT])
def test_production_entry_rejects_before_input_access(capability):
    if capability == "attribute_trap":
        capability = AttributeTrap()
    with pytest.raises(subject.ResourceGrantCandidateError, match="^resource_grant_candidate_pending$"):
        subject.prepare_resource_grant(
            capability=capability,
            context=AttributeTrap(),
            session=AttributeTrap(),
            leases=AttributeTrap(),
            transport=AttributeTrap(),
            credential=AttributeTrap(),
            complete=True,
        )


def test_candidate_and_rejection_zero_db_redis_transport_task_acl_effects(monkeypatch):
    fail = Mock(side_effect=AssertionError("forbidden effect"))
    for name in ("execute", "scalars", "flush", "commit", "add"):
        monkeypatch.setattr(Session, name, fail)
    monkeypatch.setattr(type(redis_client), "_require_client", fail)
    monkeypatch.setattr(CasdoorLocalResourceRepository, "enumerate_ids", fail)
    monkeypatch.setattr(reader._OfflineReadOperation, "read", fail)
    monkeypatch.setattr(raw, "parse_resource_config", fail)
    monkeypatch.setattr(ssrf_proxy, "make_request_with_deadline", fail)
    for owner in (RBACService.AppAccess, RBACService.DatasetAccess, RBACService.AgentAccess):
        for name in ("append_whitelist_members_batch", "replace_whitelist", "replace_user_access_policies"):
            monkeypatch.setattr(owner, name, fail)
    monkeypatch.setattr(task_owner.sync_joined_workspace_member_rbac_access_task, "delay", fail)
    assert build().targets
    rejected(context=replace(CONTEXT, generation=True))
    with pytest.raises(subject.ResourceGrantCandidateError):
        subject.prepare_resource_grant(capability=AttributeTrap())
    assert fail.call_count == 0


def test_module_has_no_effect_calls_or_dynamic_imports():
    tree = ast.parse(Path(subject.__file__).read_text())
    prohibited = {
        "execute",
        "flush",
        "commit",
        "add",
        "lpush",
        "delay",
        "apply_async",
        "read",
        "extract",
        "enumerate_ids",
        "parse_resource_config",
        "make_request_with_deadline",
        "append_whitelist_members_batch",
        "replace_whitelist",
        "replace_user_access_policies",
        "__import__",
        "open",
    }
    called = {
        node.func.attr
        if isinstance(node.func, ast.Attribute)
        else node.func.id
        if isinstance(node.func, ast.Name)
        else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & prohibited
