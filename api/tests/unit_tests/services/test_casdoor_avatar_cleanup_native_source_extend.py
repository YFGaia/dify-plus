"""Actual native fs safety under first-storage-module source contamination."""

import importlib
import json
import sys

import opendal
import pytest
import test_casdoor_avatar_cleanup_flow_extend as flow
from services.file_service import FileService
from test_casdoor_avatar_cleanup_flow_extend import (
    avatar_fixture,
    consumer,
    native,
    storage_fixture,
    telemetry,
)

avatar_fixture = avatar_fixture
consumer = consumer
native = native
storage_fixture = storage_fixture
telemetry = telemetry


def _run_with_original_store_receipt(s):
    """Observe normal save/readback/final close through the held FileService owner."""
    previous = sys.getprofile()
    results = []

    def observe(frame, event, result):
        if (
            event == "return"
            and frame.f_code is FileService.store_reserved_avatar.__code__
        ):
            s.no_sql()
            results.append(result)

    try:
        sys.setprofile(observe)
        outcome = flow.run(s)
    finally:
        sys.setprofile(previous)
    assert results == ["stored"]
    return outcome


@pytest.mark.parametrize(
    "site",
    [
        "python_descriptor",
        "foreign_native_descriptor",
        "operator_export",
        "retry_export",
        "extension_origin",
        "metadata_missing",
    ],
)
def test_before_first_storage_module_import_fails_closed(
    consumer, monkeypatch, tmp_path, site
):
    name = "extensions.storage.opendal_storage"
    provider = "core.casdoor.avatar_cleanup_provider"
    originals = {key: sys.modules.pop(key, None) for key in (name, provider)}
    old_operator = opendal.Operator
    old_retry = opendal.layers.RetryLayer
    try:
        if site == "python_descriptor":
            monkeypatch.setattr(old_operator, "delete", lambda self, path: None)

            def missing(self, path):
                raise opendal.exceptions.NotFound("synthetic replacement")

            monkeypatch.setattr(old_operator, "stat", missing)
        elif site == "foreign_native_descriptor":
            monkeypatch.setattr(old_operator, "stat", opendal.AsyncOperator.stat)
        elif site == "operator_export":

            class FakeOperator:
                def __new__(cls, **kwargs):
                    return old_operator(**kwargs)

            monkeypatch.setattr(opendal, "Operator", FakeOperator)
            monkeypatch.setattr(opendal._opendal, "Operator", FakeOperator)
        elif site == "retry_export":

            class FakeRetry:
                def __new__(cls, **kwargs):
                    return old_retry(**kwargs)

            monkeypatch.setattr(opendal.layers, "RetryLayer", FakeRetry)
        elif site == "extension_origin":
            monkeypatch.setattr(
                opendal._opendal.__spec__,
                "origin",
                "/private/tmp/synthetic-owner.abi3.so",
            )
        elif site == "metadata_missing":
            from importlib import metadata

            original_distribution = metadata.distribution

            def absent(package):
                if package == "opendal":
                    raise metadata.PackageNotFoundError("opendal")
                return original_distribution(package)

            monkeypatch.setattr(metadata, "distribution", absent)
        fresh = importlib.import_module(name)
        assert fresh._AVATAR_OPENDAL_NATIVE_SOURCE_VALID is False
        monkeypatch.setattr(flow, "OpenDALStorage", fresh.OpenDALStorage)
        s = native.__wrapped__(consumer, monkeypatch, tmp_path)
        flow.attachment_fault(s)
        assert _run_with_original_store_receipt(s).code == "unknown"
        flow.assert_retained(s)
        assert json.loads(flow.row(s)["desired_json"])["cleanup_state"] != "complete"
        assert flow.row(s)["termination_proof_kind"] != "avatar_cleanup"
    finally:
        for key, value in originals.items():
            sys.modules.pop(key, None)
            if value is not None:
                sys.modules[key] = value
                parent_name, attribute = key.rsplit(".", 1)
                setattr(sys.modules[parent_name], attribute, value)


@pytest.mark.parametrize(
    "site", ["delete", "stat", "native_export", "native_spec", "metadata_missing"]
)
def test_after_native_registration_source_change_retains_file(
    native, monkeypatch, site
):
    s = native
    flow.attachment_fault(s)

    def mutate():
        if site == "delete":
            monkeypatch.setattr(opendal.Operator, "delete", lambda self, path: None)
        elif site == "stat":
            monkeypatch.setattr(opendal.Operator, "stat", opendal.AsyncOperator.stat)
        elif site == "native_export":
            monkeypatch.setattr(opendal._opendal, "Operator", opendal.AsyncOperator)
        elif site == "native_spec":
            monkeypatch.setattr(
                opendal._opendal.__spec__,
                "origin",
                "/private/tmp/synthetic-owner.abi3.so",
            )
        elif site == "metadata_missing":
            from importlib import metadata

            import core.casdoor.avatar_cleanup_provider as provider

            def absent(*args, **kwargs):
                raise metadata.PackageNotFoundError("opendal")

            monkeypatch.setattr(provider, "version", absent)

    flow.after_store(s, mutate)
    assert _run_with_original_store_receipt(s).code == "unknown"
    flow.assert_retained(s)
    assert json.loads(flow.row(s)["desired_json"])["cleanup_state"] != "complete"
    assert flow.row(s)["termination_proof_kind"] != "avatar_cleanup"
