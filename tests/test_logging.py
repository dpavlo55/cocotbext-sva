"""The `sva` logger shows INFO (the end-of-test summary) unless the user set a level."""
import subprocess
import sys


def _level_after_import(setup: str) -> str:
    code = (f"import logging\n{setup}\nimport cocotbext.sva\n"
            "print(logging.getLevelName(logging.getLogger('sva').level))")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          check=True).stdout.strip()


def test_info_by_default():
    assert _level_after_import("") == "INFO"


def test_user_level_kept():
    assert _level_after_import("logging.getLogger('sva').setLevel(logging.WARNING)") == "WARNING"
