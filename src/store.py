from __future__ import annotations

import json
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
    stock_qty: int | None = None


@dataclass
class DaySnapshot:
    day: str
    snapshot_id: str
    created_at: str
    regular_count: int
    total_items: int
    status: str
    items: list[ItemRow]


@dataclass
class OrderCycle:
    day: str
    batch_id: int
    sequence: int
    status: str
    created_at: str
    updated_at: str
    finalized_at: str | None = None
    closed_at: str | None = None
    paid_at: str | None = None
    total_sum: float = 0.0
    selected_positions: int = 0
    selected_users: int = 0
    out_path: str = ""
    backup_path: str = ""
    executor_status: str = ""
    executor_ok_count: int = 0
    executor_total: int = 0


@dataclass
class BatchItemResult:
    day: str
    batch_id: int
    item_id: str
    name: str
    price: float
    discount_price: float
    requested_qty: int
    added_qty: int
    last_status: str
    last_error: str
    updated_at: str


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now_iso() -> str:
        return datetime.utcnow().isoformat()

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
                    stock_qty INTEGER,
                    PRIMARY KEY(day, item_id)
                )
                """
            )
            # Legacy votes table stays for migration and backward compatibility.
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS day_snapshots (
                    day TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    regular_count INTEGER NOT NULL,
                    total_items INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    items_json TEXT NOT NULL,
                    PRIMARY KEY(day, snapshot_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_cycles (
                    day TEXT NOT NULL,
                    batch_id INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finalized_at TEXT,
                    closed_at TEXT,
                    paid_at TEXT,
                    total_sum REAL NOT NULL DEFAULT 0,
                    selected_positions INTEGER NOT NULL DEFAULT 0,
                    selected_users INTEGER NOT NULL DEFAULT 0,
                    out_path TEXT NOT NULL DEFAULT '',
                    backup_path TEXT NOT NULL DEFAULT '',
                    executor_status TEXT NOT NULL DEFAULT '',
                    executor_ok_count INTEGER NOT NULL DEFAULT 0,
                    executor_total INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, batch_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_votes (
                    day TEXT NOT NULL,
                    batch_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(day, batch_id, user_id, item_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_item_results (
                    day TEXT NOT NULL,
                    batch_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    discount_price REAL NOT NULL,
                    requested_qty INTEGER NOT NULL,
                    added_qty INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(day, batch_id, item_id)
                )
                """
            )

            cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(items)").fetchall()}
            if "image_url" not in cols:
                conn.execute("ALTER TABLE items ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
            if "stock_qty" not in cols:
                conn.execute("ALTER TABLE items ADD COLUMN stock_qty INTEGER")

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
                    INSERT INTO items(day, item_id, name, price, discount_price, source, image_url, stock_qty)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, item_id) DO UPDATE SET
                      name=excluded.name,
                      price=excluded.price,
                      discount_price=excluded.discount_price,
                      source=excluded.source,
                      image_url=excluded.image_url,
                      stock_qty=excluded.stock_qty
                    """,
                    (
                        day,
                        item.item_id,
                        item.name,
                        item.price,
                        item.discount_price,
                        item.source,
                        item.image_url,
                        item.stock_qty,
                    ),
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
                    INSERT INTO items(day, item_id, name, price, discount_price, source, image_url, stock_qty)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, item_id) DO UPDATE SET
                      name=excluded.name,
                      price=excluded.price,
                      discount_price=excluded.discount_price,
                      source=excluded.source,
                      image_url=excluded.image_url,
                      stock_qty=excluded.stock_qty
                    """,
                    (
                        day,
                        item.item_id,
                        item.name,
                        item.price,
                        item.discount_price,
                        item.source,
                        item.image_url,
                        item.stock_qty,
                    ),
                )
                if item.item_id not in existing_ids:
                    fresh.append(item)

            if new_ids:
                placeholders = ",".join("?" for _ in new_ids)
                conn.execute(f"DELETE FROM items WHERE day = ? AND item_id NOT IN ({placeholders})", [day, *new_ids])
                conn.execute(f"DELETE FROM votes WHERE day = ? AND item_id NOT IN ({placeholders})", [day, *new_ids])
                conn.execute(
                    f"DELETE FROM batch_votes WHERE day = ? AND item_id NOT IN ({placeholders})",
                    [day, *new_ids],
                )
                conn.execute(
                    f"DELETE FROM batch_item_results WHERE day = ? AND item_id NOT IN ({placeholders})",
                    [day, *new_ids],
                )
            else:
                conn.execute("DELETE FROM votes WHERE day = ?", (day,))
                conn.execute("DELETE FROM batch_votes WHERE day = ?", (day,))
                conn.execute("DELETE FROM batch_item_results WHERE day = ?", (day,))
                conn.execute("DELETE FROM items WHERE day = ?", (day,))

        removed = len(existing_ids - set(new_ids))
        return fresh, removed

    def list_items(self, day: str) -> list[ItemRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_id, name, price, discount_price, source, image_url, stock_qty FROM items WHERE day = ? ORDER BY rowid",
                (day,),
            ).fetchall()
        return [ItemRow(**dict(row)) for row in rows]

    def _legacy_votes_exist(self, day: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM votes WHERE day = ? LIMIT 1", (day,)).fetchone()
        return row is not None

    def _batch_votes_exist(self, day: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM batch_votes WHERE day = ? LIMIT 1", (day,)).fetchone()
        return row is not None

    def _next_batch_id(self, conn: sqlite3.Connection, day: str) -> int:
        row = conn.execute("SELECT MAX(batch_id) AS v FROM order_cycles WHERE day = ?", (day,)).fetchone()
        current = int(row["v"]) if row and row["v"] is not None else 0
        return current + 1

    def get_cycle(self, day: str, batch_id: int) -> OrderCycle | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT day, batch_id, sequence, status, created_at, updated_at, finalized_at, closed_at, paid_at,
                       total_sum, selected_positions, selected_users, out_path, backup_path,
                       executor_status, executor_ok_count, executor_total
                FROM order_cycles
                WHERE day = ? AND batch_id = ?
                """,
                (day, batch_id),
            ).fetchone()
        return OrderCycle(**dict(row)) if row else None

    def list_cycles(self, day: str) -> list[OrderCycle]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT day, batch_id, sequence, status, created_at, updated_at, finalized_at, closed_at, paid_at,
                       total_sum, selected_positions, selected_users, out_path, backup_path,
                       executor_status, executor_ok_count, executor_total
                FROM order_cycles
                WHERE day = ?
                ORDER BY batch_id DESC
                """,
                (day,),
            ).fetchall()
        return [OrderCycle(**dict(row)) for row in rows]

    def get_latest_cycle(self, day: str, statuses: tuple[str, ...] | None = None) -> OrderCycle | None:
        with self._connect() as conn:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                row = conn.execute(
                    f"""
                    SELECT day, batch_id, sequence, status, created_at, updated_at, finalized_at, closed_at, paid_at,
                           total_sum, selected_positions, selected_users, out_path, backup_path,
                           executor_status, executor_ok_count, executor_total
                    FROM order_cycles
                    WHERE day = ? AND status IN ({placeholders})
                    ORDER BY batch_id DESC
                    LIMIT 1
                    """,
                    (day, *statuses),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT day, batch_id, sequence, status, created_at, updated_at, finalized_at, closed_at, paid_at,
                           total_sum, selected_positions, selected_users, out_path, backup_path,
                           executor_status, executor_ok_count, executor_total
                    FROM order_cycles
                    WHERE day = ?
                    ORDER BY batch_id DESC
                    LIMIT 1
                    """,
                    (day,),
                ).fetchone()
        return OrderCycle(**dict(row)) if row else None

    def get_open_cycle(self, day: str) -> OrderCycle | None:
        return self.get_latest_cycle(day, ("open",))

    def create_cycle(self, day: str, status: str = "open") -> OrderCycle:
        now = self._now_iso()
        with self._connect() as conn:
            batch_id = self._next_batch_id(conn, day)
            conn.execute(
                """
                INSERT INTO order_cycles(day, batch_id, sequence, status, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (day, batch_id, batch_id, status, now, now),
            )
        cycle = self.get_cycle(day, batch_id)
        if cycle is None:
            raise RuntimeError("Failed to create order cycle")
        return cycle

    def get_or_create_open_cycle(self, day: str) -> OrderCycle:
        self.migrate_legacy_votes(day)
        current = self.get_open_cycle(day)
        if current is not None:
            return current
        return self.create_cycle(day, "open")

    def migrate_legacy_votes(self, day: str) -> int:
        if self._batch_votes_exist(day) or not self._legacy_votes_exist(day):
            return 0
        cycle = self.get_open_cycle(day) or self.create_cycle(day, "open")
        moved = 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, user_name, item_id, qty, updated_at FROM votes WHERE day = ?",
                (day,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO batch_votes(day, batch_id, user_id, user_name, item_id, qty, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, batch_id, user_id, item_id) DO UPDATE SET
                      user_name=excluded.user_name,
                      qty=excluded.qty,
                      updated_at=excluded.updated_at
                    """,
                    (
                        day,
                        cycle.batch_id,
                        int(row["user_id"]),
                        str(row["user_name"]),
                        str(row["item_id"]),
                        int(row["qty"]),
                        str(row["updated_at"]),
                    ),
                )
                moved += 1
            conn.execute("DELETE FROM votes WHERE day = ?", (day,))
        self.refresh_cycle_summary(day, cycle.batch_id)
        return moved

    def update_cycle_status(self, day: str, batch_id: int, status: str, **fields: object) -> None:
        now = self._now_iso()
        allowed = {
            "finalized_at",
            "closed_at",
            "paid_at",
            "total_sum",
            "selected_positions",
            "selected_users",
            "out_path",
            "backup_path",
            "executor_status",
            "executor_ok_count",
            "executor_total",
        }
        updates = ["status = ?", "updated_at = ?"]
        values: list[object] = [status, now]
        for key, value in fields.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            values.append(value)
        values.extend([day, batch_id])
        with self._connect() as conn:
            conn.execute(
                f"UPDATE order_cycles SET {', '.join(updates)} WHERE day = ? AND batch_id = ?",
                values,
            )

    def refresh_cycle_summary(self, day: str, batch_id: int) -> None:
        totals = self.totals_by_item(day, batch_id=batch_id)
        users = self.votes_by_user(day, batch_id=batch_id)
        total_sum = sum(float(row["discount_price"]) * int(row["qty"]) for row in totals if int(row["qty"]) > 0)
        selected_positions = sum(1 for row in totals if int(row["qty"]) > 0)
        selected_users = len({int(row["user_id"]) for row in users if int(row.get("qty") or 0) > 0})
        self.update_cycle_status(
            day,
            batch_id,
            self.get_cycle(day, batch_id).status if self.get_cycle(day, batch_id) else "open",
            total_sum=round(total_sum, 2),
            selected_positions=selected_positions,
            selected_users=selected_users,
        )

    def set_vote(
        self,
        day: str,
        user_id: int,
        user_name: str,
        item_id: str,
        qty: int,
        batch_id: int | None = None,
    ) -> int:
        self.migrate_legacy_votes(day)
        cycle = self.get_cycle(day, batch_id) if batch_id is not None else self.get_or_create_open_cycle(day)
        if cycle is None:
            raise RuntimeError("Open cycle is not available")
        now = self._now_iso()
        with self._connect() as conn:
            if qty <= 0:
                conn.execute(
                    "DELETE FROM batch_votes WHERE day = ? AND batch_id = ? AND user_id = ? AND item_id = ?",
                    (day, cycle.batch_id, user_id, item_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO batch_votes(day, batch_id, user_id, user_name, item_id, qty, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, batch_id, user_id, item_id) DO UPDATE SET
                      user_name=excluded.user_name,
                      qty=excluded.qty,
                      updated_at=excluded.updated_at
                    """,
                    (day, cycle.batch_id, user_id, user_name, item_id, qty, now),
                )
        self.refresh_cycle_summary(day, cycle.batch_id)
        return cycle.batch_id

    def get_user_qty(self, day: str, user_id: int, item_id: str, batch_id: int | None = None) -> int:
        self.migrate_legacy_votes(day)
        target = self.get_cycle(day, batch_id) if batch_id is not None else self.get_open_cycle(day)
        if target is None:
            return 0
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT qty FROM batch_votes
                WHERE day = ? AND batch_id = ? AND user_id = ? AND item_id = ?
                """,
                (day, target.batch_id, user_id, item_id),
            ).fetchone()
        return int(row["qty"]) if row else 0

    def totals_by_item(self, day: str, batch_id: int | None = None) -> list[dict]:
        self.migrate_legacy_votes(day)
        target = self.get_cycle(day, batch_id) if batch_id is not None else self.get_open_cycle(day)
        if target is None:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT i.item_id, i.name, i.price, i.discount_price, IFNULL(SUM(v.qty), 0) AS qty
                FROM items i
                LEFT JOIN batch_votes v
                  ON v.day = i.day AND v.item_id = i.item_id AND v.batch_id = ?
                WHERE i.day = ?
                GROUP BY i.item_id, i.name, i.price, i.discount_price
                ORDER BY qty DESC, i.name
                """,
                (target.batch_id, day),
            ).fetchall()
        return [dict(row) for row in rows]

    def votes_by_user(self, day: str, batch_id: int | None = None) -> list[dict]:
        self.migrate_legacy_votes(day)
        target = self.get_cycle(day, batch_id) if batch_id is not None else self.get_open_cycle(day)
        if target is None:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, user_name, item_id, qty
                FROM batch_votes
                WHERE day = ? AND batch_id = ?
                ORDER BY user_name, item_id
                """,
                (day, target.batch_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_day(self, day: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM batch_item_results WHERE day = ?", (day,))
            conn.execute("DELETE FROM batch_votes WHERE day = ?", (day,))
            conn.execute("DELETE FROM order_cycles WHERE day = ?", (day,))
            conn.execute("DELETE FROM day_snapshots WHERE day = ?", (day,))
            conn.execute("DELETE FROM votes WHERE day = ?", (day,))
            conn.execute("DELETE FROM items WHERE day = ?", (day,))

    def clear_votes(self, day: str, batch_id: int | None = None) -> int | None:
        self.migrate_legacy_votes(day)
        target = self.get_cycle(day, batch_id) if batch_id is not None else self.get_open_cycle(day)
        with self._connect() as conn:
            conn.execute("DELETE FROM votes WHERE day = ?", (day,))
            if target is None:
                return None
            conn.execute("DELETE FROM batch_votes WHERE day = ? AND batch_id = ?", (day, target.batch_id))
        self.refresh_cycle_summary(day, target.batch_id)
        return target.batch_id

    def clear_user_votes(self, day: str, user_id: int, batch_id: int | None = None) -> int | None:
        self.migrate_legacy_votes(day)
        target = self.get_cycle(day, batch_id) if batch_id is not None else self.get_open_cycle(day)
        with self._connect() as conn:
            conn.execute("DELETE FROM votes WHERE day = ? AND user_id = ?", (day, int(user_id)))
            if target is None:
                return None
            conn.execute(
                "DELETE FROM batch_votes WHERE day = ? AND batch_id = ? AND user_id = ?",
                (day, target.batch_id, int(user_id)),
            )
        self.refresh_cycle_summary(day, target.batch_id)
        return target.batch_id

    @staticmethod
    def _serialize_items(items: list[ItemRow]) -> str:
        return json.dumps(
            [
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "price": float(item.price),
                    "discount_price": float(item.discount_price),
                    "source": item.source,
                    "image_url": item.image_url,
                    "stock_qty": int(item.stock_qty) if item.stock_qty is not None else None,
                }
                for item in items
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _deserialize_items(raw: str) -> list[ItemRow]:
        try:
            payload = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
        out: list[ItemRow] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            out.append(
                ItemRow(
                    item_id=str(item.get("item_id") or ""),
                    name=str(item.get("name") or ""),
                    price=float(item.get("price") or 0),
                    discount_price=float(item.get("discount_price") or item.get("price") or 0),
                    source=str(item.get("source") or ""),
                    image_url=str(item.get("image_url") or ""),
                    stock_qty=(int(item.get("stock_qty")) if item.get("stock_qty") not in (None, "") else None),
                )
            )
        return [item for item in out if item.item_id]

    def save_day_snapshot(
        self,
        day: str,
        snapshot_id: str,
        items: list[ItemRow],
        regular_count: int,
        status: str,
        created_at: str | None = None,
    ) -> bool:
        if not items:
            return False
        created = created_at or self._now_iso()
        with self._connect() as conn:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO day_snapshots(
                    day, snapshot_id, created_at, regular_count, total_items, status, items_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day,
                    snapshot_id,
                    created,
                    int(regular_count),
                    len(items),
                    status,
                    self._serialize_items(items),
                ),
            )
            return conn.total_changes > before

    def get_best_day_snapshot(self, day: str) -> DaySnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT day, snapshot_id, created_at, regular_count, total_items, status, items_json
                FROM day_snapshots
                WHERE day = ?
                ORDER BY regular_count DESC, total_items DESC, created_at DESC
                LIMIT 1
                """,
                (day,),
            ).fetchone()
        if not row:
            return None
        return DaySnapshot(
            day=str(row["day"]),
            snapshot_id=str(row["snapshot_id"]),
            created_at=str(row["created_at"]),
            regular_count=int(row["regular_count"]),
            total_items=int(row["total_items"]),
            status=str(row["status"]),
            items=self._deserialize_items(str(row["items_json"])),
        )

    def replace_cycle_item_results(self, day: str, batch_id: int, items: list[dict]) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM batch_item_results WHERE day = ? AND batch_id = ?", (day, batch_id))
            for row in items:
                conn.execute(
                    """
                    INSERT INTO batch_item_results(
                        day, batch_id, item_id, name, price, discount_price,
                        requested_qty, added_qty, last_status, last_error, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        day,
                        batch_id,
                        str(row["item_id"]),
                        str(row["name"]),
                        float(row["price"]),
                        float(row["discount_price"]),
                        int(row["qty"]),
                        0,
                        "pending",
                        "",
                        now,
                    ),
                )

    def list_cycle_item_results(self, day: str, batch_id: int) -> list[BatchItemResult]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT day, batch_id, item_id, name, price, discount_price, requested_qty, added_qty,
                       last_status, last_error, updated_at
                FROM batch_item_results
                WHERE day = ? AND batch_id = ?
                ORDER BY name
                """,
                (day, batch_id),
            ).fetchall()
        return [BatchItemResult(**dict(row)) for row in rows]

    def apply_executor_results(
        self,
        day: str,
        batch_id: int,
        executor_status: str,
        ok_count: int,
        total: int,
        checks: list[dict] | None,
    ) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            for row in checks or []:
                item_name = str(row.get("name") or "")
                requested = int(row.get("requested_qty") or 0)
                before_qty = int(row.get("before_qty") or 0)
                after_qty = int(row.get("after_qty") or 0)
                added_delta = int(row.get("added_delta") or max(0, after_qty - before_qty))
                ok = bool(row.get("ok"))
                reason = str(row.get("reason") or "")
                if ok and requested > 0 and added_delta <= 0 and before_qty >= requested:
                    added_delta = requested
                conn.execute(
                    """
                    UPDATE batch_item_results
                    SET added_qty = MIN(requested_qty, MAX(added_qty, ?)),
                        last_status = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE day = ? AND batch_id = ? AND name = ?
                    """,
                    (
                        max(0, added_delta),
                        "ok" if ok else "failed",
                        reason,
                        now,
                        day,
                        batch_id,
                        item_name,
                    ),
                )
            conn.execute(
                """
                UPDATE order_cycles
                SET executor_status = ?, executor_ok_count = ?, executor_total = ?, updated_at = ?
                WHERE day = ? AND batch_id = ?
                """,
                (executor_status, int(ok_count), int(total), now, day, batch_id),
            )

    def get_missing_cycle_items(self, day: str, batch_id: int) -> list[BatchItemResult]:
        return [
            row
            for row in self.list_cycle_item_results(day, batch_id)
            if int(row.requested_qty) > int(row.added_qty)
        ]
