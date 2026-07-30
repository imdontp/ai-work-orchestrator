"""Workflow graph loading and validation.

A workflow is a small DAG of nodes, each producing one deliverable artifact. This
module turns the YAML in ``workflows/`` into a typed model and enforces the rules in
``docs/WORKFLOW_SPEC.md`` before anything is executed. A graph that cannot be validated
must not start: a cycle or a missing dependency discovered halfway through a run has
already spent quota and possibly written files.

Provider-agnostic by construction — a node names a *worker requirement*, never a CLI.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class WorkflowError(ValueError):
    """The workflow graph is malformed, so no run may be started from it."""


class WorkspaceKind(StrEnum):
    #: The node runs against the primary checkout, read-only.
    SHARED_READ_ONLY = "shared_read_only"
    #: The node gets its own git worktree under containment. Required for write tasks.
    ISOLATED_WORKTREE = "isolated_worktree"


class SessionPolicy(StrEnum):
    #: Reuse this run's session for the node's worker when one exists.
    REUSE = "reuse"
    #: Always start a fresh session. Independent review depends on this.
    NEW = "new"


class ApprovalKind(StrEnum):
    PLAN = "plan"
    REVISION = "revision"
    FINAL_RESULT = "final_result"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


#: Worker requirements the runner satisfies itself rather than by calling an adapter.
BUILTIN_REQUIREMENTS = frozenset({"local_tool_runner", "human"})


class WorkflowNode(BaseModel):
    model_config = {"frozen": True}

    id: str = Field(min_length=1)
    agent_profile: str = Field(min_length=1)
    worker_requirement: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    #: Every node has a deliverable. A node that produces nothing cannot be verified
    #: and cannot be handed to the next node.
    expected_artifact: str = Field(min_length=1)
    approval_after: ApprovalKind | None = None
    workspace: WorkspaceKind = WorkspaceKind.SHARED_READ_ONLY
    session_policy: SessionPolicy = SessionPolicy.REUSE

    @property
    def is_human(self) -> bool:
        return self.worker_requirement == "human"

    @property
    def is_builtin(self) -> bool:
        return self.worker_requirement in BUILTIN_REQUIREMENTS

    @property
    def needs_worktree(self) -> bool:
        return self.workspace is WorkspaceKind.ISOLATED_WORKTREE

    @model_validator(mode="after")
    def _reject_self_dependency(self) -> WorkflowNode:
        if self.id in self.depends_on:
            raise ValueError(f"node {self.id} depends on itself")
        return self


class WorkflowDefinition(BaseModel):
    model_config = {"frozen": True}

    schema_version: str = "1.0"
    workflow_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""
    max_repair_rounds: int = Field(default=2, ge=0, le=5)
    nodes: tuple[WorkflowNode, ...] = Field(min_length=1)

    @property
    def nodes_by_id(self) -> dict[str, WorkflowNode]:
        return {node.id: node for node in self.nodes}

    def node(self, node_id: str) -> WorkflowNode:
        try:
            return self.nodes_by_id[node_id]
        except KeyError:
            raise WorkflowError(f"unknown node: {node_id}") from None

    def dependents_of(self, node_id: str) -> tuple[WorkflowNode, ...]:
        return tuple(node for node in self.nodes if node_id in node.depends_on)

    def execution_order(self) -> tuple[str, ...]:
        """Topological order. Ties broken by declaration order so runs are repeatable."""
        remaining = {node.id: set(node.depends_on) for node in self.nodes}
        declared = [node.id for node in self.nodes]
        ordered: list[str] = []

        while remaining:
            ready = [
                node_id
                for node_id in declared
                if node_id in remaining and not remaining[node_id]
            ]
            if not ready:
                raise WorkflowError(f"dependency cycle among: {sorted(remaining)}")
            for node_id in ready:
                ordered.append(node_id)
                del remaining[node_id]
            for pending in remaining.values():
                pending.difference_update(ready)

        return tuple(ordered)

    @property
    def has_write_node(self) -> bool:
        """Does any node in this graph write to a worktree?

        When one does, every node in the run reads that worktree, so they all reason
        about the same revision. When none does, there is nothing to isolate and the
        primary checkout is read directly.
        """
        return any(node.needs_worktree for node in self.nodes)

    def depends_on_write_node(self, node_id: str) -> bool:
        """Does this node transitively depend on one that writes to a worktree?

        Such a node has to read the worktree, not the primary checkout, or it is
        looking at code that predates the work it was asked about. A live run caught
        the reviewer doing exactly that.
        """
        seen: set[str] = set()
        frontier = list(self.node(node_id).depends_on)
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            node = self.node(current)
            if node.needs_worktree:
                return True
            frontier.extend(node.depends_on)
        return False

    def reaches_approval(self, node_id: str) -> bool:
        """Does an approval gate sit at or downstream of this node?"""
        seen: set[str] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            node = self.node(current)
            if node.approval_after is not None or node.is_human:
                return True
            frontier.extend(dependent.id for dependent in self.dependents_of(current))
        return False

    @model_validator(mode="after")
    def _validate_graph(self) -> WorkflowDefinition:
        ids = [node.id for node in self.nodes]
        duplicates = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate node ids: {duplicates}")

        known = set(ids)
        for node in self.nodes:
            missing = sorted(set(node.depends_on) - known)
            if missing:
                raise ValueError(f"node {node.id} depends on unknown nodes: {missing}")

        # Raises on a cycle.
        self.execution_order()

        # "Every high-risk side effect has an approval node." A node that writes to a
        # worktree is the high-risk case this milestone has, so require that its output
        # cannot reach the end of the run without a human seeing it.
        unapproved = [
            node.id
            for node in self.nodes
            if node.needs_worktree and not self.reaches_approval(node.id)
        ]
        if unapproved:
            raise ValueError(
                f"write nodes with no approval gate downstream: {sorted(unapproved)}"
            )

        return self


def load_workflow(path: Path) -> WorkflowDefinition:
    """Load and validate a workflow file."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkflowError(f"{path} does not contain a workflow mapping")

    try:
        return WorkflowDefinition.model_validate(raw)
    except ValueError as exc:
        raise WorkflowError(f"{path} is not a valid workflow: {exc}") from exc
