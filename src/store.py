from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class ItemRow:
    item_id: str
    name: str
    price: float
    discount_price: float
    source: str
    image_url: str


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    day TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    discount_price REAL NOT NULL,
                    source TEXT NOT NULL,
                    image_url TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(day, item_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS votes (
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(day, user_id, item_id)
                )
                """
            )
            # Backward-compatible migration for older local DBs.
            cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(items)").fetchall()}
            if "image_url" not in cols:
                conn.execute("ALTER TABLE items ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def upsert_items(self, day: str, items: list[ItemRow]) -> list[ItemRow]:
        existing_ids = {row.item_id for row in self.list_items(day)}
        fresh: list[ItemRow] = []
        with self._connect() as conn:
            for item in items:
                conn.execute(
                    """
                    INSERT INTO items(day, item_id, name, price, discount_price, source, image_url)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, item_id) DO UPDATE SET
                      name=excluded.name,
                      price=excluded.price,
                      discount_price=excluded.discount_price,
                      source=excluded.source,
                      image_url=excluded.image_url
                    """,
                    (day, item.item_id, item.name, item.price, item.discount_price, item.source, item.image_url),
                )
                if item.item_id not in existing_ids:
                    fresh.append(item)
        return fresh

    def sync_items(self, day: str, items: list[ItemRow]) -> tuple[list[ItemRow], int]:
        existing_ids = {row.item_id for row in self.list_items(day)}
        fresh: list[ItemRow] = []
        seen_new: set[str] = set()
        new_ids: list[str] = []

        with self._connect() as conn:
            for item in items:
                if item.item_id in seen_new:
                    continue
                seen_new.add(item.item_id)
                new_ids.append(item.item_id)
                conn.execute(
                    """
                    INSERT INTO items(day, item_id, name, price, discount_price, source, image_url)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, item_id) DO UPDATE SET
                      name=excluded.name,
                      price=excluded.price,
                      discount_price=excluded.discount_price,
                      source=excluded.source,
                      image_url=excluded.image_url
                    """,
                    (day, item.item_id, item.name, item.price, item.discount_price, item.source, item.image_url),
                )
                if item.item_id not in existing_ids:
                    fresh.append(item)

            if new_ids:
                placeholders = ",".join("?" for _ in new_ids)
                conn.execute(
                    f"DELETE FROM items WHERE day = ? AND item_id NOT IN ({placeholders})",
                    [day, *new_ids],
                )
                conn.execute(
                    f"DELETE FROM votes WHERE day = ? AND item_id NOT IN ({placeholders})",
                    [day, *new_ids],
                )
            else:
                conn.execute("DELETE FROM votes WHERE day = ?", (day,))
                conn.execute("DELETE FROM items WHERE day = ?", (day,))

        removed = len(existing_ids - set(new_ids))
        return fresh, removed

    def list_items(self, day: str) -> list[ItemRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, name, price, discount_price, source, image_url FROM items WHERE day = ? ORDER BY rowid",
                (day,),
            ).fetchall()
        return [ItemRow(**dict(row)) for row in rows]

    def set_vote(self, day: str, user_id: int, user_name: str, item_id: str, qty: int) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            if qty <= 0:
                conn.execute(
                    "DELETE FROM votes WHERE day = ? AND user_id = ? AND item_id = ?",
                    (day, user_id, item_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO votes(day, user_id, user_name, item_id, qty, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, user_id, item_id) DO UPDATE SET
                      user_name=excluded.user_name,
                      qty=excluded.qty,
                      updated_at=excluded.updated_at
                    """,
                    (day, user_id, user_name, item_id, qty, now),
                )

    def get_user_qty(self, day: str, user_id: int, item_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT qty FROM votes WHERE day = ? AND user_id = ? AND item_id = ?",
                (day, user_id, item_id),
            ).fetchone()
        return int(row["qty"]) if row else 0

    def totals_by_item(self, day: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT i.item_id, i.name, i.price, i.discount_price, IFNULL(SUM(v.qty), 0) AS qty
                FROM items i
                LEFT JOIN votes v ON v.day = i.day AND v.item_id = i.item_id
                WHERE i.day = ?
                GROUP BY i.item_id, i.name, i.price, i.discount_price
                ORDER BY qty DESC, i.name
                """,
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def votes_by_user(self, day: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, user_name, item_id, qty
                FROM votes
                WHERE day = ?
                ORDER BY user_name, item_id
                """,
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_day(self, day: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM votes WHERE day = ?", (day,))
            conn.execute("DELETE FROM items WHERE day = ?", (day,))
