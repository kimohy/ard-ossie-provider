from __future__ import annotations

import os
from pathlib import Path

from ard_ossie.application.output import ResultWriter


def result_writer(repository: Path, command: str) -> ResultWriter:
    output = Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None
    summary = (
        Path(os.environ["GITHUB_STEP_SUMMARY"])
        if os.environ.get("GITHUB_STEP_SUMMARY")
        else None
    )
    return ResultWriter(
        result_path=repository / ".ard" / "run" / f"{command}-result.json",
        github_output=output,
        github_summary=summary,
    )
