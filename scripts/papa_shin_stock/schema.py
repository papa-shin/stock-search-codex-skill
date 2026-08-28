from __future__ import annotations

import atexit
import json
import math
import re
import shutil
import sqlite3
import tempfile
import threading
import warnings
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from papa_shin_stock.cache import (
    GenerationFiles,
    load_runtime_status,
)
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.query import SearchQuery, normalize_tire_size

_UNKNOWN = {"unknown", "missing"}
_PUBLIC_CHARS = {"load_index", "speed_index"}
_MAX_TEXT = 256
_MAX_OUTPUT = 512 * 1024

# Public machine-data resource contract. A JSONL record limit excludes CR/LF.
MAX_JSONL_LINE_BYTES = 2 * 1024 * 1024
MAX_PRODUCT_ROWS = 5_000_000
MAX_OFFER_ROWS = 50_000_000
MAX_SPOOL_BYTES = 8 * 1024 * 1024 * 1024
MAX_SPOOL_RECORDS = MAX_PRODUCT_ROWS + MAX_OFFER_ROWS

_SQLITE_PAGE_BYTES = 4096
_SQLITE_CACHE_KIB = 64 * 1024
_SPOOL_CLEANUP_ATTEMPTS = 3
_CANONICAL_NONNEGATIVE_INTEGER = re.compile(r"^(?:0|\+?[1-9][0-9]*)$")
_PENDING_SPOOL_CLEANUPS: dict[Path, object] = {}
_PENDING_SPOOL_CLEANUPS_LOCK = threading.Lock()


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
        if _public_size(result) > _MAX_OUTPUT: raise StockError("query_invalid","Слишком большой запрос",4)
        unknown_counts: list[int] = []
        for product in self.products:
            result["products"].append(product.to_public_dict(self.offers[product.product_id])); result["unknown_characteristics"].extend(product.unknown_characteristics); unknown_counts.append(len(product.unknown_characteristics))
            if _public_size(result) > _MAX_OUTPUT:
                result["products"].pop()
                removed_unknown = unknown_counts.pop()
                if removed_unknown: del result["unknown_characteristics"][-removed_unknown:]
                warning = {"code":"output_truncated","message":"Вывод ограничен"}
                result["warnings"].append(warning)
                while _public_size(result) > _MAX_OUTPUT and result["products"]:
                    result["products"].pop()
                    removed_unknown = unknown_counts.pop()
                    if removed_unknown: del result["unknown_characteristics"][-removed_unknown:]
                if _public_size(result) > _MAX_OUTPUT: raise StockError("query_invalid","Слишком большой запрос",4)
                break
        return result

class _SpoolCleanupFailure(Exception):
    """A safe marker that one or more cleanup stages failed."""


def _remove_spool_directory(path: Path) -> bool:
    for _ in range(_SPOOL_CLEANUP_ATTEMPTS):
        try:
            if not path.exists():
                return True
            shutil.rmtree(path)
        except (OSError, ValueError):
            continue
        try:
            if not path.exists():
                return True
        except OSError:
            continue
    return False


def _register_spool_cleanup(path: Path, temporary: object) -> None:
    with _PENDING_SPOOL_CLEANUPS_LOCK:
        _PENDING_SPOOL_CLEANUPS[path] = temporary


def _retry_pending_spool_cleanups() -> None:
    with _PENDING_SPOOL_CLEANUPS_LOCK:
        pending = tuple(_PENDING_SPOOL_CLEANUPS.items())
    for path, temporary in pending:
        if _remove_spool_directory(path):
            try:
                temporary.cleanup()
            except (OSError, ValueError, AttributeError):
                pass
            with _PENDING_SPOOL_CLEANUPS_LOCK:
                _PENDING_SPOOL_CLEANUPS.pop(path, None)
        else:
            warnings.warn(
                "Не удалось удалить временное хранилище поиска",
                ResourceWarning,
                stacklevel=1,
            )


atexit.register(_retry_pending_spool_cleanups)


class _Spool:
    def __init__(self) -> None:
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.db: sqlite3.Connection | None = None
        self.records = 0
        try:
            self.temp = tempfile.TemporaryDirectory(prefix="papa-shin-search-")
            self.db = sqlite3.connect(str(Path(self.temp.name) / "query.sqlite3"))
            self._configure_limits()
            self.db.create_collation("decimal", _decimal_compare)
            self.db.executescript(
                "CREATE TABLE c("
                "id TEXT PRIMARY KEY,n TEXT,a TEXT,t TEXT,ch TEXT,q INTEGER,u TEXT"
                ");"
                "CREATE TABLE o(id TEXT,s TEXT,p TEXT,d INTEGER,q INTEGER);"
            )
        except (
            sqlite3.Error,
            OSError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            self._cleanup()
            raise _spool_unavailable() from error

    def __enter__(self) -> "_Spool":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        cleanup_error = self._cleanup()
        if exc is None and cleanup_error is not None:
            raise _spool_unavailable() from cleanup_error
        return False

    def _configure_limits(self) -> None:
        connection = self._connection()
        if MAX_SPOOL_BYTES < _SQLITE_PAGE_BYTES:
            raise ValueError("invalid spool byte budget")
        connection.execute(f"PRAGMA page_size={_SQLITE_PAGE_BYTES}")
        page_row = connection.execute("PRAGMA page_size").fetchone()
        if page_row is None or not isinstance(page_row[0], int) or page_row[0] <= 0:
            raise ValueError("invalid SQLite page size")
        max_pages = MAX_SPOOL_BYTES // page_row[0]
        max_page_row = connection.execute(
            f"PRAGMA max_page_count={max_pages}"
        ).fetchone()
        if max_page_row is None or max_page_row[0] > max_pages:
            raise ValueError("SQLite page limit was not applied")
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute(f"PRAGMA cache_size=-{_SQLITE_CACHE_KIB}")
        connection.execute("PRAGMA temp_store=FILE")

    def _cleanup(self) -> BaseException | None:
        failed = False
        if self.db is not None:
            try:
                self.db.close()
            except (
                sqlite3.Error,
                OSError,
                ValueError,
                TypeError,
                OverflowError,
                RecursionError,
            ):
                failed = True
            self.db = None
        if self.temp is not None:
            temporary = self.temp
            name = Path(temporary.name)
            cleanup_failed = False
            try:
                temporary.cleanup()
            except (OSError, ValueError):
                failed = True
                cleanup_failed = True
            removed = _remove_spool_directory(name)
            if not removed:
                _register_spool_cleanup(name, temporary)
                failed = True
            elif cleanup_failed:
                try:
                    temporary.cleanup()
                except (OSError, ValueError):
                    pass
            self.temp = None
        return _SpoolCleanupFailure("spool cleanup failed") if failed else None

    def _connection(self) -> sqlite3.Connection:
        if self.db is None:
            raise _spool_unavailable()
        return self.db

    def _assert_record_budget(self) -> None:
        if self.records > MAX_SPOOL_RECORDS:
            raise _spool_unavailable()

    def add_product(self, p: Product) -> None:
        try:
            self._connection().execute(
                "INSERT INTO c VALUES(?,?,?,?,?,?,?)",
                (
                    p.product_id,
                    p.name,
                    p.article,
                    p.product_type,
                    json.dumps(p.characteristics),
                    p.total_quantity,
                    json.dumps(p.unknown_characteristics),
                ),
            )
            self.records += 1
            self._assert_record_budget()
        except sqlite3.IntegrityError as error:
            raise StockError(
                "manifest_invalid", "Некорректные данные товаров", 3
            ) from error
        except OverflowError as error:
            raise StockError(
                "manifest_invalid", "Некорректные данные товаров", 3
            ) from error
        except StockError:
            raise
        except (
            sqlite3.Error,
            OSError,
            ValueError,
            TypeError,
            RecursionError,
        ) as error:
            raise _spool_unavailable() from error

    def has(self, ident: str) -> bool:
        try:
            return (
                self._connection()
                .execute("SELECT 1 FROM c WHERE id=?", (ident,))
                .fetchone()
                is not None
            )
        except (
            sqlite3.Error,
            OSError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            raise _spool_unavailable() from error

    def add_offer(self, ident: str, offer: Offer, limit: int) -> None:
        try:
            self._connection().execute(
                "INSERT INTO o VALUES(?,?,?,?,?)",
                (
                    ident,
                    offer.supplier,
                    _price_text(offer.price),
                    offer.delivery_days,
                    offer.quantity,
                ),
            )
            deleted = self._connection().execute(
                "DELETE FROM o WHERE id=? AND rowid NOT IN ("
                "SELECT rowid FROM o WHERE id=? "
                "ORDER BY p COLLATE decimal,d,q DESC LIMIT ?)",
                (ident, ident, limit),
            ).rowcount
            self.records += 1 - max(deleted, 0)
            self._assert_record_budget()
        except OverflowError as error:
            raise StockError(
                "manifest_invalid", "Некорректные данные предложений", 3
            ) from error
        except StockError:
            raise
        except (
            sqlite3.Error,
            OSError,
            ValueError,
            TypeError,
            RecursionError,
        ) as error:
            raise _spool_unavailable() from error
    def result(self, query: SearchQuery) -> tuple[SearchSummary,tuple[Product,...],dict[str,tuple[Offer,...]]]:
        try:
            required=any(x is not None for x in (query.supplier,query.max_price,query.max_delivery_days)); where="WHERE EXISTS(SELECT 1 FROM o WHERE o.id=c.id)" if required else ""
            count,total=self._connection().execute(f"SELECT COUNT(*),COALESCE(SUM(q),0) FROM c {where}").fetchone()
            rows=self._connection().execute(f"SELECT id,n,a,t,ch,q,u FROM c {where} ORDER BY CASE WHEN EXISTS(SELECT 1 FROM o WHERE o.id=c.id) THEN 0 ELSE 1 END,(SELECT p FROM o WHERE o.id=c.id ORDER BY p COLLATE decimal,d,q DESC LIMIT 1) COLLATE decimal,q DESC,id LIMIT ?",(query.limit,))
            products=[]; offers={}
            for row in rows:
                product=Product(row[0],row[1],row[2],row[3],json.loads(row[4]),row[5],tuple(json.loads(row[6]))); products.append(product)
                offers[product.product_id]=tuple(Offer(x[0],Decimal(x[1]),x[2],x[3]) for x in self._connection().execute("SELECT s,p,d,q FROM o WHERE id=? ORDER BY p COLLATE decimal,d,q DESC",(product.product_id,)))
            return SearchSummary(count,total),tuple(products),offers
        except StockError: raise
        except (sqlite3.Error,OSError,ValueError,TypeError,OverflowError,RecursionError,InvalidOperation) as error: raise _spool_unavailable() from error

class StockSearcher:
    def __init__(self, files: GenerationFiles, config: StockConfig) -> None: self.files=files; self.config=config
    def search(self, query: SearchQuery) -> SearchResult:
        with _Spool() as spool:
            generation,warnings=_generation(self.files)
            for row in _rows(self.files.products, MAX_PRODUCT_ROWS):
                assert_generation(row,self.files.generation_id); product=_product(row,self.config.resolve_product_id(row))
                if _match_product(product,row,query): spool.add_product(product)
            for row in _rows(self.files.offers, MAX_OFFER_ROWS):
                assert_generation(row,self.files.generation_id); ident=_offer_id(row,self.config.offer_product_id_field)
                if spool.has(ident):
                    offer=_offer(row)
                    if _match_offer(offer,query): spool.add_offer(ident,offer,query.offers_limit)
            summary,products,offers=spool.result(query); return SearchResult(generation,query.public_filters(),summary,products,offers,warnings)

def _rows(path: Path, maximum_rows: int):
    try:
        with path.open("rb") as stream:
            row_count = 0
            while True:
                raw = stream.readline(MAX_JSONL_LINE_BYTES + 2)
                if not raw:
                    break
                row_count += 1
                if row_count > maximum_rows:
                    raise StockError(
                        "manifest_invalid", "Превышен лимит машинных данных", 3
                    )
                payload = raw[:-1] if raw.endswith(b"\n") else raw
                if payload.endswith(b"\r"):
                    payload = payload[:-1]
                if len(payload) > MAX_JSONL_LINE_BYTES:
                    raise StockError(
                        "manifest_invalid", "Превышен лимит машинных данных", 3
                    )
                if payload.strip():
                    try:
                        value = _parse(payload.decode("utf-8"))
                    except (
                        ValueError,
                        TypeError,
                        OverflowError,
                        RecursionError,
                        UnicodeError,
                        json.JSONDecodeError,
                    ) as error:
                        raise StockError(
                            "manifest_invalid", "Некорректные машинные данные", 3
                        ) from error
                    if not isinstance(value, dict):
                        raise StockError(
                            "manifest_invalid", "Некорректные машинные данные", 3
                        )
                    yield value
    except StockError: raise
    except OSError as error: raise StockError("cache_unavailable","Проверенный кэш недоступен",7) from error

def _generation(files: GenerationFiles) -> tuple[dict[str,object],tuple[dict[str,str],...]]:
    try: manifest=_parse(files.manifest.read_text(encoding="utf-8"))
    except (OSError,UnicodeError,ValueError,TypeError,OverflowError,RecursionError) as error: raise StockError("cache_unavailable","Проверенный кэш недоступен",7) from error
    generated_at=manifest.get("generated_at") if isinstance(manifest,dict) else None
    if not isinstance(manifest,dict) or manifest.get("generation_id")!=files.generation_id or not isinstance(generated_at,str) or not generated_at or len(generated_at)>_MAX_TEXT: raise StockError("generation_mismatch","Поколение данных не согласовано",5)
    path=files.runtime_status or files.manifest.parent/"state.json"
    if path.exists() or files.runtime_status is not None:
        state=load_runtime_status(path,files.generation_id)
        checked_at=state.checked_at; stale=state.stale; code=state.warning_code
    else:
        checked_at=generated_at; stale=False; code=None
    return {"id":files.generation_id,"generated_at":generated_at,"checked_at":checked_at,"stale":stale}, (({"code":code,"message":"Используется предыдущее поколение"},) if stale and code is not None else ())

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
    if type(value) is int:
        result = value
    elif isinstance(value, str) and _CANONICAL_NONNEGATIVE_INTEGER.fullmatch(value):
        try:
            result = int(value)
        except (ValueError, OverflowError) as error:
            raise StockError(
                "manifest_invalid", "Некорректные машинные данные", 3
            ) from error
    else:
        raise StockError("manifest_invalid","Некорректные машинные данные",3)
    if result<0:raise StockError("manifest_invalid","Некорректные машинные данные",3)
    return result
def _decimal(value:object)->Decimal:
    try: result=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as error:raise StockError("manifest_invalid","Некорректные машинные данные",3) from error
    if not result.is_finite() or result<0 or abs(result.adjusted())>128:raise StockError("manifest_invalid","Некорректные машинные данные",3)
    return result
def _price_text(value:Decimal)->str:return format(value,"f")
def _decimal_compare(a:str,b:str)->int:return (_decimal(a)>_decimal(b))-(_decimal(a)<_decimal(b))
def _public_size(value:object)->int:return len(json.dumps(value,ensure_ascii=False,separators=(",",":")).encode("utf-8"))
def _spool_unavailable()->StockError:return StockError("cache_unavailable","Временное хранилище поиска недоступно",7)
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
