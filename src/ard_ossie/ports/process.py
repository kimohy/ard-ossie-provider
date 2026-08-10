from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandRequest:
    argv: tuple[str, ...]
    cwd: Path | None = None
    stdin: str | None = None
    env: Mapping[str, str] | None = None
    timeout_seconds: int = 60
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, request: CommandRequest) -> CommandResult: ...
