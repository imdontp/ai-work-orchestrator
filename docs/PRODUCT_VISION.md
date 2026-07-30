# Product Vision

## Problem

The user currently coordinates multiple AI coding and analysis tools manually. A typical workflow requires running a task in one terminal, waiting for output, copying the result, opening another CLI agent, rebuilding context, and manually deciding what happens next.

This process is slow, error-prone, difficult to audit, and does not scale to concurrent work.

## Product

Personal AI Work Orchestrator is a local-first control plane and bridge for CLI agents. It receives a goal, chooses a workflow shape, prepares task context, runs CLI workers in the background, normalizes their outputs, verifies evidence, and presents meaningful approval decisions to the human operator.

## Primary user

A single technical user operating local repositories and subscription-authenticated CLI agents.

## Core value proposition

- Eliminate repetitive CLI-to-CLI copy and paste
- Make handoffs traceable and reproducible
- Select one worker or multiple workers based on task complexity
- Preserve human authority over risk and external side effects
- Turn agent output into reviewable artifacts and evidence

## Product positioning

This is not a multi-model chat application and not an autonomous swarm.

It is a reliable AI work orchestration platform.

## Experience model

This is the long-term shape, not a milestone. **ADR-011 scopes the first dashboard far
more narrowly** — run list and detail, approval inbox, cancel — and explicitly rejects
the overview page below as the place to start. Build against ADR-011; read this for
where it is eventually going.

### Overview dashboard

Shows active runs, queue, workers, approvals, recent outputs, activity, and a quick command box.

### Run control center

Shows the current DAG, worker sessions, active step, artifacts, logs, verification evidence, and approval package.

### Source and workspace connections

Projects can later connect local folders, Git repositories, documentation, Jira, Figma, and knowledge sources. Read sources and writable execution workspaces must remain separate.

## Success

The product succeeds when a real task can move from analysis to implementation to verification to review without manual context copying, while still remaining observable, interruptible, and human-controlled.
