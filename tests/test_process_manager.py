"""Behavioural tests for the process supervisor.

These run the event loop with ``asyncio.run`` inside synchronous test functions
rather than relying on ``pytest-asyncio``, so they execute even when the optional
dev dependency is not installed.

The tree-kill test is the regression guard for blocker B1 in
``docs/spikes/M1_CLI_CAPABILITY_REPORT.md``: the timeout path used to call
``os.killpg`` unconditionally, which raises ``AttributeError`` on Windows.
"""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from execution import process_manager as process_manager_module
from execution.process_manager import ProcessManager, ProcessResult

# Writes one byte to argv[1] every 50ms, forever. Used as a liveness beacon.
HEARTBEAT_SOURCE = """
import sys, time
path = sys.argv[1]
while True:
    with open(path, "a") as handle:
        handle.write("x")
        handle.flush()
    time.sleep(0.05)
"""

# Spawns the heartbeat as a grandchild, then blocks. Killing this process must not
# leave the grandchild running.
PARENT_SOURCE = """
import subprocess, sys, time
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
time.sleep(120)
"""


def _run(coro):
    return asyncio.run(coro)


def test_rejects_empty_args(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="args must not be empty"):
        _run(
            ProcessManager().run(
                args=[],
                cwd=tmp_path,
                stdout_path=tmp_path / "out",
                stderr_path=tmp_path / "err",
                timeout_seconds=5,
            )
        )


def test_rejects_non_positive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        _run(
            ProcessManager().run(
                args=[sys.executable, "-c", "pass"],
                cwd=tmp_path,
                stdout_path=tmp_path / "out",
                stderr_path=tmp_path / "err",
                timeout_seconds=0,
            )
        )


def test_captures_streams_and_exit_code(tmp_path: Path) -> None:
    out, err = tmp_path / "out", tmp_path / "err"
    result = _run(
        ProcessManager().run(
            args=[
                sys.executable,
                "-c",
                "import sys; print('to-stdout'); print('to-stderr', file=sys.stderr); "
                "sys.exit(3)",
            ],
            cwd=tmp_path,
            stdout_path=out,
            stderr_path=err,
            timeout_seconds=30,
        )
    )

    assert result.exit_code == 3
    assert result.timed_out is False
    assert result.termination is None
    assert "to-stdout" in out.read_text()
    assert "to-stderr" in err.read_text()


def test_environment_overlay_reaches_the_child(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _run(
        ProcessManager().run(
            args=[sys.executable, "-c", "import os; print(os.environ['SPIKE_MARKER'])"],
            cwd=tmp_path,
            stdout_path=out,
            stderr_path=tmp_path / "err",
            timeout_seconds=30,
            env={"SPIKE_MARKER": "present"},
        )
    )

    assert result.exit_code == 0
    assert "present" in out.read_text()


def test_child_stdin_is_detached(tmp_path: Path) -> None:
    """A worker must read EOF, not inherit whatever the orchestrator has on stdin."""
    out = tmp_path / "out"
    result = _run(
        ProcessManager().run(
            args=[sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
            cwd=tmp_path,
            stdout_path=out,
            stderr_path=tmp_path / "err",
            timeout_seconds=30,
        )
    )

    assert result.exit_code == 0
    assert out.read_text().strip() == "''"


def test_timeout_reports_termination_instead_of_raising(tmp_path: Path) -> None:
    """The B1 regression: this raised AttributeError on Windows before the fix."""
    result = _run(
        ProcessManager().run(
            args=[sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=tmp_path,
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
            timeout_seconds=1,
        )
    )

    assert result.timed_out is True
    assert result.termination in {"sigterm", "sigkill", "taskkill_tree", "already_exited"}
    assert result.exit_code != 0


def test_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    """A grandchild must not survive its launcher being stopped.

    Deliberately not written as "set a 2s deadline and hope the grandchild started in
    time" — that races python's startup and fails on a loaded machine without telling
    you anything about process trees. Instead: wait for proof the grandchild is alive,
    then stop the launcher, then check the grandchild went quiet.
    """
    heartbeat_script = tmp_path / "heartbeat.py"
    heartbeat_script.write_text(HEARTBEAT_SOURCE, encoding="utf-8")
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(PARENT_SOURCE, encoding="utf-8")
    beacon = tmp_path / "beacon.txt"

    async def scenario() -> ProcessResult:
        manager = ProcessManager()
        handle = await manager.start(
            args=[sys.executable, str(parent_script), str(heartbeat_script), str(beacon)],
            cwd=tmp_path,
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
        )

        deadline = time.monotonic() + 30
        while not beacon.exists():
            if time.monotonic() > deadline:
                raise AssertionError("grandchild never started; the test proves nothing")
            await asyncio.sleep(0.05)

        await manager.cancel(handle)
        return await manager.wait(handle, timeout_seconds=30)

    result = _run(scenario())

    assert result.cancelled is True
    assert result.timed_out is False

    # Let anything still alive keep writing, then confirm the beacon has gone quiet.
    time.sleep(1.0)
    settled = beacon.stat().st_size
    time.sleep(1.0)
    assert beacon.stat().st_size == settled, "grandchild outlived the kill"


def test_timeout_still_kills_a_tree_it_owns(tmp_path: Path) -> None:
    """Same guarantee, reached through the deadline rather than an explicit cancel."""
    heartbeat_script = tmp_path / "heartbeat.py"
    heartbeat_script.write_text(HEARTBEAT_SOURCE, encoding="utf-8")
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(PARENT_SOURCE, encoding="utf-8")
    beacon = tmp_path / "beacon.txt"

    async def scenario() -> ProcessResult:
        manager = ProcessManager()
        handle = await manager.start(
            args=[sys.executable, str(parent_script), str(heartbeat_script), str(beacon)],
            cwd=tmp_path,
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
        )
        deadline = time.monotonic() + 30
        while not beacon.exists():
            if time.monotonic() > deadline:
                raise AssertionError("grandchild never started; the test proves nothing")
            await asyncio.sleep(0.05)
        # The grandchild is provably alive; now let the deadline do the killing.
        return await manager.wait(handle, timeout_seconds=1)

    result = _run(scenario())

    assert result.timed_out is True
    time.sleep(1.0)
    settled = beacon.stat().st_size
    time.sleep(1.0)
    assert beacon.stat().st_size == settled, "grandchild outlived the timeout kill"


# ---------------------------------------------------------------------------
# start / wait / cancel
# ---------------------------------------------------------------------------


def test_start_returns_a_live_handle(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, ProcessResult]:
        manager = ProcessManager()
        handle = await manager.start(
            args=[sys.executable, "-c", "import time; time.sleep(0.3); print('done')"],
            cwd=tmp_path,
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
        )
        pid = handle.pid
        assert handle.running is True
        return pid, await manager.wait(handle, timeout_seconds=30)

    pid, result = _run(scenario())

    assert pid > 0
    assert result.exit_code == 0
    assert result.cancelled is False
    assert "done" in (tmp_path / "out").read_text()


def test_cancel_marks_the_result_and_is_not_a_timeout(tmp_path: Path) -> None:
    async def scenario() -> ProcessResult:
        manager = ProcessManager()
        handle = await manager.start(
            args=[sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=tmp_path,
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
        )
        await manager.cancel(handle)
        return await manager.wait(handle, timeout_seconds=30)

    result = _run(scenario())

    assert result.cancelled is True
    assert result.timed_out is False
    assert result.termination is not None


def test_cancel_is_safe_on_an_already_finished_process(tmp_path: Path) -> None:
    async def scenario() -> ProcessResult:
        manager = ProcessManager()
        handle = await manager.start(
            args=[sys.executable, "-c", "pass"],
            cwd=tmp_path,
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
        )
        result = await manager.wait(handle, timeout_seconds=30)
        assert await manager.cancel(handle) == "already_exited"
        return result

    assert _run(scenario()).exit_code == 0


def test_start_rejects_empty_args(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="args must not be empty"):
        _run(
            ProcessManager().start(
                args=[],
                cwd=tmp_path,
                stdout_path=tmp_path / "out",
                stderr_path=tmp_path / "err",
            )
        )


# ---------------------------------------------------------------------------
# POSIX termination path
#
# The target machine is Windows with no WSL distribution, so this branch cannot be
# exercised for real here. These tests substitute the process-group syscalls to pin
# the two things most likely to be wrong: that the signal targets the *group id*
# rather than the pid, and that SIGTERM escalates to SIGKILL.
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Stands in for asyncio's Process. First wait() hangs; later ones return."""

    pid = 4321

    def __init__(self, *, exits_on_first_wait: bool) -> None:
        self.exits_on_first_wait = exits_on_first_wait
        self.waits = 0

    async def wait(self) -> int:
        self.waits += 1
        if self.waits == 1 and not self.exits_on_first_wait:
            await asyncio.sleep(30)
        return -9


@pytest.fixture
def fake_process_group(monkeypatch):
    """Make the POSIX branch runnable off-POSIX.

    Patches `sys.platform` as well as the syscalls, because `_terminate_posix` refuses
    to run on Windows — that guard is what keeps `os.killpg` out of the type checker's
    way when checking on Windows.
    """
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1000, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), raising=False)
    monkeypatch.setattr(signal, "SIGKILL", getattr(signal, "SIGKILL", 9), raising=False)
    monkeypatch.setattr(process_manager_module, "TERMINATE_GRACE_SECONDS", 0.05)
    return sent


def test_posix_signals_the_group_not_the_pid(fake_process_group) -> None:
    process = _FakeProcess(exits_on_first_wait=True)
    reason = _run(ProcessManager._terminate_posix(process))

    assert reason == "sigterm"
    # 5321, not 4321: the pid and the group id are only equal by convention.
    assert fake_process_group == [(5321, signal.SIGTERM)]


def test_posix_escalates_to_sigkill_after_the_grace_period(fake_process_group) -> None:
    process = _FakeProcess(exits_on_first_wait=False)
    reason = _run(ProcessManager._terminate_posix(process))

    assert reason == "sigkill"
    assert fake_process_group == [(5321, signal.SIGTERM), (5321, signal.SIGKILL)]


def test_posix_reports_a_process_that_already_exited(monkeypatch) -> None:
    def _gone(pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getpgid", _gone, raising=False)
    reason = _run(ProcessManager._terminate_posix(_FakeProcess(exits_on_first_wait=True)))

    assert reason == "already_exited"
