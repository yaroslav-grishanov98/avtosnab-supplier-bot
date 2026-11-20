"""Локальное хранилище входящих заявок для прототипа бота поставщика."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "orders.sqlite3"


class LocalOrderRepository:
    """SQLite-репозиторий, который позже можно заменить вызовами backend API."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS supplier_requests (
                    id TEXT PRIMARY KEY,
                    number TEXT NOT NULL,
                    supplier_id TEXT NOT NULL DEFAULT '*',
                    buyer_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    deadline TEXT,
                    delivery_address TEXT,
                    comment TEXT,
                    status TEXT NOT NULL DEFAULT 'new'
                );
                CREATE TABLE IF NOT EXISTS supplier_request_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    sku TEXT,
                    brand TEXT,
                    quantity REAL NOT NULL,
                    unit TEXT NOT NULL,
                    FOREIGN KEY (request_id) REFERENCES supplier_requests(id) ON DELETE CASCADE
                );
                """
            )
            if connection.execute("SELECT COUNT(*) FROM supplier_requests").fetchone()[0] == 0:
                self._seed_demo_request(connection)
            connection.commit()

    @staticmethod
    def _seed_demo_request(connection: sqlite3.Connection) -> None:
        now = datetime.now().replace(microsecond=0)
        request_id = "demo-request-001"
        connection.execute(
            """
            INSERT INTO supplier_requests (
                id, number, supplier_id, buyer_name, created_at, deadline,
                delivery_address, comment, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id, "З-2026-001", "*", "ООО «АвтоТранс»", now.isoformat(),
                (now + timedelta(days=2)).isoformat(), "Москва, ул. Промышленная, 12",
                "Просьба указать актуальные цены и ближайший срок поставки.", "new",
            ),
        )
        connection.executemany(
            """
            INSERT INTO supplier_request_items (
                request_id, position, name, sku, brand, quantity, unit
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (request_id, 1, "Колодки тормозные передние", "BP-123", "Brembo", 12, "компл."),
                (request_id, 2, "Фильтр масляный", "OF-456", "MANN-FILTER", 30, "шт."),
                (request_id, 3, "Ремень генератора", "6PK1120", "Gates", 8, "шт."),
            ],
        )

    def list_requests(self, supplier_id: str | None = None) -> list[dict[str, Any]]:
        supplier = supplier_id or ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT r.*, COUNT(i.id) AS item_count
                FROM supplier_requests r
                LEFT JOIN supplier_request_items i ON i.request_id = r.id
                WHERE r.supplier_id = '*' OR r.supplier_id = ?
                GROUP BY r.id
                ORDER BY CASE r.status WHEN 'new' THEN 0 ELSE 1 END, r.created_at DESC
                """,
                (supplier,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_request(self, request_id: str, supplier_id: str | None = None) -> dict[str, Any] | None:
        supplier = supplier_id or ""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM supplier_requests WHERE id = ? AND (supplier_id = '*' OR supplier_id = ?)",
                (request_id, supplier),
            ).fetchone()
            if row is None:
                return None
            items = connection.execute(
                """
                SELECT position, name, sku, brand, quantity, unit
                FROM supplier_request_items WHERE request_id = ? ORDER BY position
                """,
                (request_id,),
            ).fetchall()
        result = dict(row)
        result["items"] = [dict(item) for item in items]
        return result

    def mark_viewed(self, request_id: str, supplier_id: str | None = None) -> bool:
        supplier = supplier_id or ""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE supplier_requests SET status = 'viewed'
                WHERE id = ? AND status = 'new' AND (supplier_id = '*' OR supplier_id = ?)
                """,
                (request_id, supplier),
            )
            connection.commit()
        return cursor.rowcount > 0


_repository: LocalOrderRepository | None = None


def get_order_repository() -> LocalOrderRepository:
    global _repository
    if _repository is None:
        _repository = LocalOrderRepository()
    return _repository
