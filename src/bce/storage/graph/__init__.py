"""Apache AGE graph access: a thin Cypher client plus a typed repository.

The client is intentionally small and Cypher-based so the backend can later be swapped (Kuzu /
Ladybug) behind the same interface if AGE deep-traversal performance ever demands it - the rest of
the engine only depends on the repository methods, not on AGE specifics.
"""

from bce.storage.graph.client import GraphClient
from bce.storage.graph.repository import GraphRepository

__all__ = ["GraphClient", "GraphRepository"]
