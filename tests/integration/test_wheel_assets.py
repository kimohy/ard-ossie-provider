from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path


def test_wheel_contains_runtime_templates_and_schemas(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache")},
    )
    wheel = next(tmp_path.glob("*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "ard_ossie/assets/templates/data-product.md.j2" in names
    assert "ard_ossie/assets/templates/data-semantic.md.j2" in names
    assert "ard_ossie/assets/schemas/ossie/0.1.1/osi-schema.json" in names
