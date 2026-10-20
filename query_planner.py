"""Preserve natural language; parse only explicit Boolean/fielded expressions."""
from dataclasses import dataclass
from functools import lru_cache
import contextlib
import io
import re

STOP = frozenset(('a an the and or of in on for to with by from is are as at into how does do '
    'research study studies article articles paper papers about '
    'и или в во на по из для с со к о об от как что это исследование статьи найти').split())


def words(text):
    return [w for w in re.findall(r'[^\W_]+', text.casefold()) if w not in STOP and len(w)>1]


def contains(text, phrase):
    parts=[re.escape(t.rstrip('*'))+(r'\w*' if t.endswith('*') else '') for t in phrase.casefold().split()]
    return bool(parts and re.search(r'(?<!\w)'+r'[\s–—-]+'.join(parts)+r'(?!\w)',text.casefold()))


@dataclass(frozen=True)
class Facet:
    name: str
    aliases: tuple[str,...]
    query: str
    def present(self,text):
        return any(contains(text,alias) for alias in self.aliases)


@dataclass(frozen=True)
class QueryIntent:
    original: str
    translated: str
    objects: tuple[Facet,...]
    aspects: tuple[Facet,...]
    qualifiers: tuple[str,...]
    variants: tuple[str,...]
    subject: str
    constraints: tuple[Facet,...] = ()
    structured: object = None


def unique_queries(values,limit=4):
    output=[]
    for value in values:
        if not isinstance(value,str):continue
        value=' '.join(value.split())[:500]
        if not value or re.search(r'https?://|10\.\d{4,9}/',value,re.I):continue
        if value.casefold() not in [q.casefold() for q in output]:output.append(value)
    return tuple(output[:limit])


def is_structured(query):
    if re.search(r'\b(?:ti|au|abs|cat|all):',query):return False
    return bool(re.search(r'\b(?:AND|OR|NOT)\b|\[[^]]+\]',query))


def title_query(query,source):
    value='"'+query.strip().strip('"«»').replace('"','')+'"'
    fields={'pubmed':('[Title]',''), 'arxiv':('', 'ti:'),
        'europepmc':('', 'TITLE:'),'datacite':('', 'titles.title:'),
        'zenodo':('', 'title:'),'core':('', 'title:')}
    suffix,prefix=fields.get(source,('',''))
    return prefix+value+suffix


@lru_cache(maxsize=128)
def parse_structured(query):
    from search_query.parser import parse
    try:
        # The package's linter writes terminal guidance. Validation errors are
        # surfaced to the user; ANSI terminal output does not belong in a bot.
        with contextlib.redirect_stdout(io.StringIO()):
            return parse(query,platform='pubmed')
    except Exception as exc:
        raise ValueError('Некорректный структурированный запрос: проверьте скобки, поля и AND/OR/NOT.') from exc


def positive_terms(node,negative=False):
    if not node.children:
        return [] if negative else [node.value.strip('"')]
    result=[]
    for i,child in enumerate(node.children):
        result.extend(positive_terms(child,negative or node.value=='NOT' and i>0))
    return result


def source_query(query,source):
    if re.search(r'\b(?:ti|au|abs|cat|all):',query):
        if source=='arxiv':return query
        raise ValueError('Полевой синтаксис arXiv требует выбора источника arXiv.')
    if not is_structured(query):return query
    tree=parse_structured(query)
    if source=='pubmed':return tree.to_string()
    if source in ('crossref','worldbank','local','openalex'):
        # These bibliography/search routes do not implement the PubMed field
        # language. NOT operands must never become positive retrieval terms.
        return ' '.join(dict.fromkeys(positive_terms(tree)))
    fields={
        'arxiv':{'title':'ti','abstract':'abs','author':'au','all':'all'},
        'europepmc':{'title':'TITLE','abstract':'ABSTRACT','author':'AUTH','all':''},
        'datacite':{'title':'titles.title','abstract':'descriptions.description','author':'creators.name','all':''},
        'zenodo':{'title':'title','abstract':'description','author':'creators.name','all':''},
        'core':{'title':'title','abstract':'abstract','author':'authors','all':''}}
    if source not in fields:raise ValueError('Структурированный запрос не поддерживается этим каталогом.')
    def render(node):
        if node.children:
            operator=('ANDNOT' if source=='arxiv' else 'AND NOT') if node.value=='NOT' else node.value
            return '('+(' '+operator+' ').join(render(c) for c in node.children)+')'
        raw=(str(node.field or '')).strip('[]').casefold().replace(' ','')
        field={'ti':'title','title':'title','ab':'abstract','abstract':'abstract',
            'tiab':'both','title/abstract':'both','au':'author','author':'author',
            'allfields':'all','all':'all','':'all'}.get(raw)
        if field is None:raise ValueError(f'Поле {raw} не поддерживается источником {source}.')
        value='"'+node.value.strip('"').replace('"','')+'"'
        def term(f):
            prefix=fields[source][f]
            return (prefix+':' if prefix else '')+value
        return '('+term('title')+' OR '+term('abstract')+')' if field=='both' else term(field)
    return render(tree)


def structured_match(node,item):
    """Three-valued metadata check; absent abstract cannot prove a NOT clause."""
    if node.children:
        values=[structured_match(c,item) for c in node.children]
        if node.value=='NOT':
            values=[values[0]]+[None if v is None else not v for v in values[1:]]
        if node.value=='OR':return True if True in values else None if None in values else False
        return False if False in values else None if None in values else True
    field=str(node.field or '').strip('[]').casefold().replace(' ','')
    if field in ('ti','title'):text,complete=item.title,True
    elif field in ('ab','abstract'):text,complete=item.abstract_or_snippet,bool(item.abstract_or_snippet)
    elif field in ('au','author'):text,complete=' '.join(item.authors),bool(item.authors)
    elif field in ('tiab','title/abstract'):
        text=item.title+' '+(item.abstract_or_snippet or '');complete=bool(item.abstract_or_snippet)
    elif field in ('allfields','all',''):
        text=' '.join(filter(None,(item.title,item.abstract_or_snippet,str(item.metadata.get('keywords') or ''),
            item.doi,' '.join(item.authors))))
        complete=False
    else:return None
    if contains(text or '',node.value.strip('"')):return True
    return False if complete else None


def analyze_query(original,translated=None,ai_plan=None,max_variants=4):
    translated=translated or original
    # No closed object/aspect dictionary and no guessed geographic filters.
    # Free prose has one complete retrieval formulation; distinct bilingual
    # wording and explicit paid expansions are bounded alternatives.
    structured=parse_structured(original) if is_structured(original) else None
    base=original if structured else translated
    variants=[base]
    if not structured and translated.casefold()!=original.casefold():variants.append(original)
    if ai_plan and not structured:
        variants.extend(ai_plan.get('queries') or [ai_plan.get('query','')])
    terms=list(dict.fromkeys(positive_terms(structured) if structured else words(translated)))
    facets=tuple(Facet(t,(t,),t) for t in terms)
    return QueryIntent(original,translated,facets,(),(),unique_queries(variants,max_variants),'',(),structured)
