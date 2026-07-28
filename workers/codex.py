"""Codex worker adapter.

Every flag used here was executed on the target machine during the Milestone 1
capability spike and its output recorded; see
``docs/spikes/M1_CLI_CAPABILITY_REPORT.md`` sections 4 and 7.

Four observed behaviours shape this adapter:

- The flag surface differs per subcommand. ``-a/--ask-for-approval`` exists on
  ``codex`` but not on ``codex exec``; ``codex exec resume`` accepts neither
  ``-s/--sandbox`` nor ``--color`` nor ``-C``. Argv is therefore built per subcommand
  and ``health_check`` asserts the flags still exist.
- There is no ``--session-id``. Codex chooses the id and reports it as ``thread_id``
  in the first event, so the adapter captures it for a later resume. ``--last`` is
  never used: "most recent session for this cwd" is a race under concurrency.
- ``--output-schema`` rejects a file with a UTF-8 BOM, which is what PowerShell's
  default ``Set-Content -Encoding utf8`` writes. Schema files must be BOM-free.
- Missing credentials produce a 25-second retry storm before a non-zero exit, so a
  401 in the event stream is classified immediately rather than waiting for the exit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from workers.base import WorkerCapabilities, WorkerRequest
from workers.cli_base import CliOutcome, CliWorkerAdapter, WorkerAdapterError

_SANDBOX_BY_ACCESS = {
    "read_only": "read-only",
    "scoped_write": "workspace-write",
}


class CodexAdapter(CliWorkerAdapter):
    name = "codex"

    capabilities = WorkerCapabilities(
        structured_output=True,  # --json, --output-schema (BOM-free file)
        stream_events=True,  # event-level, not token-level
        resume_session=True,  # via the captured thread_id, never --last
        cancel_process=True,  # verified; see ProcessManager
        # False on purpose, and this one was measured rather than inferred: a probe
        # under -s workspace-write wrote a file above the workspace root. See ADR-010.
        scoped_write=False,
        server_mode=True,  # `codex mcp-server` / `codex app-server` exist (not exercised)
    )

    required_flags = (
        "--json",
        "--output-schema",
        "--output-last-message",
        "--sandbox",
        "--color",
        "--skip-git-repo-check",
    )

    def resolve_executable(self) -> Path:
        """Prefer the vendored native binary.

        The npm install exposes ``codex.ps1`` and an extensionless shim that
        ``CreateProcess`` cannot launch at all, plus ``codex.cmd``, which Windows runs
        through ``cmd.exe`` — reintroducing the shell parsing layer ProcessManager
        exists to avoid. The vendored executable keeps the argv-only guarantee intact.
        """
        app_data = os.environ.get("APPDATA")
        if app_data:
            vendored = (
                Path(app_data)
                / "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64"
                / "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            )
            if vendored.is_file():
                return vendored

        found = self._which("codex")
        if found is None:
            raise WorkerAdapterError("codex executable not found on PATH")
        if found.suffix.lower() in {".ps1", ".cmd", ""} and os.name == "nt":
            raise WorkerAdapterError(
                f"refusing to launch {found}: npm shims need a shell. Install the "
                "native codex build or point the adapter at the vendored codex.exe."
            )
        return found

    def build_argv(self, request: WorkerRequest) -> list[str]:
        if request.resume_from:
            return self._build_resume_argv(request)
        return self._build_exec_argv(request)

    def _build_exec_argv(self, request: WorkerRequest) -> list[str]:
        argv = [
            str(self.executable),
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            _SANDBOX_BY_ACCESS[request.filesystem_access],
        ]
        # No -C: `codex exec resume` has no such flag, so both paths rely on the
        # process working directory instead, which ProcessManager sets.
        if request.model:
            argv += ["--model", request.model]
        argv += self._schema_arguments(request)
        argv.append(request.prompt)
        return argv

    def _build_resume_argv(self, request: WorkerRequest) -> list[str]:
        # Narrower flag surface than `exec`: no --sandbox, no --color.
        argv = [str(self.executable), "exec", "resume", str(request.resume_from), "--json"]
        if request.model:
            argv += ["--model", request.model]
        argv += self._schema_arguments(request)
        argv.append(request.prompt)
        return argv

    def _schema_arguments(self, request: WorkerRequest) -> list[str]:
        if request.output_schema_path is None:
            return []
        self._reject_bom(request.output_schema_path)
        return [
            "--output-schema",
            str(request.output_schema_path),
            "--output-last-message",
            str(request.log_dir / f"{request.run_id}.last-message.json"),
        ]

    @staticmethod
    def _reject_bom(schema_path: Path) -> None:
        if schema_path.read_bytes().startswith(b"\xef\xbb\xbf"):
            raise WorkerAdapterError(
                f"{schema_path} starts with a UTF-8 BOM; codex rejects it as invalid "
                "JSON. Write the schema as BOM-free UTF-8."
            )

    def parse_output(self, stdout: str, stderr: str) -> CliOutcome:
        session_id: str | None = None
        message_parts: list[str] = []
        usage: dict[str, Any] = {}
        errors: list[str] = []

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

            kind = event.get("type")
            if kind == "thread.started" and isinstance(event.get("thread_id"), str):
                session_id = event["thread_id"]
            elif kind == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        message_parts.append(text)
            elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = event["usage"]
            elif kind == "error":
                message = event.get("message")
                errors.append(message if isinstance(message, str) else json.dumps(event))

        result_text = "\n".join(message_parts) or None
        combined_errors = " ".join(errors) + " " + stderr

        if errors:
            return CliOutcome(
                session_id=session_id,
                result_text=result_text or errors[-1],
                reported_error=True,
                error_kind=_classify(combined_errors),
                usage=usage,
            )

        if result_text is None:
            # No agent message and no error event: a usage error from the arg parser,
            # which writes to stderr and never opens an event stream.
            return CliOutcome(
                session_id=session_id,
                result_text=stderr.strip() or None,
                reported_error=True,
                error_kind="usage" if stderr.strip() else "no_result",
                usage=usage,
            )

        return CliOutcome(
            session_id=session_id,
            result_text=result_text,
            structured_result=_maybe_json(result_text),
            reported_error=False,
            usage=usage,
        )

    async def health_check(self) -> dict[str, Any]:
        version_code, version_out, _ = await self._capture("--version")
        # Observed: `codex login status` writes its answer to stderr, not stdout.
        login_code, login_out, login_err = await self._capture("login", "status")
        status_text = (login_out or login_err).strip()

        flags = await self._check_flag_surface("exec", "--help")

        return {
            "worker": self.name,
            "executable": str(self.executable),
            "version": version_out.strip() or None,
            "available": version_code == 0,
            "authenticated": login_code == 0 and "logged in" in status_text.lower(),
            "auth": {"status": status_text[:200] or None},
            "flag_surface": flags,
        }


def _classify(text: str) -> str:
    lowered = text.lower()
    if "401" in lowered or "unauthorized" in lowered or "missing bearer" in lowered:
        return "auth"
    if "429" in lowered or "rate limit" in lowered or "quota" in lowered:
        return "quota"
    if "model" in lowered and "not supported" in lowered:
        return "model"
    return "runtime"


def _maybe_json(text: str) -> Any:
    """Return the parsed final message when --output-schema made it JSON."""
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
