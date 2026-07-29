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
from workers.cli_base import CliOutcome, CliWorkerAdapter, WorkerAdapterError, extract_json

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
        argv.append(_with_system_prompt(request))
        return argv

    def _build_resume_argv(self, request: WorkerRequest) -> list[str]:
        # Narrower flag surface than `exec`: no --sandbox, no --color.
        argv = [str(self.executable), "exec", "resume", str(request.resume_from), "--json"]
        if request.model:
            argv += ["--model", request.model]
        argv += self._schema_arguments(request)
        argv.append(_with_system_prompt(request))
        return argv

    def _schema_arguments(self, request: WorkerRequest) -> list[str]:
        if request.output_schema_path is None:
            return []
        self._reject_bom(request.output_schema_path)
        derived = request.log_dir / f"{request.run_id}.output-schema.json"
        _write_strict_schema(request.output_schema_path, derived)
        return [
            "--output-schema",
            str(derived),
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

        # The last agent message is the answer; earlier ones are narration. Joining
        # them produced invalid JSON in a live run, where codex emitted two complete
        # objects in a row and the second was the real result. This matches what
        # --output-last-message writes.
        result_text = message_parts[-1] if message_parts else None
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
            structured_result=extract_json(result_text),
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


#: Keywords OpenAI's response_format subset does not accept, or that only identify the
#: document. Observed by feeding it our contracts and reading the 400s back.
_UNSUPPORTED_KEYWORDS = frozenset({"$schema", "$id", "title", "default"})


def _write_strict_schema(source: Path, destination: Path) -> Path:
    """Derive an OpenAI-acceptable schema from a published contract.

    ``--output-schema`` feeds OpenAI's ``response_format``, which enforces a strict
    subset: every key in ``properties`` must also appear in ``required``, and several
    keywords are rejected outright. Our contracts are ordinary JSON Schema and use
    optional properties, so passing one verbatim fails with, for example,
    ``'required' is required to be supplied and to be an array including every key in
    properties. Missing 'sha256'``.

    The derivation lives here rather than in the contract because it is provider
    knowledge. Widening ``required`` only constrains the model's *output*; it does not
    change what the contract accepts, so the published wire format is untouched.
    """
    schema = json.loads(source.read_text(encoding="utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    # BOM-free: codex rejects a schema file that carries one.
    destination.write_text(json.dumps(_strictify(schema), indent=2), encoding="utf-8")
    return destination


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node

    result = {
        key: _strictify(value)
        for key, value in node.items()
        if key not in _UNSUPPORTED_KEYWORDS
    }
    # Every object needs properties, a matching required list, and closed extras -
    # including one declared with no properties at all, such as `metadata`, which is
    # rejected with "'additionalProperties' is required to be supplied and to be false".
    if result.get("type") == "object" or isinstance(result.get("properties"), dict):
        properties = result.setdefault("properties", {})
        result["required"] = sorted(properties) if isinstance(properties, dict) else []
        result["additionalProperties"] = False
    return result


def _with_system_prompt(request: WorkerRequest) -> str:
    """Prepend the agent profile's role definition to the payload.

    Observed from ``codex exec --help``: there is no system-prompt flag. ``-p`` is a
    *config* profile layered from ``$CODEX_HOME``, not an agent role, so it is not a
    substitute. Prepending is the only mechanism available.
    """
    if not request.system_prompt:
        return request.prompt
    return f"{request.system_prompt.strip()}\n\n---\n\n{request.prompt}"


def _classify(text: str) -> str:
    lowered = text.lower()
    if "401" in lowered or "unauthorized" in lowered or "missing bearer" in lowered:
        return "auth"
    if "429" in lowered or "rate limit" in lowered or "quota" in lowered:
        return "quota"
    if "model" in lowered and "not supported" in lowered:
        return "model"
    return "runtime"
