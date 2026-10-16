"""Short-lived SQLite transactions and versioned, persistent user data."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from search import Material, SearchFilters, SearchResult, normalize_doi

SCHEMA_VERSION = 2


class Database:
    def __init__(self, path: Path, history_max_per_user: int = 50, history_days: int = 30):
        self.path = Path(path)
        self.history_max_per_user = history_max_per_user
        self.history_days = history_days

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
            connection.execute("BEGIN IMMEDIATE")
            if version == 0:
                schema = """
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
                """
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
            if version < 2:
                self._migrate_v2(connection)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _migrate_v2(self, connection) -> None:
        # No executescript: its implicit COMMIT would break rollback on malformed legacy JSON.
        statements = [
            "CREATE TABLE materials (id TEXT PRIMARY KEY, doi TEXT UNIQUE, material_json TEXT NOT NULL)",
            "CREATE TABLE material_aliases (alias TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES materials(id))",
            """CREATE TABLE material_sources (
                material_id TEXT NOT NULL REFERENCES materials(id), source TEXT NOT NULL, source_id TEXT NOT NULL,
                variant_json TEXT, PRIMARY KEY(material_id, source, source_id))""",
            "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "ALTER TABLE search_results RENAME TO legacy_results",
            "ALTER TABLE favorites RENAME TO legacy_favorites",
            """CREATE TABLE search_results (
                search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, material_id TEXT NOT NULL REFERENCES materials(id),
                PRIMARY KEY(search_id,position), UNIQUE(search_id,material_id))""",
            "CREATE INDEX results_material ON search_results(material_id)",
            """CREATE TABLE favorites (
                user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                material_id TEXT NOT NULL REFERENCES materials(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id,material_id))""",
        ]
        for statement in statements:
            connection.execute(statement)
        for row in connection.execute("SELECT * FROM legacy_results ORDER BY search_id,position").fetchall():
            material_id = self._store_material(connection, Material.from_dict(json.loads(row["material_json"])))
            connection.execute("INSERT OR IGNORE INTO search_results VALUES (?,?,?)",
                               (row["search_id"], row["position"], material_id))
            self._alias(connection, row["material_id"], material_id)
        for row in connection.execute("SELECT * FROM legacy_favorites ORDER BY created_at").fetchall():
            material_id = self._store_material(connection, Material.from_dict(json.loads(row["material_json"])))
            connection.execute("INSERT OR IGNORE INTO favorites VALUES (?,?,?)",
                               (row["user_id"], material_id, row["created_at"]))
            self._alias(connection, row["material_id"], material_id)
        # DOI deduplication can collapse legacy positions; preserve relative order, then compact.
        for search in connection.execute("SELECT id FROM searches").fetchall():
            ids = [r[0] for r in connection.execute(
                "SELECT material_id FROM search_results WHERE search_id=? ORDER BY position", (search[0],))]
            connection.execute("DELETE FROM search_results WHERE search_id=?", (search[0],))
            connection.executemany("INSERT INTO search_results VALUES (?,?,?)",
                                   [(search[0], pos, mid) for pos, mid in enumerate(ids)])
            connection.execute("UPDATE searches SET total_count=?,page=MIN(page,?) WHERE id=?",
                               (len(ids), max(0, (len(ids)-1)//5), search[0]))
        connection.execute("DROP TABLE legacy_results")
        connection.execute("DROP TABLE legacy_favorites")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Нарушены связи при миграции SQLite; изменения отменены.")

    @staticmethod
    def _alias(connection, alias: str, material_id: str) -> None:
        connection.execute("INSERT OR IGNORE INTO material_aliases VALUES (?,?)", (alias, material_id))

    def _store_material(self, connection, item: Material) -> str:
        doi = normalize_doi(item.doi)
        row = connection.execute("SELECT id,material_json FROM materials WHERE doi=?", (doi,)).fetchone() if doi else None
        if row is None:
            row = connection.execute("""SELECT m.id,m.material_json FROM materials m
                JOIN material_aliases a ON a.material_id=m.id WHERE a.alias=?""", (item.id,)).fetchone()
        material_id = row["id"] if row else item.id
        data = item.to_dict()
        data.update(id=material_id, doi=doi)
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if row is None:
            connection.execute("INSERT INTO materials VALUES (?,?,?)", (material_id, doi, payload))
        self._alias(connection, item.id, material_id)
        # The primary source payload lives in materials. Only differing source versions need another payload.
        variant = payload if row and row["material_json"] != payload else None
        source_id = str(item.metadata.get("paperid") or item.id)
        connection.execute("""INSERT INTO material_sources VALUES (?,?,?,?)
            ON CONFLICT(material_id,source,source_id) DO UPDATE SET
            variant_json=COALESCE(excluded.variant_json, material_sources.variant_json)""",
            (material_id, item.source, source_id, variant))
        return material_id

    def get_app_setting(self, key: str, default: bool) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return default if row is None else row[0] == "true"

    def set_app_setting(self, key: str, value: bool) -> None:
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO app_settings VALUES (?,?)", (key, "true" if value else "false"))

    def cleanup_history(self, user_id: int | None = None) -> int:
        # Materials are retained as the shared catalog, including provenance for future review references.
        with self.connect() as connection:
            cursor = connection.execute("""DELETE FROM searches WHERE id IN (
                SELECT id FROM (SELECT id,created_at,ROW_NUMBER() OVER (
                    PARTITION BY user_id ORDER BY created_at DESC,rowid DESC) AS n FROM searches
                    WHERE (? IS NULL OR user_id=?))
                WHERE n>? OR datetime(created_at)<datetime('now',?))""",
                (user_id, user_id, self.history_max_per_user, f"-{self.history_days} days"))
            return cursor.rowcount

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
            seen = set()
            for item in result.items:
                material_id = self._store_material(connection, item)
                if material_id not in seen:
                    connection.execute("INSERT INTO search_results VALUES (?,?,?)", (result.search_id, len(seen), material_id))
                    seen.add(material_id)
            connection.execute("UPDATE searches SET total_count=? WHERE id=?", (len(seen), result.search_id))
        self.cleanup_history(user_id)

    def get_search(self, user_id: int, search_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("""SELECT * FROM searches WHERE id=? AND user_id=?
                AND datetime(created_at)>=datetime('now',?)""", (search_id, user_id, f"-{self.history_days} days")).fetchone()
        if row is None:
            raise LookupError("Подборка недоступна или срок хранения истёк. Выполните /search или /saved.")
        return dict(row)

    def get_page(self, user_id: int, search_id: str, page: int, page_size: int = 5) -> list[tuple[int, Material]]:
        search = self.get_search(user_id, search_id)
        if page_size != 5 or page < 0 or page >= max(1, (search["total_count"] + 4) // 5):
            raise ValueError("Такой страницы нет.")
        with self.connect() as connection:
            rows = connection.execute("""SELECT position,material_json FROM search_results r
                JOIN materials m ON m.id=r.material_id
                WHERE search_id=? ORDER BY position LIMIT ? OFFSET ?""", (search_id, page_size, page * page_size)).fetchall()
            connection.execute("UPDATE searches SET page=? WHERE id=? AND user_id=?", (page, search_id, user_id))
        return [(row["position"], Material.from_dict(json.loads(row["material_json"]))) for row in rows]

    def result_item(self, user_id: int, search_id: str, position: int) -> Material:
        self.get_search(user_id, search_id)
        with self.connect() as connection:
            row = connection.execute("SELECT material_json FROM search_results r JOIN materials m ON m.id=r.material_id WHERE search_id=? AND position=?",
                                     (search_id, position)).fetchone()
        if row is None:
            raise LookupError("Карточка недоступна.")
        return Material.from_dict(json.loads(row[0]))

    def set_favorite(self, user_id: int, search_id: str, position: int, saved: bool) -> Material:
        item = self.result_item(user_id, search_id, position)
        with self.connect() as connection:
            if saved:
                connection.execute("INSERT OR IGNORE INTO favorites(user_id,material_id) VALUES (?,?)", (user_id, item.id))
            else:
                connection.execute("DELETE FROM favorites WHERE user_id=? AND material_id=?", (user_id, item.id))
        return item

    def is_favorite(self, user_id: int, material_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute("""SELECT 1 FROM favorites f JOIN material_aliases a ON a.material_id=f.material_id
                WHERE f.user_id=? AND a.alias=?""", (user_id, material_id)).fetchone() is not None

    def saved_search(self, user_id: int) -> str:
        with self.connect() as connection:
            rows = connection.execute("""SELECT material_json FROM favorites f JOIN materials m ON m.id=f.material_id
                WHERE user_id=? ORDER BY created_at,material_id""", (user_id,)).fetchall()
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
