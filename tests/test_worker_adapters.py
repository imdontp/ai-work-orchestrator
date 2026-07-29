"""Adapter tests.

Argv construction and output parsing are unit-tested against the exact event shapes
recorded during the M1 spike, so they run without spending quota. The lifecycle tests
drive a fake CLI — a python script that emits the same event shapes — which exercises
start/stream/cancel/collect for real without depending on a provider being reachable.

Tests that talk to the real CLIs live in `test_worker_adapters_live.py`.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from workers.base import WorkerHandle, WorkerRequest
from workers.claude_code import ClaudeCodeAdapter
from workers.cli_base import CliWorkerAdapter, WorkerAdapterError, extract_json
from workers.codex import CodexAdapter

# Recorded from the spike, trimmed to the fields the adapters read.
CLAUDE_STREAM = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1", "tools": []}),
        json.dumps({"type": "stream_event", "session_id": "sess-1"}),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "sess-1",
                "result": "OK.",
                "total_cost_usd": 0.0034,
                "num_turns": 1,
                "duration_ms": 8985,
                "terminal_reason": "completed",
                "modelUsage": {"claude-sonnet-5": {"costUSD": 0.0034}},
            }
        ),
    ]
)

CLAUDE_AUTH_FAILURE = json.dumps(
    {
        "type": "result",
        "subtype": "success",  # the trap: success even on failure
        "is_error": True,
        "session_id": "sess-2",
        "api_error_status": 401,
        "terminal_reason": "api_error",
        "result": "Invalid API key · Fix external API key",
    }
)

CODEX_STREAM = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "OK."},
            }
        ),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 17746}}),
    ]
)


def _request(tmp_path: Path, **overrides) -> WorkerRequest:
    defaults = dict(
        task_id="TASK-001",
        run_id="RUN-001",
        prompt="do the thing",
        workspace=tmp_path / "worktree",
        log_dir=tmp_path / "logs",
        output_schema_path=None,
        timeout_seconds=60,
        environment={},
    )
    defaults.update(overrides)
    request = WorkerRequest(**defaults)
    request.workspace.mkdir(parents=True, exist_ok=True)
    return request


@pytest.fixture
def claude(tmp_path: Path) -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(executable=tmp_path / "claude.exe")


@pytest.fixture
def codex(tmp_path: Path) -> CodexAdapter:
    return CodexAdapter(executable=tmp_path / "codex.exe")


# ---------------------------------------------------------------------------
# WorkerRequest invariants
# ---------------------------------------------------------------------------


def test_session_id_and_resume_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _request(tmp_path, session_id="a", resume_from="b")


def test_filesystem_access_is_constrained(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="read_only or scoped_write"):
        _request(tmp_path, filesystem_access="everything")


# ---------------------------------------------------------------------------
# Claude Code argv
# ---------------------------------------------------------------------------


def test_claude_argv_streams_and_pins_a_session(claude: ClaudeCodeAdapter, tmp_path: Path) -> None:
    argv = claude.build_argv(_request(tmp_path, session_id="chosen-by-us"))

    assert argv[1:4] == ["-p", "do the thing", "--output-format"]
    assert "stream-json" in argv
    assert "--verbose" in argv
    assert argv[argv.index("--session-id") + 1] == "chosen-by-us"
    assert "--resume" not in argv


def test_claude_argv_generates_a_session_id_when_none_is_given(
    claude: ClaudeCodeAdapter, tmp_path: Path
) -> None:
    argv = claude.build_argv(_request(tmp_path))
    assert len(argv[argv.index("--session-id") + 1]) == 36


def test_claude_argv_resumes_without_pinning(claude: ClaudeCodeAdapter, tmp_path: Path) -> None:
    argv = claude.build_argv(_request(tmp_path, resume_from="sess-9"))

    assert argv[argv.index("--resume") + 1] == "sess-9"
    assert "--session-id" not in argv


def test_claude_argv_disables_tools_for_an_analysis_node(
    claude: ClaudeCodeAdapter, tmp_path: Path
) -> None:
    argv = claude.build_argv(_request(tmp_path, allowed_tools=()))

    assert argv[argv.index("--tools") + 1] == ""
    assert "--permission-mode" not in argv


def test_claude_argv_accepts_edits_only_for_a_write_task(
    claude: ClaudeCodeAdapter, tmp_path: Path
) -> None:
    argv = claude.build_argv(
        _request(tmp_path, allowed_tools=("Read", "Write"), filesystem_access="scoped_write")
    )

    assert argv[argv.index("--tools") + 1] == "Read,Write"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_claude_argv_inlines_the_schema_because_the_cli_wants_a_string(
    claude: ClaudeCodeAdapter, tmp_path: Path
) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")

    argv = claude.build_argv(_request(tmp_path, output_schema_path=schema))
    value = argv[argv.index("--json-schema") + 1]

    # A string, not a path. Contents may be reshaped for the CLI - see
    # test_claude_strips_the_dialect_reference_from_a_contract - but the constraints
    # come through unchanged.
    assert json.loads(value) == {"type": "object"}
    assert value != str(schema)


# ---------------------------------------------------------------------------
# Claude Code parsing
# ---------------------------------------------------------------------------


def test_claude_parses_a_successful_run(claude: ClaudeCodeAdapter) -> None:
    outcome = claude.parse_output(CLAUDE_STREAM, "")

    assert outcome.session_id == "sess-1"
    assert outcome.result_text == "OK."
    assert outcome.reported_error is False
    assert outcome.error_kind is None
    assert outcome.usage["total_cost_usd"] == 0.0034
    assert outcome.usage["model_usage"] == {"claude-sonnet-5": {"costUSD": 0.0034}}


def test_claude_does_not_trust_subtype_success(claude: ClaudeCodeAdapter) -> None:
    """The envelope says subtype=success on a 401. is_error is the real signal."""
    outcome = claude.parse_output(CLAUDE_AUTH_FAILURE, "")

    assert outcome.reported_error is True
    assert outcome.error_kind == "auth"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "auth"), (403, "auth"), (404, "model"), (429, "quota"), (500, "runtime")],
)
def test_claude_classifies_api_errors(
    claude: ClaudeCodeAdapter, status: int, expected: str
) -> None:
    event = json.dumps(
        {"type": "result", "is_error": True, "api_error_status": status, "session_id": "s"}
    )
    assert claude.parse_output(event, "").error_kind == expected


def test_claude_reports_a_usage_error_that_never_produced_an_envelope(
    claude: ClaudeCodeAdapter,
) -> None:
    outcome = claude.parse_output("", "error: option '--output-format' is invalid.")

    assert outcome.reported_error is True
    assert outcome.error_kind == "usage"
    assert "invalid" in (outcome.result_text or "")


def test_claude_parsing_survives_malformed_lines(claude: ClaudeCodeAdapter) -> None:
    noisy = "not json\n{broken\n" + CLAUDE_STREAM
    assert claude.parse_output(noisy, "").result_text == "OK."


# ---------------------------------------------------------------------------
# Codex argv
# ---------------------------------------------------------------------------


def test_codex_argv_uses_exec_with_a_sandbox(codex: CodexAdapter, tmp_path: Path) -> None:
    argv = codex.build_argv(_request(tmp_path, filesystem_access="scoped_write"))

    assert argv[1:3] == ["exec", "--json"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[-1] == "do the thing"


def test_codex_argv_defaults_to_read_only(codex: CodexAdapter, tmp_path: Path) -> None:
    argv = codex.build_argv(_request(tmp_path))
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_codex_resume_omits_flags_that_subcommand_rejects(
    codex: CodexAdapter, tmp_path: Path
) -> None:
    """`codex exec resume` accepts neither --sandbox nor --color; passing them exits 2."""
    argv = codex.build_argv(_request(tmp_path, resume_from="thread-1"))

    assert argv[1:5] == ["exec", "resume", "thread-1", "--json"]
    assert "--sandbox" not in argv
    assert "--color" not in argv
    assert "--last" not in argv


def test_codex_argv_passes_the_schema_as_a_path(codex: CodexAdapter, tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")

    argv = codex.build_argv(_request(tmp_path, output_schema_path=schema))
    value = Path(argv[argv.index("--output-schema") + 1])

    # A path, not inline JSON. It points at the derived schema rather than the
    # contract - see test_codex_derives_a_strict_schema_instead_of_passing_the_contract.
    assert value.is_file()
    assert json.loads(value.read_text(encoding="utf-8"))["type"] == "object"
    assert "--output-last-message" in argv


def test_codex_derives_a_strict_schema_instead_of_passing_the_contract(
    codex: CodexAdapter, tmp_path: Path
) -> None:
    """OpenAI's response_format subset rejects our contracts verbatim.

    Measured against the real API: first `schema must have a 'type' key`, then
    `'required' ... including every key in properties. Missing 'sha256'`, then
    `'additionalProperties' is required to be supplied and to be false` for an object
    with no properties. The derivation belongs in the adapter, not the contract.
    """
    contract = tmp_path / "contract.json"
    contract.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://example/contract.json",
                "title": "Thing",
                "type": "object",
                "required": ["a"],
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "array", "items": {"type": "string"}, "default": []},
                    "metadata": {"type": "object"},
                },
            }
        ),
        encoding="utf-8",
    )
    request = _request(tmp_path, output_schema_path=contract)

    argv = codex.build_argv(request)
    derived = Path(argv[argv.index("--output-schema") + 1])
    schema = json.loads(derived.read_text(encoding="utf-8"))

    assert derived != contract, "the published contract must not be handed over as-is"
    assert "$schema" not in schema and "$id" not in schema and "title" not in schema
    # Every property promoted to required, every object closed.
    assert schema["required"] == ["a", "b", "metadata"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["metadata"]["additionalProperties"] is False
    assert schema["properties"]["metadata"]["required"] == []
    assert "default" not in schema["properties"]["b"]
    # The contract itself is untouched.
    assert "$schema" in json.loads(contract.read_text(encoding="utf-8"))


def test_codex_writes_the_derived_schema_without_a_bom(
    codex: CodexAdapter, tmp_path: Path
) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({"type": "object", "properties": {}}), encoding="utf-8")

    argv = codex.build_argv(_request(tmp_path, output_schema_path=contract))
    derived = Path(argv[argv.index("--output-schema") + 1])

    assert not derived.read_bytes().startswith(b"\xef\xbb\xbf")


def test_claude_strips_the_dialect_reference_from_a_contract(
    claude: ClaudeCodeAdapter, tmp_path: Path
) -> None:
    """`--json-schema` fails on a contract verbatim: it does not resolve $schema."""
    contract = tmp_path / "contract.json"
    contract.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://example/contract.json",
                "title": "Thing",
                "type": "object",
                "properties": {"verdict": {"type": "string"}},
                "required": ["verdict"],
            }
        ),
        encoding="utf-8",
    )

    argv = claude.build_argv(_request(tmp_path, output_schema_path=contract))
    schema = json.loads(argv[argv.index("--json-schema") + 1])

    assert "$schema" not in schema
    assert "$id" not in schema
    # Constraints survive; only the identity keywords go.
    assert schema["required"] == ["verdict"]
    assert schema["properties"]["verdict"] == {"type": "string"}


def test_codex_refuses_a_schema_file_with_a_bom(codex: CodexAdapter, tmp_path: Path) -> None:
    """A BOM makes codex reject the file as invalid JSON. Fail before spending a run."""
    schema = tmp_path / "schema.json"
    schema.write_bytes(b"\xef\xbb\xbf" + b'{"type":"object"}')

    with pytest.raises(WorkerAdapterError, match="BOM"):
        codex.build_argv(_request(tmp_path, output_schema_path=schema))


# ---------------------------------------------------------------------------
# Codex parsing
# ---------------------------------------------------------------------------


def test_codex_captures_the_thread_id_for_resume(codex: CodexAdapter) -> None:
    outcome = codex.parse_output(CODEX_STREAM, "")

    assert outcome.session_id == "thread-1"
    assert outcome.result_text == "OK."
    assert outcome.reported_error is False
    assert outcome.usage == {"input_tokens": 17746}


def test_codex_parses_a_schema_constrained_final_message(codex: CodexAdapter) -> None:
    stream = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": '{"verdict":"True","confidence":1.0}',
                    },
                }
            ),
        ]
    )
    outcome = codex.parse_output(stream, "")

    assert outcome.structured_result == {"verdict": "True", "confidence": 1.0}


def test_codex_classifies_the_401_retry_storm_as_auth(codex: CodexAdapter) -> None:
    stream = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps(
                {
                    "type": "error",
                    "message": "Reconnecting... 2/5 (unexpected status 401 Unauthorized: "
                    "Missing bearer or basic authentication in header)",
                }
            ),
        ]
    )
    outcome = codex.parse_output(stream, "")

    assert outcome.reported_error is True
    assert outcome.error_kind == "auth"
    assert outcome.session_id == "t"


def test_codex_takes_the_last_agent_message_as_the_answer(codex: CodexAdapter) -> None:
    """A live run had codex emit two complete objects; joining them broke the JSON."""
    stream = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": '{"status":"in_progress"}'},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": '{"status":"completed"}'},
                }
            ),
        ]
    )
    outcome = codex.parse_output(stream, "")

    assert outcome.structured_result == {"status": "completed"}
    assert outcome.result_text == '{"status":"completed"}'


def test_codex_reports_a_clap_usage_error(codex: CodexAdapter) -> None:
    outcome = codex.parse_output("", "error: invalid value 'nope' for '--sandbox <SANDBOX_MODE>'")

    assert outcome.reported_error is True
    assert outcome.error_kind == "usage"


# ---------------------------------------------------------------------------
# Reading JSON out of what workers actually return
# ---------------------------------------------------------------------------


def test_extract_json_reads_a_bare_object() -> None:
    assert extract_json('{"verdict":"pass"}') == {"verdict": "pass"}


def test_extract_json_unwraps_a_markdown_fence() -> None:
    """Observed live: an analysis came back fenced and followed by a paragraph."""
    text = '```json\n{"verdict": "pass"}\n```\n\nI checked both files and they look fine.'
    assert extract_json(text) == {"verdict": "pass"}


def test_extract_json_ignores_prose_after_the_value() -> None:
    assert extract_json('{"a":1}\n\nThat is my answer.') == {"a": 1}


def test_extract_json_ignores_prose_before_the_value() -> None:
    assert extract_json('Here is the result:\n{"a":1}') == {"a": 1}


def test_extract_json_takes_the_first_of_several_values() -> None:
    assert extract_json('{"first":1}\n{"second":2}') == {"first": 1}


@pytest.mark.parametrize("text", ["", None, "no json here at all", "{not valid}"])
def test_extract_json_returns_none_when_nothing_parses(text) -> None:
    assert extract_json(text) is None


# ---------------------------------------------------------------------------
# Lifecycle, driven by a fake CLI
# ---------------------------------------------------------------------------

FAKE_CLI = """
import json, sys, time
mode = sys.argv[-1]
print(json.dumps({"type": "thread.started", "thread_id": "fake-1"}), flush=True)
for index in range(3):
    print(json.dumps({"type": "progress", "step": index}), flush=True)
    time.sleep(0.1)
if mode == "hang":
    time.sleep(120)
print(json.dumps({
    "type": "item.completed",
    "item": {"type": "agent_message", "text": "done"},
}), flush=True)
"""


class FakeAdapter(CliWorkerAdapter):
    """Runs the fake CLI above, parsed with Codex's event shapes."""

    name = "fake"
    capabilities = CodexAdapter.capabilities

    def __init__(self, script: Path, mode: str = "normal") -> None:
        super().__init__(executable=Path(sys.executable))
        self._script = script
        self._mode = mode

    def resolve_executable(self) -> Path:  # pragma: no cover - executable is injected
        return Path(sys.executable)

    def build_argv(self, request: WorkerRequest) -> list[str]:
        return [sys.executable, str(self._script), self._mode]

    def parse_output(self, stdout: str, stderr: str):
        return CodexAdapter(executable=Path("unused")).parse_output(stdout, stderr)

    async def health_check(self) -> dict:  # pragma: no cover - not under test
        return {}


@pytest.fixture
def fake_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    return script


def test_lifecycle_streams_events_then_collects(fake_script: Path, tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], object]:
        adapter = FakeAdapter(fake_script)
        request = _request(tmp_path)
        handle = await adapter.start(request)
        seen = [line async for line in adapter.stream_events(handle)]
        return seen, await adapter.collect(handle)

    seen, result = asyncio.run(scenario())

    kinds = [json.loads(line)["type"] for line in seen]
    assert kinds == ["thread.started", "progress", "progress", "progress", "item.completed"]
    assert result.exit_code == 0
    assert result.session_id == "fake-1"
    assert result.reported_error is False
    assert result.timed_out is False


def test_collect_writes_a_normalized_outcome_file(fake_script: Path, tmp_path: Path) -> None:
    async def scenario():
        adapter = FakeAdapter(fake_script)
        handle = await adapter.start(_request(tmp_path))
        return await adapter.collect(handle)

    result = asyncio.run(scenario())

    assert result.result_path is not None
    payload = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert payload["worker"] == "fake"
    assert payload["task_id"] == "TASK-001"
    assert payload["session_id"] == "fake-1"
    assert payload["result_text"] == "done"
    assert payload["exit_code"] == 0


def test_logs_are_written_outside_the_workspace(fake_script: Path, tmp_path: Path) -> None:
    """Logs inside the worktree would land in the worker's own diff."""

    async def scenario():
        adapter = FakeAdapter(fake_script)
        request = _request(tmp_path)
        handle = await adapter.start(request)
        return request, await adapter.collect(handle)

    request, result = asyncio.run(scenario())

    assert list(request.workspace.iterdir()) == []
    assert result.stdout_path.parent == request.log_dir


async def _await_first_event(path: Path, timeout: float = 30.0) -> None:
    """Wait for proof the fake CLI is producing output.

    Without this the assertion below races interpreter startup: it would pass or fail
    depending on machine load rather than on adapter behaviour.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists() and b"thread.started" in path.read_bytes():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"fake CLI produced no output within {timeout}s")


def test_timeout_is_reported_as_such(fake_script: Path, tmp_path: Path) -> None:
    async def scenario():
        adapter = FakeAdapter(fake_script, mode="hang")
        handle = await adapter.start(_request(tmp_path, timeout_seconds=5))
        await _await_first_event(adapter._runs[handle.worker_run_id].stdout_path)
        return await adapter.collect(handle)

    result = asyncio.run(scenario())

    assert result.timed_out is True
    assert result.reported_error is True
    assert result.error_kind == "timeout"
    # Events produced before the kill are still captured.
    assert result.session_id == "fake-1"


def test_cancel_is_distinguished_from_timeout(fake_script: Path, tmp_path: Path) -> None:
    async def scenario():
        adapter = FakeAdapter(fake_script, mode="hang")
        handle = await adapter.start(_request(tmp_path, timeout_seconds=120))
        await _await_first_event(adapter._runs[handle.worker_run_id].stdout_path)
        await adapter.cancel(handle)
        return await adapter.collect(handle)

    result = asyncio.run(scenario())

    assert result.cancelled is True
    assert result.timed_out is False
    assert result.error_kind == "cancelled"


def test_operations_on_an_unknown_handle_fail_loudly(tmp_path: Path) -> None:
    async def scenario():
        adapter = FakeAdapter(tmp_path / "missing.py")
        await adapter.cancel(WorkerHandle(worker_run_id="nope", process_id=1))

    with pytest.raises(WorkerAdapterError, match="unknown worker run"):
        asyncio.run(scenario())
