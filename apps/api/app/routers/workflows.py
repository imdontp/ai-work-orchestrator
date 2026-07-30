"""The node graph a run is executed through.

Read-only, and static: a workflow is a YAML file the repository ships, validated at
load time. Nothing here can change how a run behaves — it reports the shape that
`orchestrator/workflow/definition.py` already validated.

Why this exists when ADR-011 says the dashboard adds no backend surface: a run record
lists the nodes that have *completed*. It cannot say which nodes exist, how they
depend on each other, which of them is a human gate, or how many there are in total.
Without that, a graph view can only draw the past, and progress cannot be expressed as
a fraction of anything. ADR-012 records the amendment and its limit — this endpoint
reads configuration, never run state.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from apps.api.app.services.orchestration import OrchestrationService, get_service
from orchestrator.workflow.definition import WorkflowDefinition, WorkflowNode

router = APIRouter(tags=["workflows"])


class WorkflowNodeView(BaseModel):
    id: str
    agent_profile: str
    worker_requirement: str
    depends_on: list[str]
    expected_artifact: str
    approval_after: str | None
    workspace: str
    session_policy: str
    #: Derived rather than left for the caller to infer from worker_requirement, which
    #: would put the "human" and "local_tool_runner" magic strings in the client.
    is_human: bool
    is_builtin: bool
    needs_worktree: bool

    @classmethod
    def of(cls, node: WorkflowNode) -> WorkflowNodeView:
        return cls(
            id=node.id,
            agent_profile=node.agent_profile,
            worker_requirement=node.worker_requirement,
            depends_on=list(node.depends_on),
            expected_artifact=node.expected_artifact,
            approval_after=node.approval_after.value if node.approval_after else None,
            workspace=node.workspace.value,
            session_policy=node.session_policy.value,
            is_human=node.is_human,
            is_builtin=node.is_builtin,
            needs_worktree=node.needs_worktree,
        )


class WorkflowView(BaseModel):
    workflow_id: str
    version: str
    description: str
    max_repair_rounds: int
    nodes: list[WorkflowNodeView]
    #: Topological order, ties broken by declaration order. Supplied so a client draws
    #: the graph the way the runner will execute it rather than re-deriving it.
    execution_order: list[str]

    @classmethod
    def of(cls, workflow: WorkflowDefinition) -> WorkflowView:
        return cls(
            workflow_id=workflow.workflow_id,
            version=workflow.version,
            description=workflow.description,
            max_repair_rounds=workflow.max_repair_rounds,
            nodes=[WorkflowNodeView.of(node) for node in workflow.nodes],
            execution_order=list(workflow.execution_order()),
        )


@router.get("/workflows/{workflow_id}", response_model=WorkflowView)
def get_workflow(
    workflow_id: str, service: OrchestrationService = Depends(get_service)
) -> WorkflowView:
    """The configured workflow, by id.

    One workflow is configured at a time — `WORKFLOW_PATH` names a single file — so an
    id that is not the configured one is a 404 rather than a lookup in a registry that
    does not exist. A run recorded against a workflow that has since been replaced will
    therefore fail to resolve its graph, which is honest: the graph on disk is no
    longer the one that run executed.
    """
    workflow = service.workflow
    if workflow_id != workflow.workflow_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no workflow named {workflow_id} is configured; "
            f"the configured workflow is {workflow.workflow_id}",
        )
    return WorkflowView.of(workflow)
