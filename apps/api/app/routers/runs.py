"""Task intake and run control.

A run is started by submitting a task, then advanced, inspected and decided on. The
advance endpoint returns immediately: driving real CLI workers takes minutes, so the
caller polls the run instead of holding a request open.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api.app.services.orchestration import (
    NotConfigured,
    OrchestrationError,
    OrchestrationService,
    get_service,
)
from orchestrator.domain.models import Task, TaskState
from orchestrator.workflow.runner import WorkflowRunError
from orchestrator.workflow.store import RunRecord, RunStoreError

router = APIRouter(tags=["runs"])


class RunSummary(BaseModel):
    run_id: str
    task_id: str
    workflow_id: str
    task_state: TaskState
    created_at: str
    completed_nodes: list[str]
    repair_rounds: int
    failure: str | None
    #: True while a background advance is in flight. The record alone cannot say -
    #: a paused run and a working run can both read RUNNING.
    advancing: bool
    awaiting_decision: str | None

    @classmethod
    def of(cls, record: RunRecord, *, advancing: bool) -> RunSummary:
        return cls(
            run_id=record.run_id,
            task_id=record.task_id,
            workflow_id=record.workflow_id,
            task_state=record.task_state,
            created_at=record.created_at,
            completed_nodes=list(record.completed_nodes),
            repair_rounds=record.repair_rounds,
            failure=record.failure,
            advancing=advancing,
            awaiting_decision=(
                record.pending_approval.approval_type if record.pending_approval else None
            ),
        )


class RunDetail(RunSummary):
    artifacts: dict[str, str]
    worktree: dict[str, str] | None
    #: Worker name -> session id. Reported so an operator can tell which conversation a
    #: repair round resumed; a session id identifies a transcript, not a credential.
    sessions: dict[str, str]
    pending_approval: dict[str, Any] | None

    @classmethod
    def of(cls, record: RunRecord, *, advancing: bool) -> RunDetail:
        summary = RunSummary.of(record, advancing=advancing)
        return cls(
            **summary.model_dump(),
            artifacts=dict(record.artifacts),
            worktree=record.worktree,
            sessions=dict(record.sessions),
            pending_approval=(
                record.pending_approval.model_dump(exclude={"resume_after_node"})
                if record.pending_approval
                else None
            ),
        )


class SubmitTaskRequest(BaseModel):
    task: Task
    #: Start advancing straight away. False leaves the run PENDING for inspection.
    start: bool = True


class DecisionRequest(BaseModel):
    decision: Literal["approve", "request_changes", "reject"]
    reason: str = Field(default="", max_length=4000)


class CancelRequest(BaseModel):
    reason: str = Field(default="", max_length=4000)


def _detail(service: OrchestrationService, record: RunRecord) -> RunDetail:
    return RunDetail.of(record, advancing=service.is_advancing(record.run_id))


def _load(service: OrchestrationService, run_id: str) -> RunRecord:
    try:
        return service.get(run_id)
    except RunStoreError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/tasks", response_model=RunDetail, status_code=status.HTTP_201_CREATED)
async def submit_task(
    request: SubmitTaskRequest,
    service: OrchestrationService = Depends(get_service),
) -> RunDetail:
    """Accept a task contract and open a run for it."""
    try:
        record = service.create_run(request.task)
    except NotConfigured as exc:
        # The service is misconfigured, not the request. 503 rather than 400.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except OrchestrationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if request.start:
        service.start_advance(record.run_id)
    return _detail(service, record)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    limit: int = 50,
    service: OrchestrationService = Depends(get_service),
) -> list[RunSummary]:
    return [
        RunSummary.of(record, advancing=service.is_advancing(record.run_id))
        for record in service.list_runs(limit)
    ]


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, service: OrchestrationService = Depends(get_service)) -> RunDetail:
    return _detail(service, _load(service, run_id))


@router.post(
    "/runs/{run_id}/advance",
    response_model=RunDetail,
    status_code=status.HTTP_202_ACCEPTED,
)
async def advance_run(
    run_id: str, service: OrchestrationService = Depends(get_service)
) -> RunDetail:
    """Resume a paused run. Returns at once; poll GET /runs/{run_id} for progress."""
    record = _load(service, run_id)
    if record.is_terminal:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"run {run_id} has finished as {record.task_state.value}"
        )
    if record.pending_approval is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"run {run_id} is waiting for a decision"
        )
    if not service.start_advance(run_id):
        raise HTTPException(status.HTTP_409_CONFLICT, f"run {run_id} is already advancing")
    return _detail(service, record)


@router.post("/runs/{run_id}/cancel", response_model=RunDetail)
async def cancel_run(
    run_id: str,
    request: CancelRequest | None = None,
    service: OrchestrationService = Depends(get_service),
) -> RunDetail:
    """Stop a run. Kills the worker if one is running, rather than waiting it out.

    A run that was advancing settles to CANCELLED on its own task, so the returned
    record may still read RUNNING. Poll GET /runs/{run_id} for the final state.
    """
    _load(service, run_id)
    try:
        record = await service.cancel(run_id, (request.reason if request else "") or "")
    except OrchestrationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except WorkflowRunError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _detail(service, record)


@router.get("/runs/{run_id}/events")
def get_events(
    run_id: str, service: OrchestrationService = Depends(get_service)
) -> list[dict[str, Any]]:
    """The audit trail, in the order things happened."""
    _load(service, run_id)
    return service.events(run_id)


@router.get("/runs/{run_id}/approval")
def get_approval(
    run_id: str, service: OrchestrationService = Depends(get_service)
) -> dict[str, Any]:
    """The approval package a human is being asked to decide on."""
    record = _load(service, run_id)
    if record.pending_approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} has no pending approval")
    return record.pending_approval.model_dump(exclude={"resume_after_node"})


@router.get("/runs/{run_id}/artifacts/{name}")
def get_artifact(
    run_id: str, name: str, service: OrchestrationService = Depends(get_service)
) -> Any:
    _load(service, run_id)
    try:
        return service.artifact(run_id, name)
    except (OrchestrationError, RunStoreError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/runs/{run_id}/decision", response_model=RunDetail)
async def decide(
    run_id: str,
    request: DecisionRequest,
    service: OrchestrationService = Depends(get_service),
) -> RunDetail:
    """Record a human decision on the pending approval, then continue the run."""
    _load(service, run_id)
    try:
        record = service.decide(run_id, request.decision, request.reason)
    except OrchestrationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:  # WorkflowRunError and friends
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    # An approval that unblocks the graph should not need a second call to take effect.
    if not record.is_terminal and record.pending_approval is None:
        service.start_advance(run_id)
    return _detail(service, record)
