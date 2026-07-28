"""Deciding what order to apply an inverse in.

Reversing a set of row changes is not just "run them backwards". The safe order
depends on the action, because foreign keys point one way:

1. Rows the operation *inserted* must be removed children-first.
2. Rows the operation *deleted* must be restored parents-first.
3. Updates go last, once every row they might reference exists again.

Within a phase, tables are ordered by their position in the foreign-key graph.
Engines supply that ranking; the sort itself is the same everywhere.
"""

from __future__ import annotations

from typing import Callable, Iterable, Sequence, TypeVar

from .model import DELETE, INSERT, UPDATE, RowVerdict

T = TypeVar("T")

PHASE = {INSERT: 0, DELETE: 1, UPDATE: 2}


def topological_rank(
    nodes: Iterable[T], edges: Iterable[tuple[T, T]]
) -> dict[T, int]:
    """Rank nodes so parents come before children.

    ``edges`` are ``(parent, child)`` pairs. Nodes in a cycle all land on the
    final rank; callers are expected to have a retry path for those.
    """
    nodes = list(nodes)
    node_set = set(nodes)
    children: dict[T, set[T]] = {n: set() for n in nodes}
    indegree: dict[T, int] = {n: 0 for n in nodes}

    for parent, child in edges:
        if parent in node_set and child in node_set and parent != child:
            if child not in children[parent]:
                children[parent].add(child)
                indegree[child] += 1

    rank: dict[T, int] = {}
    queue = sorted((n for n in nodes if indegree[n] == 0), key=repr)
    level = 0
    while queue:
        nxt: list[T] = []
        for node in queue:
            rank[node] = level
            for child in children[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    nxt.append(child)
        queue = sorted(nxt, key=repr)
        level += 1

    for node in nodes:
        rank.setdefault(node, level)
    return rank


def order_verdicts(
    verdicts: Sequence[RowVerdict],
    rank: dict[tuple[str, str], int],
    key: Callable[[RowVerdict], tuple[str, str]] | None = None,
) -> list[RowVerdict]:
    """Sort captured changes into the order their inverses should be applied."""
    if key is None:
        def key(v: RowVerdict) -> tuple[str, str]:
            return (v.change.table_schema, v.change.table_name)

    def sort_key(v: RowVerdict):
        table = key(v)
        phase = PHASE[v.change.action]
        table_rank = rank.get(table, 0)
        if phase == PHASE[INSERT]:
            # Deleting what was inserted: children (deepest) first, newest row
            # first within a table.
            return (phase, -table_rank, -v.change.seq)
        # Restoring or reverting: parents first, oldest row first.
        return (phase, table_rank, v.change.seq)

    return sorted(verdicts, key=sort_key)
