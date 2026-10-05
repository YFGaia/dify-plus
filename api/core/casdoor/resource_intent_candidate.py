"""Pure synthetic member-join candidates; no resource grant or authority issuer.

All inputs are public/offline consistency observations. Even forged values that
pass validation establish no current ownership, inventory provenance, ACL
baseline, remote completeness or completion. Candidate keys must never be used
in a production outbox. The production entry rejects before inspecting inputs.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field, fields
from typing import NoReturn
from uuid import UUID

from repositories.casdoor_local_resource_repository_extend import LocalResourceInventory
from services.enterprise.rbac_service import RBACResourceType
from tasks.initialize_created_app_rbac_access_task import (
    APP_RBAC_DEFAULT_ACCESS_POLICY_ID,
)

from core.casdoor import resource_config_read as raw
from core.casdoor.resource_config_reader import OfflineResourceConfigRead

_MAX_EPOCH = 2**63 - 1
_OPERATION = "member_join_auto_include"


class ResourceGrantCandidateError(ValueError):
    def __init__(self) -> None:
        super().__init__("resource_grant_candidate_pending")


def prepare_resource_grant(*, capability: object = None, **operation: object) -> NoReturn:
    """Reject unconditionally before attribute access, configuration or I/O."""
    raise ResourceGrantCandidateError()


def _uuid(value: object) -> str:
    if type(value) is not UUID or type(value.int) is not int or not 0 <= value.int < 2**128:
        raise ResourceGrantCandidateError()
    return str(value)


@dataclass(frozen=True, repr=False)
class ResourceGrantCandidateContext:
    """Caller-supplied consistency scalars, never current business authority."""

    integration_id: UUID
    config_digest: str
    revision_id: UUID
    namespace_id: UUID
    identity_id: UUID
    account_id: UUID
    workspace_id: UUID
    membership_id: UUID
    join_id: UUID
    generation: int
    ownership_epoch: int
    fence_epoch: int


@dataclass(frozen=True, repr=False)
class ResourceGrantTargetCandidate:
    resource_type: RBACResourceType
    resource_id: UUID
    canonical_scope: str
    scope_sha256: str
    idempotency_key: str


@dataclass(frozen=True, repr=False)
class ResourceGrantPlanCandidate(ResourceGrantCandidateContext):
    inventory: tuple[raw.ResourcePair, ...]
    observations: tuple[tuple[raw.ResourcePair, raw.ResourceConfigObservation], ...]
    attempted_queries: int
    context_id: UUID | None
    validated_account_counts: tuple[int, ...]
    targets: tuple[ResourceGrantTargetCandidate, ...]
    canonical_plan: str
    plan_sha256: str
    schema_version: int = field(default=1, init=False)
    contract: raw.ResourceConfigContract = field(default=raw.ResourceConfigContract.OFFLINE_FIXTURE_V1, init=False)
    candidate_operation: str = field(default=_OPERATION, init=False)


def _context_values(context: ResourceGrantCandidateContext) -> dict[str, str | int]:
    if type(context) is not ResourceGrantCandidateContext:
        raise ResourceGrantCandidateError()
    values: dict[str, str | int] = {}
    for item in fields(ResourceGrantCandidateContext):
        value = getattr(context, item.name)
        if item.name == "config_digest":
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ResourceGrantCandidateError()
        elif item.name in ("generation", "ownership_epoch", "fence_epoch"):
            minimum = 1 if item.name == "generation" else 0
            if type(value) is not int or not minimum <= value <= _MAX_EPOCH:
                raise ResourceGrantCandidateError()
        else:
            value = _uuid(value)
        values[item.name] = value
    return values


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(domain: str, canonical: str) -> str:
    return hashlib.sha256((f"casdoor-resource-grant-candidate-{domain}-v1:" + canonical).encode("utf-8")).hexdigest()


def _pair_json(pair: raw.ResourcePair) -> dict[str, str]:
    return {"resource_type": pair[0].value, "resource_id": _uuid(pair[1])}


def build_resource_grant_candidate(
    *,
    context: ResourceGrantCandidateContext,
    inventory: LocalResourceInventory,
    observations: OfflineResourceConfigRead,
) -> ResourceGrantPlanCandidate:
    """Project all bounded observations atomically into a synthetic candidate.

    Exact types and complete deterministic batches express consistency only.
    Unknown or invalid data rejects the entire plan before any target is issued.
    Empty/all-FALSE plans do not prove remote absence or permission completion.
    """
    try:
        values = _context_values(context)
        if (
            type(inventory) is not LocalResourceInventory
            or _uuid(inventory.workspace_id) != values["workspace_id"]
            or type(inventory.status) is not str
            or inventory.status != "LOCAL_SCAN_EXHAUSTED"
            or type(inventory.attempted_queries) is not int
            or not 3 <= inventory.attempted_queries <= 16
            or type(observations) is not OfflineResourceConfigRead
            or type(observations.batches) is not tuple
        ):
            raise ResourceGrantCandidateError()
        raw._validate_pairs(inventory.resources, raw.MAX_INVENTORY)
        for pair in inventory.resources:
            _uuid(pair[1])
        # Reuse the accepted request owner for original enum/order consistency.
        raw.build_resource_config_request(
            contract=raw.ResourceConfigContract.OFFLINE_FIXTURE_V1,
            workspace_id=inventory.workspace_id,
            context_id=context.integration_id,
            inventory=inventory.resources,
            selected=(),
        )
        minimum_queries = 3 + sum(
            (sum(pair[0] is kind for pair in inventory.resources) + raw.MAX_BATCH - 1) // raw.MAX_BATCH
            for kind in raw._KIND_ORDER
        )
        expected_batches = (len(inventory.resources) + raw.MAX_BATCH - 1) // raw.MAX_BATCH
        if inventory.attempted_queries < minimum_queries or len(observations.batches) != expected_batches:
            raise ResourceGrantCandidateError()
        vector = []
        counts = []
        context_id = None
        for index, batch in enumerate(observations.batches):
            if (
                type(batch) is not raw.ResourceConfigReadCandidate
                or type(batch.request) is not raw.ResourceConfigRequestCandidate
            ):
                raise ResourceGrantCandidateError()
            request = batch.request
            request.__post_init__()
            _uuid(request.workspace_id)
            _uuid(request.context_id)
            for pair in request.pairs:
                _uuid(pair[1])
            if context_id is None:
                context_id = request.context_id
            expected = inventory.resources[index * raw.MAX_BATCH : (index + 1) * raw.MAX_BATCH]
            if (
                request.workspace_id != inventory.workspace_id
                or request.context_id != context_id
                or request.pairs != expected
                or type(batch.observations) is not tuple
                or len(batch.observations) != len(expected)
                or type(batch.validated_account_count) is not int
                or not 0 <= batch.validated_account_count <= raw.MAX_ACCOUNTS
            ):
                raise ResourceGrantCandidateError()
            counts.append(batch.validated_account_count)
            if sum(counts) > raw.MAX_ACCOUNTS:
                raise ResourceGrantCandidateError()
            for item, pair in zip(batch.observations, expected, strict=True):
                if type(item) is not tuple or len(item) != 2:
                    raise ResourceGrantCandidateError()
                returned_pair, state = item
                raw._validate_pairs((returned_pair,), 1)
                _uuid(returned_pair[1])
                if returned_pair != pair or (
                    state is not raw.ResourceConfigObservation.RETURNED_TRUE
                    and state is not raw.ResourceConfigObservation.RETURNED_FALSE
                ):
                    raise ResourceGrantCandidateError()
                vector.append((pair, state))
        fixed = {
            "schema_version": 1,
            "contract": raw.ResourceConfigContract.OFFLINE_FIXTURE_V1.value,
            "candidate_operation": _OPERATION,
        }
        targets = []
        for pair, state in vector:
            if state is raw.ResourceConfigObservation.RETURNED_TRUE:
                scope = _canonical(
                    {
                        **fixed,
                        **values,
                        **_pair_json(pair),
                        "policy": {
                            "policy_id": APP_RBAC_DEFAULT_ACCESS_POLICY_ID,
                            "account_ids": [values["account_id"]],
                        },
                    }
                )
                targets.append(
                    ResourceGrantTargetCandidate(
                        pair[0], pair[1], scope, _hash("scope", scope), _hash("idempotency", scope)
                    )
                )
        plan = _canonical(
            {
                **fixed,
                **values,
                "inventory": [_pair_json(pair) for pair in inventory.resources],
                "observations": [{**_pair_json(pair), "observation": state.value} for pair, state in vector],
                "observation_metadata": {
                    "context_id": _uuid(context_id) if context_id is not None else None,
                    "attempted_queries": inventory.attempted_queries,
                    "validated_account_counts": counts,
                },
                "targets": [json.loads(target.canonical_scope) for target in targets],
            }
        )
        return ResourceGrantPlanCandidate(
            **{item.name: getattr(context, item.name) for item in fields(ResourceGrantCandidateContext)},
            inventory=inventory.resources,
            observations=tuple(vector),
            attempted_queries=inventory.attempted_queries,
            context_id=context_id,
            validated_account_counts=tuple(counts),
            targets=tuple(targets),
            canonical_plan=plan,
            plan_sha256=_hash("plan", plan),
        )
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        raise ResourceGrantCandidateError() from None
