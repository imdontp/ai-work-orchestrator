"""Claude Code worker adapter.

Every flag used here was executed on the target machine during the Milestone 1
capability spike and its output recorded; see
``docs/spikes/M1_CLI_CAPABILITY_REPORT.md`` sections 3 and 7. Re-run
``scripts/spike_m1.py`` after a CLI upgrade and treat these templates as unverified
until it passes.

Two observed behaviours shape this adapter:

- The JSON envelope reports ``"subtype": "success"`` even on a failed run. Failure is
  signalled by ``is_error``, ``terminal_reason`` and ``api_error_status``. Keying off
  ``subtype`` would misclassify every failure as a success.
- ``--session-id`` lets the orchestrator choose the session id up front, so session
  identity belongs to the control plane rather than being scraped from output.

**Resume is working-directory bound.** Observed: seeding a session in one workspace and
resuming it from another returned no result at all, while the same resume from the
original workspace returned the stored value. Inference: sessions are scoped to the
directory they were created in, consistent with ``--continue`` being documented as
"the current directory". A repair loop that resumes a session must therefore reuse that
run's worktree; it cannot resume into a freshly created one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from workers.base import WorkerCapabilities, WorkerRequest
from workers.cli_base import CliOutcome, CliWorkerAdapter, WorkerAdapterError

#: Observed mapping from the API status in the result envelope to a retry-relevant
#: cause. 401 was reproduced with a bad key, 404 with an unknown model.
_API_STATUS_TO_ERROR_KIND = {
    401: "auth",
    403: "auth",
    404: "model",
    429: "quota",
}


def _schema_for_cli(path: Path) -> str:
    """Turn a published contract into something ``--json-schema`` accepts.

    Observed: passing a contract verbatim fails with ``no schema with key or ref
    "https://json-schema.org/draft/2020-12/schema"`` — the CLI does not resolve the
    dialect meta-reference. Stripping the identity keywords leaves the constraints
    untouched, so the contract stays the single source of truth and this adapter owns
    the provider-shaped derivation.
    """
    schema = json.loads(path.read_text(encoding="utf-8"))
    for identity_keyword in ("$schema", "$id", "title"):
        schema.pop(identity_keyword, None)
    return json.dumps(schema)


class ClaudeCodeAdapter(CliWorkerAdapter):
    name = "claude_code"

    capabilities = WorkerCapabilities(
        structured_output=True,  # --output-format json, --json-schema
        stream_events=True,  # stream-json, token-level
        resume_session=True,  # --session-id is assignable by us, then --resume
        cancel_process=True,  # verified; see ProcessManager
        # False on purpose. The spike's out-of-scope write was declined by the model
        # with permission_denials empty: judgment, not enforcement. Containment is the
        # orchestrator's job (ADR-010).
        scoped_write=False,
        server_mode=False,
    )

    required_flags = (
        "--append-system-prompt",
        "--output-format",
        "--json-schema",
        "--include-partial-messages",
        "--session-id",
        "--resume",
        "--permission-mode",
        "--tools",
        "--model",
    )

    def resolve_executable(self) -> Path:
        found = self._which("claude")
        if found is None:
            raise WorkerAdapterError("claude executable not found on PATH")
        return found

    def build_argv(self, request: WorkerRequest) -> list[str]:
        argv = [
            str(self.executable),
            "-p",
            request.prompt,
            "--output-format",
            "stream-json",
            # stream-json requires --verbose to emit the full event stream.
            "--verbose",
            "--include-partial-messages",
        ]

        if request.system_prompt:
            # Append rather than replace: the default system prompt carries the tool
            # and environment description the CLI needs to function.
            argv += ["--append-system-prompt", request.system_prompt]

        if request.model:
            argv += ["--model", request.model]

        if request.allowed_tools is not None:
            # "" disables every tool, which is what an analysis-only node wants.
            argv += ["--tools", ",".join(request.allowed_tools)]

        if request.filesystem_access == "scoped_write":
            # No human is at the terminal to answer a prompt. The orchestrator granted
            # the tools deliberately, and the worktree barrier bounds the damage.
            argv += ["--permission-mode", "acceptEdits"]

        if request.output_schema_path is not None:
            # Claude Code takes the schema as a string, not a path.
            argv += ["--json-schema", _schema_for_cli(request.output_schema_path)]

        if request.resume_from:
            argv += ["--resume", request.resume_from]
        else:
            argv += ["--session-id", request.session_id or str(uuid4())]

        return argv

    def parse_output(self, stdout: str, stderr: str) -> CliOutcome:
        result_event: dict[str, Any] | None = None
        session_id: str | None = None

        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("session_id"), str):
                session_id = event["session_id"]
            if event.get("type") == "result":
                result_event = event

        if result_event is None:
            # No terminal event: a usage error, or the process died first. stderr
            # carries the message in that case.
            return CliOutcome(
                session_id=session_id,
                result_text=stderr.strip() or None,
                reported_error=True,
                error_kind="usage" if stderr.strip() else "no_result",
            )

        is_error = bool(result_event.get("is_error"))
        status = result_event.get("api_error_status")
        error_kind: str | None = None
        if is_error:
            mapped = _API_STATUS_TO_ERROR_KIND.get(status) if isinstance(status, int) else None
            error_kind = mapped or "runtime"

        return CliOutcome(
            session_id=session_id or result_event.get("session_id"),
            result_text=result_event.get("result"),
            structured_result=result_event.get("structured_output"),
            reported_error=is_error,
            error_kind=error_kind,
            usage={
                "total_cost_usd": result_event.get("total_cost_usd"),
                "num_turns": result_event.get("num_turns"),
                "duration_ms": result_event.get("duration_ms"),
                # modelUsage can name models the orchestrator did not request, so cost
                # attribution must read it rather than the requested model name.
                "model_usage": result_event.get("modelUsage"),
                "terminal_reason": result_event.get("terminal_reason"),
            },
        )

    async def health_check(self) -> dict[str, Any]:
        version_code, version_out, _ = await self._capture("--version")
        auth_code, auth_out, auth_err = await self._capture("auth", "status")

        auth: dict[str, Any]
        try:
            parsed = json.loads(auth_out)
        except json.JSONDecodeError:
            auth = {"raw": (auth_out or auth_err).strip()[:200]}
        else:
            # The raw payload carries the account email, org id and org name. Report
            # only what the orchestrator needs to route and warn about.
            auth = {
                "logged_in": parsed.get("loggedIn"),
                "auth_method": parsed.get("authMethod"),
                "api_provider": parsed.get("apiProvider"),
                "subscription_type": parsed.get("subscriptionType"),
            }

        flags = await self._check_flag_surface("--help")

        return {
            "worker": self.name,
            "executable": str(self.executable),
            "version": version_out.strip() or None,
            "available": version_code == 0 and auth_code == 0,
            "authenticated": bool(auth.get("logged_in")),
            "auth": auth,
            "flag_surface": flags,
        }
