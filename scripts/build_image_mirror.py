from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build local web mirror for Mini App item images.")
    p.add_argument("--db-path", default="data/state.db")
    p.add_argument("--day", default="")
    p.add_argument("--out-dir", default="webapp/img-cache/current")
    p.add_argument("--timeout", type=float, default=12.0)
    return p.parse_args()


def _guess_ext(url: str, content_type: str) -> str:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype == "image/webp":
        return ".webp"
    if ctype in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if ctype == "image/png":
        return ".png"
    if ctype == "image/avif":
        return ".avif"
    if ctype == "image/gif":
        return ".gif"

    path = urlsplit(url).path
    guessed = Path(path).suffix.lower()
    if guessed in {".webp", ".jpg", ".jpeg", ".png", ".avif", ".gif"}:
        return ".jpg" if guessed == ".jpeg" else guessed

    by_mime = mimetypes.guess_extension(ctype or "")
    if by_mime:
        return ".jpg" if by_mime.lower() == ".jpe" else by_mime.lower()
    return ".img"


def _download(url: str, timeout: float) -> tuple[bytes, str]:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    return body, ctype


def main() -> None:
    args = parse_args()
    day = args.day.strip() or datetime.now().strftime("%Y-%m-%d")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    map_path = out_dir / "map.json"

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT item_id, image_url
        FROM items
        WHERE day = ?
          AND COALESCE(image_url, '') <> ''
        """,
        (day,),
    ).fetchall()
    conn.close()

    mirrored: dict[str, str] = {}
    failed: list[dict[str, str]] = []
    updated = 0
    reused = 0

    for row in rows:
        item_id = str(row["item_id"] or "").strip()
        image_url = str(row["image_url"] or "").strip()
        if not item_id or not image_url:
            continue

        try:
            body, ctype = _download(image_url, timeout=float(args.timeout))
            ext = _guess_ext(image_url, ctype)
            filename = f"{item_id}{ext}"
            abs_path = out_dir / filename
            rel_path = f"img-cache/current/{filename}"
            if abs_path.exists() and abs_path.read_bytes() == body:
                reused += 1
            else:
                abs_path.write_bytes(body)
                updated += 1
            mirrored[item_id] = rel_path
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            failed.append({"item_id": item_id, "error": str(exc)})
            continue

    # Cleanup stale files that are not referenced anymore.
    keep_names = {Path(v).name for v in mirrored.values()}
    removed = 0
    for child in out_dir.iterdir():
        if not child.is_file():
            continue
        if child.name == "map.json":
            continue
        if child.name not in keep_names:
            try:
                child.unlink()
                removed += 1
            except OSError:
                pass

    payload = {
        "day": day,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(mirrored),
        "items": mirrored,
    }
    map_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "day": day,
                "mirrored": len(mirrored),
                "updated": updated,
                "reused": reused,
                "removed": removed,
                "failed": len(failed),
                "map": str(map_path).replace("\\", "/"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

