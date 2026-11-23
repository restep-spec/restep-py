"""La parte del cliente que no hace E/S: descubrimiento, validación local, URLs, cabeceras y
lectura de respuestas. El cliente síncrono (urllib) y el asíncrono (httpx) la comparten y solo
ponen el transporte."""
from __future__ import annotations

import datetime as _dt
import json
import re
import uuid
import warnings
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence, Union
from urllib.parse import quote, urlencode, urljoin, urlsplit

from . import errors as E

PROFILE = "https://w3id.org/restep/1/"  # identificador: enlace de perfil y tipos de error
Item = dict[str, Any]
JSON_TYPES = "application/json"


class _Auto:
    """Valor por defecto de ``idempotency_key``: una clave nueva por llamada si el servidor anuncia
    ``writes`` (§7.1); la misma en todos los reintentos de esa llamada."""

    _instance: "_Auto | None" = None

    def __new__(cls) -> "_Auto":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "AUTO"


AUTO = _Auto()
IdempotencyKey = Union[str, None, _Auto]
FilterValue = Any
Filter = Mapping[str, Any]


class Total(int):
    """El ``total`` de una colección (§6.5); ``estimated`` dice si es aproximado."""

    estimated: bool

    def __new__(cls, value: int, estimated: bool = False) -> "Total":
        obj = super().__new__(cls, value)
        obj.estimated = estimated
        return obj

    def __repr__(self) -> str:
        return f"Total({int(self)}, estimated={self.estimated})"


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None = None
    retry_safe: bool = False  # se puede repetir ante un 503 sin riesgo de duplicar el efecto


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # nombres en minúsculas; varias cabeceras iguales, unidas con ", "
    body: bytes = b""
    url: str = ""

    @property
    def is_json(self) -> bool:
        return "json" in self.headers.get("content-type", "")


def merge_headers(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in pairs:
        k = k.lower()
        out[k] = f"{out[k]}, {v}" if k in out else v
    return out


# ── Valores de filtro (§4.2, §6.1) ───────────────────────────────────────────
_INTEGER = re.compile(r"-?[0-9]+")
_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})")
_DURATION = re.compile(r"-?P(?=[0-9T])([0-9]+Y)?([0-9]+M)?([0-9]+W)?([0-9]+D)?(T(?=[0-9])([0-9]+H)?([0-9]+M)?([0-9]+(\.[0-9]+)?S)?)?")
_ID = re.compile(r"[A-Za-z0-9._~-]+")
_ACTION = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")  # la gramática de los nombres (§3)
OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "null")
FIELD_TYPES = frozenset({"string", "integer", "number", "decimal", "boolean", "date", "datetime", "duration", "enum"})

# ── Versiones (§1.1) y partes experimentales (§1.2) ──────────────────────────
_RESTEP_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-((?:draft|rc)\.(?:0|[1-9][0-9]*)))?")
# Las versiones cuyo texto implementa este cliente: el borrador 6 y la 1.0 que salga de él. Con
# otra versión, las partes experimentales no se usan (§1.2).
IMPLEMENTED_VERSIONS = frozenset({"1.0-draft.6"})  # §1.2: el texto de la 1.0 aún no existe
EXPERIMENTAL = frozenset({"sync", "batch", "filter-expr", "query-method", "inherit"})
# Dónde puede ir cada kind de aviso de retirada (§5.5); uno desconocido no se usa (§4.2).
DEPRECATION_KINDS = {
    "root": frozenset({"version", "profile", "extension", "collection", "singleton", "reason"}),
    "collection": frozenset({"field", "filter", "operator", "value", "sort", "relation", "include", "count",
                             "max_limit", "action", "method", "item_method"}),
    "singleton": frozenset({"field", "method"}),
}


def parse_version(value: Any) -> tuple[int, int, str | None] | None:
    """``(mayor, menor, prelanzamiento)`` de un valor de ``restep`` que sigue la gramática de §1.1
    (``"1.0"``, ``"1.0-draft.6"``, ``"1.0-rc.1"``), o ``None`` si no la sigue."""
    m = _RESTEP_VERSION.fullmatch(value) if isinstance(value, str) else None
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else None


def iso_duration(td: _dt.timedelta) -> str:
    total = td.total_seconds()
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    out = f"{sign}P{int(days)}D" if days else f"{sign}P"
    time = ""
    if hours:
        time += f"{int(hours)}H"
    if minutes:
        time += f"{int(minutes)}M"
    if seconds or (not days and not time):
        time += f"{seconds:g}S"
    return out + (f"T{time}" if time else "")


def _decimal_text(d: Decimal) -> str:
    return format(d, "f")


def format_filter_value(value: Any, decl: Mapping[str, Any], parameter: str) -> str:
    """El texto de un operando de filtro, comprobado contra el tipo declarado (§6.1)."""
    kind = decl.get("type", "string")
    if kind not in FIELD_TYPES:
        # §4.2: un type desconocido no se usa; quien llama ya ha tratado el filtro como no declarado.
        raise E.local_error(E.UnsupportedParameter, f"{parameter}: tipo desconocido {kind!r} (§4.2).", parameter=parameter)

    def bad(why: str) -> E.RestepError:
        return E.local_error(E.InvalidParameter, f"{parameter}: {why}", parameter=parameter)

    if isinstance(value, bool):
        if kind != "boolean":
            raise bad(f"un booleano no vale para un campo de tipo {kind}.")
        return "true" if value else "false"
    if kind == "boolean":
        if value in ("true", "false"):
            return str(value)
        raise bad("se espera true o false.")
    if isinstance(value, _dt.datetime):
        if kind != "datetime":
            raise bad(f"un datetime no vale para un campo de tipo {kind}.")
        if value.tzinfo is None:
            raise bad("el instante necesita zona horaria (RFC 3339 con desfase).")
        return value.isoformat()
    if isinstance(value, _dt.date):
        if kind != "date":
            raise bad(f"una fecha no vale para un campo de tipo {kind}.")
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        if kind != "duration":
            raise bad(f"una duración no vale para un campo de tipo {kind}.")
        return iso_duration(value)
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise bad("el número no es finito.")
        if kind == "integer":
            if isinstance(value, int) or (isinstance(value, Decimal) and value == value.to_integral_value()):
                return str(int(value))
            raise bad("se espera un entero.")
        if kind in ("number", "decimal"):
            return _decimal_text(value) if isinstance(value, Decimal) else repr(value) if isinstance(value, float) else str(value)
        if kind == "string" and isinstance(value, int):
            return str(value)
        raise bad(f"un número no vale para un campo de tipo {kind}.")
    if not isinstance(value, str):
        raise bad(f"tipo de Python no admitido: {type(value).__name__}.")
    text = value
    if kind == "integer" and not _INTEGER.fullmatch(text):
        raise bad(f"«{text}» no es un entero.")
    if kind == "number" and not _NUMBER.fullmatch(text):
        raise bad(f"«{text}» no es un número.")
    if kind == "decimal" and not _DECIMAL.fullmatch(text):
        raise bad(f"«{text}» no es un decimal (como 18.50).")
    if kind == "date":
        try:
            if not _DATE.fullmatch(text):
                raise ValueError
            _dt.date.fromisoformat(text)
        except ValueError:
            raise bad(f"«{text}» no es una fecha AAAA-MM-DD.") from None
    if kind == "datetime" and not _DATETIME.fullmatch(text):
        raise bad(f"«{text}» no es un instante RFC 3339 con desfase.")
    if kind == "duration":
        m = _DURATION.fullmatch(text)
        if not m:
            raise bad(f"«{text}» no es una duración ISO 8601.")
        if m.group(1) or m.group(2):
            raise bad("una duración con años o meses no tiene longitud fija (§6.1).")
    if kind == "enum":
        values = decl.get("values") or []
        if values and text not in values:
            raise bad(f"«{text}» no está entre los valores: {', '.join(values)}.")
    return text


# ── El descubrimiento y la API ───────────────────────────────────────────────
def is_discovery(doc: Any) -> bool:
    """§4.2: un documento de descubrimiento es un objeto JSON con ``restep`` de tipo cadena."""
    return isinstance(doc, dict) and isinstance(doc.get("restep"), str)


def links(header: str | None) -> list[tuple[str, dict[str, str]]]:
    """Los enlaces de una cabecera ``Link`` (RFC 8288), sin pretensiones."""
    out: list[tuple[str, dict[str, str]]] = []
    for m in re.finditer(r"<([^>]*)>((?:\s*;\s*[^;,=]+(?:=(?:\"[^\"]*\"|[^;,]*))?)*)", header or ""):
        params = {k.strip().lower(): v.strip('"') for k, v in re.findall(r";\s*([^;,=\s]+)\s*=\s*(\"[^\"]*\"|[^;,]*)", m.group(2))}
        out.append((m.group(1), params))
    return out


def sf_string(key: str) -> str:
    """Una cadena de Structured Fields (RFC 9651), como pide ``Idempotency-Key`` (§7.1)."""
    if any(not (0x20 <= ord(c) <= 0x7E) for c in key):
        raise ValueError("La Idempotency-Key solo puede llevar ASCII imprimible (RFC 9651).")
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return _decimal_text(o)  # §3: los importes van como cadena
    if isinstance(o, _dt.datetime):
        if o.tzinfo is None:
            raise ValueError("Un datetime sin zona horaria no es un instante RFC 3339 (§3).")
        return o.isoformat()
    if isinstance(o, _dt.date):
        return o.isoformat()
    if isinstance(o, _dt.timedelta):
        return iso_duration(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"No sé convertir a JSON un {type(o).__name__}.")


def dump_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=_json_default, separators=(",", ":")).encode("utf-8")


def etag_value(if_match: Any) -> str:
    """``if_match`` puede ser la ETag (con o sin comillas) o un elemento leído con su ``_etag``."""
    if isinstance(if_match, Mapping):
        tag = if_match.get("_etag")
        if not isinstance(tag, str):
            raise ValueError("El elemento no lleva _etag: léelo con get() o pasa la ETag.")
        return tag
    if not isinstance(if_match, str) or not if_match:
        raise TypeError("if_match tiene que ser una ETag o un elemento con _etag.")
    if if_match == "*" or if_match.startswith(('"', 'W/"')) or "," in if_match:
        return if_match
    return f'"{if_match}"'


def _problem_code(resp: Response) -> str | None:
    try:
        code = json.loads(resp.body.decode("utf-8")).get("code")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return code if isinstance(code, str) else None


class ApiCore:
    """Estado común de una conexión: raíz, credenciales, idioma, descubrimiento y opciones."""

    def __init__(self, url: str, *, token: str | None = None, language: str | None = None,
                 headers: Mapping[str, str] | None = None, validate: bool = True, retries: int = 0,
                 max_retry_wait: float = 60.0, timeout: float = 30.0) -> None:
        if not url.endswith("/"):
            url += "/"  # la raíz de la API conserva su barra final (§11.5), y así urljoin no pierde el último segmento
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"La URL de la API tiene que ser http o https absoluta: {url}")
        self.root = url
        self.origin = f"{parts.scheme}://{parts.netloc}".lower()
        self.token = token
        self.language = language
        self.extra_headers = dict(headers or {})
        self.validate = validate
        self.retries = retries
        self.max_retry_wait = max_retry_wait
        self.timeout = timeout
        self.discovery: dict[str, Any] | None = None
        self.version: tuple[int, int, str | None] | None = None

    # Descubrimiento (§4.2)
    @property
    def profiles(self) -> frozenset[str]:
        d = self.discovery
        return frozenset(d.get("profiles") or ["core"]) if d else frozenset({"core"})

    @property
    def extensions(self) -> frozenset[str]:
        d = self.discovery
        return frozenset(d.get("extensions") or []) if d else frozenset()

    @property
    def declarations(self) -> dict[str, dict[str, Any]]:
        d = self.discovery
        cols = d.get("collections") if d else None
        return cols if isinstance(cols, dict) else {}

    @property
    def singleton_declarations(self) -> dict[str, dict[str, Any]]:
        """Los singletons del descubrimiento (§5.9), sin los que tienen una plantilla en ``path`` o
        llevan ``parent``, que un cliente DEBE ignorar (formas reservadas a versiones menores)."""
        d = self.discovery
        found = d.get("singletons") if d else None
        if not isinstance(found, dict):
            return {}
        return {n: s for n, s in found.items() if isinstance(s, dict) and isinstance(s.get("path"), str)
                and "{" not in s["path"] and "parent" not in s}

    def set_discovery(self, doc: Any, url: str | None = None) -> None:
        if not is_discovery(doc):
            self.discovery, self.version = None, None
            return
        version = parse_version(doc["restep"])
        if version is None or version[0] != 1:
            # §1.1: ni descubrimiento de Restep 1 ni «solo core»: esa API no se puede usar como Restep 1.
            raise E.UnsupportedVersion(doc["restep"], url)
        self.discovery, self.version = doc, version

    @property
    def restep_version(self) -> str | None:
        return self.discovery["restep"] if self.discovery else None

    @property
    def pre_release(self) -> str | None:
        """``"draft.6"`` o ``"rc.1"`` si el servidor declara un borrador o un candidato: entonces no
        hay que suponerle las garantías de ninguna versión publicada ni de otro borrador (§1.1)."""
        return self.version[2] if self.version else None

    def usable(self, part: str) -> bool:
        """Si se puede usar un perfil o una extensión: anunciado y, si es experimental (§1.2),
        bajo una versión declarada cuyo texto implementa este cliente (para un borrador, ese mismo)."""
        if part not in self.profiles and part not in self.extensions:
            return False
        return part not in EXPERIMENTAL or self.restep_version in IMPLEMENTED_VERSIONS

    @property
    def has_credentials(self) -> bool:
        return bool(self.token) or any(k.lower() in ("authorization", "cookie") for k in self.extra_headers)

    @property
    def anonymous_keys(self) -> bool:
        """§7.1: ``"anonymous": false`` en ``idempotency`` dice que el servidor rechaza
        ``Idempotency-Key`` en peticiones sin credenciales. Falta, o cualquier otro valor, es ``true``."""
        idem = (self.discovery or {}).get("idempotency")
        return not (isinstance(idem, dict) and idem.get("anonymous") is False)

    def deprecations(self) -> list[dict[str, Any]]:
        """Los avisos de retirada del descubrimiento (§5.5), con ``where`` (``""`` para la raíz, el
        nombre de la colección o ``singleton:nombre``). Los de un ``kind`` desconocido en su sitio no
        se usan (§4.2) y no salen."""
        out: list[dict[str, Any]] = []

        def take(notices: Any, place: str, where: str) -> None:
            for n in notices if isinstance(notices, list) else []:
                if isinstance(n, dict) and n.get("kind") in DEPRECATION_KINDS[place]:
                    out.append({**n, "where": where})
        d = self.discovery or {}
        take(d.get("deprecations"), "root", "")
        for name, c in self.declarations.items():
            if isinstance(c, dict):
                take(c.get("deprecations"), "collection", name)
        for name, sg in self.singleton_declarations.items():
            take(sg.get("deprecations"), "singleton", f"singleton:{name}")
        return out

    def service_desc(self, resp: Response) -> list[str]:
        """Destinos ``service-desc`` de la respuesta de la raíz, los de tipo JSON primero (§4.2)."""
        found = [(urljoin(resp.url or self.root, target), p) for target, p in links(resp.headers.get("link"))
                 if "service-desc" in p.get("rel", "").split()]
        return [t for t, p in found if "json" in p.get("type", "")] + [t for t, p in found if "json" not in p.get("type", "")]

    # Peticiones
    def url(self, path_or_url: str) -> str:
        """Una ruta del descubrimiento es relativa a la raíz de la API; un enlace de un cuerpo
        (``next``, ``href``), absoluto o ruta absoluta que ya incluye la de la raíz (§3)."""
        return urljoin(self.root, path_or_url)

    def api_path(self, path: str) -> str:
        return urljoin(self.root, path.lstrip("/"))

    def same_origin(self, url: str) -> bool:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}".lower() == self.origin

    def build(self, method: str, url: str, *, query: Sequence[tuple[str, str]] | None = None, body: Any = None,
              has_body: bool = False, content_type: str = JSON_TYPES, headers: Mapping[str, str] | None = None,
              retry_safe: bool | None = None) -> Request:
        if query:
            url += ("&" if "?" in url else "?") + urlencode(list(query), quote_via=quote, safe=",")
        # §3: no se sigue un enlace con un esquema que no sea https ni el de la raíz (file:, data:…).
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("https", urlsplit(self.root).scheme.lower()):
            raise ValueError(f"No se sigue un enlace con el esquema {scheme or '(ninguno)'}: solo https o el de la raíz (§3).")
        h = {"accept": "application/json, application/problem+json"}
        if self.language:
            h["accept-language"] = self.language
        # §4.3: las credenciales de la API (el token y las cabeceras propias, que pueden llevarlas)
        # solo van a su origen.
        if self.same_origin(url):
            h.update({k.lower(): v for k, v in self.extra_headers.items()})
            if self.token:
                h["authorization"] = f"Bearer {self.token}"
        data = None
        if has_body or body is not None:
            data = dump_json(body)
            h["content-type"] = content_type
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        if retry_safe is None:
            retry_safe = method in ("GET", "HEAD", "PATCH", "DELETE")
        return Request(method, url, h, data, retry_safe)

    def retry_delay(self, req: Request, resp: Response, attempt: int) -> float | None:
        """Cuánto esperar antes de repetir, o ``None`` si no se repite. Un 429 no se atendió y
        siempre se puede repetir; un 409 ``request_in_progress``, también (es la misma petición
        con la misma clave, §7.1); un 503, solo si repetir no puede duplicar el efecto."""
        if attempt >= self.retries or resp.status not in (409, 429, 503):
            return None
        if resp.status == 409 and _problem_code(resp) != "request_in_progress":
            return None
        if resp.status == 503 and not req.retry_safe:
            return None
        wait = E.parse_retry_after(resp.headers.get("retry-after"))
        if wait is None:
            wait = min(0.5 * 2 ** attempt, self.max_retry_wait)
        return wait if wait <= self.max_retry_wait else None

    def parse(self, req: Request, resp: Response) -> Any:
        """El cuerpo de una respuesta correcta, o la excepción tipada de una de error (§5.4)."""
        data: Any = None
        if resp.body:
            if resp.is_json:
                try:
                    data = json.loads(resp.body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = None if resp.status < 400 else {"detail": resp.body[:500].decode("utf-8", "replace")}
            else:
                data = resp.body.decode("utf-8", "replace")
        if resp.status >= 400:
            problem = data if isinstance(data, dict) else {"detail": str(data)[:500] if data else f"HTTP {resp.status}"}
            raise E.error_from_problem(resp.status, problem, headers=resp.headers, url=req.url, method=req.method)
        if "deprecation" in resp.headers:
            sunset = resp.headers.get("sunset")
            more = f"; se retira el {sunset}" if sunset else ""
            warnings.warn(f"{req.method} {req.url} usa algo que está anunciado como retirado (§5.5){more}.",
                          E.RestepDeprecationWarning, stacklevel=5)
        return data


# ── Colecciones ───────────────────────────────────────────────────────────────
_VAR = re.compile(r"\{([^}]+)\}")


class CollectionCore:
    """Lo que una colección sabe hacer sin E/S: comprobar parámetros y construir peticiones."""

    def __init__(self, api: ApiCore, name: str, decl: Mapping[str, Any] | None, path_vars: Mapping[str, str] | None = None) -> None:
        self._api = api
        self.name = name
        self.declaration: dict[str, Any] = dict(decl or {"path": f"/{name}"})
        self._vars = dict(path_vars or {})

    # Plantilla de ruta (RFC 6570, nivel 1) de las subcolecciones
    @property
    def path_template(self) -> str:
        return str(self.declaration.get("path") or f"/{self.name}")

    @property
    def variables(self) -> list[str]:
        return _VAR.findall(self.path_template)

    @property
    def parent(self) -> str | None:
        """La colección de la que cuelgan los elementos de esta subcolección (§4.2)."""
        p = self.declaration.get("parent")
        return p if isinstance(p, str) else None

    @property
    def id_from(self) -> str:
        """Quién elige el ``id`` (§5.1): ``"server"``, ``"client"``, ``"either"`` o el valor
        desconocido tal cual (con el que no se manda ``id``)."""
        v = self.declaration.get("id_from", "server")
        return v if isinstance(v, str) else repr(v)

    def collation(self, field: str) -> str:
        """Cómo compara y ordena el servidor un campo de cadena (§6.1): ``"codepoint"`` si no está
        en ``collations``; ``"server"`` si lo dice, o si el valor no es una cadena o no se conoce.
        Con ``"server"`` no hay que reproducir ese orden ni esa igualdad en local."""
        c = self.declaration.get("collations")
        if not isinstance(c, dict) or field not in c:
            return "codepoint"
        return "codepoint" if c[field] == "codepoint" else "server"

    def ref(self, id: Any) -> dict[str, str]:
        """El objeto de relación para enlazar un elemento de esta colección (§3, §5.1). En una
        subcolección lleva ``href``, que es obligatorio para enlazarla: su ``id`` solo identifica al
        elemento bajo su padre."""
        out = {"id": str(id)}
        if self.variables:
            out["href"] = urlsplit(self._item_url(id)).path
        return out

    def inherited(self, item: Mapping[str, Any], field: str) -> Any:
        """De dónde sale el valor de un campo efectivo (§11.6): ``"own"`` (su valor propio),
        ``{"href": ...}`` (la fuente real), ``"default"``, ``"hidden"`` o ``None`` si el elemento no
        trae ``_inherited`` y no se sabe. Un ``from`` desconocido es ``"hidden"``. ``inherit`` es
        experimental: solo se usa bajo una versión cuyo texto implementa este cliente (§1.2)."""
        if not self._api.usable("inherit"):
            raise E.ExperimentalNotUsable(
                f"inherit no se usa: el servidor no lo anuncia o declara restep={self._api.restep_version!r}, "
                f"y este cliente implementa {', '.join(sorted(IMPLEMENTED_VERSIONS))} (§1.2).")
        decl = self.declaration.get("inherit")
        if not isinstance(decl, dict) or field not in decl:
            raise KeyError(f"{self.name} no declara «{field}» en inherit (§11.6).")
        info = item.get("_inherited")
        if not isinstance(info, Mapping):
            return None
        if field not in info:
            return "own"
        entry = info[field]
        frm = entry.get("from") if isinstance(entry, Mapping) else None
        if frm in ("default", "hidden"):
            return frm
        if isinstance(frm, Mapping) and isinstance(frm.get("href"), str):
            return {"href": frm["href"]}
        return "hidden"

    def _path(self) -> str:
        def fill(m: re.Match[str]) -> str:
            v = m.group(1)
            if v not in self._vars:
                raise TypeError(f"Falta la variable de ruta «{v}» de {self.path_template}: usa .at({v}=...).")
            return quote(str(self._vars[v]), safe="")
        return _VAR.sub(fill, self.path_template).rstrip("/")

    def _url(self) -> str:
        return self._api.api_path(self._path())

    def _item_url(self, id: Any) -> str:
        id = str(id)
        if self._api.validate and (not _ID.fullmatch(id) or id in (".", "..")):
            raise ValueError(f"«{id}» no es un id de Restep (§3: no vacío, solo A-Z a-z 0-9 - . _ ~, y ni . ni ..).")
        return f"{self._url()}/{quote(id, safe='')}"

    # Declaraciones
    def _has(self, profile: str) -> bool:
        return profile in self._api.profiles

    def _check_method(self, method: str, item: bool) -> None:
        if not self._api.validate:
            return
        key = "item_methods" if item else "methods"
        declared = self.declaration.get(key)
        if isinstance(declared, list) and method not in declared:
            where = "sus elementos" if item else "la colección"
            raise E.local_error(E.MethodNotAllowed, f"{self.name}: {where} no admite {method}; admite {', '.join(declared) or 'nada'} (§4.3).",
                                status=405)

    def _unsupported(self, parameter: str, why: str) -> E.RestepError:
        return E.local_error(E.UnsupportedParameter, f"{self.name}: {why}", parameter=parameter)

    def _invalid(self, parameter: str, why: str) -> E.RestepError:
        return E.local_error(E.InvalidParameter, f"{self.name}: {why}", parameter=parameter)

    def _need_query(self, parameter: str) -> None:
        if not self._has("query"):
            raise self._unsupported(parameter, f"el servidor no anuncia el perfil query, así que no admite {parameter} (§5.3).")

    # Parámetros de lectura (§6, §9)
    def read_query(self, *, filter: Filter | None = None, named: Mapping[str, Any] | None = None, sort: str | Sequence[str] | None = None,
                   fields: str | Sequence[str] | None = None, include: str | Sequence[str] | None = None,
                   limit: int | None = None, count: bool = False, item: bool = False) -> list[tuple[str, str]]:
        q: list[tuple[str, str]] = []
        check = self._api.validate
        if filter:
            q += self._filter(filter) if check else self._filter_raw(filter)
        if named:
            q += self._named(named, check)
        if sort is not None:
            keys = _split(sort)
            if check:
                self._need_query("sort")
                if not keys:
                    raise self._invalid("sort", "sort no puede ir vacío.")
                declared = self.declaration.get("sort") or []
                for k in keys:
                    if k.lstrip("-") not in declared:
                        offered = ", ".join(declared) or "ninguno"
                        raise self._unsupported("sort", f"no se puede ordenar por «{k.lstrip('-')}»; campos de orden: {offered} (§6.2).")
            q.append(("sort", ",".join(keys)))
        inc = _split(include) if include is not None else []
        if include is not None:
            if check:
                self._check_include(inc)
            q.append(("include", ",".join(inc)))
        if fields is not None:
            fl = _split(fields)
            if check:
                self._check_fields(fl, inc)
            q.append(("fields", ",".join(fl)))
        if limit is not None:
            if check:
                self._need_query("limit")
                top = self.declaration.get("max_limit")
                if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or (isinstance(top, int) and limit > top):
                    raise self._invalid("limit", f"limit tiene que ser un entero entre 1 y {top if isinstance(top, int) else 'el máximo del servidor'} (§6.4).")
            q.append(("limit", str(limit)))
        if count:
            if check:
                self._need_query("count")
                if self.declaration.get("count") not in ("exact", "estimated"):
                    # Sin declarar, o con un valor desconocido, que no se usa (§4.2).
                    raise self._unsupported("count", "la colección no declara count (§6.5).")
            q.append(("count", "true"))
        return q

    def _named(self, named: Mapping[str, Any], check: bool) -> list[tuple[str, str]]:
        """§6.1: filtros con nombre, sin operadores. ``{"featured": True}`` (un interruptor, sin valor)
        o ``{"search": "niebla"}`` (un valor de su tipo). ``None`` no lo aplica, y ``False`` tampoco en un interruptor."""
        q: list[tuple[str, str]] = []
        declared: Mapping[str, Any] = self.declaration.get("named_filters") or {}
        for name, value in named.items():
            if value is None:
                continue
            parameter = f"filter[{name}]"
            if not check:
                if value is not False:
                    q.append((parameter, "" if value is True else _plain(value)))
                continue
            self._need_query(parameter)
            decl = declared.get(name)
            if isinstance(decl, Mapping) and "type" in decl and decl["type"] not in FIELD_TYPES:
                decl = None  # §4.2: un type desconocido es como si no estuviera declarado
            if not isinstance(decl, Mapping):
                offered = ", ".join(declared) or "ninguno"
                raise self._unsupported(parameter, f"«{name}» no es un filtro con nombre; filtros con nombre: {offered} (§6.1).")
            if not decl.get("type"):
                if value is False:
                    continue
                if value is not True:
                    raise self._invalid(parameter, f"«{name}» es un interruptor: pasa True, sin valor (§6.1).")
                q.append((parameter, ""))
                continue
            if value == "" or (isinstance(value, bool) and decl["type"] != "boolean"):
                raise self._invalid(parameter, f"«{name}» necesita un valor {decl['type']} (§6.1).")
            q.append((parameter, format_filter_value(value, decl, parameter)))
        return q

    def _filter_raw(self, filter: Filter) -> list[tuple[str, str]]:
        q: list[tuple[str, str]] = []
        for name, cond in filter.items():
            if isinstance(cond, Mapping):
                for op, v in cond.items():
                    q.append((f"filter[{name}][{op}]", ",".join(map(_plain, v)) if op == "in" and isinstance(v, (list, tuple, set, frozenset)) else _plain(v)))
            elif isinstance(cond, (list, tuple, set, frozenset)):
                q.append((f"filter[{name}][in]", ",".join(map(_plain, cond))))
            else:
                q.append((f"filter[{name}]", _plain(cond)))
        return q

    def _filter(self, filter: Filter) -> list[tuple[str, str]]:
        """§6.1: ``{"category": "poesia"}`` (eq), ``{"price": {"le": "20"}}``,
        ``{"id": {"in": ["1", "2"]}}`` o ``{"id": ["1", "2"]}`` (in), ``{"rating": {"null": True}}``."""
        q: list[tuple[str, str]] = []
        declared: Mapping[str, Any] = self.declaration.get("filter") or {}
        for name, cond in filter.items():
            if cond is None:
                raise TypeError(f"filter[{name}] es None: para «sin valor» usa {{'{name}': {{'null': True}}}}.")
            if isinstance(cond, Mapping):
                conds = [(op, v, f"filter[{name}][{op}]") for op, v in cond.items()]
            elif isinstance(cond, (list, tuple, set, frozenset)):
                conds = [("in", cond, f"filter[{name}][in]")]
            else:
                conds = [("eq", cond, f"filter[{name}]")]
            for op, value, parameter in conds:
                self._need_query(parameter)
                decl = declared.get(name)
                if isinstance(decl, Mapping) and decl.get("type") not in FIELD_TYPES:
                    decl = None  # §4.2: un filtro con un type desconocido se trata como no declarado
                if not isinstance(decl, Mapping):
                    offered = ", ".join(n for n, d in declared.items() if isinstance(d, Mapping) and d.get("type") in FIELD_TYPES) or "ninguno"
                    raise self._unsupported(parameter, f"no se puede filtrar por «{name}»; campos de filtro: {offered} (§6.1, §4.2).")
                ops = decl.get("ops") or []
                if op not in ops:
                    raise self._unsupported(parameter, f"el filtro «{name}» no admite el operador {op}; admite {', '.join(ops)} (§6.1).")
                if op == "null":
                    if not isinstance(value, bool) and value not in ("true", "false"):
                        raise self._invalid(parameter, "el operando de null es true o false (§6.1).")
                    text = value if isinstance(value, str) else ("true" if value else "false")
                elif op == "in":
                    values = list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]
                    if not values:
                        raise self._invalid(parameter, "una lista in no puede ir vacía (§6.1).")
                    texts = [format_filter_value(v, decl, parameter) for v in values]
                    if any("," in t for t in texts):
                        raise self._invalid(parameter, "los valores de in no pueden llevar comas; usa eq (§6.1).")
                    text = ",".join(texts)
                else:
                    text = format_filter_value(value, decl, parameter)
                q.append((parameter, text))
        return q

    def _relation_target(self, decl: Mapping[str, Any], rel: str) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
        r = (decl.get("relations") or {}).get(rel)
        if not isinstance(r, Mapping):
            return None, None
        to = r.get("to")
        target = self._api.declarations.get(to) if isinstance(to, str) else None
        return r, target

    def _check_include(self, paths: list[str]) -> None:
        if not paths:
            raise self._invalid("include", "include no puede ir vacío.")
        if not self._has("relations"):
            raise self._unsupported("include", "el servidor no anuncia el perfil relations (§9).")
        depth = self.declaration.get("include_depth") or 1
        for path in paths:
            parts = path.split(".")
            if len(parts) > depth:
                raise self._unsupported("include", f"«{path}» pasa de include_depth ({depth}) (§9).")
            decl: Mapping[str, Any] | None = self.declaration
            for i, rel in enumerate(parts):
                r, target = self._relation_target(decl or {}, rel)
                if r is None or not r.get("include"):
                    raise self._unsupported("include", f"«{rel}» no es una relación que se pueda incluir (§9).")
                if i < len(parts) - 1 and target is None:
                    raise self._unsupported("include", f"no sé seguir «{path}» más allá de «{rel}».")
                decl = target

    def _check_fields(self, fields: list[str], include: list[str]) -> None:
        self._need_query("fields")
        if not fields:
            raise self._invalid("fields", "fields no puede ir vacío.")
        maps = self.declaration.get("language_maps") or {}
        rels = self.declaration.get("relations") or {}
        included = {".".join(p.split(".")[: i + 1]) for p in include for i in range(len(p.split(".")))}
        schema = (self.declaration.get("schemas") or {}).get("item")
        props = schema.get("properties") if isinstance(schema, Mapping) else None
        for f in fields:
            head, _, rest = f.partition(".")
            if rest:
                if head in maps:
                    raise self._invalid("fields", f"«{f}» entra en un mapa de idioma; pide «{head}» entero (§6.3).")
                if head in rels:
                    if f.rsplit(".", 1)[0] not in included:
                        raise self._invalid("fields", f"«{f}» es de una relación que no está en include (§9).")
                    continue
                raise self._invalid("fields", f"«{f}» no es un campo de {self.name} (§6.3).")
            if isinstance(props, Mapping) and f != "id" and f not in props and f not in maps:
                raise self._invalid("fields", f"«{f}» no es un campo de {self.name}; hay: {', '.join(props)} (§6.3).")

    # Peticiones
    def list_request(self, **params: Any) -> Request:
        self._check_method("GET", False)
        return self._api.build("GET", self._url(), query=self.read_query(**params))

    def get_request(self, id: Any, fields: Any = None, include: Any = None) -> Request:
        self._check_method("GET", True)
        url = self._item_url(id)
        return self._api.build("GET", url, query=self.read_query(fields=fields, include=include, item=True))

    def _key_header(self, key: IdempotencyKey, applies: bool) -> dict[str, str]:
        # §7.1: con "anonymous": false, una petición sin credenciales no lleva clave: la automática no
        # se pone, y una explícita daría 400 (salvo en una acción idempotente, que la ignora).
        anonymous_refused = not self._api.anonymous_keys and not self._api.has_credentials
        if key is AUTO:
            key = str(uuid.uuid4()) if applies and self._has("writes") and not anonymous_refused else None
        if isinstance(key, str) and anonymous_refused:
            if not applies:
                return {}
            if self._api.validate:
                raise E.local_error(E.BadRequest, "El servidor no admite Idempotency-Key sin credenciales "
                                    '("anonymous": false, §7.1): pasa idempotency_key=None o un token.',
                                    errors=[{"header": "Idempotency-Key", "code": "unknown"}])
        return {"idempotency-key": sf_string(key)} if isinstance(key, str) else {}

    def create_request(self, data: Any, idempotency_key: IdempotencyKey) -> Request:
        self._check_method("POST", False)
        id_from = self.id_from
        if self._api.validate:
            if not isinstance(data, Mapping):
                raise TypeError("create() espera un objeto (dict) con el elemento.")
            if "id" in data and id_from not in ("client", "either"):
                why = ("El servidor asigna el id: no lo envíes en create() (§5.1)." if id_from == "server" else
                       f"id_from tiene un valor desconocido ({id_from}): no se envía id (§5.1).")
                raise E.local_error(E.Invalid, why, status=422, errors=[{"pointer": "/id", "code": "read_only"}])
            if "id" not in data and id_from == "client":
                raise E.local_error(E.Invalid, f"{self.name} pide que el cliente elija el id (id_from: client, §5.1).",
                                    status=422, errors=[{"pointer": "/id", "code": "required"}])
        h = self._key_header(idempotency_key, True)
        # §5.7: un POST con un id elegido por el cliente no puede crear dos elementos.
        client_id = isinstance(data, Mapping) and "id" in data and id_from in ("client", "either")
        return self._api.build("POST", self._url(), body=data, has_body=True, headers=h, retry_safe=bool(h) or client_id)

    def update_request(self, id: Any, patch: Any, if_match: Any) -> Request:
        self._check_method("PATCH", True)
        if self._api.validate and not isinstance(patch, Mapping):
            raise TypeError("update() espera un objeto (dict) con el merge patch (RFC 7396).")
        h = {"if-match": etag_value(if_match)} if if_match is not None else {}
        return self._api.build("PATCH", self._item_url(id), body=patch, has_body=True,
                               content_type="application/merge-patch+json", headers=h)

    def delete_request(self, id: Any, if_match: Any) -> Request:
        self._check_method("DELETE", True)
        h = {"if-match": etag_value(if_match)} if if_match is not None else {}
        return self._api.build("DELETE", self._item_url(id), headers=h)

    def action_request(self, id: Any, name: str, data: Any, idempotency_key: IdempotencyKey) -> Request:
        if not _ACTION.fullmatch(name):
            raise ValueError(f"«{name}» no es un nombre de acción (§3, §11.1).")
        decl = (self.declaration.get("actions") or {}).get(name)
        # §4.2: una acción con un on o un method desconocidos se trata como no declarada, y no se usa.
        if isinstance(decl, Mapping) and (decl.get("on") not in ("item", "collection") or decl.get("method", "POST") not in ("GET", "POST")):
            raise E.local_error(E.NotFound, f"{self.name}:{name} se declara con un on o un method desconocidos; no se usa (§4.2).", status=404)
        if self._api.validate:
            if "actions" not in self._api.extensions:
                raise E.local_error(E.NotFound, "El servidor no anuncia la extensión actions (§11.1).", status=404)
            if not isinstance(decl, Mapping):
                offered = ", ".join(self.declaration.get("actions") or {}) or "ninguna"
                raise E.local_error(E.NotFound, f"{self.name} no declara la acción «{name}»; declara: {offered} (§11.1).", status=404)
            on = decl.get("on")
            if on == "item" and id is None:
                raise TypeError(f"La acción «{name}» es de elemento: pasa su id.")
            if on == "collection" and id is not None:
                raise TypeError(f"La acción «{name}» es de colección: pasa None como id.")
        decl = decl if isinstance(decl, Mapping) else {}
        method = "GET" if decl.get("method") == "GET" else "POST"
        if method == "GET" and data is not None:
            raise TypeError(f"La acción «{name}» usa GET y no lleva cuerpo (§11.1).")
        url = (self._item_url(id) if id is not None else self._url()) + ":" + name
        if method == "GET":
            return self._api.build("GET", url)
        idempotent = decl.get("idempotent") is True
        h = self._key_header(idempotency_key, not idempotent)
        return self._api.build("POST", url, body=data, headers=h, retry_safe=idempotent or bool(h))


class SingletonCore:
    """Un singleton (§5.9): un recurso sin colección ni ``id``, como ``/me``. Admite ``GET`` y, si lo
    declara, ``PATCH`` con merge patch; con ``query``, ``fields``. Nada más: ni ``POST`` ni ``DELETE``
    (dan ``405``) ni ``include`` ni otros parámetros reservados."""

    def __init__(self, api: ApiCore, name: str, decl: Mapping[str, Any]) -> None:
        self._api = api
        self.name = name
        self.declaration: dict[str, Any] = dict(decl)

    @property
    def path(self) -> str:
        return str(self.declaration["path"])

    def _url(self) -> str:
        return self._api.api_path(self.path)

    def _check_method(self, method: str) -> None:
        declared = self.declaration.get("methods")
        if self._api.validate and isinstance(declared, list) and method not in declared:
            raise E.local_error(E.MethodNotAllowed, f"El singleton {self.name} no admite {method}; admite {', '.join(declared)} (§5.9).",
                                status=405)

    def get_request(self, fields: Any = None) -> Request:
        self._check_method("GET")
        q: list[tuple[str, str]] = []
        if fields is not None:
            fl = _split(fields)
            if self._api.validate:
                if "query" not in self._api.profiles:
                    raise E.local_error(E.UnsupportedParameter, f"{self.name}: el servidor no anuncia query, así que no admite fields (§5.3).",
                                        parameter="fields")
                if not fl:
                    raise E.local_error(E.InvalidParameter, f"{self.name}: fields no puede ir vacío.", parameter="fields")
            q.append(("fields", ",".join(fl)))
        return self._api.build("GET", self._url(), query=q)

    def update_request(self, patch: Any, if_match: Any) -> Request:
        self._check_method("PATCH")
        if self._api.validate and not isinstance(patch, Mapping):
            raise TypeError("update() espera un objeto (dict) con el merge patch (RFC 7396).")
        h = {"if-match": etag_value(if_match)} if if_match is not None else {}
        return self._api.build("PATCH", self._url(), body=patch, has_body=True,
                               content_type="application/merge-patch+json", headers=h)


def with_etag(resp: Response, data: Any) -> Any:
    """§7.2: el elemento completo sin codificación de contenido lleva su ETag de elemento en la
    cabecera, no en ``_etag``. Se copia a ``_etag`` para poder escribir con ``if_match=elemento``."""
    tag = resp.headers.get("etag")
    if isinstance(data, dict) and tag and not tag.startswith("W/") and "content-encoding" not in resp.headers and "_etag" not in data:
        data["_etag"] = tag
    return data


@dataclass
class PageData:
    items: list[Item]
    next: str | None
    total: Total | None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_body(cls, body: Any) -> "PageData":
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise E.RestepError(200, {"detail": "La respuesta de la colección no tiene items (§5.2)."}, local=True)
        total = body.get("total")
        t = Total(total, bool(body.get("total_estimated"))) if isinstance(total, int) and not isinstance(total, bool) else None
        nxt = body.get("next")
        return cls(body["items"], nxt if isinstance(nxt, str) and nxt else None, t, body)


def _split(v: str | Sequence[str]) -> list[str]:
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(s) for s in v]


def _plain(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return _decimal_text(v)
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, _dt.timedelta):
        return iso_duration(v)
    return str(v)
