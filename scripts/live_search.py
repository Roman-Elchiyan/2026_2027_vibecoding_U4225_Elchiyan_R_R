"""Real integrated RU search, exact DOI, snapshots and favorites in an isolated DB."""
import asyncio
from collections import Counter
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lab_config import load_lab_config
from lab_handlers import LabController
from lab_storage import LabDatabase
from research import ResearchFilters
from semantic_search import SemanticSearch
from sources import CsvSource


async def main():
    config = load_lab_config(require_token=False,external=True)
    db = LabDatabase(ROOT/'runtime/live-search.db')
    db.initialize()
    db.register(-20001)
    controller = LabController(config,db,CsvSource(config.csv_path))
    output = {}
    try:
        manager = SemanticSearch(controller.source,config,db)
        controller.search_service.semantic = manager
        await asyncio.to_thread(manager.engine.prepare)
        output['e5_device'] = manager.engine.device
        f = ResearchFilters(topic='Водоснабжение и качество питьевой воды')
        started = perf_counter()
        result = await controller.research.search(f)
        output['russian_search'] = {'count':len(result.items),'seconds':round(perf_counter()-started,3),
            'sources':dict(Counter(m.source for m in result.items)),'warnings':result.warnings}
        db.save_search(-20001,f,result)
        if result.items:
            saved = db.set_favorite(-20001,result.search_id,0,True)
            reopened = LabDatabase(db.path)
            reopened.initialize()
            output['snapshot_and_favorite_after_reopen'] = reopened.is_favorite(-20001,saved.id) and bool(reopened.get_page(-20001,result.search_id,0))
        doi = controller.source.load()[0].doi
        result = await controller.research.search(ResearchFilters(topic=doi))
        output['exact_doi'] = {'count':len(result.items),'exact':all(m.doi==doi for m in result.items)}
    except Exception as exc:
        output['failure_type'] = type(exc).__name__
    finally:
        await controller.close()
    text = json.dumps(output,ensure_ascii=False,indent=2)
    print(text)
    (ROOT/'runtime/live-search-checks.json').write_text(text,encoding='utf-8')


if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8')
    asyncio.run(main())
