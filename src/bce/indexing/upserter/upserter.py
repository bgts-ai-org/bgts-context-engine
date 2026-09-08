"""Graph write operations for indexing.

In Phase 0 this is used for full indexing. The ``delete_file_subgraph`` + ``upsert_file_fragment``
pair is exactly the incremental "delete-then-rewrite the changed file's subgraph" strategy that
Phase 1 drives from git diffs.
"""

from __future__ import annotations

from bce.domain.enums import NodeLabel
from bce.domain.models import GraphFragment, GraphNode
from bce.storage.graph.repository import GraphRepository


class Upserter:
    def __init__(self, repository: GraphRepository) -> None:
        self.repository = repository

    def ensure_repo_node(
        self,
        repo_id: str,
        name: str,
        default_branch: str,
        commit: str,
        remote_url: str | None = None,
    ) -> None:
        properties: dict[str, object] = {
            "name": name,
            "default_branch": default_branch,
            "last_indexed_commit": commit,
        }
        if remote_url:
            properties["remote_url"] = remote_url
        self.repository.upsert_node(GraphNode(NodeLabel.REPO, repo_id, properties))

    def upsert_file_fragment(self, fragment: GraphFragment) -> None:
        self.repository.upsert_fragment(fragment)

    def delete_file_subgraph(self, file_id: str) -> None:
        self.repository.delete_file_subgraph(file_id)
