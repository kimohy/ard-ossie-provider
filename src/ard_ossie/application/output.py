from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from ard_ossie.application.contracts import WorkflowResult

_OUTPUT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class ResultWriter:
    def __init__(
        self,
        *,
        result_path: Path,
        github_output: Path | None = None,
        github_summary: Path | None = None,
    ) -> None:
        self.result_path = result_path
        self.github_output = github_output
        self.github_summary = github_summary

    def write(self, result: WorkflowResult) -> None:
        output_text = _render_outputs(result.outputs) if self.github_output is not None else None
        summary_text = _render_summary(result) if self.github_summary is not None else None
        if self.github_output is not None and output_text is not None:
            _append_text(self.github_output, output_text)
        if self.github_summary is not None and summary_text is not None:
            _append_text(self.github_summary, summary_text)
        self._write_result(result)

    def _write_result(self, result: WorkflowResult) -> None:
        self.result_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.result_path.parent,
                prefix=f".{self.result_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(result.model_dump_json(indent=2))
                temporary.write("\n")
                temporary.flush()
                temporary_path = Path(temporary.name)
            temporary_path.replace(self.result_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def _render_outputs(outputs: dict[str, Any]) -> str:
    rendered: list[str] = []
    for key in sorted(outputs):
        if not _OUTPUT_KEY.fullmatch(key):
            raise ValueError(f"INVALID_GITHUB_OUTPUT_KEY: {key}")
        value = _github_value(outputs[key])
        if "\n" not in value and "\r" not in value:
            rendered.append(f"{key}={value}\n")
            continue
        delimiter = _output_delimiter(key, value)
        rendered.append(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    return "".join(rendered)


def _render_summary(result: WorkflowResult) -> str:
    artifacts = "<br>".join(_markdown_cell(item) for item in result.artifacts) or "None"
    mutations = (
        "<br>".join(
            _markdown_cell(f"{item.resource}:{item.action}:{item.target}")
            for item in result.mutations
        )
        or "None"
    )
    rows = (
        ("Command", result.command),
        ("Status", result.status.value),
        ("Artifacts", artifacts),
        ("Findings", str(len(result.findings))),
        ("Mutations", mutations),
        ("Retryable", str(result.retryable).lower()),
    )
    rendered = ["## ARD workflow result\n\n| Field | Value |\n| --- | --- |\n"]
    rendered.extend(f"| {label} | {_markdown_cell(value)} |\n" for label, value in rows)
    return "".join(rendered)


def _append_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def _github_value(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _output_delimiter(key: str, value: str) -> str:
    occupied = set(value.splitlines())
    counter = 0
    while True:
        material = f"{key}\0{counter}\0{value}".encode()
        delimiter = f"ARD_OUTPUT_{hashlib.sha256(material).hexdigest()[:24].upper()}"
        if delimiter not in occupied:
            return delimiter
        counter += 1


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")
