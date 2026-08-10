from __future__ import annotations

import subprocess
import sys


def test_application_services_import_without_concrete_filesystem_adapter() -> None:
    """Application modules must remain loadable when infrastructure adapters are absent."""
    program = """
import builtins

original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "ard_ossie.adapters.filesystem":
        raise AssertionError("module imported a concrete filesystem adapter at load time")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import ard_ossie.application.modeling
import ard_ossie.application.parsing
import ard_ossie.cli
"""

    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
