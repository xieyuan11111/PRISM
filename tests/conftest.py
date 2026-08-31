"""Test-session compatibility shims for the PRISM test suite.

This sandboxed environment denies access to directories created with a POSIX
mode of ``0o700`` (pytest's hardcoded mode for its temporary directories),
which would otherwise make the ``tmp_path`` fixtures unusable.  Neutralize the
mode so every temporary directory is created with the default
Windows-friendly ACL; the mode is meaningless on Windows anyway.
"""

from __future__ import annotations

import pathlib

_original_mkdir = pathlib.Path.mkdir


def _mkdir_ignore_mode(self, mode=0o777, parents=False, exist_ok=False):
    return _original_mkdir(self, mode=0o777, parents=parents, exist_ok=exist_ok)


pathlib.Path.mkdir = _mkdir_ignore_mode
