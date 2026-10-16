"""Short-lived SQLite transactions and versioned, persistent user data."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from search import Material, SearchFilters, SearchResult

SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("Версия SQLite новее приложения. Данные не изменены.")
            connection.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE users (
                        telegram_id INTEGER PRIMARY KEY,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE settings (
                        user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
                        filters_json TEXT NOT NULL DEFAULT '{}',
                        state TEXT NOT NULL DEFAULT 'idle'
                    );
                    CREATE TABLE searches (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                        filters_json TEXT NOT NULL,
                        warnings_json TEXT NOT NULL,
                        total_count INTEGER NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'search',
                        page INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX searches_owner ON searches(user_id, created_at);
                    CREATE TABLE search_results (
                        search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        material_id TEXT NOT NULL,
                        material_json TEXT NOT NULL,
                        PRIMARY KEY (search_id, position),
                        UNIQUE (search_id, material_id)
                    );
                    CREATE TABLE favorites (
                        user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                        material_id TEXT NOT NULL,
                        material_json TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, material_id)
                    );
                    PRAGMA user_version=1;
                    COMMIT;
                """)

    def register(self, user_id: int) -> None:
        with self.connect() as connection:
            connection.execute("""INSERT INTO users(telegram_id) VALUES (?)
                ON CONFLICT(telegram_id) DO UPDATE SET updated_at=CURRENT_TIMESTAMP""", (user_id,))
            connection.execute("INSERT OR IGNORE INTO settings(user_id) VALUES (?)", (user_id,))

    def get_settings(self, user_id: int) -> tuple[SearchFilters, str]:
        with self.connect() as connection:
            row = connection.execute("SELECT filters_json, state FROM settings WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            raise LookupError("Пользователь не зарегистрирован.")
        return SearchFilters.from_dict(json.loads(row["filters_json"])), row["state"]

    def set_settings(self, user_id: int, filters: SearchFilters, state: str = "idle") -> None:
        with self.connect() as connection:
            connection.execute("UPDATE settings SET filters_json=?, state=? WHERE user_id=?",
                               (json.dumps(filters.to_dict(), ensure_ascii=False), state, user_id))

    def save_search(self, user_id: int, filters: SearchFilters, result: SearchResult, kind: str = "search") -> None:
        if result.total_count != len(result.items):
            raise ValueError("Количество результатов не совпадает со снимком выдачи.")
        with self.connect() as connection:
            connection.execute("""INSERT INTO searches(id,user_id,filters_json,warnings_json,total_count,kind)
                VALUES (?,?,?,?,?,?)""", (result.search_id, user_id, json.dumps(filters.to_dict(), ensure_ascii=False),
                json.dumps(result.warnings, ensure_ascii=False), result.total_count, kind))
            connection.executemany("INSERT INTO search_results VALUES (?,?,?,?)", [
                (result.search_id, position, item.id, json.dumps(item.to_dict(), ensure_ascii=False))
                for position, item in enumerate(result.items)
            ])

    def get_search(self, user_id: int, search_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM searches WHERE id=? AND user_id=?", (search_id, user_id)).fetchone()
        if row is None:
            raise LookupError("Подборка недоступна. Выполните /search или /saved.")
        return dict(row)

    def get_page(self, user_id: int, search_id: str, page: int, page_size: int = 5) -> list[tuple[int, Material]]:
        search = self.get_search(user_id, search_id)
        if page_size != 5 or page < 0 or page >= max(1, (search["total_count"] + 4) // 5):
            raise ValueError("Такой страницы нет.")
        with self.connect() as connection:
            rows = connection.execute("""SELECT position,material_json FROM search_results
                WHERE search_id=? ORDER BY position LIMIT ? OFFSET ?""", (search_id, page_size, page * page_size)).fetchall()
            connection.execute("UPDATE searches SET page=? WHERE id=? AND user_id=?", (page, search_id, user_id))
        return [(row["position"], Material.from_dict(json.loads(row["material_json"]))) for row in rows]

    def result_item(self, user_id: int, search_id: str, position: int) -> Material:
        self.get_search(user_id, search_id)
        with self.connect() as connection:
            row = connection.execute("SELECT material_json FROM search_results WHERE search_id=? AND position=?",
                                     (search_id, position)).fetchone()
        if row is None:
            raise LookupError("Карточка недоступна.")
        return Material.from_dict(json.loads(row[0]))

    def set_favorite(self, user_id: int, search_id: str, position: int, saved: bool) -> Material:
        item = self.result_item(user_id, search_id, position)
        with self.connect() as connection:
            if saved:
                connection.execute("INSERT OR IGNORE INTO favorites(user_id,material_id,material_json) VALUES (?,?,?)",
                                   (user_id, item.id, json.dumps(item.to_dict(), ensure_ascii=False)))
            else:
                connection.execute("DELETE FROM favorites WHERE user_id=? AND material_id=?", (user_id, item.id))
        return item

    def is_favorite(self, user_id: int, material_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM favorites WHERE user_id=? AND material_id=?", (user_id, material_id)).fetchone() is not None

    def saved_search(self, user_id: int) -> str:
        with self.connect() as connection:
            rows = connection.execute("SELECT material_json FROM favorites WHERE user_id=? ORDER BY created_at,material_id", (user_id,)).fetchall()
        items = [Material.from_dict(json.loads(row[0])) for row in rows]
        result = SearchResult(items, [], uuid4().hex, len(items))
        self.save_search(user_id, SearchFilters(topic="Избранное"), result, kind="saved")
        return result.search_id

    def health(self) -> bool:
        # mode=rw must not silently create an empty DB if the file was removed.
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=3)
        try:
            return (connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    and connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
                    and connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 0)
        finally:
            connection.close()

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("users", "searches", "favorites")}
