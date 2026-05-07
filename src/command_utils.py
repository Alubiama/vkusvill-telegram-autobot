from __future__ import annotations

import os
import shlex
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _strip_wrapping_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


def command_to_args(command: str) -> list[str]:
    # Expand Windows-style %VAR% and POSIX-style $VAR env refs before splitting.
    expanded = os.path.expandvars(command or "").strip()
    if not expanded:
        return []
    raw = shlex.split(expanded, posix=False)
    return [_strip_wrapping_quotes(x) for x in raw]
