import sys as _avatar_sys
from importlib.machinery import EXTENSION_SUFFIXES as _avatar_extension_suffixes
from importlib.machinery import ExtensionFileLoader as _AvatarExtensionFileLoader
from importlib.metadata import distribution as _avatar_distribution
from types import BuiltinFunctionType as _AvatarBuiltinFunctionType
from types import MappingProxyType as _AvatarMappingProxyType
from types import MethodDescriptorType as _AvatarMethodDescriptorType
from types import WrapperDescriptorType as _AvatarWrapperDescriptorType


import logging
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any, override

import opendal
from dotenv import dotenv_values
from opendal import Operator

from extensions.storage.base_storage import BaseStorage

logger = logging.getLogger(__name__)


def _get_opendal_kwargs(*, scheme: str, env_file_path: str = ".env", prefix: str = "OPENDAL_"):
    kwargs = {}
    config_prefix = prefix + scheme.upper() + "_"
    for key, value in os.environ.items():
        if key.startswith(config_prefix):
            kwargs[key[len(config_prefix) :].lower()] = value

    file_env_vars: dict[str, Any] = dotenv_values(env_file_path) or {}
    for key, value in file_env_vars.items():
        if key.startswith(config_prefix) and key[len(config_prefix) :].lower() not in kwargs and value:
            kwargs[key[len(config_prefix) :].lower()] = value

    return kwargs


class OpenDALStorage(BaseStorage):
    def __init__(self, scheme: str, **kwargs):
        kwargs = kwargs or _get_opendal_kwargs(scheme=scheme)

        if scheme == "fs":
            root = kwargs.setdefault("root", "storage")
            Path(root).mkdir(parents=True, exist_ok=True)

        retry_layer = opendal.layers.RetryLayer(max_times=3, factor=2.0, jitter=True)
        self.op = Operator(scheme=scheme, **kwargs).layer(retry_layer)
        from core.casdoor.avatar_cleanup_provider import _register_native_avatar_provider

        _register_native_avatar_provider(self, scheme, kwargs, retry_layer)
        logger.debug("opendal operator created with scheme %s", scheme)
        logger.debug("added retry layer to opendal operator")

    @override
    def save(self, filename: str, data: bytes):
        self.op.write(path=filename, bs=data)
        logger.debug("file %s saved", filename)

    @override
    def load_once(self, filename: str) -> bytes:
        if not self.exists(filename):
            raise FileNotFoundError("File not found")

        content: bytes = self.op.read(path=filename)
        logger.debug("file %s loaded", filename)
        return content

    @override
    def load_stream(self, filename: str) -> Generator:
        if not self.exists(filename):
            raise FileNotFoundError("File not found")

        batch_size = 4096
        with self.op.open(
            path=filename,
            mode="rb",
            chunck=batch_size,
        ) as file:
            while chunk := file.read(batch_size):
                yield chunk
        logger.debug("file %s loaded as stream", filename)

    @override
    def download(self, filename: str, target_filepath: str):
        if not self.exists(filename):
            raise FileNotFoundError("File not found")

        Path(target_filepath).write_bytes(self.op.read(path=filename))
        logger.debug("file %s downloaded to %s", filename, target_filepath)

    @override
    def exists(self, filename: str) -> bool:
        return self.op.exists(path=filename)

    @override
    def delete(self, filename: str):
        if self.exists(filename):
            self.op.delete(path=filename)
            logger.debug("file %s deleted", filename)
            return
        logger.debug("file %s not found, skip delete", filename)

    @override
    def scan(self, path: str, files: bool = True, directories: bool = False) -> list[str]:
        if not self.exists(path):
            raise FileNotFoundError("Path not found")

        # Use the new OpenDAL 0.46.0+ API with recursive listing
        lister = self.op.list(path, recursive=True)
        if files and directories:
            logger.debug("files and directories on %s scanned", path)
            return [entry.path for entry in lister]
        if files:
            logger.debug("files on %s scanned", path)
            return [entry.path for entry in lister if not entry.metadata.is_dir]
        elif directories:
            logger.debug("directories on %s scanned", path)
            return [entry.path for entry in lister if entry.metadata.is_dir]
        else:
            raise ValueError("At least one of files or directories must be True")


_AVATAR_OPENDAL_CONSTRUCTOR = OpenDALStorage.__init__
_AVATAR_OPENDAL_NOT_FOUND = opendal.exceptions.NotFound
_AVATAR_OPENDAL_OWNER_METHODS = {
    name: getattr(OpenDALStorage, name) for name in ("save", "load_stream", "delete", "exists")
}


# Capture before the constructor's lazy provider import. Native descriptors are
# mutable: timing alone cannot establish that a captured method is genuine.

_AVATAR_OPENDAL_NATIVE = _avatar_sys.modules.get("opendal._opendal")
_AVATAR_OPENDAL_NATIVE_SPEC = getattr(_AVATAR_OPENDAL_NATIVE, "__spec__", None)
_AVATAR_OPENDAL_PACKAGE_SPEC = opendal.__spec__
_AVATAR_OPENDAL_NATIVE_LOADER = getattr(_AVATAR_OPENDAL_NATIVE, "__loader__", None)
_AVATAR_OPENDAL_OPERATOR = Operator
_AVATAR_OPENDAL_RETRY_LAYER = opendal.layers.RetryLayer
_AVATAR_OPENDAL_LAYER_BASE = opendal.layers.Layer
_AVATAR_OPENDAL_NATIVE_LAYERS = opendal.layers
_AVATAR_OPENDAL_NATIVE_EXCEPTIONS = opendal.exceptions
_AVATAR_OPENDAL_NATIVE_CONSTRUCTORS = _AvatarMappingProxyType(
    {
        owner: owner.__new__
        for owner in (Operator, _AVATAR_OPENDAL_RETRY_LAYER, _AVATAR_OPENDAL_LAYER_BASE)
    }
)
_AVATAR_OPENDAL_NATIVE_INITIALIZERS = _AvatarMappingProxyType(
    {
        owner: owner.__init__
        for owner in (Operator, _AVATAR_OPENDAL_RETRY_LAYER, _AVATAR_OPENDAL_LAYER_BASE)
    }
)
_AVATAR_OPENDAL_OPERATOR_REPR = Operator.__repr__
_AVATAR_OPENDAL_NATIVE_METHODS = _AvatarMappingProxyType(
    {
        name: getattr(Operator, name, None)
        for name in (
            "layer",
            "open",
            "read",
            "write",
            "stat",
            "copy",
            "rename",
            "remove_all",
            "create_dir",
            "delete",
            "exists",
            "list",
            "scan",
            "capability",
            "check",
            "to_async_operator",
            "__getnewargs_ex__",
        )
    }
)
try:
    _avatar_dist = _avatar_distribution("opendal")
    _AVATAR_OPENDAL_NATIVE_ORIGIN = str(
        Path(_AVATAR_OPENDAL_NATIVE_SPEC.origin).resolve()
    )
    _AVATAR_OPENDAL_NATIVE_SOURCE_VALID = (
        _avatar_dist.version == "0.46.0"
        and type(_AVATAR_OPENDAL_NATIVE_LOADER) is _AvatarExtensionFileLoader
        and _AVATAR_OPENDAL_NATIVE_LOADER.name == "opendal._opendal"
        and str(Path(_AVATAR_OPENDAL_NATIVE_LOADER.path).resolve())
        == _AVATAR_OPENDAL_NATIVE_ORIGIN
        and any(
            _AVATAR_OPENDAL_NATIVE_ORIGIN.endswith(suffix)
            for suffix in _avatar_extension_suffixes
        )
        and Path(_AVATAR_OPENDAL_NATIVE_ORIGIN).parent
        == Path(_avatar_dist.locate_file("opendal")).resolve()
        and Path(opendal.__file__).resolve()
        == Path(_avatar_dist.locate_file("opendal/__init__.py")).resolve()
        and opendal._opendal is _AVATAR_OPENDAL_NATIVE
        and _AVATAR_OPENDAL_NATIVE.Operator is Operator is opendal.Operator
        and _AVATAR_OPENDAL_NATIVE.layers is opendal.layers
        and _AVATAR_OPENDAL_NATIVE.exceptions is opendal.exceptions
        and Operator.__module__ == "opendal"
        and Operator.__name__ == "Operator"
        and Operator.__bases__ == (object,)
        and not Operator.__flags__
        & (1 << 10)  # Original PyO3 Operator is not subclassable.
        and _AVATAR_OPENDAL_RETRY_LAYER.__module__ == "opendal.layers"
        and _AVATAR_OPENDAL_RETRY_LAYER.__name__ == "RetryLayer"
        and _AVATAR_OPENDAL_RETRY_LAYER.__bases__ == (_AVATAR_OPENDAL_LAYER_BASE,)
        and not _AVATAR_OPENDAL_RETRY_LAYER.__flags__ & (1 << 10)
        and _AVATAR_OPENDAL_NOT_FOUND.__module__ == "opendal.exceptions"
        and _AVATAR_OPENDAL_NOT_FOUND.__name__ == "NotFound"
        and _AVATAR_OPENDAL_NOT_FOUND.__bases__ == (opendal.exceptions.Error,)
        and all(
            type(constructor) is _AvatarBuiltinFunctionType
            and constructor.__name__ == "__new__"
            and constructor.__self__ is owner
            for owner, constructor in _AVATAR_OPENDAL_NATIVE_CONSTRUCTORS.items()
        )
        and all(
            initializer is object.__init__
            for initializer in _AVATAR_OPENDAL_NATIVE_INITIALIZERS.values()
        )
        and type(_AVATAR_OPENDAL_OPERATOR_REPR) is _AvatarWrapperDescriptorType
        and _AVATAR_OPENDAL_OPERATOR_REPR.__name__ == "__repr__"
        and _AVATAR_OPENDAL_OPERATOR_REPR.__objclass__ is Operator
        and all(
            type(method) is _AvatarMethodDescriptorType
            and method.__name__ == name
            and method.__objclass__ is Operator
            for name, method in _AVATAR_OPENDAL_NATIVE_METHODS.items()
        )
    )
except (AttributeError, TypeError, ValueError, OSError, ImportError):
    _AVATAR_OPENDAL_NATIVE_ORIGIN = None
    _AVATAR_OPENDAL_NATIVE_SOURCE_VALID = False
