"""Index lifecycle, bounded work, hybrid fusion, and opt-in real cached E5 checks."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from config import Config, ROOT
from db import Database
from search import SearchFilters, SearchService, reciprocal_rank_fusion
from semantic_search import DIMENSION, SemanticIndex, SemanticSearch, document_text
from sources import CsvSource


class FakeEncoder:
    def __init__(self):
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        # Non-unit vectors verify normalization by our index, independently of the encoder option.
        values = np.ones((len(texts), DIMENSION), dtype=np.float32)
        values[:, 0] = 2
        return values

    def to(self, device):
        return self


class IndexTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "runtime/tests").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.csv = self.directory / "catalog.csv"
        shutil.copyfile(ROOT / "data/ploswater.csv", self.csv)
        self.config = replace(Config(), csv_path=self.csv, semantic_index_dir=self.directory / "index")
        self.encoder = FakeEncoder()
        self.engine = SemanticIndex(CsvSource(self.csv), self.config, lambda: self.encoder)

    def test_prefixes_normalization_and_query_encodes_only_one_document(self):
        self.engine.prepare()
        self.assertEqual(len(self.encoder.calls), 1)
        self.assertEqual(len(self.encoder.calls[0][0]), 434)
        self.assertTrue(all(t.startswith("passage: ") for t in self.encoder.calls[0][0]))
        self.assertEqual(self.encoder.calls[0][1]["batch_size"], 4)
        self.assertTrue(np.allclose(np.linalg.norm(self.engine.vectors, axis=1), 1))
        ids = set(self.engine.metadata["ids"])
        self.engine.rank("drinking water quality", ids)
        self.engine.rank("Водоснабжение и качество питьевой воды", ids)
        self.assertEqual([len(call[0]) for call in self.encoder.calls], [434, 1, 1])
        self.assertTrue(all(call[0][0].startswith("query: ") for call in self.encoder.calls[1:]))
        self.assertEqual(self.engine.metadata["dimension"], 384)
        self.assertEqual(self.engine.metadata["model"], "intfloat/multilingual-e5-small")
        self.assertEqual(len(self.engine.metadata["csv_sha256"]), 64)

    def test_taxonomy_parent_repetitions_do_not_dilute_document_text(self):
        item = self.engine.source.load()[0]
        item.metadata["keywords"] = "/Science/Water/Drinking water; /Science/Water/Water quality; /Other/Drinking water"
        original = item.to_dict()
        self.assertEqual(document_text(item), item.title + ". Keywords: Drinking water; Water quality")
        self.assertEqual(item.to_dict(), original)

    def test_cached_index_reused_after_restart_without_document_encoding(self):
        self.engine.prepare()
        new_encoder = FakeEncoder()
        reopened = SemanticIndex(CsvSource(self.csv), self.config, lambda: new_encoder)
        reopened.prepare()
        self.assertEqual(new_encoder.calls, [])
        self.assertTrue(reopened.metrics["index_reused"])
        self.assertTrue(np.array_equal(self.engine.vectors, reopened.vectors))

    def test_csv_change_rebuilds_even_when_publication_ids_are_unchanged(self):
        self.engine.prepare()
        checksum = self.engine.metadata["csv_sha256"]
        self.csv.write_bytes(self.csv.read_bytes().replace(b"Water", b"WATER", 1))
        self.engine.prepare()
        self.assertNotEqual(checksum, self.engine.metadata["csv_sha256"])
        self.assertEqual(len(self.encoder.calls), 2)

    def test_model_revision_and_index_schema_changes_rebuild(self):
        self.engine.prepare()
        changed = SemanticIndex(CsvSource(self.csv), replace(self.config, semantic_revision="different"), lambda: self.encoder)
        changed.prepare()
        self.assertFalse(changed.metrics["index_reused"])
        with patch("semantic_search.INDEX_SCHEMA", 200):
            changed.prepare()
        self.assertEqual(len(self.encoder.calls), 3)

    def test_corrupt_index_is_rebuilt_and_failed_rebuild_preserves_previous_file(self):
        self.engine.prepare()
        path = self.config.semantic_index_dir / "index.npz"
        path.write_bytes(b"broken index")
        reopened = SemanticIndex(CsvSource(self.csv), self.config, lambda: self.encoder)
        reopened.prepare()
        previous = path.read_bytes()
        self.csv.write_bytes(self.csv.read_bytes() + b"\n")
        with patch.object(reopened, "encode", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                reopened.prepare()
        self.assertEqual(path.read_bytes(), previous)

    def test_cuda_encoding_failure_retries_cpu(self):
        self.engine.model = self.encoder
        self.engine.device = "cuda"
        original = self.encoder.encode
        def encode(texts, **kwargs):
            if kwargs["device"] == "cuda":
                raise RuntimeError("CUDA out of memory")
            return original(texts, **kwargs)
        with patch.object(self.encoder, "encode", side_effect=encode):
            self.assertEqual(self.engine.encode(["query: water"]).shape, (1, 384))
        self.assertEqual(self.engine.device, "cpu")


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.source = CsvSource(ROOT / "data/ploswater.csv")
        self.service = SearchService(self.source)
        self.addCleanup(self.service.close)

    def test_fuzzy_corrects_transpositions_and_missing_letters(self):
        results = self.service.search(SearchFilters(topic="drnking watre qualty"))
        self.assertTrue(results.items)
        self.assertTrue(all("drinking water" in m.title.casefold() or
            "drinking water" in str(m.metadata.get("keywords")).casefold() for m in results.items[:5]))

    def test_or_candidates_and_coverage_find_partial_phrase(self):
        result = self.service.search(SearchFilters(topic="drinking water quality infrastructure"))
        self.assertTrue(result.items)
        self.assertIn("drinking water", result.items[0].title.casefold())

    def test_rrf_ranks_weights_deduplication_and_zero_weight(self):
        self.assertEqual(reciprocal_rank_fusion([["a", "a", "b"], ["b", "c"]], [1, 1]), ["b", "a", "c"])
        self.assertEqual(reciprocal_rank_fusion([["a"], ["b"]], [0, 1]), ["b"])
        self.assertEqual(reciprocal_rank_fusion([["a"], ["b"]], [2, 1]), ["a", "b"])

    def test_semantic_candidates_cannot_bypass_year_or_type_filters(self):
        items = self.source.load()
        result = self.service.search(SearchFilters(topic="water", year_from=2022, year_to=2022,
            material_types=("Review",)), [m.id for m in items]*2)
        self.assertTrue(result.items)
        self.assertTrue(all(m.year == 2022 and m.material_type == "Review" for m in result.items))
        self.assertEqual(len(result.items), len({m.id for m in result.items}))

    def test_nonexistent_and_malformed_doi_never_use_semantic_substitutes(self):
        ranking = [m.id for m in self.source.load()]
        for topic in ("10.1371/nonexistent", "https://doi.org/invalid", "doi: bad"):
            self.assertEqual(self.service.search(SearchFilters(topic=topic), ranking).items, [])

    def test_concurrent_catalog_reads_publish_complete_counts(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as workers:
            counts = list(workers.map(lambda _: len(self.source.load()), range(24)))
        self.assertEqual(counts, [434]*24)
        self.assertEqual(self.source.row_count, 434)
        self.assertTrue(self.source.supports_types)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / "runtime/tests").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.config = replace(Config(), semantic_timeout=0.03, semantic_build_timeout=0.03)
        self.db = Database(Path(self.temp.name)/"worker.db")
        self.db.initialize()
        self.source = CsvSource(ROOT / "data/ploswater.csv")
        self.release = threading.Event()
        self.entered = threading.Event()

    async def asyncTearDown(self):
        self.release.set()
        if hasattr(self, "manager") and self.manager._future:
            for _ in range(100):
                if self.manager._future.done():
                    break
                await asyncio.sleep(0.005)
        self.temp.cleanup()

    def make_slow(self, preparing=False):
        parent = self
        class SlowEngine:
            vectors = None if preparing else np.ones((1, 384))
            device = "cpu"
            calls = 0
            def rank(self, *args):
                self.calls += 1
                parent.entered.set()
                parent.release.wait(2)
                return ["fake-id"]
            def prepare(self):
                self.rank()
                self.vectors = np.ones((1, 384))
        self.manager = SemanticSearch(self.source, self.config, self.db, SlowEngine())
        return self.manager

    async def test_timeout_opens_circuit_without_accumulating_tasks(self):
        manager = self.make_slow()
        ranks, warning = await manager.rank("water", {"fake-id"})
        self.assertEqual(ranks, [])
        self.assertIn("время", warning)
        original_future = manager._future
        for _ in range(20):
            self.assertEqual((await manager.rank("water", {"fake-id"}))[0], [])
        self.assertIs(original_future, manager._future)
        self.assertEqual(manager.engine.calls, 1)
        manager.set_enabled(True)
        self.assertEqual((await manager.rank("water", {"fake-id"}))[0], [])
        self.assertEqual(manager.engine.calls, 1)
        self.release.set()
        while not manager._future.done():
            await asyncio.sleep(0.005)
        manager.set_enabled(True)
        self.assertEqual((await manager.rank("water", {"fake-id"}))[0], ["fake-id"])

    async def test_building_index_keeps_event_loop_responsive_and_has_deadline(self):
        manager = self.make_slow(preparing=True)
        manager.start()
        ticks = 0
        for _ in range(10):
            await asyncio.sleep(0.005)
            ticks += 1
        ranks, warning = await manager.rank("water", set())
        self.assertEqual(ranks, [])
        self.assertEqual(ticks, 10)
        self.assertIn("защита", warning)
        self.assertEqual(manager.engine.calls, 1)

    async def test_switch_off_discards_inflight_result_and_persists_independent_flags(self):
        manager = self.make_slow()
        task = asyncio.create_task(manager.rank("water", {"fake-id"}))
        while not self.entered.is_set():
            await asyncio.sleep(0.001)
        manager.set_enabled(False)
        self.release.set()
        self.assertEqual((await task)[0], [])
        manager.set_enabled(True, external=True)
        reopened = SemanticSearch(self.source, self.config, self.db)
        self.assertFalse(reopened.enabled)
        self.assertTrue(reopened.external_enabled)

    async def test_unavailable_model_falls_back_without_retries_per_request(self):
        engine = SemanticIndex(self.source, self.config, lambda: (_ for _ in ()).throw(ImportError("missing E5")))
        self.manager = SemanticSearch(self.source, self.config, self.db, engine)
        service = SearchService(self.source, self.config, self.manager)
        self.addCleanup(service.close)
        self.manager.start()
        while not self.manager._future.done():
            await asyncio.sleep(0.005)
        result = await service.async_search(SearchFilters(topic="drinking water quality"))
        self.assertTrue(result.items)
        self.assertIn("ограничен", result.warnings[0])
        future = self.manager._future
        await service.async_search(SearchFilters(topic="water"))
        self.assertIs(future, self.manager._future)


@unittest.skipUnless(os.getenv("RUN_E5_TESTS") == "1", "Set RUN_E5_TESTS=1 to verify the real locally cached E5 model")
class RealE5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = Config()
        cls.source = CsvSource(cls.config.csv_path)
        cls.engine = SemanticIndex(cls.source, cls.config)
        cls.engine.prepare()
        cls.service = SearchService(cls.source, cls.config)
        cls.ids = set(cls.engine.metadata["ids"])

    @classmethod
    def tearDownClass(cls):
        cls.service.close()

    def query(self, topic):
        return self.service.search(SearchFilters(topic=topic), self.engine.rank(topic, self.ids))

    def test_russian_control_query_has_relevant_english_publications(self):
        results = self.query("Водоснабжение и качество питьевой воды")
        self.assertTrue(results.items)
        self.assertIn("10.1371/journal.pwat.0000421", [m.doi for m in results.items[:5]])
        self.assertIn("10.1371/journal.pwat.0000281", [m.doi for m in results.items[:5]])

    def test_english_query_and_rank(self):
        results = self.query("drinking water quality")
        self.assertEqual(results.items[0].doi, "10.1371/journal.pwat.0000362")

    def test_russian_and_english_meaning_overlap(self):
        russian = {m.id for m in self.query("качество питьевой воды").items[:10]}
        english = {m.id for m in self.query("drinking water quality").items[:10]}
        self.assertGreaterEqual(len(russian & english), 3)

    def test_french_query_maps_to_english_water_materials(self):
        results = self.query("qualité de l’eau potable")
        self.assertTrue(results.items)
        self.assertTrue(any("drinking water" in m.title.casefold() or "potable water" in m.title.casefold() for m in results.items[:5]))

    def test_irrelevant_and_noise_queries_do_not_fill_output(self):
        for query in ("zzzz_no_matching_material_zzzz", "quantum entanglement in superconducting qubits",
                      "квантовая запутанность сверхпроводящих кубитов"):
            self.assertEqual(self.query(query).items, [], query)

    def test_actual_device_probe_and_memory_measurements(self):
        self.assertIn(self.engine.device, ("cpu", "cuda"))
        if self.engine.device == "cuda":
            self.assertEqual(self.engine.metrics["cuda_probe"], "passed")
        self.assertEqual(self.engine.vectors.shape, (434, 384))
        self.assertGreater(self.engine.metrics["model_load_seconds"], 0)
