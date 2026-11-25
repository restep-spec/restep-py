"""The part of the client that does no I/O: discovery, local validation, URLs, headers and
reading responses. The synchronous client (urllib) and the asynchronous one (httpx) share it and only
supply the transport."""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
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
    """Default value of ``idempotency_key``: a new key per call if the server announces
    ``writes`` (§7.1); the same one on every retry of that call."""

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
    """The ``total`` of a collection (§6.5); ``estimated`` says whether it is approximate."""

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
    retry_safe: bool = False  # can be repeated on a 503 without risk of duplicating the effect
    # When retry_safe comes only from the Idempotency-Key: the key's lifetime in seconds, after which
    # the request is not repeated automatically (§5.7).
    key_ttl: float | None = None
    first_sent: float | None = None  # monotonic time of the first attempt


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lowercase names; several equal headers, joined with ", "
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
# §6.1, §11.1: an integer or number operand (and a GET action's param of those types) follows the
# number grammar of RFC 8259 (section 6), exponent included: 2e1 is 20; +20, .5, 020 and 0x14 are not
# numbers. An integer one needs a zero fractional part.
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})")
# §3: a local date-time is full-date "T" partial-time of RFC 3339, without an offset and never with a
# leap second (:60), which a server rejects in a request body.
_LOCALDATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](\.[0-9]+)?")
# §3: the duration grammar of RFC 3339 (Appendix A), plus a fraction of up to nine digits only in
# the seconds. Letters are case-insensitive (RFC 5234, 2.3); this client writes them in uppercase.
_DUR_SECOND = r"[0-9]+(?:\.[0-9]{1,9})?S"
_DUR_TIME = rf"T(?:[0-9]+H(?:[0-9]+M(?:{_DUR_SECOND})?)?|[0-9]+M(?:{_DUR_SECOND})?|{_DUR_SECOND})"
_DUR_DATE = r"(?:[0-9]+Y(?:[0-9]+M(?:[0-9]+D)?)?|[0-9]+M(?:[0-9]+D)?|[0-9]+D)"
_DURATION = re.compile(rf"P(?:(?P<date>{_DUR_DATE})(?:{_DUR_TIME})?|{_DUR_TIME}|[0-9]+W)", re.IGNORECASE)
_ID = re.compile(r"[A-Za-z0-9._~-]+")
_ACTION = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")  # the grammar of names (§3)
OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "nin", "null", "prefix", "contains")  # §6.1
LIST_OPS = ("in", "nin")
FIELD_TYPES = frozenset({"string", "integer", "number", "decimal", "boolean", "date", "datetime", "localdatetime",
                         "timezone", "duration", "enum"})  # §4.2
QUANTIFIERS = ("some",)  # §6.1; other values are reserved for minor versions
EFFECTS = frozenset({"destroys", "external", "charges"})  # §11.1
# §11.1: a collection never declares an action named changes (sync uses it), and its action new is
# always the safe GET collection action that gives a new item's starting values.
RESERVED_ACTIONS = frozenset({"changes"})
DEFAULT_KEY_TTL = 86400.0  # §5.7, §7.1: 24 hours where idempotency.ttl is not declared

# ── Versiones (§1.1) y partes experimentales (§1.2) ──────────────────────────
_RESTEP_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-((?:draft|rc)\.(?:0|[1-9][0-9]*)))?")
# The versions whose text this client implements: draft 7 and the 1.0 that comes out of it. With
# another version, the experimental parts are not used (§1.2).
IMPLEMENTED_VERSIONS = frozenset({"1.0-draft.7"})  # §1.2: the text of 1.0 does not exist yet
EXPERIMENTAL = frozenset({"sync", "batch", "filter-expr", "query-method", "inherit"})
# Where each kind of retirement notice can go (§5.5); an unknown one is not used (§4.2).
DEPRECATION_KINDS = {
    "root": frozenset({"version", "profile", "extension", "collection", "singleton", "reason"}),
    "collection": frozenset({"field", "filter", "operator", "value", "sort", "relation", "include", "count",
                             "max_limit", "action", "method", "item_method"}),
    "singleton": frozenset({"field", "method", "action"}),
}


def parse_version(value: Any) -> tuple[int, int, str | None] | None:
    """``(major, minor, pre-release)`` of a ``restep`` value that follows the grammar of §1.1
    (``"1.0"``, ``"1.0-draft.7"``, ``"1.0-rc.1"``), or ``None`` if it does not follow it."""
    m = _RESTEP_VERSION.fullmatch(value) if isinstance(value, str) else None
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else None


def iso_duration(td: _dt.timedelta) -> str:
    """A timedelta in the duration grammar of §3: days and a time part, the fraction only in the
    seconds (microseconds, so up to six digits). The grammar chains the parts, so between hours and
    seconds the minutes go even when they are 0 (``PT1H0M5S``). It has no sign."""
    if td < _dt.timedelta(0):
        raise ValueError("A Restep duration cannot be negative (§3).")
    days, micros = td.days, td.seconds * 1_000_000 + td.microseconds
    hours, micros = divmod(micros, 3_600_000_000)
    minutes, micros = divmod(micros, 60_000_000)
    whole, frac = divmod(micros, 1_000_000)
    seconds = f"{whole}.{frac:06d}".rstrip("0") + "S" if frac else f"{whole}S"
    parts = []
    if hours:
        parts.append(f"{hours}H")
    if minutes or (hours and (whole or frac)):
        parts.append(f"{minutes}M")
    if whole or frac or (not days and not parts):
        parts.append(seconds)
    return (f"P{days}D" if days else "P") + ("T" + "".join(parts) if parts else "")


def is_duration(text: Any) -> bool:
    """Whether a string follows the duration grammar of §3 (case-insensitive)."""
    return isinstance(text, str) and _DURATION.fullmatch(text) is not None


def duration_seconds(text: str) -> Decimal:
    """The exact length of a duration in seconds, a day counting as 24 hours, as a filter compares
    it (§6.1). Years and months have no fixed length: they give ``ValueError``."""
    m = _DURATION.fullmatch(text) if isinstance(text, str) else None
    if not m:
        raise ValueError(f"{text!r} is not a duration (§3).")
    total = Decimal(0)
    units = {"W": 604800, "D": 86400, "H": 3600, "S": 1}
    in_time = False
    for number, unit in re.findall(r"([0-9.]*)([A-Za-z])", text.upper()[1:]):
        if unit == "T":
            in_time = True
            continue
        if unit in ("Y",) or (unit == "M" and not in_time):
            raise ValueError("A duration with years or months has no fixed length (§6.1).")
        total += Decimal(number) * (60 if unit == "M" else units[unit])
    return total


def _decimal_text(d: Decimal) -> str:
    return format(d, "f")


def format_filter_value(value: Any, decl: Mapping[str, Any], parameter: str) -> str:
    """The text of a filter operand, checked against the declared type (§6.1)."""
    kind = decl.get("type", "string")
    if kind not in FIELD_TYPES:
        # §4.2: an unknown type is not used; the caller has already treated the filter as undeclared.
        raise E.local_error(E.UnsupportedParameter, f"{parameter}: unknown type {kind!r} (§4.2).", parameter=parameter)

    def bad(why: str) -> E.RestepError:
        return E.local_error(E.InvalidParameter, f"{parameter}: {why}", parameter=parameter)

    if isinstance(value, bool):
        if kind != "boolean":
            raise bad(f"a boolean is not valid for a field of type {kind}.")
        return "true" if value else "false"
    if kind == "boolean":
        if value in ("true", "false"):
            return str(value)
        raise bad("true or false expected.")
    if isinstance(value, _dt.datetime):
        if kind == "localdatetime":
            if value.tzinfo is not None:
                raise bad("a local date-time has no offset (§3): pass a naive datetime.")
            return value.isoformat()
        if kind != "datetime":
            raise bad(f"a datetime is not valid for a field of type {kind}.")
        if value.tzinfo is None:
            raise bad("the instant needs a time zone (RFC 3339 with offset).")
        return value.isoformat()
    if isinstance(value, _dt.date):
        if kind != "date":
            raise bad(f"a date is not valid for a field of type {kind}.")
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        if kind != "duration":
            raise bad(f"a duration is not valid for a field of type {kind}.")
        try:
            return iso_duration(value)
        except ValueError as e:
            raise bad(str(e)) from None
    if isinstance(value, (int, float, Decimal)):
        # §6.1: an operand is never replaced by another value, such as an infinity.
        if (isinstance(value, float) and (value != value or value in (float("inf"), float("-inf")))) \
                or (isinstance(value, Decimal) and not value.is_finite()):
            raise bad("the number is not finite.")
        if kind == "integer":
            # §6.1: an operand of an integer field matches its type when its exact decimal value has no
            # fractional part: 20.0 is the same operand as 20, and 20.5 gives invalid_parameter.
            if isinstance(value, int) or (isinstance(value, float) and value.is_integer()) \
                    or (isinstance(value, Decimal) and value == value.to_integral_value()):
                return str(int(value))
            raise bad(f"{value} has a fractional part, so it is not an integer (§6.1).")
        if kind in ("number", "decimal"):
            return _decimal_text(value) if isinstance(value, Decimal) else repr(value) if isinstance(value, float) else str(value)
        if kind == "string" and isinstance(value, int):
            return str(value)
        raise bad(f"a number is not valid for a field of type {kind}.")
    if not isinstance(value, str):
        raise bad(f"unsupported Python type: {type(value).__name__}.")
    text = value
    if kind == "integer" and not _INTEGER.fullmatch(text):
        # §6.1: "20.0" and "2e1" are the same operand as "20"; "20.5" is not an integer.
        if not _NUMBER.fullmatch(text) or (d := Decimal(text)) != d.to_integral_value():
            raise bad(f"'{text}' is not an integer: a number of RFC 8259 without a fractional part, such as 20, 20.0 or 2e1 "
                      "(never +20, 020 or 0x14) (§6.1).")
    if kind == "number" and not _NUMBER.fullmatch(text):
        raise bad(f"'{text}' is not a number of RFC 8259, such as 20, 20.5 or 2e1 (never +20, .5, 020 or 0x14) (§6.1).")
    if kind == "decimal" and not _DECIMAL.fullmatch(text):
        raise bad(f"'{text}' is not a decimal (such as 18.50).")
    if kind == "date":
        try:
            if not _DATE.fullmatch(text):
                raise ValueError
            _dt.date.fromisoformat(text)
        except ValueError:
            raise bad(f"'{text}' is not a YYYY-MM-DD date.") from None
    if kind == "datetime" and not _DATETIME.fullmatch(text):
        raise bad(f"'{text}' is not an RFC 3339 instant with offset.")
    if kind == "localdatetime" and not _LOCALDATETIME.fullmatch(text):
        raise bad(f"'{text}' is not a local date-time without offset, such as 2027-03-30T19:00:00 (§3).")
    if kind == "duration":
        if not is_duration(text):
            raise bad(f"'{text}' is not a duration of §3 (such as PT1H30M or PT0.250S; a fraction only in the seconds, "
                      "up to nine digits).")
        try:
            duration_seconds(text)
        except ValueError:
            raise bad("a duration with years or months has no fixed length (§6.1).") from None
    if kind == "enum":
        values = decl.get("values") or []
        if values and text not in values:
            raise bad(f"'{text}' is not one of the values: {', '.join(values)}.")
    return text


# ── Discovery and the API ────────────────────────────────────────────────────
def is_discovery(doc: Any) -> bool:
    """§4.2: a discovery document is a JSON object with ``restep`` of type string."""
    return isinstance(doc, dict) and isinstance(doc.get("restep"), str)


def links(header: str | None) -> list[tuple[str, dict[str, str]]]:
    """The links of a ``Link`` header (RFC 8288), without frills."""
    out: list[tuple[str, dict[str, str]]] = []
    for m in re.finditer(r"<([^>]*)>((?:\s*;\s*[^;,=]+(?:=(?:\"[^\"]*\"|[^;,]*))?)*)", header or ""):
        params = {k.strip().lower(): v.strip('"') for k, v in re.findall(r";\s*([^;,=\s]+)\s*=\s*(\"[^\"]*\"|[^;,]*)", m.group(2))}
        out.append((m.group(1), params))
    return out


def sf_string(key: str) -> str:
    """A Structured Fields string (RFC 9651), as ``Idempotency-Key`` requires (§7.1)."""
    if any(not (0x20 <= ord(c) <= 0x7E) for c in key):
        raise ValueError("An Idempotency-Key can only carry printable ASCII (RFC 9651).")
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return _decimal_text(o)  # §3: amounts go as a string
    if isinstance(o, _dt.datetime):
        if o.tzinfo is None:
            raise ValueError("A datetime without a time zone is not an RFC 3339 instant (§3).")
        return o.isoformat()
    if isinstance(o, _dt.date):
        return o.isoformat()
    if isinstance(o, _dt.timedelta):
        return iso_duration(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"Cannot convert a {type(o).__name__} to JSON.")


_NOT_IJSON = re.compile("[\ud800-\udfff\ufdd0-\ufdef\ufffe\uffff" + "".join(
    f"{chr(plane << 16 | 0xFFFE)}{chr(plane << 16 | 0xFFFF)}" for plane in range(1, 17)) + "]")


def check_ijson(value: Any, where: str = "") -> None:
    """§3: what a client sends is an I-JSON message (RFC 7493, section 2): no unpaired surrogates or
    noncharacters in any string or member name, and no number that is not finite. Member names cannot
    repeat in a dict, so that rule holds by construction."""
    if isinstance(value, str):
        m = _NOT_IJSON.search(value)
        if m:
            raise ValueError(f"{where or '/'}: U+{ord(m.group()):04X} is an unpaired surrogate or a noncharacter, "
                             "which an I-JSON message cannot carry (§3, RFC 7493).")
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{where or '/'}: a number that is not finite is not JSON (§3).")
    elif isinstance(value, Mapping):
        for k, v in value.items():
            check_ijson(str(k), where)
            check_ijson(v, f"{where}/{str(k).replace('~', '~0').replace('/', '~1')}")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for i, v in enumerate(value):
            check_ijson(v, f"{where}/{i}")


def dump_json(value: Any) -> bytes:
    check_ijson(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=_json_default, separators=(",", ":")).encode("utf-8")


def etag_value(if_match: Any) -> str:
    """``if_match`` can be the ETag (with or without quotes) or an item read with its ``_etag``."""
    if isinstance(if_match, Mapping):
        tag = if_match.get("_etag")
        if not isinstance(tag, str):
            raise ValueError("The item has no _etag: read it with get() or pass the ETag.")
        return tag
    if not isinstance(if_match, str) or not if_match:
        raise TypeError("if_match must be an ETag or an item with _etag.")
    if if_match == "*" or if_match.startswith(('"', 'W/"')) or "," in if_match:
        return if_match
    return f'"{if_match}"'


def _problem(resp: Response) -> dict[str, Any]:
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = None
    return data if isinstance(data, dict) else {"detail": f"HTTP {resp.status}"}


def _problem_code(resp: Response) -> str | None:
    code = _problem(resp).get("code")
    return code if isinstance(code, str) else None


class ApiCore:
    """Common state of a connection: root, credentials, language, discovery and options."""

    def __init__(self, url: str, *, token: str | None = None, language: str | None = None,
                 headers: Mapping[str, str] | None = None, validate: bool = True, retries: int = 0,
                 max_retry_wait: float = 60.0, timeout: float = 30.0) -> None:
        if not url.endswith("/"):
            url += "/"  # the API root keeps its trailing slash (§11.5), so urljoin does not lose the last segment
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"The API URL must be an absolute http or https URL: {url}")
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
        """The singletons of discovery (§5.9), without those a client MUST ignore: a ``path`` that is
        a template without ``parent``, or a ``parent`` that names no collection or singleton."""
        d = self.discovery
        found = d.get("singletons") if d else None
        if not isinstance(found, dict):
            return {}
        valid = {n: s for n, s in found.items() if isinstance(s, dict) and isinstance(s.get("path"), str)}
        known = set(self.declarations) | set(valid)
        return {n: s for n, s in valid.items()
                if ("parent" in s and s["parent"] in known) or ("parent" not in s and "{" not in s["path"])}

    def set_discovery(self, doc: Any, url: str | None = None) -> None:
        if not is_discovery(doc):
            self.discovery, self.version = None, None
            return
        version = parse_version(doc["restep"])
        if version is None or version[0] != 1:
            # §1.1: neither a Restep 1 discovery document nor "core only": that API cannot be used as Restep 1.
            raise E.UnsupportedVersion(doc["restep"], url)
        self.discovery, self.version = doc, version

    @property
    def default_language(self) -> str | None:
        """The API's default language (§5.6), which a response is in when no language of
        ``Accept-Language`` matches; declared where a collection has localized fields."""
        lang = (self.discovery or {}).get("default_language")
        return lang if isinstance(lang, str) else None

    @property
    def key_ttl(self) -> float:
        """How long the server keeps an ``Idempotency-Key``: ``idempotency.ttl``, or 24 hours where it
        is not declared (§5.7, §7.1)."""
        ttl = ((self.discovery or {}).get("idempotency") or {}).get("ttl")
        return float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 else DEFAULT_KEY_TTL

    def discovery_response(self, resp: Response) -> tuple[Any, bool]:
        """§4.2: what a response to ``GET`` on the root (or on a ``service-desc`` target) says.
        ``(document, False)`` for a ``2xx`` whose body is a JSON object with a string ``restep``;
        ``(None, True)`` for a ``2xx`` without one or a ``404``, after which a client assumes
        ``core`` only (if no ``service-desc`` target gives a document); any other error raises,
        since it says nothing about the profiles."""
        if 200 <= resp.status < 300:
            try:
                doc = json.loads(resp.body.decode("utf-8")) if resp.body and resp.is_json else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                doc = None
            return (doc, False) if is_discovery(doc) else (None, True)
        if resp.status == 404:
            return None, True
        problem = _problem(resp)
        raise E.error_from_problem(resp.status, problem, headers=resp.headers, url=resp.url, method="GET")

    @staticmethod
    def restep1(doc: Any) -> bool:
        """Whether a document is a discovery document of Restep 1 (§1.1)."""
        v = parse_version(doc.get("restep")) if is_discovery(doc) else None
        return v is not None and v[0] == 1

    @property
    def restep_version(self) -> str | None:
        return self.discovery["restep"] if self.discovery else None

    @property
    def pre_release(self) -> str | None:
        """``"draft.7"`` or ``"rc.1"`` if the server declares a draft or a candidate: then the
        guarantees of no published version nor of another draft are to be assumed (§1.1)."""
        return self.version[2] if self.version else None

    def usable(self, part: str) -> bool:
        """Whether a profile or an extension can be used: announced and, if experimental (§1.2),
        under a declared version whose text this client implements (for a draft, that same one)."""
        if part not in self.profiles and part not in self.extensions:
            return False
        return part not in EXPERIMENTAL or self.restep_version in IMPLEMENTED_VERSIONS

    @property
    def has_credentials(self) -> bool:
        return bool(self.token) or any(k.lower() in ("authorization", "cookie") for k in self.extra_headers)

    @property
    def anonymous_keys(self) -> bool:
        """§7.1: ``"anonymous": false`` in ``idempotency`` says the server rejects
        ``Idempotency-Key`` on requests without credentials. Missing, or any other value, it is ``true``."""
        idem = (self.discovery or {}).get("idempotency")
        return not (isinstance(idem, dict) and idem.get("anonymous") is False)

    def deprecations(self) -> list[dict[str, Any]]:
        """The retirement notices in discovery (§5.5), with ``where`` (``""`` for the root, the
        collection name or ``singleton:name``). Those with an unknown ``kind`` in their place are not
        used (§4.2) and do not appear."""
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
        """``service-desc`` targets of the root response, the JSON ones first (§4.2)."""
        found = [(urljoin(resp.url or self.root, target), p) for target, p in links(resp.headers.get("link"))
                 if "service-desc" in p.get("rel", "").split()]
        return [t for t, p in found if "json" in p.get("type", "")] + [t for t, p in found if "json" not in p.get("type", "")]

    # Peticiones
    def url(self, path_or_url: str) -> str:
        """A path in discovery is relative to the API root; a link in a body
        (``next``, ``href``), absolute or an absolute path that already includes the root's (§3)."""
        return urljoin(self.root, path_or_url)

    def api_path(self, path: str) -> str:
        return urljoin(self.root, path.lstrip("/"))

    def same_origin(self, url: str) -> bool:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}".lower() == self.origin

    def build(self, method: str, url: str, *, query: Sequence[tuple[str, str]] | None = None, body: Any = None,
              has_body: bool = False, content_type: str = JSON_TYPES, headers: Mapping[str, str] | None = None,
              retry_safe: bool | None = None, raw: bytes | None = None) -> Request:
        if query:
            url += ("&" if "?" in url else "?") + urlencode(list(query), quote_via=quote, safe=",")
        # §3: a link with a scheme other than https or the root's (file:, data:…) is not followed.
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("https", urlsplit(self.root).scheme.lower()):
            raise ValueError(f"Not following a link with scheme {scheme or '(none)'}: only https or the root's (§3).")
        h = {"accept": "application/json, application/problem+json"}
        if self.language:
            h["accept-language"] = self.language
        # §4.3: the API's credentials (the token and the own headers, which may carry them)
        # only go to its origin.
        if self.same_origin(url):
            h.update({k.lower(): v for k, v in self.extra_headers.items()})
            if self.token:
                h["authorization"] = f"Bearer {self.token}"
        data = None
        if raw is not None:
            data = raw  # the bytes of an upload (§5.1), with their own Content-Type
            h["content-type"] = content_type
        elif has_body or body is not None:
            data = dump_json(body)
            h["content-type"] = content_type
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        if retry_safe is None:
            retry_safe = method in ("GET", "HEAD", "PATCH", "DELETE")
        # §5.7: a POST that is safe to repeat only because of its key is repeated only within the key's lifetime.
        key_ttl = self.key_ttl if method == "POST" and retry_safe and "idempotency-key" in h else None
        return Request(method, url, h, data, retry_safe, key_ttl)

    # Statuses this client repeats (§5.7), and the transports where 421 can go over another connection.
    new_connection_per_request = False

    def retry_delay(self, req: Request, resp: Response, attempt: int) -> float | None:
        """How long to wait before repeating, or ``None`` to not repeat (§5.7).

        - ``429`` and ``503`` with a ``problem+json`` body come from the API, which never gives them
          to a request that had an effect: they are repeated whatever the method. Without that body
          they may come from an intermediary, and only a request safe to repeat is.
        - ``409 request_in_progress`` is repeated with the same key (§7.1).
        - ``408`` (not received in time) and ``425`` (this client never sends TLS early data) are
          repeated as is; ``421`` only over another connection, so only where the transport opens
          one per request.
        - ``502`` and ``504`` say nothing about the effect: only a request safe to repeat is.
        - A request safe to repeat only because of its ``Idempotency-Key`` is never repeated once
          ``idempotency.ttl`` (24 hours if not declared) has passed since its first attempt."""
        if attempt >= self.retries:
            return None
        status, problem = resp.status, "problem+json" in resp.headers.get("content-type", "")
        if status in (429, 503):
            ok = problem or req.retry_safe
        elif status == 409:
            ok = _problem_code(resp) == "request_in_progress"
        elif status in (408, 425):
            ok = True
        elif status == 421:
            ok = self.new_connection_per_request
        elif status in (502, 504):
            ok = req.retry_safe
        else:
            ok = False
        if not ok:
            return None
        wait = E.parse_retry_after(resp.headers.get("retry-after"))
        if wait is None:
            wait = min(0.5 * 2 ** attempt, self.max_retry_wait)
        if wait > self.max_retry_wait:
            return None
        if req.key_ttl is not None and req.first_sent is not None and time.monotonic() - req.first_sent + wait >= req.key_ttl:
            return None
        return wait

    def parse(self, req: Request, resp: Response) -> Any:
        """The body of a successful response, or the typed exception for an error one (§5.4)."""
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
            more = f"; it is retired on {sunset}" if sunset else ""
            warnings.warn(f"{req.method} {req.url} uses something announced as deprecated (§5.5){more}.",
                          E.RestepDeprecationWarning, stacklevel=5)
        return data


# ── Colecciones ───────────────────────────────────────────────────────────────
_VAR = re.compile(r"\{([^}]+)\}")


class CollectionCore:
    """What a collection can do without I/O: check parameters and build requests."""

    def __init__(self, api: ApiCore, name: str, decl: Mapping[str, Any] | None, path_vars: Mapping[str, str] | None = None) -> None:
        self._api = api
        self.name = name
        self.declaration: dict[str, Any] = dict(decl or {"path": f"/{name}"})
        self._vars = dict(path_vars or {})

    # Path template (RFC 6570, level 1) of subcollections
    @property
    def path_template(self) -> str:
        return str(self.declaration.get("path") or f"/{self.name}")

    @property
    def variables(self) -> list[str]:
        return _VAR.findall(self.path_template)

    @property
    def parent(self) -> str | None:
        """The collection the items of this subcollection hang from (§4.2)."""
        p = self.declaration.get("parent")
        return p if isinstance(p, str) else None

    @property
    def id_from(self) -> str:
        """Who chooses the ``id`` (§5.1): ``"server"``, ``"client"``, ``"either"`` or the unknown
        value as is (with which no ``id`` is sent)."""
        v = self.declaration.get("id_from", "server")
        return v if isinstance(v, str) else repr(v)

    def collation(self, field: str) -> str:
        """How the server compares and orders a string field (§6.1): ``"codepoint"`` if it is not
        in ``collations``; ``"server"`` if it says so, or if the value is not a string or is not known.
        With ``"server"`` there is no need to reproduce that order or that equality locally."""
        c = self.declaration.get("collations")
        if not isinstance(c, dict) or field not in c:
            return "codepoint"
        return "codepoint" if c[field] == "codepoint" else "server"

    @property
    def localized(self) -> dict[str, str]:
        """The localized fields (§3): read-only fields with the text of a language map (their
        ``source``) in the language of the response. A client writes the map; a write that carries one
        of these fields goes to the server, which ignores it with the value the item has (§5.1)."""
        found = self.declaration.get("localized")
        if not isinstance(found, Mapping):
            return {}
        return {f: d["source"] for f, d in found.items() if isinstance(d, Mapping) and isinstance(d.get("source"), str)}

    @property
    def local_times(self) -> dict[str, str | None]:
        """The fields that hold local date-times (§3, §4.2), each with the path of the field that gives
        its time zone, or ``None`` for a floating local time, which denotes no instant by itself."""
        found = self.declaration.get("local_times")
        if not isinstance(found, Mapping):
            return {}
        return {f: (d.get("zone") if isinstance(d.get("zone"), str) else None) for f, d in found.items() if isinstance(d, Mapping)}

    def instant(self, item: Mapping[str, Any], field: str) -> _dt.datetime | None:
        """The instant a local date-time of the item denotes (§3), with the zone ``local_times``
        declares for it (a field of the item, or a dotted path through an included relationship).
        ``None`` if the field has no value; ``LookupError`` if it is floating or its zone is not in
        the item."""
        if field not in self.local_times:
            raise KeyError(f"{self.name} does not declare '{field}' in local_times (§4.2).")
        zone_path = self.local_times[field]
        if zone_path is None:
            raise LookupError(f"'{field}' is a floating local time: it denotes no instant by itself (§4.2).")
        value: Any = item
        for part in zone_path.split("."):
            value = value.get(part) if isinstance(value, Mapping) else None
        if not isinstance(value, str):
            raise LookupError(f"The item does not carry the zone of '{field}' at '{zone_path}' (include the relationship or ask for the field).")
        local = item.get(field)
        return None if local is None else local_to_instant(local, value)

    @property
    def upload_declaration(self) -> Mapping[str, Any] | None:
        """``upload`` (§5.1): the media types a ``POST`` may carry as a file, ``max_size`` and ``multipart``."""
        u = self.declaration.get("upload")
        return u if isinstance(u, Mapping) and isinstance(u.get("media_types"), list) else None

    @property
    def creates_from_json(self) -> bool:
        """Whether a JSON ``POST`` creates an item (§5.1): ``schemas.create`` announces it. A collection
        with ``upload`` and without ``create`` takes only files; with both, a JSON ``POST`` is always a
        creation and a JSON file goes only as the ``file`` part of ``multipart/form-data``."""
        if self.upload_declaration is None:
            return True
        schemas = self.declaration.get("schemas")
        return isinstance(schemas, Mapping) and schemas.get("create") is not None

    def effects(self, operation: str, *, item: bool = False) -> frozenset[str]:
        """The effects of an action or of an unsafe method (§11.1, §4.3) that a person may want to
        approve first: ``destroys``, ``external``, ``charges``. Without a declaration, none for a safe
        action and all three for an unsafe operation; with a value this client does not know, all three;
        a ``DELETE`` not declared has ``destroys``; safe methods none."""
        if operation in ("GET", "HEAD"):
            return frozenset()
        if operation in ("POST", "PATCH", "DELETE"):
            declared = self.declaration.get("item_method_effects" if item else "method_effects")
            value = declared.get(operation) if isinstance(declared, Mapping) else None
            if value is None and operation == "DELETE":
                return frozenset({"destroys"})
        else:
            decl = (self.declaration.get("actions") or {}).get(operation)
            if not isinstance(decl, Mapping):
                raise KeyError(f"{self.name} does not declare the action '{operation}' (§11.1).")
            return effects_of(decl.get("effects"), safe=is_safe_action(decl))
        return effects_of(value)

    def ref(self, id: Any) -> dict[str, str]:
        """The relation object to link an item of this collection (§3, §5.1). In a
        subcollection it carries ``href``, which is required to link it: its ``id`` only identifies the
        item under its parent."""
        out = {"id": str(id)}
        if self.variables:
            out["href"] = urlsplit(self._item_url(id)).path
        return out

    def inherited(self, item: Mapping[str, Any], field: str) -> Any:
        """Where the value of an effective field comes from (§11.6): ``"own"`` (its own value),
        ``{"href": ...}`` (the real source), ``"default"``, ``"hidden"`` or ``None`` if the item does not
        carry ``_inherited`` and it is not known. An unknown ``from`` is ``"hidden"``. ``inherit`` is
        experimental: it is only used under a version whose text this client implements (§1.2)."""
        if not self._api.usable("inherit"):
            raise E.ExperimentalNotUsable(
                f"inherit is not used: the server does not announce it or declares restep={self._api.restep_version!r}, "
                f"and this client implements {', '.join(sorted(IMPLEMENTED_VERSIONS))} (§1.2).")
        decl = self.declaration.get("inherit")
        if not isinstance(decl, dict) or field not in decl:
            raise KeyError(f"{self.name} does not declare '{field}' in inherit (§11.6).")
        if not isinstance(decl[field], Mapping) or any(k not in INHERIT_MEMBERS and not k.startswith("x-") for k in decl[field]):
            # §11.6, §4.2: a declaration with a member this client does not know is treated as not inherited.
            raise KeyError(f"{self.name}: '{field}' is declared in inherit with a member this client does not know, "
                           "so it is treated as not inherited (§11.6).")
        info = item.get("_inherited")
        if not isinstance(info, Mapping):
            return None
        if field not in info:
            return "own"
        entry = info[field]
        if isinstance(entry, Mapping) and "from" not in entry and isinstance(entry.get("fields"), Mapping):
            # §11.6, "combine": "fields": each inherited member names its own source.
            return {"fields": {m: _inherited_from(e.get("from") if isinstance(e, Mapping) else None)
                               for m, e in entry["fields"].items()}}
        return _inherited_from(entry.get("from") if isinstance(entry, Mapping) else None)

    def _path(self) -> str:
        def fill(m: re.Match[str]) -> str:
            v = m.group(1)
            if v not in self._vars:
                raise TypeError(f"Missing the path variable '{v}' of {self.path_template}: use .at({v}=...).")
            return quote(str(self._vars[v]), safe="")
        return _VAR.sub(fill, self.path_template).rstrip("/")

    def _url(self) -> str:
        return self._api.api_path(self._path())

    def _item_url(self, id: Any) -> str:
        id = str(id)
        if self._api.validate and (not _ID.fullmatch(id) or id in (".", "..")):
            raise ValueError(f"'{id}' is not a Restep id (§3: not empty, only A-Z a-z 0-9 - . _ ~, and neither . nor ..).")
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
            where = "its items" if item else "the collection"
            raise E.local_error(E.MethodNotAllowed, f"{self.name}: {where} does not allow {method}; it allows {', '.join(declared) or 'nothing'} (§4.3).",
                                status=405)

    def _unsupported(self, parameter: str, why: str) -> E.RestepError:
        return E.local_error(E.UnsupportedParameter, f"{self.name}: {why}", parameter=parameter)

    def _invalid(self, parameter: str, why: str) -> E.RestepError:
        return E.local_error(E.InvalidParameter, f"{self.name}: {why}", parameter=parameter)

    def _need_query(self, parameter: str) -> None:
        if not self._has("query"):
            raise self._unsupported(parameter, f"the server does not announce the query profile, so it does not support {parameter} (§5.3).")

    # Read parameters (§6, §9)
    def read_query(self, *, filter: Filter | None = None, named: Mapping[str, Any] | None = None, sort: str | Sequence[str] | None = None,
                   fields: str | Sequence[str] | None = None, include: str | Sequence[str] | None = None,
                   limit: int | None = None, count: bool = False, item: bool = False) -> list[tuple[str, str]]:
        q: list[tuple[str, str]] = []
        check = self._api.validate
        if filter:
            q += self._filter(filter) if check else self._filter_raw(filter)
        if named:
            q += self._named(named, check)
        if check and not item:
            sent = {p.split("]")[0] + "]" for p, _ in q if p.startswith("filter[")}
            for needed in self.required_filters:
                if needed not in sent:
                    raise self._invalid(needed, f"{needed} is required in every list request (§6.1).")
        if sort is not None:
            keys = _split(sort)
            if check:
                self._need_query("sort")
                if not keys:
                    raise self._invalid("sort", "sort cannot be empty.")
                declared = self.declaration.get("sort") or []
                for k in keys:
                    if k.lstrip("-") not in declared:
                        offered = ", ".join(declared) or "none"
                        raise self._unsupported("sort", f"cannot sort by '{k.lstrip('-')}'; sort fields: {offered} (§6.2).")
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
                    raise self._invalid("limit", f"limit must be an integer between 1 and {top if isinstance(top, int) else 'the server maximum'} (§6.4).")
            q.append(("limit", str(limit)))
        if count:
            if check:
                self._need_query("count")
                if self.declaration.get("count") not in ("exact", "estimated"):
                    # Undeclared, or with an unknown value, which is not used (§4.2).
                    raise self._unsupported("count", "the collection does not declare count (§6.5).")
            q.append(("count", "true"))
        return q

    def _named(self, named: Mapping[str, Any], check: bool) -> list[tuple[str, str]]:
        """§6.1: named filters, without operators. ``{"featured": True}`` (a switch, with no value)
        or ``{"search": "niebla"}`` (a value of its type). ``None`` does not apply it, and neither does ``False`` on a switch."""
        q: list[tuple[str, str]] = []
        declared: Mapping[str, Any] = self.declaration.get("named_filters") or {}
        for name, value in named.items():
            if value is None:
                continue
            parameter = f"filter[{name}]"
            if not check:
                if value is not False or name in self.default_filters:
                    q.append((parameter, "" if value is True else _plain(value)))
                continue
            self._need_query(parameter)
            decl = self.named_declaration(name)
            if decl is None:
                offered = ", ".join(n for n in declared if self.named_declaration(n)) or "none"
                raise self._unsupported(parameter, f"'{name}' is not a named filter; named filters: {offered} (§6.1).")
            if not decl.get("type"):
                if value is False:
                    # §6.1: false turns a default filter off; on another switch it would be invalid, so it is not sent.
                    if decl.get("default") is True:
                        q.append((parameter, "false"))
                    continue
                if value is not True:
                    raise self._invalid(parameter, f"'{name}' is a switch: pass True, with no value (§6.1).")
                q.append((parameter, ""))
                continue
            if value == "" or (isinstance(value, bool) and decl["type"] != "boolean"):
                raise self._invalid(parameter, f"'{name}' needs a {decl['type']} value (§6.1).")
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
        ``{"id": {"in": ["1", "2"]}}`` or ``{"id": ["1", "2"]}`` (in), ``{"rating": {"null": True}}``."""
        q: list[tuple[str, str]] = []
        declared: Mapping[str, Any] = self.declaration.get("filter") or {}
        # §6.1: more than one null filter on the same to-many relationship or list field, its own or that
        # of a path through it, gives invalid_parameter, even when their values agree.
        nulls: dict[str, str] = {}
        for name, cond in filter.items():
            if isinstance(cond, Mapping) and "null" in cond and self.filter_declaration(name) is not None \
                    and (group := self.null_group(name)) is not None:
                if group in nulls:
                    raise self._invalid(f"filter[{name}][null]", f"only one null filter on {group}, its own or that of a path "
                                        f"through it; there is also filter[{nulls[group]}][null] (§6.1).")
                nulls[group] = name
        for name, cond in filter.items():
            if cond is None:
                raise TypeError(f"filter[{name}] is None: for 'no value' use {{'{name}': {{'null': True}}}}.")
            if isinstance(cond, Mapping):
                conds = [(op, v, f"filter[{name}][{op}]") for op, v in cond.items()]
            elif isinstance(cond, (list, tuple, set, frozenset)):
                conds = [("in", cond, f"filter[{name}][in]")]
            else:
                conds = [("eq", cond, f"filter[{name}]")]
            for op, value, parameter in conds:
                self._need_query(parameter)
                decl = self.filter_declaration(name)
                if decl is None:
                    offered = ", ".join(n for n in declared if self.filter_declaration(n)) or "none"
                    raise self._unsupported(parameter, f"cannot filter by '{name}'; filter fields: {offered} (§6.1, §4.2).")
                ops = self.filter_ops(name)
                if op not in ops:
                    raise self._unsupported(parameter, f"the filter '{name}' does not allow the operator {op}; it allows {', '.join(ops)} (§6.1).")
                if op == "null":
                    if not isinstance(value, bool) and value not in ("true", "false"):
                        raise self._invalid(parameter, "the operand of null is true or false (§6.1).")
                    text = value if isinstance(value, str) else ("true" if value else "false")
                elif op in LIST_OPS:
                    values = list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]
                    if not values:
                        raise self._invalid(parameter, f"an {op} list cannot be empty (§6.1).")
                    texts = [format_filter_value(v, decl, parameter) for v in values]
                    if any("," in t for t in texts):
                        raise self._invalid(parameter, f"{op} values cannot contain commas; use eq (§6.1).")
                    if texts == [""]:
                        raise self._invalid(parameter, f"filter[{name}][{op}]= is an empty list, so a list with only \"\" cannot be sent (§6.1).")
                    text = ",".join(texts)
                else:
                    text = format_filter_value(value, decl, parameter)
                    if op in ("prefix", "contains") and text == "":
                        raise self._invalid(parameter, f"the operand of {op} cannot be empty (§6.1).")
                q.append((parameter, text))
        return q

    def filter_declaration(self, name: str) -> Mapping[str, Any] | None:
        """The declaration of a filter in ``filter``, or ``None`` where it is not declared or must be
        treated as not declared: a ``type`` or a ``quantifier`` this client does not know (§4.2, §6.1)."""
        decl = (self.declaration.get("filter") or {}).get(name)
        if not isinstance(decl, Mapping) or decl.get("type") not in FIELD_TYPES:
            return None
        if "quantifier" in decl and decl["quantifier"] not in QUANTIFIERS:
            return None
        return decl

    def null_group(self, name: str) -> str | None:
        """The to-many relationship or list field that a ``null`` filter on ``name`` tests, its own or
        that of a path through it (§6.1), or ``None``. A segment is followed through to-one relationships
        into their target; a to-many relationship (``relations``, ``"many": true``) or a list field (an
        array in ``schemas.item``) ends the group. Without either, a dotless filter declared with
        ``quantifier`` is itself a list or a relationship."""
        parts = name.split(".")
        decl: Mapping[str, Any] | None = self.declaration
        for i, seg in enumerate(parts):
            if decl is None:
                break
            r, target = self._relation_target(decl, seg)
            if r is not None:
                if r.get("many") is True:
                    return ".".join(parts[: i + 1])
                decl = target
                continue
            schema = (decl.get("schemas") or {}).get("item")
            prop = (schema.get("properties") or {}).get(seg) if isinstance(schema, Mapping) else None
            t = prop.get("type") if isinstance(prop, Mapping) else None
            if t == "array" or (isinstance(t, list) and "array" in t):
                return ".".join(parts[: i + 1])
            break
        d = self.filter_declaration(name)
        return name if len(parts) == 1 and d is not None and "quantifier" in d else None

    def filter_ops(self, name: str) -> list[str]:
        """The operators of a filter this client uses (§6.1): an operator it does not know is ignored on
        its own (§4.2); on a path declared with ``quantifier`` neither ``ne`` nor ``nin`` exists, and
        ``prefix`` and ``contains`` exist only on strings."""
        decl = self.filter_declaration(name)
        if decl is None:
            return []
        ops = [o for o in decl.get("ops") or [] if o in OPS]
        if "quantifier" in decl:
            ops = [o for o in ops if o not in ("ne", "nin")]
        if decl.get("type") != "string":
            ops = [o for o in ops if o not in ("prefix", "contains")]
        return ops

    def named_declaration(self, name: str) -> Mapping[str, Any] | None:
        """A named filter (§6.1), or ``None`` where it is not declared or its ``type`` is unknown (§4.2)."""
        decl = (self.declaration.get("named_filters") or {}).get(name)
        if not isinstance(decl, Mapping) or ("type" in decl and decl["type"] not in FIELD_TYPES):
            return None
        return decl

    @property
    def default_filters(self) -> list[str]:
        """The named filters that apply to every list request unless it turns them off (§6.1): switches
        declared ``"default": true``."""
        names = self.declaration.get("named_filters") or {}
        return [n for n in names if (d := self.named_declaration(n)) is not None and not d.get("type") and d.get("default") is True]

    @property
    def required_filters(self) -> list[str]:
        """The parameters a list request must carry (§6.1): ``filter[name]`` of each filter or named
        filter declared ``"required": true``."""
        out = [f"filter[{n}]" for n in self.declaration.get("filter") or {}
               if (d := self.filter_declaration(n)) is not None and d.get("required") is True]
        out += [f"filter[{n}]" for n in self.declaration.get("named_filters") or {}
                if (d := self.named_declaration(n)) is not None and d.get("required") is True]
        return out

    def copy_query(self) -> list[tuple[str, str]]:
        """The query of the request for a full copy (§5.2): every default filter turned off and no
        other filter. It exists only where the collection supports ``GET`` and requires no filter."""
        self._check_method("GET", False)
        if self.required_filters:
            raise E.local_error(E.InvalidParameter, f"{self.name} requires {', '.join(self.required_filters)}, so it has no full copy (§5.2, §6.1).",
                                parameter=self.required_filters[0])
        return [(f"filter[{n}]", "false") for n in self.default_filters]

    def _relation_target(self, decl: Mapping[str, Any], rel: str) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
        r = (decl.get("relations") or {}).get(rel)
        if not isinstance(r, Mapping):
            return None, None
        to = r.get("to")
        target = self._api.declarations.get(to) if isinstance(to, str) else None
        return r, target

    def _check_include(self, paths: list[str]) -> None:
        if not paths:
            raise self._invalid("include", "include cannot be empty.")
        if not self._has("relations"):
            raise self._unsupported("include", "the server does not announce the relations profile (§9).")
        depth = self.declaration.get("include_depth") or 1
        for path in paths:
            parts = path.split(".")
            if len(parts) > depth:
                raise self._unsupported("include", f"'{path}' goes beyond include_depth ({depth}) (§9).")
            decl: Mapping[str, Any] | None = self.declaration
            for i, rel in enumerate(parts):
                r, target = self._relation_target(decl or {}, rel)
                if r is None or not r.get("include"):
                    raise self._unsupported("include", f"'{rel}' is not an includable relation (§9).")
                if i < len(parts) - 1 and target is None:
                    raise self._unsupported("include", f"cannot follow '{path}' beyond '{rel}'.")
                decl = target

    def _check_fields(self, fields: list[str], include: list[str]) -> None:
        self._need_query("fields")
        if not fields:
            raise self._invalid("fields", "fields cannot be empty.")
        maps = self.declaration.get("language_maps") or {}
        rels = self.declaration.get("relations") or {}
        included = {".".join(p.split(".")[: i + 1]) for p in include for i in range(len(p.split(".")))}
        schema = (self.declaration.get("schemas") or {}).get("item")
        props = schema.get("properties") if isinstance(schema, Mapping) else None
        for f in fields:
            head, _, rest = f.partition(".")
            if rest:
                if head in maps:
                    raise self._invalid("fields", f"'{f}' goes into a language map; ask for the whole '{head}' (§6.3).")
                if head in rels:
                    if f.rsplit(".", 1)[0] not in included:
                        raise self._invalid("fields", f"'{f}' belongs to a relation that is not in include (§9).")
                    continue
                raise self._invalid("fields", f"'{f}' is not a field of {self.name} (§6.3).")
            if isinstance(props, Mapping) and f != "id" and f not in props and f not in maps:
                raise self._invalid("fields", f"'{f}' is not a field of {self.name}; there are: {', '.join(props)} (§6.3).")

    # Peticiones
    def list_request(self, **params: Any) -> Request:
        self._check_method("GET", False)
        return self._api.build("GET", self._url(), query=self.read_query(**params))

    def get_request(self, id: Any, fields: Any = None, include: Any = None) -> Request:
        self._check_method("GET", True)
        url = self._item_url(id)
        return self._api.build("GET", url, query=self.read_query(fields=fields, include=include, item=True))

    def _key_header(self, key: IdempotencyKey, applies: bool) -> dict[str, str]:
        return key_header(self._api, key, applies)

    def create_request(self, data: Any, idempotency_key: IdempotencyKey) -> Request:
        self._check_method("POST", False)
        id_from = self.id_from
        if self._api.validate:
            up = self.upload_declaration
            if up is not None and not self.creates_from_json:
                err = E.local_error(E.UnsupportedMediaType, f"{self.name} declares upload without create, so it takes only files: "
                                    "use upload() (§5.1).", status=415)
                err.headers["accept-post"] = ", ".join(r for r in up["media_types"] if isinstance(r, str))
                raise err
            if not isinstance(data, Mapping):
                raise TypeError("create() expects an object (dict) with the item.")
            if "id" in data and id_from not in ("client", "either"):
                why = ("The server assigns the id: do not send it in create() (§5.1)." if id_from == "server" else
                       f"id_from has an unknown value ({id_from}): no id is sent (§5.1).")
                raise E.local_error(E.Invalid, why, status=422, errors=[{"pointer": "/id", "code": "read_only"}])
            if "id" not in data and id_from == "client":
                raise E.local_error(E.Invalid, f"{self.name} requires the client to choose the id (id_from: client, §5.1).",
                                    status=422, errors=[{"pointer": "/id", "code": "required"}])
        h = self._key_header(idempotency_key, True)
        # §5.7: a POST with a client-chosen id cannot create two items.
        client_id = isinstance(data, Mapping) and "id" in data and id_from in ("client", "either")
        req = self._api.build("POST", self._url(), body=data, has_body=True, headers=h, retry_safe=bool(h) or client_id)
        if client_id and not self.reusable_ids:
            req.key_ttl = None  # §5.7: a client-chosen id is the protection that does not expire
        return req

    @property
    def reusable_ids(self) -> bool:
        """``"reusable_ids": true`` (§5.1): an ``id`` can come back after its item is deleted, so a
        ``PATCH`` or ``DELETE`` is safe to repeat only with ``If-Match`` (§5.7)."""
        return self.declaration.get("reusable_ids") is True

    @property
    def upsert(self) -> bool:
        """``"upsert": true`` (§5.1): a ``PATCH`` to a free ``id`` creates the item (``201``)."""
        return self.declaration.get("upsert") is True

    def update_request(self, id: Any, patch: Any, if_match: Any, if_none_match: str | None = None) -> Request:
        self._check_method("PATCH", True)
        if self._api.validate and not isinstance(patch, Mapping):
            raise TypeError("update() expects an object (dict) with the merge patch (RFC 7396).")
        h = {"if-match": etag_value(if_match)} if if_match is not None else {}
        if if_none_match is not None:
            h["if-none-match"] = etag_value(if_none_match)  # "*": only if it creates (upsert, §5.1)
        return self._api.build("PATCH", self._item_url(id), body=patch, has_body=True,
                               content_type="application/merge-patch+json", headers=h,
                               retry_safe=if_match is not None or not self.reusable_ids)

    def delete_request(self, id: Any, if_match: Any) -> Request:
        self._check_method("DELETE", True)
        h = {"if-match": etag_value(if_match)} if if_match is not None else {}
        return self._api.build("DELETE", self._item_url(id), headers=h, retry_safe=if_match is not None or not self.reusable_ids)

    def upload_request(self, data: bytes, media_type: str, filename: str | None, idempotency_key: IdempotencyKey) -> Request:
        """A ``POST`` that carries the bytes of a file instead of JSON (§5.1)."""
        self._check_method("POST", False)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("upload() expects the bytes of the file.")
        data = bytes(data)
        if self._api.validate:
            up = self.upload_declaration
            if up is None:
                raise E.local_error(E.UnsupportedMediaType, f"{self.name} does not declare upload, so it takes no file (§5.1).", status=415)
            if not any(media_type_matches(media_type, r) for r in up["media_types"] if isinstance(r, str)):
                err = E.local_error(E.UnsupportedMediaType, f"{self.name} takes {', '.join(map(str, up['media_types']))}, not {media_type} (§5.1).",
                                    status=415)
                err.headers["accept-post"] = ", ".join(r for r in up["media_types"] if isinstance(r, str))  # as the server would say it
                raise err
            top = up.get("max_size")
            if isinstance(top, int) and not isinstance(top, bool) and len(data) > top:
                raise E.local_error(E.ContentTooLarge, f"The file has {len(data)} bytes and {self.name} takes up to {top} (§5.1).", status=413)
        h = self._key_header(idempotency_key, True)
        # §5.1: an outer Content-Type of multipart/form-data always selects multipart upload, so a file of
        # that type goes only as the file part of a multipart body, never to a collection without
        # upload.multipart. Only application/json itself selects creation from JSON (a +json type is a
        # file like any other): where the collection also creates items from JSON, a JSON file goes only
        # as the file part of multipart/form-data, which it must declare; where it does not, a JSON file
        # its media_types admit goes as its bytes.
        up = self.upload_declaration
        is_form = media_type_matches(media_type, "multipart/form-data")
        if up is not None and (is_form or (self.creates_from_json and media_type_matches(media_type, "application/json"))):
            if up.get("multipart") is not True:
                why = ("does not declare multipart, so it takes no multipart/form-data body" if is_form else
                       "also creates items from JSON and does not declare multipart, so it cannot take a JSON file")
                raise E.local_error(E.UnsupportedMediaType, f"{self.name} {why} (§5.1).", status=415)
            body, form_type = multipart_body(data, media_type, filename)
            return self._api.build("POST", self._url(), raw=body, content_type=form_type, headers=h,
                                   retry_safe=bool(h.get("idempotency-key")))
        if filename is not None:
            h["content-disposition"] = content_disposition(filename)
        return self._api.build("POST", self._url(), raw=data, content_type=media_type, headers=h, retry_safe=bool(h.get("idempotency-key")))

    def action_request(self, id: Any, name: str, data: Any, idempotency_key: IdempotencyKey,
                       params: Mapping[str, Any] | None = None) -> Request:
        decl = (self.declaration.get("actions") or {}).get(name)
        if isinstance(decl, Mapping) and not usable_action(name, decl, collection=True):
            raise E.local_error(E.NotFound, f"{self.name}:{name} is declared with an unknown on or method, or breaks the rules of "
                                "its reserved name; it is not used (§4.2, §11.1).", status=404)
        if self._api.validate and isinstance(decl, Mapping):
            on = decl.get("on")
            if on == "item" and id is None:
                raise TypeError(f"The action '{name}' is an item action: pass its id.")
            if on == "collection" and id is not None:
                raise TypeError(f"The action '{name}' is a collection action: pass None as the id.")
        target = self._item_url(id) if id is not None else self._url()
        return action_request(self, self.name, self.declaration.get("actions"), target, name, data, idempotency_key, params)


class SingletonCore:
    """A singleton (§5.9): a resource with no collection or ``id``, like ``/me``. It supports ``GET`` and, if it
    declares it, ``PATCH`` with merge patch; with ``query``, ``fields``. Nothing else: no ``POST`` or ``DELETE``
    (they give ``405``), no ``include`` and no other reserved parameters."""

    def __init__(self, api: ApiCore, name: str, decl: Mapping[str, Any], path_vars: Mapping[str, str] | None = None) -> None:
        self._api = api
        self.name = name
        self.declaration: dict[str, Any] = dict(decl)
        self._vars = dict(path_vars or {})

    @property
    def path(self) -> str:
        return str(self.declaration["path"])

    @property
    def parent(self) -> str | None:
        """The collection or singleton it hangs from (§5.9), or ``None``."""
        p = self.declaration.get("parent")
        return p if isinstance(p, str) else None

    @property
    def variables(self) -> list[str]:
        """The variables of its path template, which take the ``id``s of the parent items (§5.9)."""
        return _VAR.findall(self.path)

    def _url(self) -> str:
        def fill(m: re.Match[str]) -> str:
            v = m.group(1)
            if v not in self._vars:
                raise TypeError(f"Missing the path variable '{v}' of {self.path}: use .at({v}=...).")
            return quote(str(self._vars[v]), safe="")
        return self._api.api_path(_VAR.sub(fill, self.path))

    def effects(self, operation: str) -> frozenset[str]:
        """The effects of an action of the singleton, or of a method (§11.1, §4.3, §5.9): without a
        declaration, none for a safe action and all three otherwise; all three with an unknown value;
        a ``PATCH`` has what ``method_effects`` declares for it (its only key), and all three where it
        declares nothing; none for ``GET``."""
        if operation in ("GET", "HEAD"):
            return frozenset()
        if operation == "PATCH":
            declared = self.declaration.get("method_effects")
            return effects_of(declared.get("PATCH") if isinstance(declared, Mapping) else None)
        decl = (self.declaration.get("actions") or {}).get(operation)
        if not isinstance(decl, Mapping):
            raise KeyError(f"The singleton {self.name} does not declare the action '{operation}' (§11.1).")
        return effects_of(decl.get("effects"), safe=is_safe_action(decl))

    def action_request(self, name: str, data: Any, idempotency_key: IdempotencyKey,
                       params: Mapping[str, Any] | None = None) -> Request:
        """An action of the singleton, at ``{path}:{name}`` (§11.1)."""
        decl = (self.declaration.get("actions") or {}).get(name)
        if isinstance(decl, Mapping) and not usable_action(name, decl, collection=False):
            raise E.local_error(E.NotFound, f"{self.name}:{name} is declared with an unknown method, or with on; it is not used (§4.2, §11.1).",
                                status=404)
        return action_request(self, f"singleton {self.name}", self.declaration.get("actions"), self._url(), name, data,
                              idempotency_key, params)

    def _key_header(self, key: IdempotencyKey, applies: bool) -> dict[str, str]:
        return key_header(self._api, key, applies)

    def _check_method(self, method: str) -> None:
        declared = self.declaration.get("methods")
        if self._api.validate and isinstance(declared, list) and method not in declared:
            raise E.local_error(E.MethodNotAllowed, f"The singleton {self.name} does not allow {method}; it allows {', '.join(declared)} (§5.9).",
                                status=405)

    def get_request(self, fields: Any = None) -> Request:
        self._check_method("GET")
        q: list[tuple[str, str]] = []
        if fields is not None:
            fl = _split(fields)
            if self._api.validate:
                if "query" not in self._api.profiles:
                    raise E.local_error(E.UnsupportedParameter, f"{self.name}: the server does not announce query, so it does not support fields (§5.3).",
                                        parameter="fields")
                if not fl:
                    raise E.local_error(E.InvalidParameter, f"{self.name}: fields cannot be empty.", parameter="fields")
            q.append(("fields", ",".join(fl)))
        return self._api.build("GET", self._url(), query=q)

    def update_request(self, patch: Any, if_match: Any, if_none_match: str | None = None) -> Request:
        self._check_method("PATCH")
        if self._api.validate and not isinstance(patch, Mapping):
            raise TypeError("update() expects an object (dict) with the merge patch (RFC 7396).")
        h = {"if-match": etag_value(if_match)} if if_match is not None else {}
        if if_none_match is not None:
            h["if-none-match"] = etag_value(if_none_match)
        return self._api.build("PATCH", self._url(), body=patch, has_body=True,
                               content_type="application/merge-patch+json", headers=h)


class Created(dict):  # type: ignore[type-arg]
    """The item of a ``201`` (§5.1): ``create``, ``upload`` or an ``update`` that created it. ``location``
    is its ``Location``, as an absolute URL, or ``None``. After a replayed ``201`` without body (§7.1),
    it is empty and the item, with its ETag, is read at ``location`` (§7.3)."""

    location: str | None = None


def written(resp: Response, body: Any, root: str) -> Item:
    """The result of a write that answers with the item: ``_etag`` from the header (§7.2) and, on a
    ``201``, its ``Location``, which is kept also when the body is empty."""
    data = body if isinstance(body, dict) else {}
    if resp.status == 201:
        data = Created(data)
        at = resp.headers.get("location")
        data.location = urljoin(resp.url or root, at) if at else None
    return with_etag(resp, data)


def with_etag(resp: Response, data: Any) -> Any:
    """§7.2: the complete item without content encoding carries its item ETag in the
    header, not in ``_etag``. It is copied into ``_etag`` so that one can write with ``if_match=item``."""
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
            raise E.RestepError(200, {"detail": "The collection response has no items (§5.2)."}, local=True)
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


# ── Shared by collections and singletons ─────────────────────────────────────
INHERIT_MEMBERS = frozenset({"own", "from", "default", "combine", "switch", "description"})  # §11.6


def _inherited_from(frm: Any) -> Any:
    # §11.6: a from this client does not know is "hidden".
    if frm in ("default", "hidden"):
        return frm
    if isinstance(frm, Mapping) and isinstance(frm.get("href"), str):
        return {"href": frm["href"]}
    return "hidden"


def effects_of(value: Any, *, safe: bool = False) -> frozenset[str]:
    """§11.1: the declared effects. Without a declaration, none for a safe action and all three for
    an unsafe operation; with a value this client does not know, all three for any operation."""
    if value is None and safe:
        return frozenset()
    if isinstance(value, list) and all(isinstance(v, str) and v in EFFECTS for v in value):
        return frozenset(value)
    return EFFECTS


def local_to_instant(value: str | _dt.datetime, zone: str) -> _dt.datetime:
    """The instant (in UTC) that a local date-time denotes in an IANA time zone, as §3 asks (RFC 5545,
    3.3.5; RFC 8984, 1.4.5): a local time that occurs twice is the first of them, and one in a gap
    takes the offset from before the gap. Python's ``fold=0`` does exactly that (PEP 495). A fraction
    beyond microseconds is truncated."""
    from zoneinfo import ZoneInfo
    if isinstance(value, str):
        m = _LOCALDATETIME.fullmatch(value)
        if not m:
            raise ValueError(f"{value!r} is not a local date-time (§3).")
        frac = (m.group(1) or ".")[1:]
        local = _dt.datetime.fromisoformat(value[:19].upper()).replace(microsecond=int((frac + "000000")[:6]))
    elif isinstance(value, _dt.datetime) and value.tzinfo is None:
        local = value
    else:
        raise TypeError("A local date-time is a string or a naive datetime (§3).")
    return local.replace(tzinfo=ZoneInfo(zone), fold=0).astimezone(_dt.timezone.utc)


def media_type_matches(media_type: str, media_range: str) -> bool:
    """Whether a media type is one ``upload.media_types`` declares; ``*`` matches any subtype (§5.1)."""
    have = media_type.split(";")[0].strip().lower()
    want = media_range.split(";")[0].strip().lower()
    if want == "*/*":
        return "/" in have
    if want.endswith("/*"):
        return have.split("/")[0] == want[:-2]
    return have == want


def multipart_body(data: bytes, media_type: str, filename: str | None) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body (§5.1, RFC 7578) with exactly one part, ``file``, that holds the
    bytes with their own ``Content-Type`` and a filename (in UTF-8, with ``"``, CR, LF and ``\\``
    escaped as ``%XX``, as section 4.2 allows): the original name, or the generic ``file`` when there
    is none, because the client MUST send one (§5.1). Returns the body and its media type."""
    boundary = f"restep-{uuid.uuid4().hex}"
    name = '; filename="' + re.sub(r'["\r\n\\]', lambda m: quote(m.group(0), safe=""), "file" if filename is None else filename) + '"'
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"{name}\r\n"
            f"Content-Type: {media_type}\r\n\r\n").encode("utf-8")
    return head + data + f"\r\n--{boundary}--\r\n".encode("ascii"), f"multipart/form-data; boundary={boundary}"


def content_disposition(filename: str) -> str:
    """``Content-Disposition`` with the file's original name (§5.1, RFC 6266): ``filename`` in ASCII,
    and ``filename*`` in UTF-8 when the name needs it."""
    ascii_name = "".join(c if 0x20 <= ord(c) < 0x7F and c not in '"\\' else "_" for c in filename)
    out = f'attachment; filename="{ascii_name}"'
    if ascii_name != filename:
        out += "; filename*=UTF-8''" + quote(filename, safe="")
    return out


def key_header(api: ApiCore, key: IdempotencyKey, applies: bool) -> dict[str, str]:
    # §7.1: with "anonymous": false, a request without credentials carries no key: the automatic one is
    # not set, and an explicit one would give 400 (except on an idempotent action, which ignores it).
    anonymous_refused = not api.anonymous_keys and not api.has_credentials
    if key is AUTO:
        key = str(uuid.uuid4()) if applies and "writes" in api.profiles and not anonymous_refused else None
    if isinstance(key, str) and anonymous_refused:
        if not applies:
            return {}
        if api.validate:
            raise E.local_error(E.BadRequest, "The server does not accept Idempotency-Key without credentials "
                                '("anonymous": false, §7.1): pass idempotency_key=None or a token.',
                                errors=[{"header": "Idempotency-Key", "code": "unknown"}])
    return {"idempotency-key": sf_string(key)} if isinstance(key, str) else {}


def usable_action(name: str, decl: Mapping[str, Any], *, collection: bool) -> bool:
    """Whether a declared action can be used (§4.2, §11.1): a known method and, on a collection, a
    known ``on``; a singleton's action carries no ``on``. A collection's action named ``changes`` is
    reserved for sync, and its ``new`` is only the safe ``GET`` collection action."""
    if decl.get("method", "POST") not in ("GET", "POST"):
        return False
    if not collection:
        return "on" not in decl
    if decl.get("on") not in ("item", "collection") or name in RESERVED_ACTIONS:
        return False
    return name != "new" or (decl.get("on") == "collection" and decl.get("safe") is True and decl.get("method") == "GET")


def is_safe_action(decl: Any) -> bool:
    """§11.1: an action declared ``"safe": true`` must also be idempotent; one that is not is treated as unsafe."""
    return isinstance(decl, Mapping) and decl.get("safe") is True and decl.get("idempotent") is True


def action_params(decl: Mapping[str, Any], params: Mapping[str, Any] | None, validate: bool, label: str) -> list[tuple[str, str]]:
    """The query of a ``GET`` action from its declared ``params`` (§11.1): a list parameter goes as
    values separated by commas, which no value can contain; a scalar ``string`` is not split. An
    action without ``params`` is one declared with ``"params": {}``: it takes no query parameters."""
    declared = decl.get("params", {})
    q: list[tuple[str, str]] = []

    def plain(v: Any) -> str:
        return ",".join(map(_plain, v)) if isinstance(v, (list, tuple, set, frozenset)) else _plain(v)

    for k, v in (params or {}).items():
        if v is None:
            continue
        pd = declared.get(k) if isinstance(declared, Mapping) else None
        if not isinstance(pd, Mapping) or pd.get("type") not in FIELD_TYPES:
            if validate and isinstance(declared, Mapping):
                offered = ", ".join(declared) or "none"
                raise E.local_error(E.UnsupportedParameter, f"{label}: the action does not declare the parameter '{k}'; it declares: {offered} (§11.1).",
                                    parameter=k)
            q.append((k, plain(v)))
            continue
        if not validate:
            q.append((k, plain(v)))
        elif pd.get("list") is True:
            values = list(v) if isinstance(v, (list, tuple, set, frozenset)) else [v]
            texts = [format_filter_value(x, pd, k) for x in values]
            if not texts or texts == [""]:
                raise E.local_error(E.InvalidParameter, f"{label}: the list '{k}' cannot be empty (§11.1).", parameter=k)
            if any("," in t for t in texts):
                raise E.local_error(E.InvalidParameter, f"{label}: the values of '{k}' cannot contain commas; send the input "
                                    "in the body of a safe POST action instead (§11.1).", parameter=k)
            q.append((k, ",".join(texts)))
        else:
            q.append((k, format_filter_value(v, pd, k)))
    if validate and isinstance(declared, Mapping):
        sent = {k for k, _ in q}
        for k, pd in declared.items():
            if isinstance(pd, Mapping) and pd.get("required") is True and k not in sent:
                raise E.local_error(E.InvalidParameter, f"{label}: the parameter '{k}' is required (§11.1).", parameter=k)
    return q


def action_request(owner: Any, label: str, actions: Any, target: str, name: str, data: Any,
                   idempotency_key: IdempotencyKey, params: Mapping[str, Any] | None) -> Request:
    """The request of an action at ``{target}:{name}`` (§11.1)."""
    api: ApiCore = owner._api
    if not _ACTION.fullmatch(name):
        raise ValueError(f"'{name}' is not an action name (§3, §11.1).")
    decl = actions.get(name) if isinstance(actions, Mapping) else None
    if api.validate:
        if "actions" not in api.extensions:
            raise E.local_error(E.NotFound, "The server does not announce the actions extension (§11.1).", status=404)
        if not isinstance(decl, Mapping):
            offered = ", ".join(actions or {}) if isinstance(actions, Mapping) else ""
            raise E.local_error(E.NotFound, f"{label} does not declare the action '{name}'; it declares: {offered or 'none'} (§11.1).", status=404)
    decl = decl if isinstance(decl, Mapping) else {}
    url = f"{target}:{name}"
    if decl.get("method") == "GET":
        if data is not None:
            raise TypeError(f"The action '{name}' uses GET and carries no body (§11.1).")
        return api.build("GET", url, query=action_params(decl, params, api.validate, f"{label}:{name}"))
    if params:
        raise TypeError(f"The action '{name}' uses POST: its input goes in the body, not in params (§11.1).")
    idempotent = decl.get("idempotent") is True
    h = owner._key_header(idempotency_key, not idempotent)
    req = api.build("POST", url, body=data, headers=h, retry_safe=idempotent or bool(h))
    if idempotent:
        req.key_ttl = None  # repeating it is safe without the key (§5.7)
    return req


@dataclass
class ActionResult:
    """What an action answered (§11.1): its status, body, ``Location`` (a ``201``) and
    ``Content-Location``. ``item`` is the item's new representation, with ``_etag``, when the response
    carried it with ``Content-Location`` (§7.2); ``stale`` says that a stored copy of the item must be
    revalidated before it is used again: an unsafe action on it succeeded without giving that copy. A
    replay (§7.1) whose body is not the item's current representation carries no ``Content-Location``,
    so its body is never taken as the new copy, and the stored one is stale."""
    status: int
    body: Any
    location: str | None = None
    content_location: str | None = None
    item: dict[str, Any] | None = None
    stale: bool = False


def action_result(api: ApiCore, resp: Response, body: Any, item_url: str | None, safe: bool) -> ActionResult:
    cl = resp.headers.get("content-location")
    item = None
    if item_url is not None and cl and resp.status == 200 and isinstance(body, dict):
        from urllib.parse import unquote
        if unquote(urlsplit(urljoin(api.root, cl)).path) == unquote(urlsplit(item_url).path):
            item = with_etag(resp, body)
    stale = item_url is not None and not safe and 200 <= resp.status < 300 and item is None
    return ActionResult(resp.status, body, resp.headers.get("location"), cl, item, stale)
