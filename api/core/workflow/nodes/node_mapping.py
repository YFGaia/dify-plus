from collections.abc import Mapping

from core.workflow.enums import NodeType
from core.workflow.nodes.base.node import Node

LATEST_VERSION = "latest"


class _LazyNodeTypeClassesMapping(Mapping[NodeType, Mapping[str, type[Node]]]):
    """
    Lazy wrapper so the mapping is built on first access, not at import time.
    Avoids circular import when modules under core.workflow.nodes (e.g. node_factory)
    import from node_mapping.
    """

    _cache: Mapping[NodeType, Mapping[str, type[Node]]] | None = None

    def _get(self) -> Mapping[NodeType, Mapping[str, type[Node]]]:
        if self._cache is None:
            self._cache = Node.get_node_type_classes_mapping()
        return self._cache

    def __getitem__(self, key: NodeType) -> Mapping[str, type[Node]]:
        return self._get().__getitem__(key)

    def __iter__(self):
        return iter(self._get())

    def __len__(self) -> int:
        return len(self._get())

    def __contains__(self, key: object) -> bool:
        return key in self._get()


# Built on first access to avoid circular import with node_factory and other dependents.
NODE_TYPE_CLASSES_MAPPING: Mapping[NodeType, Mapping[str, type[Node]]] = _LazyNodeTypeClassesMapping()
