"""POSIX file modes, checked exactly where the OS can express them (B3.3, Windows CI).

On Windows `os.chmod` sets only the read-only flag and `os.stat` reports 0o666 or 0o444 (0o777
or 0o555 for a folder) whatever the file's ACLs say, so a 0o600 or 0o700 cannot be read back
there. The tests that pin a mode call `mode_problems`: the exact mode on Linux and macOS; on
Windows, that the file exists, is the right kind, and its owner can still write it when the
mode says so (a rerun rewrites it). The rest of each test runs on every OS.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

POSIX_MODES = sys.platform != "win32"


def mode_problems(path: Path, want: int, *, posix: bool | None = None) -> list[str]:
    posix = POSIX_MODES if posix is None else posix
    st = path.stat()  # raises when the file was never written
    got = stat.S_IMODE(st.st_mode)
    if posix:
        return [] if got == want else [f"{path.name}: mode {oct(got)}, expected {oct(want)}"]
    if want & stat.S_IWUSR and not got & stat.S_IWUSR:
        return [f"{path.name}: read-only, expected the owner to be able to write it"]
    return []
