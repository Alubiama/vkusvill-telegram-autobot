from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clone Chrome profile to a local non-default user-data directory.")
    parser.add_argument("--src-root", default=None, help="Source Chrome User Data root.")
    parser.add_argument(
        "--profile-name",
        default="auto",
        help='Profile folder name (Default, Profile 1, ...). Use "auto" for last used profile.',
    )
    parser.add_argument("--dst-root", default="data/chrome-user-data", help="Destination user data root.")
    return parser.parse_args()


def _default_src_root() -> Path:
    local_app_data = Path.home() / "AppData" / "Local"
    return local_app_data / "Google" / "Chrome" / "User Data"


def _copy_file(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _detect_last_profile(src_root: Path) -> str:
    local_state = src_root / "Local State"
    if not local_state.exists():
        return "Default"
    try:
        payload = json.loads(local_state.read_text(encoding="utf-8"))
        prof = payload.get("profile", {}).get("last_used")
        if isinstance(prof, str) and prof.strip():
            return prof.strip()
    except Exception:
        pass
    return "Default"


def main() -> None:
    args = parse_args()
    src_root = Path(args.src_root) if args.src_root else _default_src_root()
    dst_root = Path(args.dst_root)
    profile_name = args.profile_name
    if profile_name.lower() == "auto":
        profile_name = _detect_last_profile(src_root)

    src_profile = src_root / profile_name
    dst_profile = dst_root / profile_name

    if not src_profile.exists():
        raise SystemExit(f"Source profile not found: {src_profile}")

    if dst_root.exists():
        shutil.rmtree(dst_root, ignore_errors=True)
    dst_root.mkdir(parents=True, exist_ok=True)

    # Needed for cookie decryption / profile metadata.
    _copy_file(src_root / "Local State", dst_root / "Local State")

    ignore = shutil.ignore_patterns(
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "DawnCache",
        "ShaderCache",
        "Crashpad",
        "BrowserMetrics",
        "Singleton*",
        "LOCK",
        "lockfile",
        "*.log",
        "Safe Browsing",
    )
    shutil.copytree(src_profile, dst_profile, dirs_exist_ok=True, ignore=ignore)

    print(f"profile_name={profile_name}")
    print(f"cloned_profile={src_profile}")
    print(f"destination={dst_profile}")


if __name__ == "__main__":
    main()
