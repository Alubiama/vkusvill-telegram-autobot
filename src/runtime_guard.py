from __future__ import annotations

import os
from pathlib import Path


PROJECT_KEY = "vkusvill-bot"
_registry_env = os.environ.get("REGISTRY_PATH")
REGISTRY_PATH: Path | None = Path(_registry_env) if _registry_env else None


def current_project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_registry_project_path(
    project_key: str = PROJECT_KEY,
    registry_path: Path | None = None,
) -> Path | None:
    path = registry_path or REGISTRY_PATH
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    header = f"### {project_key}"
    for idx, line in enumerate(lines):
        if line.strip() != header:
            continue
        for candidate in lines[idx + 1 : idx + 8]:
            stripped = candidate.strip()
            if stripped.startswith("### "):
                break
            prefix = "- **Path:**"
            if stripped.startswith(prefix):
                raw = stripped[len(prefix) :].strip()
                if not raw:
                    return None
                try:
                    return Path(raw).expanduser().resolve()
                except OSError:
                    return Path(raw).expanduser()
        break
    return None


def describe_runtime_root(
    project_root: Path | None = None,
    registry_path: Path | None = None,
    project_key: str = PROJECT_KEY,
) -> tuple[str, str, Path | None]:
    current_root = (project_root or current_project_root()).resolve()
    registered_root = read_registry_project_path(project_key=project_key, registry_path=registry_path)
    if registered_root is None:
        return (
            "warning",
            f"registry entry {project_key} not found in {registry_path or REGISTRY_PATH}",
            None,
        )

    registered_root = registered_root.resolve()
    if registered_root != current_root:
        return (
            "error",
            f"current={current_root}; canonical={registered_root}",
            registered_root,
        )

    return (
        "ok",
        f"canonical workspace confirmed: {current_root}",
        registered_root,
    )
