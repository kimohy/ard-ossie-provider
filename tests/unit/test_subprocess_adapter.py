from __future__ import annotations

import subprocess
import sys
import time

import pytest

from ard_ossie.adapters.subprocess import SubprocessRunner
from ard_ossie.application.contracts import WorkflowTransientError
from ard_ossie.ports.process import CommandRequest


def test_runner_executes_argument_array_and_captures_both_streams() -> None:
    """Dropping either stream or changing argument boundaries would hide command evidence."""
    result = SubprocessRunner().run(
        CommandRequest(
            argv=(
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr)",
            ),
            timeout_seconds=5,
        )
    )

    assert result.returncode == 0
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


def test_runner_redacts_registered_secrets_from_both_streams() -> None:
    """A registered secret must never survive in returned process evidence."""
    request = CommandRequest(
        argv=(
            sys.executable,
            "-c",
            "import sys; value=sys.stdin.read(); print(value); print(value, file=sys.stderr)",
        ),
        stdin="sentinel-key",
        secrets=("sentinel-key",),
    )

    result = SubprocessRunner().run(request)

    assert result.stdout == "***\n"
    assert result.stderr == "***\n"


def test_runner_returns_nonzero_exit_without_reclassifying_it() -> None:
    """Adapters above the runner need the real return code to classify failures."""
    result = SubprocessRunner().run(
        CommandRequest(argv=(sys.executable, "-c", "raise SystemExit(17)"))
    )

    assert result.returncode == 17


def test_runner_redacts_timeout_evidence() -> None:
    """Timeout diagnostics must not bypass the ordinary stream redaction path."""
    request = CommandRequest(
        argv=(
            sys.executable,
            "-c",
            "import sys,time; print(sys.stdin.read(), flush=True); time.sleep(5)",
        ),
        stdin="sentinel-key",
        secrets=("sentinel-key",),
        timeout_seconds=1,
    )

    with pytest.raises(WorkflowTransientError, match="COMMAND_TIMEOUT") as raised:
        SubprocessRunner().run(request)

    assert "sentinel-key" not in str(raised.value)


def test_runner_bounds_each_returned_stream() -> None:
    """Untrusted tools must not return unbounded stdout or stderr to workflow results."""
    result = SubprocessRunner(max_output_bytes=1024).run(
        CommandRequest(
            argv=(
                sys.executable,
                "-c",
                "import sys; print('x'*2000); print('y'*2000, file=sys.stderr)",
            )
        )
    )

    assert len(result.stdout.encode()) <= 1024
    assert len(result.stderr.encode()) <= 1024


def test_runner_does_not_buffer_complete_child_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The byte limit must apply while pipes are drained, not after capture completes."""

    def reject_unbounded_capture(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run capture_output buffers complete streams")

    monkeypatch.setattr(subprocess, "run", reject_unbounded_capture)

    result = SubprocessRunner(max_output_bytes=128).run(
        CommandRequest(
            argv=(
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x'*100000); sys.stderr.write('y'*100000)",
            )
        )
    )

    assert result.returncode == 0
    assert len(result.stdout.encode()) == 128
    assert len(result.stderr.encode()) == 128


def test_binary_runner_bounds_capture_and_reports_truncation() -> None:
    result = SubprocessRunner().run_bytes(
        CommandRequest(
            argv=(
                sys.executable,
                "-c",
                "import sys; "
                "sys.stdout.buffer.write(b'x'*100000); "
                "sys.stderr.buffer.write(b'y'*100000)",
            )
        ),
        max_output_bytes=128,
    )

    assert result.returncode == 0
    assert result.stdout == b"x" * 128
    assert result.stderr == b"y" * 128
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_binary_runner_preserves_non_utf8_bytes() -> None:
    result = SubprocessRunner().run_bytes(
        CommandRequest(
            argv=(
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(bytes([0, 255, 128]))",
            )
        ),
        max_output_bytes=128,
    )

    assert result.stdout == b"\x00\xff\x80"
    assert result.stdout_truncated is False


def test_runner_deadline_is_not_held_open_by_descendant_pipes() -> None:
    """A spawned descendant must not extend the command beyond its overall deadline."""
    started = time.monotonic()

    result = SubprocessRunner().run(
        CommandRequest(
            argv=(
                sys.executable,
                "-c",
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)'])",
            ),
            timeout_seconds=1,
        )
    )

    assert result.returncode == 0
    assert time.monotonic() - started < 2


@pytest.mark.parametrize(
    "command_request",
    [
        CommandRequest(argv=()),
        CommandRequest(argv=("tool\x00name",)),
        CommandRequest(argv=("tool",), timeout_seconds=0),
    ],
)
def test_runner_rejects_malformed_requests(command_request: CommandRequest) -> None:
    """Malformed command boundaries must fail before a child process is started."""
    with pytest.raises(ValueError):
        SubprocessRunner().run(command_request)
