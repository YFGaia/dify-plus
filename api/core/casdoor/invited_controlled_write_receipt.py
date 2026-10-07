"""Closed v2 controlled LOCAL facts; shape and bytes confer no authority."""


def validate_controlled_withdrawals(value):
    from core.casdoor.invited_write_receipt import _closed, _counter, _require, _uuid

    withdrawals = value["withdrawals"]
    _require(type(withdrawals) is list and len(withdrawals) <= 100)
    fields = (
        "workspace_id",
        "membership_id",
        "removed_join_id",
        "prior_role",
        "epoch_before",
        "epoch_after",
        "generation",
        "fence_epoch",
    )
    for item in withdrawals:
        _closed(item, fields)
        for field in ("workspace_id", "membership_id", "removed_join_id"):
            _uuid(item[field])
        for field in ("epoch_before", "epoch_after", "generation", "fence_epoch"):
            _counter(item[field])
        _require(type(item["prior_role"]) is str and item["prior_role"] in ("admin", "editor", "normal"))
        _require(item["epoch_after"] == item["epoch_before"] + 1)
        _require(item["generation"] == value["generation_after"] and item["fence_epoch"] == value["fence_epoch"])
        _require(item["workspace_id"] != value["references"]["workspace_id"])
    workspaces = [item["workspace_id"] for item in withdrawals]
    _require(workspaces == sorted(set(workspaces)))
    for field in ("membership_id", "removed_join_id"):
        _require(len({item[field] for item in withdrawals}) == len(withdrawals))
    _require(set(workspaces).isdisjoint(item["workspace_id"] for item in value["results"]))
    _require(
        {item["membership_id"] for item in withdrawals}.isdisjoint(
            item["membership_id"] for item in value["results"] if item["membership_id"] is not None
        )
    )
    _require({item["removed_join_id"] for item in withdrawals}.isdisjoint(item["join_id"] for item in value["results"]))
    _require(bool(withdrawals) or any(item["membership_regranted"] for item in value["results"]))
    _require(len(controlled_finalization_ids(value)) <= 100)


def controlled_finalization_ids(value):
    """Exactly pending rows declared by the strict original producing receipt."""
    ids = [
        item["membership_id"] for item in value["results"] if item["membership_created"] or item["membership_regranted"]
    ]
    ids.extend(item["membership_id"] for item in value.get("withdrawals", ()))
    if len(ids) != len(set(ids)):
        raise ValueError("invited_write_receipt_invalid")
    return sorted(ids)
