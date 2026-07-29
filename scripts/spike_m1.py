"""Milestone 1 CLI capability spike runner.

Runs a fixed set of probes against the installed LLM CLI workers and records
*observed* behaviour: the exact argv, the exit code, the wall time, and the
stdout/stderr streams with per-line arrival timestamps.

Design constraints (mirrors ``execution/process_manager.py``):

- argv lists only; ``shell=False`` always. A probe that needs a shell is a
  finding, not something the runner papers over.
- every process gets its own process group so the timeout path can kill the
  whole tree (POSIX ``killpg`` / Windows ``taskkill /T``).
- stdout and stderr are drained concurrently so a chatty worker cannot deadlock
  on a full pipe buffer.
- output is redacted before it is written to disk.

Evidence is written to ``artifacts/m1-spike/<timestamp>/`` which is gitignored;
the curated report lives in ``docs/spikes/``.

Usage::

    python scripts/spike_m1.py --list
    python scripts/spike_m1.py --suite claude --sandbox C:/path/to/sandbox
    python scripts/spike_m1.py --probe claude-headless-json
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "<REDACTED_API_KEY>"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "<REDACTED_EMAIL>"),
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
        "<UUID>",
    ),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer <REDACTED_TOKEN>"),
    (re.compile(r"(?i)(access|refresh|id)_token\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{16,}"),
     r"\1_token=<REDACTED_TOKEN>"),
]

_USER_HOME = str(Path.home())


def redact(text: str) -> str:
    """Strip credentials and machine identity from captured output."""
    out = text.replace(_USER_HOME, "<HOME>")
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


# ---------------------------------------------------------------------------
# Worker executables
# ---------------------------------------------------------------------------


def _resolve_claude() -> list[str] | None:
    exe = shutil.which("claude")
    return [exe] if exe else None


def _resolve_codex() -> list[str] | None:
    """Resolve codex to a directly-executable argv prefix.

    The npm install exposes ``codex.cmd`` / ``codex.ps1`` shims on Windows,
    neither of which ``CreateProcess`` can launch without a shell. The real
    entrypoint is a Node script, so prefer ``node <codex.js>`` which *is*
    argv-executable. Fall back to whatever ``which`` finds on POSIX.
    """
    js = (
        Path(os.environ.get("APPDATA", ""))
        / "npm/node_modules/@openai/codex/bin/codex.js"
    )
    node = shutil.which("node")
    if IS_WINDOWS and js.is_file() and node:
        return [node, str(js)]
    exe = shutil.which("codex")
    return [exe] if exe else None


WORKER_RESOLVERS: dict[str, Callable[[], list[str] | None]] = {
    "claude": _resolve_claude,
    "codex": _resolve_codex,
}


# ---------------------------------------------------------------------------
# Probe model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """One observable question asked of one CLI."""

    id: str
    worker: str
    dimension: str
    question: str
    args: list[str]
    timeout_s: float = 120.0
    stdin: str | None = None
    in_sandbox: bool = True
    env: dict[str, str] = field(default_factory=dict)
    expect: str = ""
    # Probes that deliberately exercise the timeout/cancel path.
    expect_timeout: bool = False


@dataclass
class ProbeResult:
    probe: Probe
    argv: list[str]
    cwd: str
    started_at: str
    duration_s: float
    exit_code: int | None
    timed_out: bool
    killed_how: str | None
    stdout: str
    stderr: str
    stdout_timeline: list[tuple[float, str]]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.probe.id,
            "worker": self.probe.worker,
            "dimension": self.probe.dimension,
            "question": self.probe.question,
            "expect": self.probe.expect,
            "argv": self.argv,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 3),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "killed_how": self.killed_how,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_timeline": [[round(t, 3), line] for t, line in self.stdout_timeline],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _spawn_kwargs() -> dict[str, Any]:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen[str]) -> str:
    """Terminate the whole process group. Returns how it was killed."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return "taskkill /F /T"
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
            return "SIGTERM to process group"
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return "SIGKILL to process group"
    except ProcessLookupError:  # pragma: no cover - race with natural exit
        return "already exited"


def _drain(stream: Any, sink: queue.Queue[tuple[str, float, str]], name: str, t0: float) -> None:
    try:
        for line in iter(stream.readline, ""):
            sink.put((name, time.monotonic() - t0, line.rstrip("\r\n")))
    finally:
        stream.close()


def run_probe(probe: Probe, prefix: list[str], cwd: Path) -> ProbeResult:
    argv = [*prefix, *probe.args]
    env = {**os.environ, **probe.env}
    started_at = datetime.now(UTC).isoformat()
    t0 = time.monotonic()

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_spawn_kwargs(),
        )
    except OSError as exc:
        return ProbeResult(
            probe=probe,
            argv=argv,
            cwd=str(cwd),
            started_at=started_at,
            duration_s=time.monotonic() - t0,
            exit_code=None,
            timed_out=False,
            killed_how=None,
            stdout="",
            stderr="",
            stdout_timeline=[],
            error=f"{type(exc).__name__}: {exc}",
        )

    sink: queue.Queue[tuple[str, float, str]] = queue.Queue()
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, sink, "stdout", t0), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, sink, "stderr", t0), daemon=True),
    ]
    for t in threads:
        t.start()

    if probe.stdin is not None:
        try:
            proc.stdin.write(probe.stdin)
        except OSError:
            pass
    try:
        proc.stdin.close()
    except OSError:
        pass

    timed_out = False
    killed_how: str | None = None
    try:
        proc.wait(timeout=probe.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed_how = _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            killed_how = (killed_how or "") + " (still running after 10s)"

    for t in threads:
        t.join(timeout=5)
    duration = time.monotonic() - t0

    out_lines: list[tuple[float, str]] = []
    err_lines: list[str] = []
    while not sink.empty():
        name, elapsed, line = sink.get()
        if name == "stdout":
            out_lines.append((elapsed, line))
        else:
            err_lines.append(line)
    out_lines.sort(key=lambda x: x[0])

    return ProbeResult(
        probe=probe,
        argv=argv,
        cwd=str(cwd),
        started_at=started_at,
        duration_s=duration,
        exit_code=proc.returncode,
        timed_out=timed_out,
        killed_how=killed_how,
        stdout=redact("\n".join(line for _, line in out_lines)),
        stderr=redact("\n".join(err_lines)),
        stdout_timeline=[(t, redact(line)) for t, line in out_lines],
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def write_evidence(results: list[ProbeResult], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probes.json").write_text(
        json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    lines = [
        "# M1 CLI capability spike - raw evidence",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Platform: {sys.platform} ({os.name})",
        "",
        "| Probe | Dimension | Exit | Duration | Timed out |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| `{r.probe.id}` | {r.probe.dimension} | {r.exit_code} | "
            f"{r.duration_s:.1f}s | {'yes' if r.timed_out else 'no'} |"
        )
    lines.append("")

    for r in results:
        lines += [
            f"## `{r.probe.id}`",
            "",
            f"- **Dimension:** {r.probe.dimension}",
            f"- **Question:** {r.probe.question}",
            f"- **Expected signal:** {r.probe.expect or 'n/a'}",
            f"- **cwd:** `{redact(r.cwd)}`",
            f"- **exit code:** `{r.exit_code}`  **duration:** {r.duration_s:.2f}s"
            f"  **timed out:** {r.timed_out}"
            + (f"  **killed via:** {r.killed_how}" if r.killed_how else ""),
            "",
            "```",
            " ".join(redact(a) for a in r.argv),
            "```",
            "",
        ]
        if r.error:
            lines += ["**spawn error**", "", "```", r.error, "```", ""]
        if r.stdout:
            lines += ["**stdout**", "", "```", _truncate(r.stdout), "```", ""]
        if r.stderr:
            lines += ["**stderr**", "", "```", _truncate(r.stderr), "```", ""]
        if len(r.stdout_timeline) > 1:
            first, last = r.stdout_timeline[0][0], r.stdout_timeline[-1][0]
            lines += [
                f"**stdout arrival:** {len(r.stdout_timeline)} lines, "
                f"first at {first:.2f}s, last at {last:.2f}s "
                f"(spread {last - first:.2f}s)",
                "",
            ]

    (out_dir / "evidence.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(probes: list[Probe]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", type=Path, help="disposable repo probes run inside")
    parser.add_argument("--suite", default="all", help="worker name, or 'all'")
    parser.add_argument("--probe", action="append", default=[], help="run specific probe ids")
    parser.add_argument("--list", action="store_true", help="list probes and exit")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "artifacts" / "m1-spike",
        help="evidence root",
    )
    args = parser.parse_args()

    if args.list:
        for p in probes:
            print(f"{p.id:34s} {p.worker:8s} {p.dimension:24s} {p.question}")
        return 0

    selected = [p for p in probes if not args.probe or p.id in args.probe]
    if args.suite != "all":
        selected = [p for p in selected if p.worker == args.suite]
    if not selected:
        print("no probes selected", file=sys.stderr)
        return 2

    if args.sandbox is None:
        print(
            "--sandbox is required (probes must not run in the primary checkout)",
            file=sys.stderr,
        )
        return 2
    sandbox = args.sandbox.resolve()
    if not sandbox.is_dir():
        print(f"sandbox not found: {sandbox}", file=sys.stderr)
        return 2
    if REPO_ROOT == sandbox or REPO_ROOT in sandbox.parents:
        print("refusing to run probes inside the primary checkout", file=sys.stderr)
        return 2

    prefixes: dict[str, list[str]] = {}
    for worker in {p.worker for p in selected}:
        prefix = WORKER_RESOLVERS[worker]()
        if prefix is None:
            print(f"worker executable not found: {worker}", file=sys.stderr)
            return 3
        prefixes[worker] = prefix

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = args.out / stamp
    results: list[ProbeResult] = []

    for probe in selected:
        cwd = sandbox if probe.in_sandbox else REPO_ROOT
        print(f"[probe] {probe.id} ...", flush=True)
        result = run_probe(probe, prefixes[probe.worker], cwd)
        status = "TIMEOUT" if result.timed_out else f"exit={result.exit_code}"
        print(f"[probe] {probe.id} -> {status} in {result.duration_s:.1f}s", flush=True)
        results.append(result)

    write_evidence(results, out_dir)
    print(f"\nEvidence written to {out_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from spike_probes import PROBES  # type: ignore[import-not-found]

    raise SystemExit(main(PROBES))
