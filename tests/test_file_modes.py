"""The mode helper the setup and report tests use is exact on POSIX and still bites on
Windows for what Windows can express (tests/file_modes.py)."""

import os
import stat
import sys

import pytest

from tests.file_modes import mode_problems


def test_mode_check_is_exact_on_posix(tmp_path):
    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8", newline="\n")
    if sys.platform != "win32":
        os.chmod(f, 0o600)
        assert mode_problems(f, 0o600, posix=True) == []
        os.chmod(f, 0o644)
        assert mode_problems(f, 0o600, posix=True) == ["f: mode 0o644, expected 0o600"]


def test_mode_check_on_windows_needs_the_file_and_an_owner_write_bit(tmp_path):
    f = tmp_path / "f"
    with pytest.raises(FileNotFoundError):
        mode_problems(f, 0o600, posix=False)
    f.write_text("x", encoding="utf-8", newline="\n")
    os.chmod(f, 0o666)  # what Windows reports for a writable file
    assert mode_problems(f, 0o600, posix=False) == []
    os.chmod(f, stat.S_IRUSR)  # Windows' read-only flag
    try:
        assert mode_problems(f, 0o600, posix=False) != []
    finally:
        os.chmod(f, 0o600)
