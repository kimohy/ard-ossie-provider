from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from ard_ossie.application.contracts import (
    WorkflowConfigurationError,
    WorkflowTransientError,
)
from ard_ossie.ports.process import BinaryCommandResult, CommandRequest, CommandResult

_DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_BINARY_OUTPUT_BYTES = 32 * 1024 * 1024
_MAX_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class _CapturedCommand:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


class SubprocessRunner:
    def __init__(self, *, max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES) -> None:
        if not 1 <= max_output_bytes <= _DEFAULT_MAX_OUTPUT_BYTES:
            raise ValueError("max_output_bytes must be between 1 and 1048576")
        self.max_output_bytes = max_output_bytes

    def run(self, request: CommandRequest) -> CommandResult:
        capture_limit = self.max_output_bytes + max(
            (len(secret.encode("utf-8")) for secret in request.secrets),
            default=0,
        )
        captured = _execute_command(request, capture_limit=capture_limit)
        sanitized_stdout = self._sanitize(captured.stdout, request.secrets)
        sanitized_stderr = self._sanitize(captured.stderr, request.secrets)
        if captured.timed_out:
            evidence = _timeout_evidence(sanitized_stdout, sanitized_stderr)
            raise WorkflowTransientError(
                "COMMAND_TIMEOUT",
                f"command exceeded {request.timeout_seconds} seconds{evidence}",
            ) from None

        return CommandResult(
            returncode=captured.returncode,
            stdout=sanitized_stdout,
            stderr=sanitized_stderr,
        )

    def run_bytes(
        self,
        request: CommandRequest,
        *,
        max_output_bytes: int,
    ) -> BinaryCommandResult:
        if not 1 <= max_output_bytes <= _MAX_BINARY_OUTPUT_BYTES:
            raise ValueError("max_output_bytes must be between 1 and 33554432")
        if request.secrets:
            raise ValueError("binary commands cannot expose registered secrets")
        captured = _execute_command(request, capture_limit=max_output_bytes + 1)
        if captured.timed_out:
            stdout = _truncate_utf8(_as_text(captured.stdout), self.max_output_bytes)
            stderr = _truncate_utf8(_as_text(captured.stderr), self.max_output_bytes)
            evidence = _timeout_evidence(stdout, stderr)
            raise WorkflowTransientError(
                "COMMAND_TIMEOUT",
                f"command exceeded {request.timeout_seconds} seconds{evidence}",
            ) from None
        return BinaryCommandResult(
            returncode=captured.returncode,
            stdout=captured.stdout[:max_output_bytes],
            stderr=captured.stderr[:max_output_bytes],
            stdout_truncated=len(captured.stdout) > max_output_bytes,
            stderr_truncated=len(captured.stderr) > max_output_bytes,
        )

    def _sanitize(self, value: Any, secrets: tuple[str, ...]) -> str:
        text = _as_text(value)
        redacted = _redact(text, secrets)
        return _truncate_utf8(redacted, self.max_output_bytes)


def _execute_command(
    request: CommandRequest,
    *,
    capture_limit: int,
) -> _CapturedCommand:
    _validate_request(request)
    deadline = time.monotonic() + request.timeout_seconds
    try:
        process = subprocess.Popen(
            list(request.argv),
            cwd=request.cwd,
            env=dict(request.env) if request.env is not None else None,
            stdin=subprocess.PIPE if request.stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name == "posix",
        )
    except (FileNotFoundError, PermissionError, OSError) as error:
        message = _redact(str(error), request.secrets)
        raise WorkflowConfigurationError("COMMAND_START_FAILED", message) from None

    stdout = bytearray()
    stderr = bytearray()
    readers = [
        threading.Thread(
            target=_drain_bounded,
            args=(process.stdout, stdout, capture_limit),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, stderr, capture_limit),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    writer = _start_stdin_writer(process, request.stdin)
    timed_out = False
    try:
        process.wait(timeout=max(deadline - time.monotonic(), 0.001))
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.kill()
    finally:
        _terminate_process_group(process)
        cleanup_deadline = max(deadline, time.monotonic() + 0.25)
        streams_and_threads = [
            (process.stdin, writer),
            (process.stdout, readers[0]),
            (process.stderr, readers[1]),
        ]
        for stream, thread in streams_and_threads:
            if thread is None:
                continue
            thread.join(timeout=max(cleanup_deadline - time.monotonic(), 0))
            if thread.is_alive():
                timed_out = True
                if stream is not None:
                    with suppress(OSError):
                        stream.close()

    return _CapturedCommand(
        returncode=int(process.returncode) if process.returncode is not None else -1,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
        timed_out=timed_out,
    )


def _validate_request(request: CommandRequest) -> None:
    if not request.argv:
        raise ValueError("argv must not be empty")
    if not 1 <= request.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout_seconds must be between 1 and 3600")
    values = (*request.argv, *(request.env or {}).keys(), *(request.env or {}).values())
    if any("\x00" in value for value in values):
        raise ValueError("command values must not contain NUL")


def _drain_bounded(
    stream: Any,
    retained: bytearray,
    maximum: int,
) -> None:
    if stream is None:
        return
    try:
        try:
            while chunk := stream.read(65536):
                remaining = maximum - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
        except (OSError, ValueError):
            pass
    finally:
        with suppress(OSError):
            stream.close()


def _start_stdin_writer(
    process: subprocess.Popen[bytes],
    value: str | None,
) -> threading.Thread | None:
    if value is None or process.stdin is None:
        return None

    def write() -> None:
        try:
            process.stdin.write(value.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    return writer


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        return
    if process.poll() is None:
        process.kill()


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        redacted = redacted.replace(secret, "***")
    return redacted


def _truncate_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _timeout_evidence(stdout: str, stderr: str) -> str:
    evidence = []
    if stdout:
        evidence.append(f"stdout={stdout!r}")
    if stderr:
        evidence.append(f"stderr={stderr!r}")
    return f" ({', '.join(evidence)})" if evidence else ""
