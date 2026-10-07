"""Fresh native-owner anchors must predate provider-method replacement."""

import json
import sys

import opendal

from test_casdoor_avatar_cleanup_flow_extend import (
    avatar_fixture,
    attachment_fault,
    consumer,
    native,
    reservation,
    row,
    run,
    storage_fixture,
    telemetry,
)

avatar_fixture = avatar_fixture
consumer = consumer
native = native
storage_fixture = storage_fixture
telemetry = telemetry


def test_native_operator_method_replacement_before_lazy_provider_import_is_not_trusted(
    consumer, monkeypatch, tmp_path
):
    """A patched native delete/stat pair must not certify physical absence."""
    genuine_not_found = opendal.exceptions.NotFound

    def skip_delete(self, path):
        return None

    def false_missing(self, path):
        raise genuine_not_found("synthetic replacement descriptor")

    original_delete = opendal.Operator.delete
    original_stat = opendal.Operator.stat
    provider_name = "core.casdoor.avatar_cleanup_provider"
    prior_provider = sys.modules.pop(provider_name, None)
    try:
        monkeypatch.setattr(opendal.Operator, "delete", skip_delete)
        monkeypatch.setattr(opendal.Operator, "stat", false_missing)

        # The production owner imports this module lazily from its genuine constructor.
        s = native.__wrapped__(consumer, monkeypatch, tmp_path)
        attachment_fault(s)
        outcome = run(s)
        key = reservation(s)["storage_key"]

        assert outcome.code == "unknown"
        assert s.native.exists(key) is True
        assert json.loads(row(s)["desired_json"])["cleanup_state"] != "complete"
        assert row(s)["termination_proof_kind"] != "avatar_cleanup"
    finally:
        monkeypatch.setattr(opendal.Operator, "delete", original_delete)
        monkeypatch.setattr(opendal.Operator, "stat", original_stat)
        sys.modules.pop(provider_name, None)
        if prior_provider is not None:
            sys.modules[provider_name] = prior_provider
