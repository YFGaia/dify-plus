"""Knowledge index workflow node package."""

# NOTE: the constant must be defined before importing the submodule, because
# knowledge_index_node.py imports KNOWLEDGE_INDEX_NODE_TYPE back from this package.
KNOWLEDGE_INDEX_NODE_TYPE = "knowledge-index"

from .knowledge_index_node import KnowledgeIndexNode

__all__ = ["KNOWLEDGE_INDEX_NODE_TYPE", "KnowledgeIndexNode"]
