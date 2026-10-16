"""Real v1 payload migration, transactional rollback, deduplication and retention."""
from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from config import ROOT
from db import Database, SCHEMA_VERSION
from search import SearchFilters, SearchResult
from sources import CsvSource


LEGACY_SCHEMA = """
CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE settings (user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id), filters_json TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE searches (id TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    filters_json TEXT NOT NULL,warnings_json TEXT NOT NULL,total_count INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'search',page INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE search_results (search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,position INTEGER NOT NULL,
    material_id TEXT NOT NULL, material_json TEXT NOT NULL,PRIMARY KEY(search_id,position),UNIQUE(search_id,material_id));
CREATE TABLE favorites (user_id INTEGER NOT NULL REFERENCES users(telegram_id),material_id TEXT NOT NULL,
    material_json TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,material_id));
PRAGMA user_version=1;
"""


class MigrationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "runtime/tests").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"legacy.db"
        self.items = CsvSource(ROOT / "data/ploswater.csv").load()[:8]
        self.sid = uuid4().hex
        self.filters = SearchFilters(topic="water", year_from=2022, year_to=2025)
        connection = sqlite3.connect(self.path)
        connection.executescript(LEGACY_SCHEMA)
        connection.execute("INSERT INTO users(telegram_id) VALUES (100)")
        connection.execute("INSERT INTO settings VALUES (?,?,?)", (100, json.dumps(self.filters.to_dict()), "years"))
        connection.execute("INSERT INTO searches(id,user_id,filters_json,warnings_json,total_count,page) VALUES (?,?,?,?,?,?)",
                           (self.sid, 100, json.dumps(self.filters.to_dict()), '["legacy warning"]', len(self.items), 1))
        connection.executemany("INSERT INTO search_results VALUES (?,?,?,?)", [
            (self.sid, i, m.id, json.dumps(m.to_dict(), ensure_ascii=False)) for i, m in enumerate(self.items)])
        connection.execute("INSERT INTO favorites VALUES (?,?,?,?)", (100, self.items[0].id,
                           json.dumps(self.items[0].to_dict()), "2026-01-01 00:00:00"))
        connection.commit()
        connection.close()
        self.db = Database(self.path)

    def test_v1_migration_preserves_user_settings_search_order_and_favorites_after_restart(self):
        self.db.initialize()
        self.db.initialize()
        reopened = Database(self.path)
        self.assertEqual(reopened.get_settings(100), (self.filters, "years"))
        record = reopened.get_search(100, self.sid)
        self.assertEqual(json.loads(record["warnings_json"]), ["legacy warning"])
        self.assertEqual(json.loads(record["filters_json"]), json.loads(json.dumps(self.filters.to_dict())))
        self.assertEqual(record["page"], 1)
        self.assertEqual([m for _, m in reopened.get_page(100, self.sid, 0)], self.items[:5])
        self.assertEqual([m for _, m in reopened.get_page(100, self.sid, 1)], self.items[5:])
        self.assertTrue(reopened.is_favorite(100, self.items[0].id))
        with reopened.connect() as connection:
            self.assertEqual(connection.execute("SELECT created_at FROM favorites").fetchone()[0], "2026-01-01 00:00:00")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_metadata_is_stored_once_for_repeated_results_and_favorites(self):
        self.db.initialize()
        for _ in range(3):
            self.db.save_search(100, self.filters, SearchResult(self.items, [], uuid4().hex, len(self.items)))
        self.db.set_favorite(100, self.sid, 2, True)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0], len(self.items))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM material_sources WHERE variant_json IS NOT NULL").fetchone()[0], 0)
            for table in ("search_results", "favorites"):
                self.assertNotIn("material_json", [r[1] for r in connection.execute(f"PRAGMA table_info({table})")])

    def test_doi_dedup_retains_provenance_aliases_and_relative_order(self):
        duplicate = replace(self.items[0], id="external-copy", doi="https://doi.org/"+self.items[0].doi.upper(),
                            source="future-source", metadata={"external_id": "123", "original": "preserved"})
        with self.db.connect() as connection:
            connection.execute("INSERT INTO search_results VALUES (?,?,?,?)", (self.sid, 8, duplicate.id, json.dumps(duplicate.to_dict())))
            connection.execute("UPDATE searches SET total_count=9")
            connection.execute("INSERT INTO favorites(user_id,material_id,material_json) VALUES (?,?,?)", (100, duplicate.id, json.dumps(duplicate.to_dict())))
        self.db.initialize()
        self.assertEqual(self.db.get_search(100, self.sid)["total_count"], 8)
        self.assertTrue(self.db.is_favorite(100, "external-copy"))
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM favorites").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 8)
            source = connection.execute("SELECT variant_json FROM material_sources WHERE source='future-source'").fetchone()
            self.assertEqual(json.loads(source[0])["metadata"]["original"], "preserved")
        self.assertEqual([m.id for _, m in self.db.get_page(100, self.sid, 1)], [m.id for m in self.items[5:]])

    def test_invalid_legacy_payload_rolls_back_every_schema_change(self):
        with self.db.connect() as connection:
            connection.execute("UPDATE favorites SET material_json='invalid json'")
        with self.assertRaises(json.JSONDecodeError):
            self.db.initialize()
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM search_results").fetchone()[0], 8)
            self.assertIn("material_json", [r[1] for r in connection.execute("PRAGMA table_info(search_results)")])
            self.assertFalse(connection.execute("SELECT 1 FROM sqlite_master WHERE name='materials'").fetchone())
        # Fixing the record allows the exact same migration to run successfully.
        with self.db.connect() as connection:
            connection.execute("UPDATE favorites SET material_json=?", (json.dumps(self.items[0].to_dict()),))
        self.db.initialize()
        self.assertTrue(self.db.health())

    def test_real_existing_v1_database_copy(self):
        original = ROOT / "runtime/backups/bot-before-v2.db"
        if not original.exists():
            self.skipTest("No local pre-migration database backup; synthetic v1 migration is still tested")
        target = Path(self.temp.name)/"real-copy.db"
        with closing(sqlite3.connect(original)) as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
        with closing(sqlite3.connect(target)) as connection:
            before = {t: connection.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
                      for t in ("users", "settings", "searches")}
            snapshots = connection.execute("SELECT search_id,position,material_json FROM search_results ORDER BY search_id,position").fetchall()
        migrated = Database(target)
        migrated.initialize()
        with migrated.connect() as connection:
            for table, rows in before.items():
                self.assertEqual([tuple(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")], rows)
            for sid, pos, payload in snapshots:
                owner = connection.execute("SELECT user_id FROM searches WHERE id=?", (sid,)).fetchone()[0]
                self.assertEqual(migrated.result_item(owner, sid, pos).to_dict(), json.loads(payload) | {"authors": tuple(json.loads(payload)["authors"])})
        self.assertTrue(migrated.health())


class RetentionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "runtime/tests").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name)/"retention.db", history_max_per_user=3, history_days=30)
        self.db.initialize()
        self.db.register(100)
        self.db.register(200)
        self.items = CsvSource(ROOT / "data/ploswater.csv").load()[:2]

    def save(self, user_id=100):
        result = SearchResult(self.items, [], uuid4().hex, len(self.items))
        self.db.save_search(user_id, SearchFilters(topic="water"), result)
        return result.search_id

    def test_cap_is_per_user_favorites_and_review_references_are_preserved(self):
        first = self.save()
        self.db.set_favorite(100, first, 0, True)
        foreign = self.save(200)
        latest = [self.save() for _ in range(5)]
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM searches WHERE user_id=100").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 2)
            connection.execute("CREATE TABLE future_review_reference (material_id TEXT REFERENCES materials(id))")
            connection.execute("INSERT INTO future_review_reference VALUES (?)", (self.items[0].id,))
        with self.assertRaisesRegex(LookupError, "срок хранения"):
            self.db.get_search(100, first)
        self.assertTrue(self.db.is_favorite(100, self.items[0].id))
        self.assertEqual(self.db.get_search(200, foreign)["total_count"], 2)
        self.assertEqual(self.db.get_search(100, latest[-1])["total_count"], 2)

    def test_age_cleanup_cascades_results_and_old_buttons_expire(self):
        sid = self.save()
        self.db.set_favorite(100, sid, 0, True)
        with self.db.connect() as connection:
            connection.execute("UPDATE searches SET created_at=datetime('now','-31 days') WHERE id=?", (sid,))
        self.assertEqual(self.db.cleanup_history(), 1)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM search_results").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM favorites").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        for action in (lambda: self.db.get_page(100, sid, 0), lambda: self.db.result_item(100, sid, 0),
                       lambda: self.db.set_favorite(100, sid, 0, True)):
            with self.assertRaisesRegex(LookupError, "истёк"):
                action()
