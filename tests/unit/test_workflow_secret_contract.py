from __future__ import annotations

import re
import unittest
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

ROOT = Path(__file__).parents[2]
PROCESSOR = "./.github/workflows/ard-process.yml"
TRUSTED_CALLERS = (
    Path(".github/workflows/ard-issue-intake.yml"),
    Path(".github/workflows/ard-direct-change.yml"),
)
SECRET_CONTEXT = re.compile(r"\$\{\{.*?\bsecrets\b.*?\}\}", re.DOTALL)


def _workflow(path: Path) -> dict[str, object]:
    payload = yaml.load((ROOT / path).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict)
    return payload


def _workflow_paths(root: Path) -> tuple[Path, ...]:
    workflows = root / ".github/workflows"
    return tuple(sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml"))))


def _secret_references(value: object) -> Iterator[str]:
    if isinstance(value, str):
        if SECRET_CONTEXT.search(value):
            yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _secret_references(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _secret_references(item)


class TestWorkflowSecretContract(unittest.TestCase):
    def test_trusted_processor_callers_inherit_secrets(self) -> None:
        for path in TRUSTED_CALLERS:
            with self.subTest(path=path):
                jobs = _workflow(path)["jobs"]
                self.assertIsInstance(jobs, dict)
                process = jobs["process"]
                self.assertIsInstance(process, dict)

                self.assertEqual(process["uses"], PROCESSOR)
                self.assertEqual(process.get("secrets"), "inherit")

    def test_only_trusted_processor_calls_inherit_secrets(self) -> None:
        actual: set[tuple[Path, str]] = set()
        for workflow_path in _workflow_paths(ROOT):
            relative = workflow_path.relative_to(ROOT)
            jobs = _workflow(relative)["jobs"]
            self.assertIsInstance(jobs, dict)
            for job_name, job in jobs.items():
                if isinstance(job, dict) and job.get("secrets") == "inherit":
                    actual.add((relative, job_name))

        self.assertEqual(actual, {(path, "process") for path in TRUSTED_CALLERS})

    def test_attachment_secret_is_limited_to_private_intake_commands(self) -> None:
        issue_path = Path(".github/workflows/ard-issue-intake.yml")
        jobs = _workflow(issue_path)["jobs"]
        self.assertIsInstance(jobs, dict)
        references: set[tuple[str, str]] = set()
        for job_name, job in jobs.items():
            if isinstance(job, dict):
                for step in job.get("steps", []):
                    if isinstance(step, dict) and "ARD_ATTACHMENT_TOKEN" in str(step):
                        references.add((job_name, str(step.get("id"))))

        self.assertEqual(
            references,
            {("intake", "intake"), ("base_sync", "base_sync")},
        )
        for path in _workflow_paths(ROOT):
            if path.relative_to(ROOT) != issue_path:
                self.assertNotIn(
                    "ARD_ATTACHMENT_TOKEN",
                    path.read_text(encoding="utf-8"),
                )

    def test_workflow_discovery_covers_both_supported_yaml_extensions(self) -> None:
        with TemporaryDirectory() as value:
            root = Path(value)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "first.yml").touch()
            (workflows / "second.yaml").touch()

            self.assertEqual(
                tuple(path.name for path in _workflow_paths(root)),
                ("first.yml", "second.yaml"),
            )

    def test_secret_reference_detector_covers_github_expression_forms(self) -> None:
        expressions = (
            "${{ secrets.ARD_LLM_API_KEY }}",
            "${{ secrets['ARD_LLM_API_KEY'] }}",
            "${{ toJSON(secrets) }}",
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                self.assertEqual(tuple(_secret_references({"value": expression})), (expression,))

    def test_only_protected_validation_and_processor_jobs_reference_secrets(self) -> None:
        jobs = _workflow(Path(".github/workflows/ard-process.yml"))["jobs"]
        self.assertIsInstance(jobs, dict)
        validation = jobs["validate"]
        self.assertIsInstance(validation, dict)
        self.assertEqual(validation["environment"], "ard-llm")
        self.assertEqual(
            {name for name in validation["env"] if name.startswith("ARD_")},
            {"ARD_SEMANTIC_PDF_PIPELINE"},
        )
        source_check = next(
            step for step in validation["steps"] if step.get("id") == "source_check"
        )
        self.assertTrue(
            all(
                not tuple(_secret_references(step))
                for step in validation["steps"]
                if step is not source_check
            )
        )

        process = jobs["process"]
        self.assertIsInstance(process, dict)
        self.assertEqual(process["environment"], "ard-llm")
        for env in (source_check["env"], process["env"]):
            self.assertEqual(
                env["ARD_LLM_API_KEY"],
                "${{ secrets.ARD_LLM_API_KEY }}",
            )
            self.assertEqual(
                env["ARD_AZURE_OPENAI_API_KEY"],
                "${{ secrets.ARD_AZURE_OPENAI_API_KEY }}",
            )
            self.assertEqual(
                env["ARD_VERTEX_CREDENTIALS_JSON"],
                "${{ secrets.ARD_VERTEX_CREDENTIALS_JSON }}",
            )
        self.assertEqual(
            {name for name, job in jobs.items() if tuple(_secret_references(job))},
            {"validate", "process"},
        )
