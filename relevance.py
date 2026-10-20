"""Evidence-based tiers; model logits are ranks, never relevance probabilities."""
from dataclasses import dataclass, replace
from datetime import datetime
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from search import normalize_doi


def material_text(item):
    return '\n'.join(filter(None, (item.title, item.abstract_or_snippet,
        str(item.metadata.get('keywords') or ''))))[:10000]


def _zenodo_version_groups(entries):
    """Only explicit repository version relationships can bridge different DOIs."""
    parents, concepts = {}, set()

    def doi(value):
        value = normalize_doi(value) if isinstance(value, str) else None
        return value if value and re.fullmatch(r'10\.5281/zenodo\.\d+', value) else None

    def root(value):
        parents.setdefault(value, value)
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def join(a, b):
        a, b = root(a), root(b)
        parents[max(a, b)] = min(a, b)

    for _, _, item in entries:
        own = doi(item.doi)
        if not own or item.metadata.get('category') == 'statistics':
            continue
        concept = doi(item.metadata.get('concept_doi'))
        if concept:
            join(own, concept)
            concepts.add(concept)
        related = item.metadata.get('related_identifiers') or []
        for relation in related if isinstance(related, list) else []:
            if not isinstance(relation, dict):
                continue
            kind = relation.get('relationType') or relation.get('relation') or relation.get('relation_type')
            if isinstance(kind, dict):
                kind = kind.get('id')
            kind = str(kind or '').casefold()
            target = doi(relation.get('relatedIdentifier') or relation.get('identifier'))
            if target and kind in ('isversionof', 'hasversion', 'isnewversionof', 'ispreviousversionof'):
                join(own, target)
                if kind == 'isversionof': concepts.add(target)
                elif kind == 'hasversion': concepts.add(own)
        # Previously collapsed results retain their proven DOI membership.
        for member in item.metadata.get('version_dois') or []:
            target = doi(member)
            if target: join(own, target)
    return {value: root(value) for value in parents}, concepts


def _version_order(item, concepts):
    try:
        created = datetime.fromisoformat(str(item.metadata.get('version_created') or '').replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError, OSError):
        created = 0
    version = re.fullmatch(r'v?(\d+(?:\.\d+)*)', str(item.metadata.get('version') or ''))
    number = tuple(map(int, version[1].split('.'))) if version else ()
    doi = normalize_doi(item.doi) or ''
    record = re.fullmatch(r'10\.5281/zenodo\.(\d+)', doi)
    return doi not in concepts, created, number, int(record[1]) if record else 0


def merge_candidates(batches, *, collapse_versions=True):
    """Fuse provider ranks, preserving exact IDs and explicit Zenodo version families."""
    entries = [(batch_id, rank, item) for batch_id, batch in enumerate(batches)
        for rank, item in enumerate(batch, 1)]
    version_groups, concepts = _zenodo_version_groups(entries) if collapse_versions else ({}, set())
    parent = list(range(len(entries)))
    dois = [{normalize_doi(m.doi)} - {None, ''} for _, _, m in entries]
    if collapse_versions:
        for i, (_, _, item) in enumerate(entries):
            dois[i].update(d for value in item.metadata.get('version_dois') or []
                           if isinstance(value, str) and (d := normalize_doi(value)) in version_groups)
    aliases = {}

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (_, _, item) in enumerate(entries):
        keys = ['id:' + item.id]
        doi = normalize_doi(item.doi)
        if doi: keys.append('doi:' + doi)
        if doi in version_groups: keys.append('zenodo-work:' + version_groups[doi])
        for field in ('pmid', 'pmcid', 'arxiv_id'):
            value = str(item.metadata.get(field) or '').strip()
            if value:
                if field == 'arxiv_id': value = re.sub(r'v\d+$', '', value)
                keys.append(field + ':' + value.upper())
        statistical = item.metadata.get('category') == 'statistics'
        if statistical:
            # A portal URL identifies neither a commodity nor a data series.
            keys = ['statistics:' + json.dumps([item.source, item.id,
                item.metadata.get('dataset_ref'), item.metadata.get('commodity'),
                item.metadata.get('dataset'), item.metadata.get('dimensions'),
                item.metadata.get('period')], sort_keys=True)]
        if item.url and not statistical:
            parsed = urlsplit(item.url)
            query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith('utm_')])
            if parsed.path not in ('','/'):
                keys.append('url:' + urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, query, '')))
        if len(keys) == 1:
            title = ' '.join(re.findall(r'[^\W_]+', item.title.casefold()))
            if len(title) >= 30 and item.year and item.authors:
                author=' '.join(re.findall(r'[^\W_]+',item.authors[0].casefold()))
                keys.append('title:' + title + ':' + str(item.year)+':'+author)
        for key in keys:
            owners = aliases.setdefault(key, [])
            for owner in owners:
                a, b = root(i), root(owner)
                if a == b: continue
                # Different DOIs are joined only inside a proven version family.
                identities = {version_groups.get(d, d) for d in dois[a] | dois[b]}
                if len(identities) > 1: continue
                first, second = sorted((a, b))
                parent[second] = first
                dois[first] |= dois[second]
            owners.append(i)
    groups, ranks, provenance, versions = {}, {}, {}, {}
    for i, (batch_id, rank, item) in enumerate(entries):
        key = root(i)
        # Query variants are alternative retrievals of one provider, not extra
        # independent votes. Repeated hits cannot multiply that source's weight.
        provider = item.source.casefold() or item.id.split(':', 1)[0]
        ranks.setdefault(key, {})[provider] = min(rank, ranks.get(key, {}).get(provider, rank))
        origin = {'source': item.source, 'id': item.id, 'url': item.url, 'doi': normalize_doi(item.doi)}
        origins = provenance.setdefault(key, [])
        for previous in item.metadata.get('provenance') or []:
            if isinstance(previous, dict) and previous not in origins: origins.append(previous)
        if origin not in origins: origins.append(origin)
        version = {'doi': normalize_doi(item.doi), 'id': item.id, 'source': item.source,
                   'url': item.url, 'year': item.year, 'version': item.metadata.get('version')}
        members = versions.setdefault(key, [])
        for previous in item.metadata.get('versions') or []:
            if isinstance(previous, dict) and previous.get('doi') and not any(v['doi'] == previous['doi'] for v in members):
                members.append(previous)
        if version['doi'] and not any(v['doi'] == version['doi'] for v in members): members.append(version)
        if key not in groups:
            groups[key] = replace(item, doi=normalize_doi(item.doi))
            continue
        old = groups[key]
        if old.doi and item.doi and normalize_doi(old.doi) != normalize_doi(item.doi):
            # A selected version must keep its own abstract, access and links.
            preferred = max((old, item), key=lambda m: _version_order(m, concepts))
            groups[key] = replace(preferred, doi=normalize_doi(preferred.doi), metadata=dict(preferred.metadata))
            continue
        metadata = dict(item.metadata)
        metadata.update({k: v for k, v in old.metadata.items() if v not in (None, '', [])})
        groups[key] = replace(old, doi=normalize_doi(old.doi) or normalize_doi(item.doi),
            abstract_or_snippet=max((old.abstract_or_snippet or '', item.abstract_or_snippet or ''), key=len) or None,
            authors=old.authors or item.authors, year=old.year or item.year,
            open_url=old.open_url or item.open_url,
            access_status='open' if 'open' in (old.access_status, item.access_status) else old.access_status or item.access_status,
            metadata=metadata)
    for key, item in list(groups.items()):
        if len(dois[key]) > 1:
            item = replace(item, metadata={**item.metadata, 'version_dois': sorted(dois[key]),
                                          'versions': versions[key]})
            groups[key] = item
        if len(provenance[key]) > 1:
            groups[key] = replace(item, metadata={**item.metadata, 'provenance': provenance[key]})
    return [groups[key] for key in sorted(groups,
        key=lambda k: (-sum(1 / (60 + rank) for rank in ranks[k].values()), k))]


@dataclass(frozen=True)
class Judgment:
    tier: str
    objects: tuple[str, ...]
    aspects: tuple[str, ...]
    missing: tuple[str, ...]
    reason: str
    can_promote: bool = True


def judge(intent, item):
    from query_planner import structured_match
    text = item.title + '\n' + (item.abstract_or_snippet or '') + '\n' + (item.material_type or '')
    if intent.structured is not None:
        matched = structured_match(intent.structured, item)
        if matched is False:
            return Judgment('irrelevant', (), (), (), 'Метаданные не соответствуют явному Boolean-условию.', False)
        if matched is None:
            return Judgment('unconfirmed', (), (), (), 'Недостаточно метаданных для проверки полей и отрицательных условий.', False)
        return Judgment('relevant', (), (), (), 'Явное структурированное условие выполнено доступными метаданными.')
    if any(not f.present(text) for f in intent.constraints):
        return Judgment('irrelevant', (), (), tuple(f.name for f in intent.constraints if not f.present(text)),
            'Явно заданная география не подтверждена метаданными.')
    objects = tuple(f.name for f in intent.objects if f.present(text))
    aspects = tuple(f.name for f in intent.aspects if f.present(text))
    missing = tuple(f.name for f in (*intent.objects, *intent.aspects) if not f.present(text))
    if not intent.objects or not objects:
        if any(f.present(material_text(item)) for f in intent.objects):
            return Judgment('partial', (), (), missing, 'Совпадение только по рубрикам; содержание не подтверждено названием/аннотацией.')
        return Judgment('unconfirmed', objects, aspects, missing, 'Объект задачи не подтверждён метаданными.')
    if re.match(r'^(correction|erratum|retraction)\s*:', item.title, re.I) and not re.search(r'correction|erratum|retraction|исправлен|отзыв статьи', intent.original, re.I):
        return Judgment('partial', objects, aspects, missing, 'Уведомление об исправлении/отзыве, не самостоятельное исследование.', False)
    qualifiers = [word for word in intent.qualifiers if word.isalpha() and
        not re.search(r'(?<!\w)' + re.escape(word[:max(4, len(word) - 2)]) + r'\w*(?!\w)', text.casefold())]
    if qualifiers:
        return Judgment('partial', objects, aspects, missing + tuple(qualifiers),
            'Дополнительные условия запроса не подтверждены: ' + ', '.join(qualifiers))
    if len(objects) == len(intent.objects) and len(aspects) == len(intent.aspects):
        return Judgment('relevant', objects, aspects, (), 'Лексическое совпадение условий в метаданных; смысловое соответствие требует проверки.')
    return Judgment('partial', objects, aspects, missing,
        'Подтверждена только часть задачи.' + (' Аннотация отсутствует.' if not item.abstract_or_snippet else ''))


def load_calibration(path, model_name, revision, max_length=384, query_language='original'):
    """Only measured, versioned calibration can introduce CE score cutoffs."""
    try:
        from reranker import PREPROCESSING
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if (data.get('schema') != 1 or data.get('model') != model_name
                or data.get('revision', '') != revision or data.get('labeled_pairs', 0) < 20
                or data.get('query_count', 0) < 5 or not data.get('dataset_sha256')
                or data.get('preprocessing') != PREPROCESSING or data.get('max_length') != max_length
                or data.get('query_language','original') != query_language):
            return None
        lower, upper = float(data['partial_min']), float(data['relevant_min'])
        if not (-1e6 < lower <= upper < 1e6):
            return None
        data.update(partial_min=lower, relevant_min=upper)
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None


def classify(intent, items, scores=None, calibration=None):
    judgments = {}
    relevant, partial = [], []
    for item in items:
        decision = judge(intent, item)
        score = (scores or {}).get(item.id)
        # Missing dictionary terms are not semantic evidence of irrelevance.
        # Hard user filters have already been enforced outside this function.
        if calibration and score is not None and decision.can_promote:
            if score < calibration['partial_min']:
                decision = replace(decision, tier='irrelevant', reason='Ниже порога, измеренного на размеченном наборе.')
            elif score >= calibration['relevant_min'] and item.abstract_or_snippet and decision.can_promote:
                decision = replace(decision, tier='relevant', reason='Подтверждено откалиброванным Cross-Encoder по названию и аннотации.')
            elif decision.tier == 'unconfirmed' or score < calibration['relevant_min']:
                decision = replace(decision, tier='partial', reason='Частичное соответствие по откалиброванному Cross-Encoder.')
        judgments[item.id] = decision
        if decision.tier == 'relevant':
            relevant.append(item)
        elif decision.tier in ('partial', 'unconfirmed'):
            partial.append(item)
    return relevant, partial, judgments
