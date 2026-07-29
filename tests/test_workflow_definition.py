"""Workflow graph loading and validation.

A malformed graph must be rejected before a run starts. Discovering a cycle halfway
through has already spent quota and possibly written files.
"""

from pathlib import Path

import pytest
import yaml

from orchestrator.workflow.definition import (
    ApprovalKind,
    SessionPolicy,
    WorkflowDefinition,
    WorkflowError,
    WorkspaceKind,
    load_workflow,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_WORKFLOW = REPO_ROOT / "workflows" / "analyze-implement-review.yaml"


def _graph(**overrides) -> dict:
    base = {
        "workflow_id": "test",
        "version": "0.1.0",
        "nodes": [
            {
                "id": "a",
                "agent_profile": "p",
                "worker_requirement": "claude_code",
                "expected_artifact": "a.json",
            },
            {
                "id": "b",
                "agent_profile": "p",
                "worker_requirement": "human",
                "depends_on": ["a"],
                "expected_artifact": "b.json",
            },
        ],
    }
    base.update(overrides)
    return base


def _build(**overrides) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(_graph(**overrides))


# ---------------------------------------------------------------------------
# The workflow this repository actually ships
# ---------------------------------------------------------------------------


def test_the_shipped_workflow_validates() -> None:
    workflow = load_workflow(SHIPPED_WORKFLOW)

    assert workflow.workflow_id == "analyze-implement-review"
    assert workflow.execution_order() == (
        "analyze",
        "implement",
        "verify",
        "review",
        "final_approval",
    )


def test_the_shipped_workflow_isolates_the_implementation_node() -> None:
    workflow = load_workflow(SHIPPED_WORKFLOW)
    assert workflow.node("implement").needs_worktree is True
    assert workflow.node("analyze").needs_worktree is False


def test_the_shipped_workflow_reviews_in_a_fresh_session() -> None:
    """Independent review is worthless if it inherits the implementer's context."""
    assert load_workflow(SHIPPED_WORKFLOW).node("review").session_policy is SessionPolicy.NEW


def test_the_shipped_workflow_gates_the_plan() -> None:
    assert load_workflow(SHIPPED_WORKFLOW).node("analyze").approval_after is ApprovalKind.PLAN


# ---------------------------------------------------------------------------
# Validation rules from docs/WORKFLOW_SPEC.md
# ---------------------------------------------------------------------------


def test_missing_dependency_is_rejected() -> None:
    graph = _graph()
    graph["nodes"][1]["depends_on"] = ["nope"]
    with pytest.raises(ValueError, match="depends on unknown nodes"):
        WorkflowDefinition.model_validate(graph)


def test_dependency_cycle_is_rejected() -> None:
    graph = _graph()
    graph["nodes"][0]["depends_on"] = ["b"]
    with pytest.raises(ValueError, match="cycle"):
        WorkflowDefinition.model_validate(graph)


def test_self_dependency_is_rejected() -> None:
    graph = _graph()
    graph["nodes"][0]["depends_on"] = ["a"]
    with pytest.raises(ValueError, match="depends on itself"):
        WorkflowDefinition.model_validate(graph)


def test_duplicate_node_ids_are_rejected() -> None:
    graph = _graph()
    # A fresh node rather than renaming `b`, which depends on `a` and would trip the
    # self-dependency check first.
    graph["nodes"].append(
        {
            "id": "a",
            "agent_profile": "p",
            "worker_requirement": "claude_code",
            "expected_artifact": "dup.json",
        }
    )
    with pytest.raises(ValueError, match="duplicate node ids"):
        WorkflowDefinition.model_validate(graph)


def test_a_node_without_a_deliverable_is_rejected() -> None:
    graph = _graph()
    del graph["nodes"][0]["expected_artifact"]
    with pytest.raises(ValueError):
        WorkflowDefinition.model_validate(graph)


def test_a_write_node_with_no_approval_downstream_is_rejected() -> None:
    """AGENTS.md rule 9: a high-risk effect needs a human in the path."""
    graph = {
        "workflow_id": "unsafe",
        "version": "0.1.0",
        "nodes": [
            {
                "id": "implement",
                "agent_profile": "p",
                "worker_requirement": "codex",
                "expected_artifact": "worker-result.json",
                "workspace": "isolated_worktree",
            }
        ],
    }
    with pytest.raises(ValueError, match="no approval gate downstream"):
        WorkflowDefinition.model_validate(graph)


def test_a_write_node_is_accepted_when_an_approval_follows_transitively() -> None:
    graph = {
        "workflow_id": "safe",
        "version": "0.1.0",
        "nodes": [
            {
                "id": "implement",
                "agent_profile": "p",
                "worker_requirement": "codex",
                "expected_artifact": "worker-result.json",
                "workspace": "isolated_worktree",
            },
            {
                "id": "verify",
                "agent_profile": "p",
                "worker_requirement": "local_tool_runner",
                "depends_on": ["implement"],
                "expected_artifact": "verification-result.json",
            },
            {
                "id": "sign_off",
                "agent_profile": "p",
                "worker_requirement": "human",
                "depends_on": ["verify"],
                "expected_artifact": "approval-decision.json",
            },
        ],
    }
    assert WorkflowDefinition.model_validate(graph).node("implement").needs_worktree


def test_execution_order_is_stable_for_independent_nodes() -> None:
    """Ties break on declaration order, so two runs of one graph agree."""
    graph = {
        "workflow_id": "fan",
        "version": "0.1.0",
        "nodes": [
            {"id": "x", "agent_profile": "p", "worker_requirement": "w", "expected_artifact": "x"},
            {"id": "y", "agent_profile": "p", "worker_requirement": "w", "expected_artifact": "y"},
            {
                "id": "z",
                "agent_profile": "p",
                "worker_requirement": "human",
                "depends_on": ["x", "y"],
                "expected_artifact": "z",
            },
        ],
    }
    workflow = WorkflowDefinition.model_validate(graph)
    assert workflow.execution_order() == ("x", "y", "z") == workflow.execution_order()


def test_defaults_are_the_safe_ones() -> None:
    node = _build().node("a")
    assert node.workspace is WorkspaceKind.SHARED_READ_ONLY
    assert node.session_policy is SessionPolicy.REUSE
    assert node.approval_after is None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_rejects_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("nodes: [unclosed", encoding="utf-8")
    with pytest.raises(WorkflowError, match="not valid YAML"):
        load_workflow(path)


def test_load_rejects_a_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(WorkflowError, match="does not contain a workflow mapping"):
        load_workflow(path)


def test_load_wraps_validation_failures_with_the_path(tmp_path: Path) -> None:
    graph = _graph()
    graph["nodes"][1]["depends_on"] = ["nope"]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(graph), encoding="utf-8")

    with pytest.raises(WorkflowError, match="is not a valid workflow"):
        load_workflow(path)
