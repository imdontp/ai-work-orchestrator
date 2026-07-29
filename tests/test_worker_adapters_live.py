"""Adapter tests that invoke the real Claude Code and Codex CLIs.

Skipped unless ``AIWO_LIVE_TESTS=1``. They spend real subscription quota and depend on
the machine being authenticated, so they must not run in the default suite — but the
whole point of the M1 spike was that only a recorded local run counts as evidence, and
these are what re-establish that after a CLI upgrade.

    AIWO_LIVE_TESTS=1 pytest tests/test_worker_adapters_live.py -v
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from workers.base import WorkerRequest
from workers.claude_code import ClaudeCodeAdapter
from workers.codex import CodexAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("AIWO_LIVE_TESTS") != "1",
    reason="live CLI tests spend quota; set AIWO_LIVE_TESTS=1 to run them",
)

TINY_PROMPT = "Reply with exactly: OK. No other text."


def _sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "worktree"
    repo.mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=t@l", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=t@l", "-c", "user.name=t", "commit", "-q", "-m", "base"],
    ):
        subprocess.run(argv, cwd=str(repo), capture_output=True, check=True)
    return repo


def _request(tmp_path: Path, **overrides) -> WorkerRequest:
    defaults = dict(
        task_id="TASK-LIVE",
        run_id="RUN-LIVE",
        prompt=TINY_PROMPT,
        log_dir=tmp_path / "logs",
        output_schema_path=None,
        timeout_seconds=180,
        environment={},
        model="sonnet",
        tool_access="none",
    )
    defaults.update(overrides)
    # Built only when the caller did not supply one. `setdefault` would not do: its
    # second argument is evaluated either way, so the repo would still be created.
    if "workspace" not in defaults:
        defaults["workspace"] = _sandbox_repo(tmp_path)
    return WorkerRequest(**defaults)


async def _run_to_completion(adapter, request: WorkerRequest) -> tuple[list[str], object]:
    handle = await adapter.start(request)
    events = [line async for line in adapter.stream_events(handle)]
    return events, await adapter.collect(handle)


# ---------------------------------------------------------------------------
# Health checks — no quota spent
# ---------------------------------------------------------------------------


def test_claude_health_check_reports_auth_and_flag_surface() -> None:
    health = asyncio.run(ClaudeCodeAdapter().health_check())

    assert health["available"] is True
    assert health["authenticated"] is True
    assert health["version"]
    assert health["flag_surface"]["ok"] is True, health["flag_surface"]["missing"]
    # The account email and org id must not leak into a health payload.
    assert "email" not in json.dumps(health)


def test_codex_health_check_reports_auth_and_flag_surface() -> None:
    health = asyncio.run(CodexAdapter().health_check())

    assert health["available"] is True
    assert health["authenticated"] is True
    assert health["version"]
    assert health["flag_surface"]["ok"] is True, health["flag_surface"]["missing"]


def test_codex_resolves_a_shell_free_executable() -> None:
    """npm's .ps1 and extensionless shims cannot be launched without a shell."""
    executable = CodexAdapter().resolve_executable()
    assert executable.suffix.lower() == ".exe"
    assert executable.is_file()


# ---------------------------------------------------------------------------
# Real runs
# ---------------------------------------------------------------------------


def test_claude_runs_headless_and_streams(tmp_path: Path) -> None:
    events, result = asyncio.run(_run_to_completion(ClaudeCodeAdapter(), _request(tmp_path)))

    assert result.exit_code == 0
    assert result.reported_error is False
    assert result.session_id
    assert "OK" in (result.result_path.read_text(encoding="utf-8"))
    # Streaming, not one flush at exit.
    assert len(events) > 2
    assert any(json.loads(line).get("type") == "result" for line in events)


def test_claude_session_id_is_ours_and_resume_carries_state(tmp_path: Path) -> None:
    """The orchestrator owns session identity: we pick the id, then resume it.

    Both runs share one workspace. Claude Code scopes a session to the directory it was
    created in, so resuming from a different cwd finds nothing — see the note in
    `workers/claude_code.py`.
    """
    adapter = ClaudeCodeAdapter()
    chosen = "8c2f1a76-4d3b-4f21-9a55-1e7c0b9d3f42"
    workspace = _sandbox_repo(tmp_path / "shared")

    seed = _request(
        tmp_path,
        workspace=workspace,
        prompt="Remember this codeword: ORCHID. Reply with exactly: STORED.",
        session_id=chosen,
    )
    _, seed_result = asyncio.run(_run_to_completion(adapter, seed))
    assert seed_result.session_id == chosen

    resumed = _request(
        tmp_path,
        workspace=workspace,
        prompt="What was the codeword? Reply with just the word.",
        resume_from=chosen,
    )
    _, resume_result = asyncio.run(_run_to_completion(adapter, resumed))

    payload = json.loads(resume_result.result_path.read_text(encoding="utf-8"))
    assert "ORCHID" in (payload["result_text"] or "")


def test_claude_structured_output_conforms_to_the_supplied_schema(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"verdict": {"type": "string"}, "confidence": {"type": "number"}},
                "required": ["verdict", "confidence"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    request = _request(
        tmp_path,
        prompt="Judge this statement: 2+2=4. Answer with verdict and confidence.",
        output_schema_path=schema,
    )

    _, result = asyncio.run(_run_to_completion(ClaudeCodeAdapter(), request))
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    answer = json.loads(payload["result_text"])

    assert set(answer) == {"verdict", "confidence"}


def test_codex_runs_headless_and_reports_its_thread_id(tmp_path: Path) -> None:
    request = _request(tmp_path, model=None)
    events, result = asyncio.run(_run_to_completion(CodexAdapter(), request))

    assert result.exit_code == 0
    assert result.reported_error is False
    # Codex assigns the id; the adapter must capture it or a resume is impossible.
    assert result.session_id
    assert any(json.loads(line).get("type") == "thread.started" for line in events)


def test_codex_writes_only_inside_its_workspace(tmp_path: Path) -> None:
    """Not a containment guarantee — that is ADR-010's job — but the happy path."""
    request = _request(
        tmp_path,
        prompt="Create a file named adapter_probe.txt containing exactly HELLO. Then stop.",
        model=None,
        filesystem_access="scoped_write",
        timeout_seconds=300,
    )
    _, result = asyncio.run(_run_to_completion(CodexAdapter(), request))

    assert result.exit_code == 0
    assert (request.workspace / "adapter_probe.txt").read_text().strip() == "HELLO"
