"""Statistical catalog discovery and bounded observations; no publication reranking."""
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode

from external_sources import SourceUnavailable, plain, year_of
from search import Material

STAT_SOURCES=frozenset(("oecd","imf","comtrade","eurostat","ilostat"))
XML_ACCEPT={"Accept":"application/vnd.sdmx.structure+xml;version=2.1"}
COUNTRY_DIMS=frozenset(("REF_AREA","COUNTRY","GEO","REPORTER","REP_AREA"))
STOP={"data","dataset","statistics","statistical","indicator","the","of","and","in","for"}


# Adapted from OpenSDMX discovery.set_filters/_normalise_dim, revision
# 1464ed1b640969470bfabbb36a20c6ee84f94923. Copyright (c) 2026 Andrea
# Borruso, MIT; full notice in data/third_party_licenses.txt.
def validate_dimensions(dimensions, selected, labels, available=None):
    exact = {d.upper(): d for d in dimensions}
    normal = {}
    for dim in dimensions:
        normal.setdefault(dim.upper().replace('-', '_'), []).append(dim)
    result = {}
    for key, value in selected.items():
        actual = exact.get(key.upper())
        if actual is None:
            candidates = normal.get(key.upper().replace('-', '_'), [])
            actual = candidates[0] if len(candidates) == 1 else None
        if actual is None:
            import difflib
            hint = difflib.get_close_matches(key.upper(), dimensions, n=1)
            raise ValueError(f"Неизвестное измерение {key}. " +
                (f"Возможно, {hint[0]}. " if hint else '') +
                "Доступные измерения: " + ', '.join(dimensions))
        codes = [c for c in str(value).split('+') if c and c != '*']
        allowed = (available or {}).get(actual) or labels.get(actual)
        if allowed and any(code not in allowed for code in codes):
            invalid = [code for code in codes if code not in allowed]
            raise ValueError(f"Недопустимый код {actual}={'+'.join(invalid)}. " +
                'Допустимые коды: ' + ', '.join(list(allowed)[:25]))
        if actual in result and result[actual] != str(value):
            raise ValueError(f'Конфликт значений измерения {actual}.')
        result[actual] = str(value)
    return result


def read_structure(text, structure_id=None):
    import sdmx
    message = sdmx.read_sdmx(io.BytesIO(text.encode('utf-8')))
    definitions = message.structure
    dsd = definitions.get(structure_id) if structure_id else None
    if dsd is None and len(definitions) == 1:
        dsd = next(iter(definitions.values()))
    if dsd is None:
        raise SourceUnavailable('Однозначная структура SDMX не получена.')
    dimensions = [d.id for d in sorted(dsd.dimensions, key=lambda d: d.order or 999)
                  if d.__class__.__name__ != 'TimeDimension']
    labels = {}
    for component in list(dsd.dimensions) + list(dsd.attributes):
        representation = component.local_representation or getattr(component.concept_identity,'core_representation',None)
        codes = representation.enumerated if representation else None
        labels[component.id] = {c.id: str(c.name) for c in codes} if codes is not None else {}
    available = {}
    for constraint in message.constraint.values():
        for region in getattr(constraint, 'data_content_region', []):
            if not region.included:
                continue
            for dim, selection in region.member.items():
                codes = {str(v.value) for v in selection.values}
                if codes:
                    available.setdefault(dim.id, set()).update(codes)
    return dsd, dimensions, labels, available

def local(tag):return tag.rsplit("}",1)[-1]
def children(node,name):return [n for n in node.iter() if local(n.tag)==name]
def name_of(node):
    names=[n for n in node if local(n.tag)=="Name"]
    return next((n.text for n in names if n.get("{http://www.w3.org/XML/1998/namespace}lang")=="en"),None) or (names[0].text if names else "")
def score(query,text):
    words=[w for w in re.findall(r"[^\W_]+",query.casefold()) if w not in STOP and len(w)>1]
    return sum(w in text.casefold() for w in dict.fromkeys(words))

@dataclass(frozen=True)
class StatisticalObservation:
    source:str
    dataset:str
    indicator:str
    territory:str
    period:str
    unit:str
    value:object
    url:str
    dimensions:dict

    def material(self):
        ident=self.source+":"+hashlib.sha256(json.dumps(
            [self.dataset,self.dimensions,self.period],sort_keys=True).encode()).hexdigest()[:24]
        title=self.indicator+" — "+self.territory+" — "+self.period
        return Material(ident,"statistical-series",title,year=year_of(self.period),
            url=self.url,open_url=self.url,access_status="open",
            abstract_or_snippet=f"{self.value} {self.unit or '(единицы не указаны источником)'}",
            source=self.source,metadata={"category":"statistics","statistical_kind":"observation",
                "dataset":self.dataset,"indicator":self.indicator,"territory":self.territory,
                "period":self.period,"unit":self.unit,"value":self.value,"dimensions":self.dimensions,
                "unit_multiplier": self.dimensions.get('UNIT_MULT', self.dimensions.get('SCALE')),
                "observation_status": self.dimensions.get('OBS_STATUS'),
                "preview": bool(self.dimensions.get('preview', False)),
                "data_url":self.url,"sampled":True})

class SDMXSource:
    def __init__(self,http,name,base,agency="all"):
        self.http,self.name,self.base,self.agency=http,name,base,agency

    async def search(self,query,doi=None,limit=20,*,filters=None):
        if doi:return []
        text=await self.http.get_text(self.name,self.base+f"/dataflow/{self.agency}/all/latest",
            {"references":"none","detail":"allstubs"},headers=XML_ACCEPT,ttl=86400)
        if not text:return []
        root=ET.fromstring(text);matches=[]
        for flow in children(root,"Dataflow"):
            agency=flow.get("agencyID","");ident=flow.get("id","");version=flow.get("version","latest")
            if self.name=="oecd" and not agency.startswith("OECD"):continue
            title=name_of(flow) or ident
            desc=" ".join(n.text or "" for n in flow if local(n.tag)=="Description")
            weight=100 if query.strip().casefold()==ident.casefold() else score(query,title+" "+desc+" "+ident)
            if not weight:continue
            structure=next((n for s in flow if local(s.tag)=="Structure" for n in s.iter()
                if local(n.tag)=="Ref" and n.get("class")=="DataStructure"),None)
            ref=",".join((agency,ident,version))
            md={"category":"statistics","statistical_kind":"dataset","dataset_ref":ref,
                "source_id":ident,"dataset_base":self.base,"requested_period":
                    [getattr(filters,"year_from",None),getattr(filters,"year_to",None)]}
            if structure is not None:md["structure_ref"]={k:structure.get(k) for k in ("agencyID","id","version")}
            url=self.base+"/dataflow/"+"/".join(quote(x,safe="@") for x in (agency,ident,version))
            item=Material(self.name+":"+ref,"statistical-dataset",title,url=url,open_url=url,
                access_status="open",abstract_or_snippet=desc or None,source=self.name.upper(),metadata=md)
            matches.append((weight,item))
        return [item for _,item in sorted(matches,key=lambda pair:-pair[0])[:limit]]

    async def schema(self, item):
        ref = item.metadata['dataset_ref']
        if not re.fullmatch(r'[A-Za-z0-9_@.,-]+', ref):
            raise ValueError('Неверный набор.')
        sref = item.metadata.get('structure_ref')
        if not sref:
            agency, ident, version = ref.split(',')
            text = await self.http.get_text(self.name,
                self.base + '/dataflow/' + '/'.join(quote(v, safe='@') for v in (agency, ident, version)),
                {'references': 'descendants' if self.name=='eurostat' else 'all'}, headers=XML_ACCEPT, ttl=86400)
            root = ET.fromstring(text)
            reference = next((n for f in children(root, 'Dataflow') for s in f
                if local(s.tag) == 'Structure' for n in s.iter()
                if local(n.tag) == 'Ref' and n.get('class') == 'DataStructure'), None)
            if reference is None:
                raise SourceUnavailable('Источник не указал структуру измерений набора.')
            sref = dict(reference.attrib)
            if children(root, 'DataStructure'):
                return read_structure(text, sref['id'])
        url = self.base + '/datastructure/' + '/'.join(
            quote(sref.get(k) or 'latest', safe='@') for k in ('agencyID', 'id', 'version'))
        text = await self.http.get_text(self.name, url,
            {'references': 'descendants' if self.name=='eurostat' else 'all'}, headers=XML_ACCEPT, ttl=86400)
        return read_structure(text, sref['id'])

    async def data(self,item,filters):
        territories=tuple(getattr(filters,"territories",()))
        raw_key=getattr(filters,"statistics_key","").strip()
        if not territories and not raw_key:
            raise ValueError("Укажите территорию в фильтрах статистики перед загрузкой значений.")
        ref=item.metadata["dataset_ref"]
        if not re.fullmatch(r"[A-Za-z0-9_@.,-]+",ref):raise ValueError("Неверный набор.")
        dsd, dimensions, labels, available = await self.schema(item)
        selected = validate_dimensions(dimensions, filters.statistics_dimensions or {}, labels, available)
        geo = next((d for d in dimensions if d.upper() in COUNTRY_DIMS), None)
        if territories:
            if not geo:
                raise ValueError('В этом наборе нет измерения территории; используйте точный ключ ряда.')
            supplied = validate_dimensions(dimensions, {geo: '+'.join(territories)}, labels, available)
            if geo in selected and selected[geo] != supplied[geo]:
                raise ValueError('Территория конфликтует с фильтром измерений.')
            selected.update(supplied)
        if raw_key:
            parts = raw_key.split('.')
            if len(parts) != len(dimensions):
                raise ValueError('Порядок ключа: ' + '.'.join(dimensions))
            key_values = validate_dimensions(dimensions, dict(zip(dimensions, parts)), labels, available)
            if any(key_values[k] != v for k, v in selected.items()):
                raise ValueError('Точный ключ конфликтует с выбранными фильтрами измерений/территорий.')
            selected = key_values
        else:
            raw_key = '.'.join(selected.get(d, '') for d in dimensions)
        if not re.fullmatch(r"[A-Za-z0-9_+.*:-]{1,500}",raw_key):raise ValueError("Неверный ключ статистического ряда.")
        params={"lastNObservations":min(20,filters.max_results)}
        if filters.year_from:params["startPeriod"]=str(filters.year_from)
        if filters.year_to:params["endPeriod"]=str(filters.year_to)
        url=self.base+"/data/"+quote(ref,safe=",@")+"/"+quote(raw_key,safe=".+*:-")
        text=await self.http.get_text(self.name,url,params,
            headers={"Accept":"application/vnd.sdmx.data+csv;version=1.0"},ttl=3600)
        if not text:return []
        rows=parse_observations(text, dsd=dsd)
        result=[]
        public_url=url+"?"+urlencode(params)
        for row in rows:
            period=str(row.get("TIME_PERIOD") or row.get("time") or "")
            value=row.get("OBS_VALUE")
            if value in (None,"") or str(value).casefold() in ('nan','null','na',':','..'):continue
            year=year_of(period)
            if filters.year_from and (not year or year<filters.year_from):continue
            if filters.year_to and (not year or year>filters.year_to):continue
            if any(v and v != '*' and str(row.get(k, '')) not in v.split('+') for k, v in selected.items()):
                continue
            area=next((str(v) for k,v in row.items() if k.upper() in COUNTRY_DIMS),"")
            area=labels.get(geo,{}).get(area,area)
            indicator=str(row.get("INDICATOR") or row.get("MEASURE") or row.get("VARIABLE") or item.title)
            for field in ('INDICATOR','MEASURE','VARIABLE'):
                if row.get(field):
                    indicator=labels.get(field,{}).get(str(row[field]),str(row[field]))
                    break
            unit=str(row.get("UNIT_MEASURE") or row.get("UNIT") or "")
            unit=labels.get('UNIT_MEASURE', labels.get('UNIT', {})).get(unit, unit)
            result.append(StatisticalObservation(self.name,ref,indicator,area,period,unit,value,
                public_url,{k:v for k,v in row.items() if k not in ("OBS_VALUE","TIME_PERIOD")}).material())
            if len(result)>=filters.max_results:break
        return result

def parse_observations(text, dsd=None):
    """Both CSV and SDMX Generic/StructureSpecific XML are accepted."""
    if not text.lstrip().startswith("<"):
        return list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    if dsd is not None:
        import sdmx
        message = sdmx.read_sdmx(io.BytesIO(text.encode('utf-8')), dsd=dsd)
        rows = []
        for dataset in message.data:
            for obs in dataset.obs:
                if obs.value is None:continue
                row = {k: str(v.value) for k, v in obs.key.values.items()}
                row.update({k: str(v.value) for k, v in obs.attrib.items()})
                row['OBS_VALUE'] = str(obs.value)
                rows.append(row)
        return rows
    root=ET.fromstring(text);rows=[]
    datasets=children(root,"DataSet")
    for ds in datasets:
        defaults=dict(ds.attrib)
        for series in [n for n in ds if local(n.tag)=="Series"]:
            fields={**defaults,**series.attrib}
            for group in series:
                if local(group.tag) in ("SeriesKey","Attributes"):
                    fields.update({v.get("id"):v.get("value") for v in group if local(v.tag)=="Value"})
            for obs in [n for n in series if local(n.tag)=="Obs"]:
                row={**fields,**obs.attrib}
                for field in obs:
                    if local(field.tag)=="ObsDimension":row["TIME_PERIOD"]=field.get("value")
                    elif local(field.tag)=="ObsValue":row["OBS_VALUE"]=field.get("value")
                    elif local(field.tag)=="Attributes":row.update({v.get("id"):v.get("value") for v in field})
                rows.append(row)
        for obs in [n for n in ds if local(n.tag)=="Obs"]:rows.append({**defaults,**obs.attrib})
    return rows

class Eurostat(SDMXSource):
    def __init__(self,http):super().__init__(http,"eurostat",
        "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1","ESTAT")

    async def data(self,item,filters):
        if filters.statistics_key:
            return await super().data(item, filters)
        ref=item.metadata["dataset_ref"].split(",")[1]
        _, dimensions, labels, available = await self.schema(item)
        selected = validate_dimensions(dimensions, filters.statistics_dimensions or {}, labels, available)
        geo = next((d for d in dimensions if d.upper() == 'GEO'), None)
        if not geo:
            raise ValueError('Схема Eurostat не содержит geo.')
        validate_dimensions(dimensions, {geo: '+'.join(filters.territories)}, labels, available)
        if geo in selected and selected[geo].split('+') != list(filters.territories):
            raise ValueError('Территория конфликтует с фильтром geo.')
        params={"lang":"en"}
        if filters.territories:params["geo"]=list(filters.territories)
        if filters.year_from:params["sinceTimePeriod"]=str(filters.year_from)
        if filters.year_to:params["untilTimePeriod"]=str(filters.year_to)
        for k,v in selected.items():params[k.lower()]=v.split('+')
        if not filters.territories:raise ValueError("Выберите территорию для Eurostat.")
        url="https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"+quote(ref,safe="_")
        data=await self.http.get(self.name,url,params)
        if data.get("error"):raise SourceUnavailable("Eurostat отклонил набор или фильтры.")
        ids=data.get("id") or [];sizes=data.get("size") or [];dims=data.get("dimension") or {}
        indexes={}
        for dim in ids:
            category=dims[dim].get("category") or {}
            idx=category.get("index") or {}
            indexes[dim]=dict(enumerate(idx)) if isinstance(idx,list) else {v:k for k,v in idx.items()}
        vals=data.get("value") or {}
        values=list(enumerate(vals)) if isinstance(vals,list) else [(int(k),v) for k,v in vals.items()]
        output=[]
        for flat,value in sorted(values):
            if value is None:continue
            coords={};rest=flat
            for dim,size in reversed(list(zip(ids,sizes))):
                coords[dim]=indexes[dim].get(rest%size,"");rest//=size
            period=str(coords.get("time",""))
            area=str(coords.get("geo",""))
            year=year_of(period)
            if area not in filters.territories:continue
            if filters.year_from and (year is None or year<filters.year_from):continue
            if filters.year_to and (year is None or year>filters.year_to):continue
            if any(v and v!='*' and coords.get(k.lower()) not in v.split('+') for k,v in selected.items()):continue
            status=data.get('status') or {}
            observation_status=status[flat] if isinstance(status,list) and flat<len(status) else status.get(str(flat)) if isinstance(status,dict) else None
            if observation_status is not None:coords['OBS_STATUS']=observation_status
            unit_code=coords.get("unit","")
            unit=((dims.get("unit") or {}).get("category") or {}).get("label",{}).get(unit_code,unit_code)
            area=((dims.get("geo") or {}).get("category") or {}).get("label",{}).get(area,area)
            output.append(StatisticalObservation(self.name,ref,data.get("label") or item.title,area,period,unit,
                value,url+"?"+urlencode(params,doseq=True),coords).material())
            if len(output)>=filters.max_results:break
        return output

class Comtrade:
    name="comtrade"
    def __init__(self,http):self.http=http

    async def search(self,query,doi=None,limit=20,*,filters=None):
        if doi:return []
        data=await self.http.get(self.name,"https://comtradeapi.un.org/files/v1/app/reference/HS.json",{},ttl=86400)
        rows=data.get("results") or []
        exact=re.fullmatch(r"(?:HS[:\s]*)?(TOTAL|\d{2,6})",query.strip(),re.I)
        ranked=[]
        for row in rows:
            ident=str(row["id"]);title=plain(row.get("text"))
            weight=100 if exact and ident==exact[1].upper() else score(query,title) if not exact else 0
            if not weight:continue
            ranked.append((weight,Material("comtrade:HS:"+ident,"statistical-dataset",title,
                url="https://comtradeplus.un.org/",open_url="https://comtradeplus.un.org/",access_status="open",
                source="UN Comtrade",metadata={"category":"statistics","statistical_kind":"dataset",
                    "dataset_ref":"HS:"+ident,"commodity":ident,"source_id":ident,
                    "indicator":title,"unit":row.get("standardUnitAbbr")})))
        return [m for _,m in sorted(ranked,key=lambda p:-p[0])[:limit]]

    async def data(self,item,filters):
        if not filters.territories or not (filters.year_from or filters.year_to):
            raise ValueError("Для торговли укажите территорию и год/период.")
        countries=await self.http.get(self.name,"https://comtradeapi.un.org/files/v1/app/reference/Reporters.json",{},ttl=86400)
        mapping={}
        for c in countries.get("results",[]):
            for k in ("id","reporterCodeIsoAlpha2","reporterCodeIsoAlpha3"):
                if c.get(k) is not None:mapping[str(c[k]).upper()]=str(c["id"])
        try:reporters=",".join(mapping[c.upper()] for c in filters.territories)
        except KeyError:raise ValueError("Территория не найдена в справочнике UN Comtrade.") from None
        lo=filters.year_from or filters.year_to;hi=filters.year_to or lo
        if hi-lo>4:raise ValueError("За один запрос торговли допускается до пяти лет.")
        selected=filters.statistics_dimensions or {}
        allowed=('flowCode','partnerCode','partner2Code','customsCode','motCode')
        names={k.casefold():k for k in allowed}
        if any(k.casefold() not in names for k in selected):
            raise ValueError('Измерения Comtrade: '+', '.join(allowed))
        selected={names[k.casefold()]:v for k,v in selected.items()}
        if filters.statistics_key:
            raise ValueError('Comtrade использует коды измерений, а не SDMX-ключ ряда.')
        if any(v not in ('M','X','RM','RX','MIP','XIP','MOP','XOP') for v in selected.get('flowCode','M,X').split(',')):
            raise ValueError('Неизвестный flowCode Comtrade.')
        params={"period":",".join(str(y) for y in range(lo,hi+1)),"reporterCode":reporters,
            "cmdCode":item.metadata["commodity"],"flowCode":selected.get("flowCode","M,X"),"partnerCode":selected.get("partnerCode","0"),
            "partner2Code":selected.get('partner2Code','0'),"customsCode":selected.get('customsCode','C00'),
            "motCode":selected.get('motCode','0'),"maxRecords":min(500,filters.max_results)}
        slot=getattr(self.http.config,"un_comtrade_key_slot",1)
        key=getattr(self.http.config,"un_comtrade_api_key_"+str(slot),"")
        # Slots are neutral names; never rotate keys on quota/authentication errors.
        headers={"Ocp-Apim-Subscription-Key":key} if key else None
        url="https://comtradeapi.un.org/"+("data/v1/get/C/A/HS" if key else "public/v1/preview/C/A/HS")
        data=await self.http.get(self.name,url,params,headers=headers)
        output=[]
        for row in data.get("data") or []:
            value=row.get("primaryValue")
            if value is None:continue
            year=year_of(row.get('period'))
            if year is None or year<lo or year>hi:continue
            if str(row.get('reporterCode','')) not in reporters.split(','):continue
            if str(row.get('flowCode','')) not in str(params['flowCode']).split(','):continue
            if row.get('cmdCode') is not None and str(row['cmdCode'])!=str(params['cmdCode']):continue
            if any(str(row.get(k,'')) not in str(v).split(',') for k,v in selected.items()):continue
            indicator=plain(row.get("cmdDesc") or item.title)+"; "+plain(row.get("flowDesc") or row.get("flowCode"))
            md={**row,"preview":not bool(key),"subscription_slot":slot if key else None}
            output.append(StatisticalObservation(self.name,item.metadata["dataset_ref"],indicator,
                plain(row.get("reporterDesc") or row.get("reporterISO") or row.get("reporterCode")),
                str(row.get("period") or ""),"USD",value,url+"?"+urlencode(params),md).material())
        return output[:filters.max_results]

def statistical_adapters(http):
    return {
        "oecd":SDMXSource(http,"oecd","https://sdmx.oecd.org/public/rest/v1"),
        "imf":SDMXSource(http,"imf","https://api.imf.org/external/sdmx/2.1"),
        # The ILO-documented dissemination endpoint has 1,215 flows; the
        # sdmx.data.ilo.org exchange endpoint only contains one IMF flow.
        "ilostat":SDMXSource(http,"ilostat","https://sdmx.ilo.org/rest/v1","ILO"),
        "eurostat":Eurostat(http),
        "comtrade":Comtrade(http),
    }
