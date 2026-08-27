from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from papa_shin_stock.cache import GenerationFiles
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.query import SearchQuery, normalize_tire_size

_UNKNOWN = {"unknown", "missing"}
_PUBLIC_CHARS = {"load_index", "speed_index"}
_MAX_TEXT = 256
_MAX_OUTPUT = 512 * 1024


def assert_generation(row: dict[str, object], expected: str) -> None:
    if row.get("content_generation_id") != expected:
        raise StockError("generation_mismatch", "Поколение данных не согласовано", 5)


@dataclass(frozen=True, slots=True)
class Offer:
    supplier: str; price: Decimal; delivery_days: int; quantity: int
    def to_public_dict(self) -> dict[str, object]: return {"supplier": self.supplier, "price": _price_text(self.price), "delivery_days": self.delivery_days, "quantity": self.quantity}

@dataclass(frozen=True, slots=True)
class Product:
    product_id: str; name: str; article: str; product_type: str; characteristics: dict[str, str]; total_quantity: int; unknown_characteristics: tuple[dict[str, str], ...]
    def to_public_dict(self, offers: tuple[Offer, ...]) -> dict[str, object]: return {"product_id":self.product_id,"name":self.name,"article":self.article,"product_type":self.product_type,"characteristics":self.characteristics,"total_quantity":self.total_quantity,"minimum_price":_price_text(offers[0].price) if offers else None,"offers":[x.to_public_dict() for x in offers]}

@dataclass(frozen=True, slots=True)
class SearchSummary:
    sku_count: int; total_quantity: int
    def to_public_dict(self) -> dict[str, int]: return {"sku_count":self.sku_count,"total_quantity":self.total_quantity}

@dataclass(frozen=True, slots=True)
class SearchResult:
    generation: dict[str, object]; filters: dict[str, object]; summary: SearchSummary; products: tuple[Product, ...]; offers: dict[str, tuple[Offer, ...]]; warnings: tuple[dict[str, str], ...]=(); status: str="ok"
    @property
    def unknown_characteristics(self) -> tuple[dict[str, str], ...]: return tuple(x for p in self.products for x in p.unknown_characteristics)
    def to_public_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status":self.status,"generation":self.generation,"filters":self.filters,"summary":self.summary.to_public_dict(),"products":[],"unknown_characteristics":[],"warnings":list(self.warnings)}
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > _MAX_OUTPUT: raise StockError("query_invalid","Слишком большой запрос",4)
        for product in self.products:
            result["products"].append(product.to_public_dict(self.offers[product.product_id])); result["unknown_characteristics"].extend(product.unknown_characteristics)
            if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > _MAX_OUTPUT:
                result["products"].pop()
                if product.unknown_characteristics: del result["unknown_characteristics"][-len(product.unknown_characteristics):]
                result["warnings"].append({"code":"output_truncated","message":"Вывод ограничен"})
                if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) > _MAX_OUTPUT: raise StockError("query_invalid","Слишком большой запрос",4)
                break
        return result

class _Spool:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="papa-shin-search-"); self.db=sqlite3.connect(str(Path(self.temp.name)/"query.sqlite3")); self.db.create_collation("decimal", _decimal_compare)
        self.db.executescript("CREATE TABLE c(id TEXT PRIMARY KEY,n TEXT,a TEXT,t TEXT,ch TEXT,q INTEGER,u TEXT); CREATE TABLE o(id TEXT,s TEXT,p TEXT,d INTEGER,q INTEGER)")
    def close(self) -> None: self.db.close(); self.temp.cleanup()
    def add_product(self, p: Product) -> None:
        try: self.db.execute("INSERT INTO c VALUES(?,?,?,?,?,?,?)",(p.product_id,p.name,p.article,p.product_type,json.dumps(p.characteristics),p.total_quantity,json.dumps(p.unknown_characteristics)))
        except sqlite3.IntegrityError as error: raise StockError("manifest_invalid","Некорректные данные товаров",3) from error
    def has(self, ident: str) -> bool: return self.db.execute("SELECT 1 FROM c WHERE id=?",(ident,)).fetchone() is not None
    def add_offer(self, ident: str, offer: Offer, limit: int) -> None:
        self.db.execute("INSERT INTO o VALUES(?,?,?,?,?)",(ident,offer.supplier,_price_text(offer.price),offer.delivery_days,offer.quantity)); self.db.execute("DELETE FROM o WHERE id=? AND rowid NOT IN (SELECT rowid FROM o WHERE id=? ORDER BY p COLLATE decimal,d,q DESC LIMIT ?)",(ident,ident,limit))
    def result(self, query: SearchQuery) -> tuple[SearchSummary,tuple[Product,...],dict[str,tuple[Offer,...]]]:
        required=any(x is not None for x in (query.supplier,query.max_price,query.max_delivery_days)); where="WHERE EXISTS(SELECT 1 FROM o WHERE o.id=c.id)" if required else ""
        count,total=self.db.execute(f"SELECT COUNT(*),COALESCE(SUM(q),0) FROM c {where}").fetchone()
        rows=self.db.execute(f"SELECT id,n,a,t,ch,q,u FROM c {where} ORDER BY CASE WHEN EXISTS(SELECT 1 FROM o WHERE o.id=c.id) THEN 0 ELSE 1 END,(SELECT p FROM o WHERE o.id=c.id ORDER BY p COLLATE decimal,d,q DESC LIMIT 1) COLLATE decimal,q DESC,id LIMIT ?",(query.limit,))
        products=[]; offers={}
        for row in rows:
            product=Product(row[0],row[1],row[2],row[3],json.loads(row[4]),row[5],tuple(json.loads(row[6]))); products.append(product)
            offers[product.product_id]=tuple(Offer(x[0],Decimal(x[1]),x[2],x[3]) for x in self.db.execute("SELECT s,p,d,q FROM o WHERE id=? ORDER BY p COLLATE decimal,d,q DESC",(product.product_id,)))
        return SearchSummary(count,total),tuple(products),offers

class StockSearcher:
    def __init__(self, files: GenerationFiles, config: StockConfig) -> None: self.files=files; self.config=config
    def search(self, query: SearchQuery) -> SearchResult:
        spool=_Spool()
        try:
            generation,warnings=_generation(self.files)
            for row in _rows(self.files.products):
                assert_generation(row,self.files.generation_id); product=_product(row,self.config.resolve_product_id(row))
                if _match_product(product,row,query): spool.add_product(product)
            for row in _rows(self.files.offers):
                assert_generation(row,self.files.generation_id); ident=_offer_id(row,self.config.offer_product_id_field)
                if spool.has(ident):
                    offer=_offer(row)
                    if _match_offer(offer,query): spool.add_offer(ident,offer,query.offers_limit)
            summary,products,offers=spool.result(query); return SearchResult(generation,query.public_filters(),summary,products,offers,warnings)
        finally: spool.close()

def _rows(path: Path):
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    try: value=_parse(line)
                    except (ValueError,TypeError,OverflowError,RecursionError,json.JSONDecodeError) as error: raise StockError("manifest_invalid","Некорректные машинные данные",3) from error
                    if not isinstance(value,dict): raise StockError("manifest_invalid","Некорректные машинные данные",3)
                    yield value
    except StockError: raise
    except (OSError,UnicodeError) as error: raise StockError("cache_unavailable","Проверенный кэш недоступен",7) from error

def _generation(files: GenerationFiles) -> tuple[dict[str,object],tuple[dict[str,str],...]]:
    try: manifest=_parse(files.manifest.read_text(encoding="utf-8"))
    except (OSError,UnicodeError,ValueError,TypeError) as error: raise StockError("cache_unavailable","Проверенный кэш недоступен",7) from error
    if not isinstance(manifest,dict) or manifest.get("generation_id")!=files.generation_id or not isinstance(manifest.get("generated_at"),str): raise StockError("generation_mismatch","Поколение данных не согласовано",5)
    state=None; path=files.manifest.parent/"state.json"
    if path.is_file() and not path.is_symlink():
        try: state=_parse(path.read_text(encoding="utf-8"))
        except (OSError,UnicodeError,ValueError,TypeError) as error: raise StockError("cache_unavailable","Проверенный кэш недоступен",7) from error
    if state is not None and (not isinstance(state,dict) or state.get("generation_id") != files.generation_id or not isinstance(state.get("checked_at"),str) or not state["checked_at"] or len(state["checked_at"])>_MAX_TEXT or not isinstance(state.get("stale",False),bool) or (state.get("warning_code") is not None and (not isinstance(state["warning_code"],str) or len(state["warning_code"])>_MAX_TEXT))): raise StockError("cache_unavailable","Проверенный кэш недоступен",7)
    stale=isinstance(state,dict) and state.get("stale") is True; code=state.get("warning_code") if isinstance(state,dict) else None
    return {"id":files.generation_id,"generated_at":manifest["generated_at"],"checked_at":state.get("checked_at",manifest["generated_at"]) if isinstance(state,dict) else manifest["generated_at"],"stale":stale}, (({"code":code,"message":"Используется предыдущее поколение"},) if stale and isinstance(code,str) else ())

def _product(row: dict[str,object], ident: str) -> Product:
    chars=row.get("characteristics",{});
    if not isinstance(chars,dict): raise StockError("manifest_invalid","Некорректные данные товаров",3)
    public={k:v for k,v in chars.items() if k in _PUBLIC_CHARS and isinstance(v,str) and len(v)<=_MAX_TEXT}
    return Product(ident,_text(row.get("name")),_text(row.get("article")),_text(row.get("product_type")),public,_int(row.get("total_quantity")),_unknown(ident,row,chars))
def _match_product(p: Product,row: dict[str,object],q: SearchQuery) -> bool:
    if p.total_quantity<q.min_total_quantity:return False
    if q.size is not None and (not isinstance(row.get("size"),str) or normalize_tire_size(row["size"])!=q.size):return False
    for field in ("product_type","season","spikes","run_flat","disk_type","truck_axis","truck_construction"):
        if (wanted:=getattr(q,field)) is not None and (p.product_type if field=="product_type" else row.get(field))!=wanted:return False
    return True
def _offer_id(row: dict[str,object],field:str)->str:
    value=row.get(field)
    if not isinstance(value,(str,int)) or isinstance(value,bool) or not str(value):raise StockError("query_invalid","У предложения отсутствует идентификатор товара",4)
    return str(value)
def _offer(row:dict[str,object])->Offer:return Offer(_text(row.get("supplier")),_decimal(row.get("price")),_int(row.get("delivery_days")),_int(row.get("quantity")))
def _match_offer(o:Offer,q:SearchQuery)->bool:return (q.supplier is None or o.supplier==q.supplier) and (q.max_price is None or o.price<=q.max_price) and (q.max_delivery_days is None or o.delivery_days<=q.max_delivery_days)
def _unknown(ident:str,row:dict[str,object],chars:dict[str,object])->tuple[dict[str,str],...]:
    result=[]
    for field,value in list((x,row.get(x)) for x in ("size","season","spikes","run_flat","disk_type","truck_axis","truck_construction"))+[(x,chars[x]) for x in sorted(_PUBLIC_CHARS) if x in chars]:
        if isinstance(value,dict) and value.get("status") in _UNKNOWN:result.append({"product_id":ident,"characteristic":field,"status":value["status"]})
    return tuple(result)
def _text(value:object)->str:
    if not isinstance(value,str) or not value or len(value)>_MAX_TEXT:raise StockError("manifest_invalid","Некорректные машинные данные",3)
    return value
def _int(value:object)->int:
    try: result=int(value)
    except (TypeError,ValueError,OverflowError) as error:raise StockError("manifest_invalid","Некорректные машинные данные",3) from error
    if isinstance(value,bool) or result<0:raise StockError("manifest_invalid","Некорректные машинные данные",3)
    return result
def _decimal(value:object)->Decimal:
    try: result=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as error:raise StockError("manifest_invalid","Некорректные машинные данные",3) from error
    if not result.is_finite() or result<0 or abs(result.adjusted())>128:raise StockError("manifest_invalid","Некорректные машинные данные",3)
    return result
def _price_text(value:Decimal)->str:return format(value,"f")
def _decimal_compare(a:str,b:str)->int:return (_decimal(a)>_decimal(b))-(_decimal(a)<_decimal(b))
def _parse(value:str)->object:
    parsed=json.loads(value,object_pairs_hook=_unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError("non-finite"))); _finite(parsed); return parsed
def _finite(value:object)->None:
    if isinstance(value,float) and not math.isfinite(value): raise ValueError("non-finite")
    if isinstance(value,dict):
        for item in value.values(): _finite(item)
    elif isinstance(value,list):
        for item in value: _finite(item)
def _unique(pairs:list[tuple[str,object]])->dict[str,object]:
    result={}
    for key,value in pairs:
        if key in result:raise ValueError("duplicate")
        result[key]=value
    return result
