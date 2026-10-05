"""Independent adversarial checks for the synthetic I15-B3 projection."""

from dataclasses import fields, replace
from uuid import UUID

import pytest
from core.casdoor import resource_config_read as raw
from core.casdoor import resource_config_reader as reader
from core.casdoor import resource_intent_candidate as subject
from repositories.casdoor_local_resource_repository_extend import LocalResourceInventory
from services.enterprise.rbac_service import RBACResourceType as Kind

CTX = subject.ResourceGrantCandidateContext(
    integration_id=UUID(int=101), config_digest="1" * 64,
    revision_id=UUID(int=102), namespace_id=UUID(int=103),
    identity_id=UUID(int=104), account_id=UUID(int=105),
    workspace_id=UUID(int=106), membership_id=UUID(int=107),
    join_id=UUID(int=108), generation=3, ownership_epoch=4, fence_epoch=5,
)
ORDER = (Kind.APP, Kind.DATASET, Kind.AGENT)


def make(n=1, *, truth=True, context=CTX, counts=None, context_id=UUID(int=200)):
    pairs = tuple((kind, UUID(int=i + 1)) for kind in ORDER for i in range(n // 3 + (kind is ORDER[0] and n % 3)))
    # The constructor above gives a canonical kind order and unique IDs per kind.
    inventory = LocalResourceInventory(
        context.workspace_id, pairs,
        3 + sum((sum(p[0] is k for p in pairs) + 499) // 500 for k in ORDER),
    )
    batches = []
    for offset in range(0, len(pairs), 500):
        selected = pairs[offset:offset + 500]
        request = raw.ResourceConfigRequestCandidate(
            raw.ResourceConfigContract.OFFLINE_FIXTURE_V1,
            context.workspace_id, context_id, selected,
        )
        batches.append(raw.ResourceConfigReadCandidate(
            request,
            tuple((p, raw.ResourceConfigObservation.RETURNED_TRUE if truth else raw.ResourceConfigObservation.RETURNED_FALSE) for p in selected),
            (counts or {}).get(len(batches), 0),
        ))
    return inventory, reader.OfflineResourceConfigRead(tuple(batches))


def project(inventory, observations, context=CTX):
    return subject.build_resource_grant_candidate(
        context=context, inventory=inventory, observations=observations,
    )


def assert_rejected(inventory, observations, context=CTX):
    with pytest.raises(subject.ResourceGrantCandidateError) as error:
        project(inventory, observations, context)
    assert str(error.value) == "resource_grant_candidate_pending"
    assert error.value.__cause__ is None


def forge_exact(value, **changes):
    """Construct an exact instance with fields changed, bypassing frozen init checks."""
    result = object.__new__(type(value))
    for item in fields(value):
        object.__setattr__(result, item.name, changes.get(item.name, getattr(value, item.name)))
    return result


def test_valid_batch_partition_is_inventory_order_and_metadata_is_not_business_key():
    inv, obs = make(501)
    candidate = project(inv, obs)
    assert tuple(pair for pair, _state in candidate.observations) == inv.resources
    assert tuple((target.resource_type, target.resource_id) for target in candidate.targets) == inv.resources
    first, second = obs.batches
    changed = replace(obs, batches=(
        replace(first, request=replace(first.request, context_id=UUID(int=201))),
        replace(second, request=replace(second.request, context_id=UUID(int=201))),
    ))
    reread = project(inv, changed)
    assert reread.plan_sha256 != candidate.plan_sha256
    assert tuple((x.scope_sha256, x.idempotency_key) for x in reread.targets) == tuple(
        (x.scope_sha256, x.idempotency_key) for x in candidate.targets
    )


def test_single_batch_boundary_tampering_rejects_without_partial_target():
    inv, obs = make(501)
    good_prefix = obs.batches[0]
    bad_tail = replace(obs.batches[1], observations=())
    assert_rejected(inv, replace(obs, batches=(good_prefix, bad_tail)))
    assert_rejected(inv, replace(obs, batches=(obs.batches[1], good_prefix)))
    assert_rejected(inv, replace(obs, batches=(good_prefix, obs.batches[1], obs.batches[1])))


def test_inventory_order_is_global_kind_then_uuid_not_just_batch_local():
    pairs = ((Kind.APP, UUID(int=1)), (Kind.DATASET, UUID(int=2)), (Kind.AGENT, UUID(int=3)))
    inv, obs = make(1)
    inv = replace(inv, resources=pairs, attempted_queries=6)
    req = replace(obs.batches[0].request, pairs=pairs)
    obs = replace(obs, batches=(replace(obs.batches[0], request=req, observations=tuple(
        (p, raw.ResourceConfigObservation.RETURNED_TRUE) for p in pairs
    )),))
    assert len(project(inv, obs).targets) == 3
    # A locally well-formed request cannot legitimize a globally misordered inventory.
    bad_pairs = (pairs[1], pairs[0], pairs[2])
    bad_inv = replace(inv, resources=bad_pairs)
    bad_req = forge_exact(req, pairs=bad_pairs)
    bad_batch = forge_exact(obs.batches[0], request=bad_req, observations=tuple(
        (p, raw.ResourceConfigObservation.RETURNED_TRUE) for p in bad_pairs
    ))
    assert_rejected(bad_inv, reader.OfflineResourceConfigRead((bad_batch,)))


def test_forged_exact_request_contract_and_invalid_resource_enum_reject():
    inv, obs = make()
    original = obs.batches[0]
    invalid_contract = forge_exact(original.request, contract="offline_fixture_v1")
    assert_rejected(inv, reader.OfflineResourceConfigRead((forge_exact(original, request=invalid_contract),)))
    invalid_pair = (("app", inv.resources[0][1]),)
    invalid_request = forge_exact(original.request, pairs=invalid_pair)
    assert_rejected(inv, reader.OfflineResourceConfigRead((forge_exact(original, request=invalid_request),)))


def test_attempt_budget_minimum_and_exact_bool_rejection():
    inv, obs = make()
    assert_rejected(replace(inv, attempted_queries=3), obs)
    assert_rejected(replace(inv, attempted_queries=True), obs)
    assert len(project(replace(inv, attempted_queries=16), obs).targets) == 1


def test_validated_account_counts_are_parser_budget_only_and_aggregate_bounded():
    inv, obs = make(1000, counts={0: 4096, 1: 0})
    candidate = project(inv, obs)
    assert candidate.validated_account_counts == (4096, 0)
    inv2, obs2 = make(1000, counts={0: 4096, 1: 1})
    assert_rejected(inv2, obs2)
    inv3, obs3 = make(1, counts={0: 4097})
    assert_rejected(inv3, obs3)


def test_empty_and_all_false_are_not_absence_or_completion_signals():
    empty_inv, empty_obs = make(0)
    empty = project(empty_inv, empty_obs)
    assert empty.targets == () and empty.observations == () and empty.context_id is None
    assert not any(hasattr(empty, name) for name in ("complete", "known_absent", "authorized", "receipt"))
    one_inv, one_obs = make(1, truth=False)
    all_false = project(one_inv, one_obs)
    assert all_false.inventory == one_inv.resources and all_false.targets == ()
    assert all_false.observations[0][1] is raw.ResourceConfigObservation.RETURNED_FALSE
    assert_rejected(one_inv, reader.OfflineResourceConfigRead(()))


def test_every_resource_scope_has_exact_business_key_material():
    inv, obs = make(3)
    result = project(inv, obs)
    assert len(result.targets) == 3
    scopes = [target.canonical_scope for target in result.targets]
    assert len(set(scopes)) == 3
    for target, pair in zip(result.targets, inv.resources, strict=True):
        assert target.resource_type is pair[0] and target.resource_id == pair[1]
        assert f'"resource_type":"{pair[0].value}"' in target.canonical_scope
        assert f'"resource_id":"{pair[1]}"' in target.canonical_scope
        assert len(target.scope_sha256) == len(target.idempotency_key) == 64
        assert target.scope_sha256 != target.idempotency_key


def test_production_entry_traps_all_inputs_before_reading_any_attribute():
    class Trap:
        def __getattribute__(self, _name):
            raise AssertionError("input inspected")

    trap = Trap()
    shapes = [
        {}, {"capability": trap}, {"inventory": trap}, {"context": trap},
        {"operation": trap}, {"capability": trap, "inventory": trap,
         "context": trap, "session": trap, "transport": trap,
         "credential": trap, "acl_baseline": trap, "authorized": trap},
    ]
    for kwargs in shapes:
        with pytest.raises(subject.ResourceGrantCandidateError, match="^resource_grant_candidate_pending$"):
            subject.prepare_resource_grant(**kwargs)


def test_result_and_nested_targets_are_immutable():
    inv, obs = make()
    result = project(inv, obs)
    with pytest.raises((AttributeError, TypeError)):
        result.plan_sha256 = "f" * 64
    with pytest.raises((AttributeError, TypeError)):
        result.targets[0].idempotency_key = "f" * 64
