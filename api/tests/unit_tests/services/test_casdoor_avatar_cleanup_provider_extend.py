"""Constructor/provider owner counterexamples and actual native I/O boundary observations."""

import copy
import sys

import pytest
from extensions.ext_storage import Storage, storage
from extensions.storage.opendal_storage import OpenDALStorage
from services.file_service import FileService
from test_casdoor_avatar_cleanup_flow_extend import (
    after_store,
    assert_retained,
    attachment_fault,
    avatar_fixture,
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


def test_pre_first_provider_import_replaced_storage_wrapper_is_not_original(consumer, monkeypatch, tmp_path):
    # Fresh standalone selector runs before any provider import/registration.
    assert "core.casdoor.avatar_cleanup_provider" not in sys.modules
    original = Storage.save

    def replacement(*args, **kwargs):
        return original(*args, **kwargs)

    monkeypatch.setattr(Storage, "save", replacement)
    s = native.__wrapped__(consumer, monkeypatch, tmp_path)
    attachment_fault(s)
    run(s)
    assert_retained(s)


@pytest.mark.parametrize("method", ["save", "load_stream", "delete", "exists"])
def test_preconstruction_replaced_provider_method_cannot_register_as_original(consumer, monkeypatch, tmp_path, method):
    original = getattr(OpenDALStorage, method)

    def replacement(*args, **kwargs):
        return original(*args, **kwargs)

    monkeypatch.setattr(OpenDALStorage, method, replacement)
    s = native.__wrapped__(consumer, monkeypatch, tmp_path)
    attachment_fault(s)
    run(s)
    assert_retained(s)


@pytest.mark.parametrize("method", ["save", "load_stream", "delete", "exists", "__init__"])
def test_registered_original_owner_replacement_denies_cleanup(native, monkeypatch, method):
    s = native
    attachment_fault(s)
    original = getattr(OpenDALStorage, method)

    def replacement(*args, **kwargs):
        return original(*args, **kwargs)

    after_store(s, lambda: monkeypatch.setattr(OpenDALStorage, method, replacement))
    run(s)
    assert_retained(s)


def test_preconstruction_replaced_constructor_denies_registration(consumer, monkeypatch, tmp_path):
    original = OpenDALStorage.__init__

    def replacement(*args, **kwargs):
        return original(*args, **kwargs)

    monkeypatch.setattr(OpenDALStorage, "__init__", replacement)
    s = native.__wrapped__(consumer, monkeypatch, tmp_path)
    attachment_fault(s)
    run(s)
    assert_retained(s)


def test_operator_identity_replacement_after_store_denies_cleanup(native):
    s = native
    attachment_fault(s)
    replacement = OpenDALStorage(scheme="fs", root=str(s.native_root))
    after_store(s, lambda: setattr(s.native, "op", replacement.op))
    run(s)
    assert_retained(s)


def test_package_not_found_type_replacement_cannot_promote_arbitrary_stat_error(native, monkeypatch):
    import opendal

    s = native
    attachment_fault(s)
    after_store(s, lambda: monkeypatch.setattr(opendal.exceptions, "NotFound", Exception))
    run(s)
    assert_retained(s)


def test_preconstruction_extra_fs_options_never_register_as_root_only(consumer, monkeypatch, tmp_path):
    from core.casdoor.avatar_cleanup_provider import _native_avatar_domain

    s = native.__wrapped__(consumer, monkeypatch, tmp_path)
    directory = s.native_root.parent / "atomic"
    directory.mkdir()
    extra = OpenDALStorage(scheme="fs", root=str(s.native_root), atomic_write_dir=str(directory))
    monkeypatch.setattr(storage, "storage_runner", extra)
    assert _native_avatar_domain() is None
    attachment_fault(s)
    run(s)
    assert_retained(s)


def test_constructed_copied_replayed_permits_and_declarations_never_authorize_io(
    native,
):
    from core.casdoor import avatar_termination as owner

    s = native
    attachment_fault(s)
    captured = {}
    previous = sys.getprofile()

    def profile(frame, event, _arg):
        if event == "call":
            if frame.f_code.co_name == "_prepare_avatar_cleanup":
                captured["declaration"] = frame.f_locals["declaration"]
            elif frame.f_code is FileService._cleanup_reserved_avatar.__code__:
                captured["permit"] = frame.f_locals["permit"]
                s.no_sql()
                with pytest.raises(ValueError):
                    owner._consume_avatar_cleanup_io(object.__new__(type(captured["permit"])))

    try:
        sys.setprofile(profile)
        run(s)
    finally:
        sys.setprofile(previous)
    assert row(s)["termination_proof_kind"] == "avatar_cleanup"
    for name, consume in (
        ("declaration", owner._consume_avatar_cleanup),
        ("permit", owner._consume_avatar_cleanup_io),
    ):
        actual = captured[name]
        with pytest.raises(ValueError):
            consume(actual)
        with pytest.raises(ValueError):
            consume(object.__new__(type(actual)))
        with pytest.raises(TypeError):
            type(actual)()
        with pytest.raises(TypeError):
            copy.copy(actual)


@pytest.mark.parametrize("failure", ["none", "after_delete", "stat"])
def test_actual_original_native_delete_and_stat_observed_outside_all_sql_roots(native, failure):
    s = native
    attachment_fault(s)
    previous = sys.getprofile()
    calls = []

    def profile(frame, event, arg):
        if event == "call" and frame.f_code is OpenDALStorage.save.__code__:
            s.no_sql()
            calls.append("save")
        if event in ("c_call", "c_return") and getattr(arg, "__self__", None) is s.native.op:
            name = getattr(arg, "__name__", "")
            if name in ("delete", "stat", "write", "open", "exists"):
                s.no_sql()
                if event == "c_call":
                    calls.append(name)
            if name == "delete" and event == "c_return" and failure == "after_delete":
                raise RuntimeError("synthetic post-native-delete loss")
            if name == "stat" and event == "c_call" and failure == "stat":
                raise RuntimeError("synthetic native-stat observation loss")

    try:
        sys.setprofile(profile)
        run(s)
    finally:
        sys.setprofile(previous)
    assert calls.count("save") == calls.count("write") == calls.count("delete") == 1
    assert s.native.exists(reservation(s)["storage_key"]) is False
    expected = "complete" if failure == "none" else "pending"
    import json

    assert json.loads(row(s)["desired_json"])["cleanup_state"] == expected
    if failure == "none":
        assert calls.count("stat") == 1
    else:
        assert run(s).code == "unknown"
        assert json.loads(row(s)["desired_json"])["cleanup_state"] == "pending"
