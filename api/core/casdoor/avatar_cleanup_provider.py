"""Private native fs completion domain; never a provider-independent capability.

OpenDAL Python 0.46.0/core 0.54.0 blocking write drains and closes the fs
writer on success. Only the original root-only constructor/default executor
and RetryLayer are admitted. Error/late/cancel and remote schemes are excluded.
External filesystem mutation, SQL and plugin writers are outside this domain.
"""

from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from weakref import WeakKeyDictionary, ref

import opendal
from extensions.ext_storage import _AVATAR_STORAGE_OWNER_METHODS, Storage, storage
from extensions.storage.opendal_storage import (
    _AVATAR_OPENDAL_LAYER_BASE,
    _AVATAR_OPENDAL_NATIVE,
    _AVATAR_OPENDAL_NATIVE_CONSTRUCTORS,
    _AVATAR_OPENDAL_NATIVE_EXCEPTIONS,
    _AVATAR_OPENDAL_NATIVE_INITIALIZERS,
    _AVATAR_OPENDAL_NATIVE_LAYERS,
    _AVATAR_OPENDAL_NATIVE_LOADER,
    _AVATAR_OPENDAL_NATIVE_METHODS,
    _AVATAR_OPENDAL_NATIVE_ORIGIN,
    _AVATAR_OPENDAL_NATIVE_SOURCE_VALID,
    _AVATAR_OPENDAL_NATIVE_SPEC,
    _AVATAR_OPENDAL_NOT_FOUND,
    _AVATAR_OPENDAL_OPERATOR,
    _AVATAR_OPENDAL_OPERATOR_REPR,
    _AVATAR_OPENDAL_PACKAGE_SPEC,
    _AVATAR_OPENDAL_RETRY_LAYER,
)

_OPERATOR = _AVATAR_OPENDAL_OPERATOR
_LAYER = _AVATAR_OPENDAL_RETRY_LAYER
_NOT_FOUND = _AVATAR_OPENDAL_NOT_FOUND
_OPERATOR_METHODS = _AVATAR_OPENDAL_NATIVE_METHODS
_STORAGE_METHODS = dict(_AVATAR_STORAGE_OWNER_METHODS)
_domains = WeakKeyDictionary()


def _native_anchors_valid():
    """Fixed source and export identities cannot be minted by lazy import."""
    import sys

    try:
        return (
            _AVATAR_OPENDAL_NATIVE_SOURCE_VALID
            and sys.modules.get("opendal") is opendal
            and sys.modules.get("opendal._opendal") is _AVATAR_OPENDAL_NATIVE
            and opendal.__spec__ is _AVATAR_OPENDAL_PACKAGE_SPEC
            and opendal._opendal is _AVATAR_OPENDAL_NATIVE
            and _AVATAR_OPENDAL_NATIVE.__spec__ is _AVATAR_OPENDAL_NATIVE_SPEC
            and _AVATAR_OPENDAL_NATIVE.__loader__ is _AVATAR_OPENDAL_NATIVE_LOADER
            and _AVATAR_OPENDAL_NATIVE_SPEC.loader is _AVATAR_OPENDAL_NATIVE_LOADER
            and _AVATAR_OPENDAL_NATIVE_SPEC.origin == _AVATAR_OPENDAL_NATIVE_ORIGIN
            and _AVATAR_OPENDAL_NATIVE.__file__ == _AVATAR_OPENDAL_NATIVE_ORIGIN
            and _AVATAR_OPENDAL_NATIVE_LOADER.path == _AVATAR_OPENDAL_NATIVE_ORIGIN
            and _AVATAR_OPENDAL_NATIVE_LOADER.name == "opendal._opendal"
            and _AVATAR_OPENDAL_NATIVE.Operator is opendal.Operator is _OPERATOR
            and _AVATAR_OPENDAL_NATIVE.layers
            is opendal.layers
            is _AVATAR_OPENDAL_NATIVE_LAYERS
            and _AVATAR_OPENDAL_NATIVE.exceptions
            is opendal.exceptions
            is _AVATAR_OPENDAL_NATIVE_EXCEPTIONS
            and opendal.layers.RetryLayer is _LAYER
            and opendal.layers.Layer is _AVATAR_OPENDAL_LAYER_BASE
            and opendal.exceptions.NotFound is _NOT_FOUND
            and version("opendal") == "0.46.0"
            and _OPERATOR.__repr__ is _AVATAR_OPENDAL_OPERATOR_REPR
            and all(
                owner.__init__ is initializer
                for owner, initializer in _AVATAR_OPENDAL_NATIVE_INITIALIZERS.items()
            )
            and all(
                owner.__new__ is constructor
                for owner, constructor in _AVATAR_OPENDAL_NATIVE_CONSTRUCTORS.items()
            )
            and all(
                getattr(_OPERATOR, name, None) is owner
                for name, owner in _OPERATOR_METHODS.items()
            )
        )
    except (AttributeError, TypeError, ValueError, ImportError):
        return False


@dataclass(frozen=True, repr=False)
class _NativeAvatarDomain:
    runner: object
    operator: object
    root: str
    methods: tuple


def _register_native_avatar_provider(runner, scheme, kwargs, retry_layer):
    """Called only at the original constructor after its operator/layer creation."""
    import inspect

    from extensions.storage.opendal_storage import (
        _AVATAR_OPENDAL_CONSTRUCTOR,
        _AVATAR_OPENDAL_OWNER_METHODS,
        OpenDALStorage,
        Operator,
    )

    frame = inspect.currentframe()
    try:
        if frame.f_back.f_code is not _AVATAR_OPENDAL_CONSTRUCTOR.__code__:
            return
    finally:
        del frame

    if (
        _native_anchors_valid()
        and type(runner) is OpenDALStorage
        and scheme == "fs"
        and set(kwargs) == {"root"}
        and type(kwargs["root"]) is str
        and 0 < len(kwargs["root"].encode()) <= 4096
        and Operator is _OPERATOR
        and opendal.Operator is _OPERATOR
        and type(runner.op) is _OPERATOR
        and type(retry_layer) is _LAYER
        and opendal.layers.RetryLayer is _LAYER
        and version("opendal") == "0.46.0"
        and opendal.exceptions.NotFound is _NOT_FOUND
        and all(
            getattr(_OPERATOR, name) is owner
            for name, owner in _OPERATOR_METHODS.items()
        )
        and OpenDALStorage.__init__ is _AVATAR_OPENDAL_CONSTRUCTOR
        and all(
            getattr(OpenDALStorage, name) is owner
            for name, owner in _AVATAR_OPENDAL_OWNER_METHODS.items()
        )
        and all(
            getattr(Storage, name) is owner for name, owner in _STORAGE_METHODS.items()
        )
    ):
        _domains[runner] = _NativeAvatarDomain(
            ref(runner),
            runner.op,
            str(Path(kwargs["root"]).resolve()),
            tuple(_AVATAR_OPENDAL_OWNER_METHODS.items()),
        )


def _native_avatar_domain():
    """Recheck identities rather than trusting caller attributes or configuration."""
    runner = getattr(storage, "storage_runner", None)
    try:
        domain = _domains.get(runner)
    except TypeError:
        return None
    if domain is None:
        return None
    from extensions.storage.opendal_storage import (
        _AVATAR_OPENDAL_CONSTRUCTOR,
        OpenDALStorage,
    )

    if (
        not _native_anchors_valid()
        or type(runner) is not OpenDALStorage
        or OpenDALStorage.__init__ is not _AVATAR_OPENDAL_CONSTRUCTOR
        or runner.op is not domain.operator
        or opendal.Operator is not _OPERATOR
        or opendal.layers.RetryLayer is not _LAYER
        or version("opendal") != "0.46.0"
        or opendal.exceptions.NotFound is not _NOT_FOUND
        or any(
            getattr(_OPERATOR, name) is not owner
            for name, owner in _OPERATOR_METHODS.items()
        )
        or any(
            getattr(Storage, name) is not owner or name in storage.__dict__
            for name, owner in _STORAGE_METHODS.items()
        )
        or any(
            getattr(OpenDALStorage, name) is not owner or name in runner.__dict__
            for name, owner in domain.methods
        )
    ):
        return None
    return domain


def _delete_native_avatar(domain, reservation):
    """Exact original delete plus a fresh blocking stat, with no retry by this owner."""
    import inspect

    from core.casdoor.avatar_termination import _CLEANUP_FILE_OWNER

    frame = inspect.currentframe()
    try:
        if frame.f_back.f_code is not _CLEANUP_FILE_OWNER.__code__:
            raise ValueError("avatar_cleanup_invalid")
    finally:
        del frame
    if _native_avatar_domain() is not domain:
        return False
    storage.delete(reservation.storage_key)
    if _native_avatar_domain() is not domain:
        return False
    # Stat is a distinct fresh native observation; only genuine NotFound means absence.
    try:
        domain.operator.stat(reservation.storage_key)
    except _NOT_FOUND:
        return _native_avatar_domain() is domain
    return False
