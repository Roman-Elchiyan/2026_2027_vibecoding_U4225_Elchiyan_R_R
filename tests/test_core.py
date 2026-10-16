"""Offline tests against the original CSV and real SQLite."""
import asyncio
import csv
from dataclasses import replace
import hashlib
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai import DisabledAi
from bot import SafeFormatter, build_application
from budget import DisabledBudget
from config import Config, ConfigError, ROOT, load_config, project_path
from db import Database, SCHEMA_VERSION
from handlers import Controller, card_keyboard, format_card
from search import SearchFilters, SearchService, deduplicate, tokens
from sources import CatalogError, CsvSource

CATALOG = ROOT / "data/ploswater.csv"


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = CsvSource(CATALOG)
        cls.items = cls.source.load()
        cls.first = cls.items[0]
        cls.service = SearchService(cls.source)

    def test_original_csv_count_and_checksum(self):
        with CATALOG.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertGreater(len(rows), 0)
        self.assertEqual(len(rows), len(self.items))
        self.assertEqual(self.source.row_count, len(rows))
        digest = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
        self.assertIn(digest, (ROOT / "data/SOURCE.md").read_text(encoding="utf-8"))

    def test_actual_mapping_and_missing_data(self):
        with CATALOG.open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(self.first.title, row["title"])
        self.assertEqual(self.first.year, int(row["published_year"]))
        self.assertEqual(self.first.authors, (row["first_author_name"],))
        self.assertEqual(self.first.material_type, row["article_type"])
        self.assertIsNone(self.first.abstract_or_snippet)
        self.assertIsNone(self.first.open_url)
        self.assertIsNone(self.first.access_status)
        self.assertIsNone(self.first.metadata["supp_url"])

    def test_stable_ids(self):
        self.assertEqual([m.id for m in self.items], [m.id for m in self.source.load()])
        self.assertEqual(len(self.items), len({m.id for m in self.items}))

    def test_real_topic_title_and_keywords(self):
        topic = " ".join(self.first.title.split()[:3])
        result = self.service.search(SearchFilters(topic=topic.upper()))
        self.assertIn(self.first.id, [m.id for m in result.items])
        self.assertEqual(result.total_count, len(result.items))
        self.assertEqual(len(self.service.search(SearchFilters(topic=self.first.title)).items), 1)
        item = next(m for m in self.items if m.metadata.get("keywords") and "Climatology" in m.metadata["keywords"] and "climatology" not in m.title.casefold())
        self.assertIn(item.id, [m.id for m in self.service.search(SearchFilters(topic="Climatology")).items])

    def test_exact_doi_and_url(self):
        for topic in (self.first.doi, "https://doi.org/" + self.first.doi.upper(), "http://dx.doi.org/" + self.first.doi, "doi: " + self.first.doi):
            self.assertEqual([self.first.id], [m.id for m in self.service.search(SearchFilters(topic=topic)).items])

    def test_year_range(self):
        year = self.first.year
        result = self.service.search(SearchFilters(topic="water", year_from=year, year_to=year))
        self.assertTrue(result.items)
        expected = sum(m.year == year and "water" in tokens(m.title + " " + (m.metadata.get("keywords") or "")) for m in self.items)
        self.assertEqual(result.total_count, expected)
        self.assertTrue(all(m.year == year for m in result.items))

    def test_material_type_filter(self):
        result = self.service.search(SearchFilters(topic=self.first.title, material_types=(self.first.material_type,)))
        self.assertEqual([self.first.id], [m.id for m in result.items])
        other = next(m.material_type for m in self.items if m.material_type != self.first.material_type)
        self.assertEqual([], self.service.search(SearchFilters(topic=self.first.title, material_types=(other,))).items)

    def test_empty_results(self):
        result = self.service.search(SearchFilters(topic="zzzz_no_matching_material_zzzz"))
        self.assertEqual(([], 0), (result.items, result.total_count))

    def test_invalid_filters(self):
        for filters in (SearchFilters(), SearchFilters(topic="x" * 501), SearchFilters(topic="water", year_from=2025, year_to=2022), SearchFilters(topic="water", year_from=22)):
            with self.assertRaises(ValueError):
                self.service.search(filters)

    def test_unsupported_filters_rejected_explicitly(self):
        for options in ({"language": "en"}, {"open_only": True}, {"scope": "crossref"}, {"ai_enabled": True}, {"sort": "relevance"}):
            with self.assertRaisesRegex(ValueError, "недоступен"):
                self.service.search(SearchFilters(topic="water", **options))

    def test_deduplication_without_title_merging(self):
        duplicate = replace(self.first, id="different-id", doi="https://doi.org/" + self.first.doi.upper())
        same_id = replace(self.first, doi=None)
        other = replace(self.items[1], title=self.first.title)
        self.assertEqual([self.first, other], deduplicate([self.first, duplicate, same_id, other]))

    def test_search_cannot_use_network(self):
        with patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden")), patch("socket.create_connection", side_effect=AssertionError("Network is forbidden")):
            self.assertTrue(self.service.search(SearchFilters(topic=self.first.title)).items)
        async def check_disabled():
            with patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden")):
                for factory in (lambda: DisabledAi().search(1, SearchFilters(topic="water")), lambda: DisabledBudget().get_limits(1), lambda: DisabledBudget().request_limit(1, "test")):
                    with self.assertRaises(NotImplementedError):
                        await factory()
        # Windows creates a local socketpair while constructing its event loop.
        asyncio.run(check_disabled())

    def test_card_and_callback_limits_for_all_real_records(self):
        for index, item in enumerate(self.items):
            text = format_card(item, index + 1)
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
            for row in card_keyboard(item, "a" * 32, index, False).inline_keyboard:
                for button in row:
                    if button.callback_data:
                        self.assertLessEqual(len(button.callback_data.encode()), 64)
            self.assertIn(item.doi, text)

    def test_html_escaping_and_extreme_card_length(self):
        item = replace(self.first, title="<&'\"" * 5000, authors=("&" * 5000,), source="&" * 5000,
                       doi="&" * 5000, material_type="&" * 5000, metadata={"keywords": "&" * 5000}, url="javascript:alert(1)")
        card = format_card(item, 1)
        self.assertNotIn("javascript:", card)
        self.assertNotIn("<&", card)
        self.assertLess(len(card), 4096)
        self.assertIn("&lt;", card)
        emoji_item = replace(item, title="💎" * 5000, authors=("💎" * 5000,), source="💎" * 5000,
                             doi="💎" * 5000, material_type="💎" * 5000, metadata={key: "💎"*5000 for key in
                                ("keywords", "publication_date", "volume", "issue", "num_authors", "first_author_affiliation", "first_author_orcid")})
        self.assertLessEqual(len(format_card(emoji_item, 1).encode("utf-16-le")) // 2, 4096)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "runtime/tests").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.db = Database(self.path)
        self.db.initialize()
        self.db.register(100)
        self.db.register(200)
        self.source = CsvSource(CATALOG)
        self.filters = SearchFilters(topic="water")
        self.result = SearchService(self.source).search(self.filters)
        self.db.save_search(100, self.filters, self.result)

    def test_versioned_schema_is_idempotent(self):
        self.db.initialize()
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.db.counts()["users"], 2)
        self.assertTrue(self.db.health())

    def test_newer_schema_is_never_downgraded(self):
        with self.db.connect() as connection:
            connection.execute("PRAGMA user_version=99")
        with self.assertRaises(RuntimeError):
            self.db.initialize()
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_settings_and_input_state_survive_restart(self):
        self.db.set_settings(100, self.filters, "years")
        filters, state = Database(self.path).get_settings(100)
        self.assertEqual(filters, self.filters)
        self.assertEqual(state, "years")

    def test_favorites_add_remove_reopen_and_idempotence(self):
        item = self.db.set_favorite(100, self.result.search_id, 0, True)
        self.db.set_favorite(100, self.result.search_id, 0, True)
        reopened = Database(self.path)
        self.assertTrue(reopened.is_favorite(100, item.id))
        self.assertFalse(reopened.is_favorite(200, item.id))
        saved_id = reopened.saved_search(100)
        self.assertEqual(reopened.get_page(100, saved_id, 0)[0][1], item)
        reopened.set_favorite(100, saved_id, 0, False)
        reopened.set_favorite(100, saved_id, 0, False)
        self.assertFalse(Database(self.path).is_favorite(100, item.id))

    def test_pagination_and_snapshot_survive_restart(self):
        reopened = Database(self.path)
        first = reopened.get_page(100, self.result.search_id, 0)
        second = reopened.get_page(100, self.result.search_id, 1)
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5)
        self.assertTrue(set(m.id for _, m in first).isdisjoint(m.id for _, m in second))
        self.assertEqual(first[0][1], self.result.items[0])
        self.assertEqual(reopened.get_search(100, self.result.search_id)["page"], 1)

    def test_no_foreign_search_or_favorite_access(self):
        for action in (lambda: self.db.get_page(200, self.result.search_id, 0), lambda: self.db.set_favorite(200, self.result.search_id, 0, True)):
            with self.assertRaises(LookupError):
                action()
        self.assertEqual(self.db.counts()["favorites"], 0)

    def test_invalid_page_and_result_position(self):
        for page in (-1, 10 ** 6):
            with self.assertRaises(ValueError):
                self.db.get_page(100, self.result.search_id, page)
        with self.assertRaises(LookupError):
            self.db.set_favorite(100, self.result.search_id, 10 ** 6, True)

    def test_csv_missing_and_corrupt_are_diagnostic_errors(self):
        with self.assertRaises(CatalogError):
            CsvSource(Path(self.temp.name) / "missing.csv").load()
        invalid = Path(self.temp.name) / "invalid.csv"
        invalid.write_text("title,year\ninvalid,2023\n", encoding="utf-8")
        with self.assertRaises(CatalogError):
            CsvSource(invalid).load()

    def test_missing_type_column_does_not_infer_type(self):
        with CATALOG.open(encoding="utf-8", newline="") as stream:
            row = next(csv.DictReader(stream))
        del row["article_type"]
        path = Path(self.temp.name) / "no_type.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        source = CsvSource(path)
        self.assertIsNone(source.load()[0].material_type)
        with self.assertRaisesRegex(ValueError, "нет типа"):
            SearchService(source).search(SearchFilters(topic="water", material_types=("Review",)))

    def test_status_actually_checks_catalog_and_database(self):
        controller = Controller(Config(db_path=self.path), self.db, self.source)
        self.assertIn(f"строк {len(self.source.load())}", controller.status_text())
        controller.source = CsvSource(Path(self.temp.name) / "missing.csv")
        with self.assertLogs("handlers", level="ERROR"):
            self.assertIn("CSV: отсутствует", controller.status_text())
        controller.source = self.source
        with self.db.connect() as connection:
            connection.execute("PRAGMA user_version=99")
        self.assertIn("SQLite: проверка не пройдена", controller.status_text())


class ConfigurationTests(unittest.TestCase):
    def test_only_bot_token_required_for_polling(self):
        with patch.dict(os.environ, {}, clear=True), patch("config.load_dotenv"):
            with self.assertRaises(ConfigError):
                load_config()
            self.assertEqual(load_config(require_token=False).admin_telegram_ids, frozenset())
        token = "123456:" + "x" * 30
        with patch.dict(os.environ, {"BOT_TOKEN": token}, clear=True), patch("config.load_dotenv"):
            config = load_config()
            self.assertFalse(config.ai_enabled)
            self.assertNotIn(token, repr(config))

    def test_admin_ids_and_feature_flags(self):
        with patch.dict(os.environ, {"ADMIN_TELEGRAM_IDS": "11, 22"}, clear=True), patch("config.load_dotenv"):
            self.assertEqual(load_config(False).admin_telegram_ids, frozenset((11, 22)))
        for settings in ({"ADMIN_TELEGRAM_IDS": "bad"}, {"ADMIN_TELEGRAM_IDS": "-1"}, {"ENABLE_AI": "true"}, {"ENABLE_EXTERNAL_SOURCES": "true"}, {"BOT_STAGE": "lab2"}):
            with patch.dict(os.environ, settings, clear=True), patch("config.load_dotenv"):
                with self.assertRaises(ConfigError):
                    load_config(False)

    def test_paths_stay_inside_repository(self):
        with self.assertRaises(ConfigError):
            project_path("../outside.db")

    def test_logs_redact_tokens_and_omit_exception_payload(self):
        token = "123456:" + "x" * 30
        formatter = SafeFormatter((token,))
        try:
            raise RuntimeError("private search phrase " + token)
        except RuntimeError:
            import sys
            record = logging.LogRecord("test", logging.ERROR, __file__, 0, "event=failure " + token, (), sys.exc_info())
        text = formatter.format(record)
        self.assertNotIn(token, text)
        self.assertNotIn("private search phrase", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("Traceback", text)

    def test_application_builds_without_network(self):
        with patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden")):
            config = Config(bot_token="123456:" + "x" * 30)
            app = build_application(config, Controller(config, Database(ROOT / "runtime/unused.db"), CsvSource(CATALOG)))
        self.assertEqual(len(app.handlers[0]), 14)


if __name__ == "__main__":
    unittest.main()
