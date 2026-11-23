"""Pruebas de restep-py. Desde restep/:  python3 tools/restep-py/test.py

Arranca el servidor de referencia en 8890 (y con RESTEP_TOKEN en 8891), contra el que se prueban
también subcolecciones, include, relaciones con href y polimórficas y acciones (clase Relations), y
dos servidores de pega en Python (8892 y 8893) para lo que la referencia no tiene o no provoca:
acciones con valores desconocidos, reintentos, service-desc y un servidor solo core.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RESTEP = HERE.parents[1]
sys.path.insert(0, str(HERE))

import restep  # noqa: E402
import restep.aio  # noqa: E402

REF, REF_TOKEN, MOCK, MOCK2 = 8890, 8891, 8892, 8893
TOKEN = "secreto-de-prueba"
procs: list[subprocess.Popen[bytes]] = []
servers: list[ThreadingHTTPServer] = []


def wait_port(port: int, seconds: float = 10) -> None:
    end = time.time() + seconds
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"El puerto {port} no se abre.")


def start_ref(port: int, token: str | None = None) -> None:
    env = {**os.environ, "PORT": str(port)}
    if token:
        env["RESTEP_TOKEN"] = token
    procs.append(subprocess.Popen(["node", str(HERE / "vendor/restep-server.mjs")], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    wait_port(port)


# ── Servidor de pega con acciones, subcolecciones y fallos a demanda ─────────
AT = "2026-12-01T00:00:00Z"
MOCK_DISCOVERY = {
    "restep": "1.0-draft.6", "profiles": ["core", "query", "writes"], "extensions": ["actions"],
    "deprecations": [{"kind": "version", "announced": AT}, {"kind": "galaxia", "announced": AT}],
    "collections": {
        "orders": {"path": "/orders", "methods": ["GET"], "item_methods": ["GET"],
                   "actions": {"cancel": {"on": "item", "safe": False, "idempotent": False},
                               "preview": {"on": "item", "safe": True, "idempotent": True, "method": "GET"},
                               "import": {"on": "collection", "safe": False, "idempotent": True},
                               "zap": {"on": "item", "safe": False, "idempotent": False, "method": "PUT"},
                               "weird": {"on": "everywhere", "safe": False, "idempotent": False}}},
        "notes": {"path": "/notes", "methods": ["GET", "POST"], "item_methods": ["GET"]},
        "books": {"path": "/books"},
        "reviews": {"path": "/books/{book_id}/reviews", "parent": "books", "sort": ["id"], "max_limit": 10},
        "old": {"path": "/old", "deprecations": [{"kind": "field", "name": "x", "announced": AT}, {"kind": "nada", "announced": AT}]},
        "events": {"path": "/events", "count": "aproximado",
                   "filter": {"at": {"type": "datetime", "ops": ["ge"]}, "mood": {"type": "vibra", "ops": ["eq"]}},
                   "collations": {"title": "es-u-co-trad", "name": 7, "code": "codepoint"}},
        "tags": {"path": "/tags", "id_from": "client"},
        "slugs": {"path": "/slugs", "id_from": "magia"},
    },
    "singletons": {
        "me": {"path": "/me", "methods": ["GET", "PATCH"]},
        "templated": {"path": "/me/{x}"},
        "child": {"path": "/kid", "parent": "me"},
    },
}
ANON_DISCOVERY = {"restep": "1.0-draft.6", "profiles": ["core", "writes"], "idempotency": {"ttl": 86400, "anonymous": False},
                  "collections": {"notes": {"path": "/notes"}}}


class Mock(BaseHTTPRequestHandler):
    log: list[dict[str, Any]] = []
    fails: dict[str, list[tuple[int, dict[str, str]]]] = {}  # "MÉTODO ruta" → respuestas de error pendientes
    conflict = False  # un 409 pendiente da conflict en vez de request_in_progress

    def log_message(self, *args: Any) -> None:
        pass

    def reply(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        ctype = "application/problem+json" if status >= 400 else "application/json"
        if data:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        Mock.log.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body})
        key = f"{self.command} {self.path.split('?')[0]}"
        pending = Mock.fails.get(key)
        if pending:
            status, headers = pending.pop(0)
            code = {409: "conflict" if Mock.conflict else "request_in_progress", 429: "rate_limited", 503: "unavailable"}.get(status, "internal")
            return self.reply(status, {"type": "about:blank", "title": code, "status": status, "code": code}, headers)
        path = self.path.split("?")[0]
        if path == "/":
            return self.reply(200, MOCK_DISCOVERY)
        if path == "/anon/":
            return self.reply(200, ANON_DISCOVERY)
        if path == "/anon/notes" and self.command == "POST":
            return self.reply(201, {"id": "1"}, {"Location": "/anon/notes/1"})
        if path == "/me":
            return self.reply(200, {"name": "Ana"}, {"ETag": '"me-1"'})
        if path.startswith("/orders/") and path.endswith(":cancel"):
            return self.reply(200, {"id": path[8:-7], "status": "cancelled"})
        if path.endswith(":preview"):
            return self.reply(200, {"preview": True})
        if path == "/orders:import":
            return self.reply(204)
        if path == "/old":
            return self.reply(200, {"items": [], "next": None}, {"Deprecation": "@1798761600", "Sunset": "Thu, 01 Jul 2027 00:00:00 GMT"})
        if path in ("/orders", "/books/7/reviews"):
            return self.reply(200, {"items": [{"id": "1"}], "next": None})
        if path == "/notes" and self.command == "POST":
            return self.reply(201, {"id": "1", **json.loads(body or b"{}")}, {"Location": "/notes/1"})
        return self.reply(404, {"type": "about:blank", "title": "No", "status": 404, "code": "not_found", "detail": "no existe"})

    do_GET = do_POST = do_PATCH = do_DELETE = handle_any


class Mock2(BaseHTTPRequestHandler):
    """/a/: la raíz no es el descubrimiento y lo enlaza con service-desc. /b/: un servidor solo core."""

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        headers: dict[str, str] = {}
        if path == "/a/":
            body: Any = {"hola": "esto no es Restep"}
            headers["Link"] = '</docs/openapi.yaml>; rel="service-desc"; type="application/yaml", </a/restep.json>; rel="service-desc"; type="application/json"'
        elif path == "/a/restep.json":
            body = {"restep": "1.0", "profiles": ["core"], "collections": {"things": {"path": "/things"}}}
        elif path == "/b/":
            body = {"hola": "sin descubrimiento"}
        elif path == "/v2/":
            body = {"restep": "2.0", "profiles": ["core"], "collections": {}}
        elif path == "/bad/":
            body = {"restep": "1.00", "collections": {}}
        elif path == "/d5/":
            body = {"restep": "1.0-draft.5", "profiles": ["core"], "extensions": ["inherit"], "collections": {
                "products": {"path": "/d5/products", "relations": {"category": {"to": "categories"}},
                             "inherit": {"tax_rate": {"own": "tax_rate_own", "from": ["category"]}}}}}
        elif path in ("/b/things", "/a/things"):
            body = {"items": [{"id": "x"}], "next": None}
        else:
            body = {"type": "about:blank", "title": "No", "status": 404, "code": "not_found"}
        data = json.dumps(body).encode()
        self.send_response(404 if "code" in body else 200)
        self.send_header("Content-Type", "application/problem+json" if "code" in body else "application/json")
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_mock(port: int, handler: type[BaseHTTPRequestHandler]) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    servers.append(server)


def setUpModule() -> None:
    start_ref(REF)
    start_ref(REF_TOKEN, TOKEN)
    start_mock(MOCK, Mock)
    start_mock(MOCK2, Mock2)


def tearDownModule() -> None:
    for p in procs:
        p.terminate()
        p.wait()
    for s in servers:
        s.shutdown()
        s.server_close()


REF_URL = f"http://localhost:{REF}/"
MOCK_URL = f"http://localhost:{MOCK}/"


# ── Contra el servidor de referencia ─────────────────────────────────────────
class Discovery(unittest.TestCase):
    def test_connect(self) -> None:
        api = restep.connect(REF_URL)
        # Lo que anuncia la referencia del borrador 6: cache es un perfil aparte de writes (§8).
        self.assertTrue({"core", "query", "writes", "cache"} <= api.profiles)
        self.assertEqual(api.profiles, frozenset(api.discovery["profiles"]))
        self.assertEqual((api.version, api.pre_release), ("1.0-draft.6", "draft.6"))
        # Las colecciones se leen del descubrimiento: la referencia va añadiendo (authors, reviews…).
        self.assertEqual(set(api.collections), set(api.discovery["collections"]))
        self.assertTrue({"books", "products", "categories", "authors", "reviews"} <= set(api.collections))
        self.assertIn("books", api)
        self.assertEqual(api["books"].path_template, "/books")
        with self.assertRaises(KeyError):
            api["nada"]

    def test_versions(self) -> None:
        # §1.1: ni otra versión mayor ni algo fuera de la gramática es Restep 1, y tampoco «solo core».
        for path, version in (("v2", "2.0"), ("bad", "1.00")):
            with self.assertRaises(restep.UnsupportedVersion) as cm:
                restep.connect(f"http://localhost:{MOCK2}/{path}/")
            self.assertEqual(cm.exception.version, version)
        self.assertEqual(restep.parse_version("1.0-rc.1"), (1, 0, "rc.1"))
        for bad in ("1", "01.0", "1.0-draft", "1.0-beta.1", "1.0-draft.06", 1.0):
            self.assertIsNone(restep.parse_version(bad), bad)

    def test_experimental_needs_known_version(self) -> None:
        # §1.2: inherit es experimental; con un borrador cuyo texto no implementa, no se usa.
        api = restep.connect(REF_URL)
        self.assertTrue(api.usable("inherit"))
        # El texto de la 1.0 aún no existe: solo el borrador 6 cuenta como implementado.
        self.assertEqual(restep.IMPLEMENTED_VERSIONS, frozenset({"1.0-draft.6"}))
        old = restep.connect(f"http://localhost:{MOCK2}/d5/")
        self.assertIn("inherit", old.extensions)
        self.assertFalse(old.usable("inherit"))
        self.assertTrue(old.usable("core"))
        with self.assertRaises(restep.ExperimentalNotUsable):
            old["products"].inherited({"tax_rate": "1", "_inherited": {}}, "tax_rate")

    def test_singletons(self) -> None:
        api = restep.connect(REF_URL)
        shop = api.singletons["shop"]
        got = shop.get()
        self.assertTrue(got["_etag"].startswith('"'))
        self.assertEqual(set(shop.get(fields="name")) - {"_etag"}, {"name"})
        changed = shop.update({"phone": "+34 900 000 000"}, if_match=got)
        try:
            self.assertEqual(changed["phone"], "+34 900 000 000")
            with self.assertRaises(restep.PreconditionFailed):
                shop.update({"phone": "x"}, if_match=got)
        finally:
            shop.update({"phone": got["phone"]})
        # §5.9: los de plantilla o con parent se ignoran.
        mock = restep.connect(MOCK_URL)
        self.assertEqual(set(mock.singletons), {"me"})
        self.assertEqual(mock.singletons["me"].get(), {"name": "Ana", "_etag": '"me-1"'})

    def test_status(self) -> None:
        self.assertTrue(restep.connect(REF_URL).status())

    def test_language(self) -> None:
        def detail(lang: str) -> str:
            api = restep.connect(REF_URL, language=lang)
            with self.assertRaises(restep.NotFound) as cm:
                api["books"].get("9999")
            return cm.exception.detail or ""
        self.assertNotEqual(detail("es"), detail("en"))


class Reads(unittest.TestCase):
    api: restep.Api

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = restep.connect(REF_URL)

    def test_list_filter_sort_count(self) -> None:
        page = self.api["books"].list(filter={"price": {"le": "20"}, "category": ["novela-negra", "poesia"]},
                                      sort="-rating", limit=2, count=True)
        self.assertEqual(len(page), 2)
        self.assertIsInstance(page.total, restep.Total)
        self.assertFalse(page.total.estimated)
        ratings = [b["rating"] for b in page]
        self.assertEqual(ratings, sorted(ratings, reverse=True))
        everything = [page.items]
        while (page := page.next_page()) is not None:
            everything.append(page.items)
        ids = [b["id"] for p in everything for b in p]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), self.api["books"].count(filter={"price": {"le": "20"}, "category": ["novela-negra", "poesia"]}))

    def test_iter(self) -> None:
        books = self.api["books"]
        all_ids = [b["id"] for b in books.iter(limit=2)]
        self.assertEqual(len(all_ids), books.count())
        self.assertEqual(len(set(all_ids)), len(all_ids))

    def test_typed_values(self) -> None:
        from decimal import Decimal
        books = self.api["books"]
        page = books.list(filter={"price": {"ge": Decimal("15.00")}, "year": {"ge": 2023}, "rating": {"null": False}})
        self.assertTrue(all(Decimal(b["price"]) >= 15 and b["year"] >= 2023 for b in page))

    def test_get_and_fields(self) -> None:
        book = self.api["books"].get("7")
        self.assertTrue(book["_etag"].startswith('"'))
        part = self.api["books"].get("7", fields=["title"])
        self.assertEqual(set(part), {"id", "title", "_etag"})
        self.assertEqual(part["_etag"], book["_etag"])

    def test_language_map_fields(self) -> None:
        products = self.api["products"]
        self.assertIsInstance(products.list(fields="name").items[0]["name"], dict)
        with self.assertRaises(restep.InvalidParameter) as cm:
            products.list(fields="name.es")
        self.assertTrue(cm.exception.local)
        page = products.list(filter={"name.es-mx": "Separador"})
        self.assertEqual(len(page), 1)


class LocalValidation(unittest.TestCase):
    """Lo que el descubrimiento dice que fallaría se detecta antes de enviar, con la misma excepción."""
    api: restep.Api
    raw: restep.Api

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = restep.connect(REF_URL)
        cls.raw = restep.connect(REF_URL, validate=False)

    def same(self, exc: type[restep.RestepError], parameter: str, **params: Any) -> None:
        """La validación local y el servidor dan la misma clase de error y el mismo parámetro."""
        with self.assertRaises(exc) as local:
            self.api["books"].list(**params)
        with self.assertRaises(exc) as server:
            self.raw["books"].list(**params)
        self.assertTrue(local.exception.local)
        self.assertFalse(server.exception.local)
        self.assertEqual(local.exception.parameter, parameter)
        self.assertEqual(server.exception.parameter, parameter)

    def test_named_filters(self) -> None:
        # §6.1: un interruptor va con True y sin valor; uno con type, con un valor de su tipo.
        books = self.api["books"]
        self.assertEqual([b["id"] for b in books.list(named={"featured": True}, filter={"price": {"le": "15"}}).items], ["30"])
        self.assertEqual([b["id"] for b in books.list(named={"search": "NIEBLA"}, sort="id").items], ["50", "7"])
        self.assertEqual(books.count(named={"featured": True}), 2)
        self.assertEqual(len(books.list(named={"featured": False, "search": None}).items), len(books.list().items))
        self.same(restep.InvalidParameter, "filter[featured]", named={"featured": "sí"})
        self.same(restep.InvalidParameter, "filter[search]", named={"search": True})
        self.same(restep.UnsupportedParameter, "filter[cheap]", named={"cheap": True})

    def test_unsupported(self) -> None:
        self.same(restep.UnsupportedParameter, "filter[nope]", filter={"nope": "1"})
        self.same(restep.UnsupportedParameter, "filter[price][ne]", filter={"price": {"ne": "1"}})
        self.same(restep.UnsupportedParameter, "sort", sort="-nope")
        # books ya declara author con include: true; una relación que no existe sigue dando unsupported.
        self.same(restep.UnsupportedParameter, "include", include="publisher")

    def test_invalid(self) -> None:
        self.same(restep.InvalidParameter, "filter[year][ge]", filter={"year": {"ge": "dos mil"}})
        self.same(restep.InvalidParameter, "filter[price][le]", filter={"price": {"le": "barato"}})
        self.same(restep.InvalidParameter, "filter[category]", filter={"category": "teatro"})
        self.same(restep.InvalidParameter, "filter[id][in]", filter={"id": []})
        self.same(restep.InvalidParameter, "limit", limit=500)
        self.same(restep.InvalidParameter, "limit", limit=0)
        self.same(restep.InvalidParameter, "fields", fields=["nope"])

    def test_bad_types_never_leave(self) -> None:
        import datetime as dt
        with self.assertRaises(restep.InvalidParameter):
            self.api["books"].list(filter={"year": {"ge": True}})
        with self.assertRaises(restep.InvalidParameter):
            self.api["books"].list(filter={"year": {"ge": dt.date(2020, 1, 1)}})
        with self.assertRaises(TypeError):
            self.api["books"].list(filter={"rating": None})
        with self.assertRaises(ValueError):
            self.api["books"].get("a/b")

    def test_unknown_discovery_values(self) -> None:
        # §4.2: un filtro con un type desconocido, un count desconocido o una acción con on o method
        # desconocidos se tratan como no declarados.
        api = restep.connect(MOCK_URL)
        with self.assertRaises(restep.UnsupportedParameter) as cm:
            api["events"].list(filter={"mood": "feliz"})
        self.assertTrue(cm.exception.local)
        with self.assertRaises(restep.UnsupportedParameter):
            api["events"].list(count=True)
        for name in ("zap", "weird"):
            with self.assertRaises(restep.NotFound):
                api["orders"].action("5", name)
        # §6.1: una collation que no es una cadena o que no se conoce es "server".
        events = api["events"]
        self.assertEqual([events.collation(f) for f in ("title", "name", "code", "other")], ["server", "server", "codepoint", "codepoint"])
        self.assertEqual(self.api["categories"].collation("name"), "server")
        # §5.5: avisos de retirada con announced; los de un kind desconocido no salen.
        self.assertEqual([(d["where"], d["kind"]) for d in api.deprecations], [("", "version"), ("old", "field")])

    def test_plus_and_media_types(self) -> None:
        # §5.2: un + literal va como %2B (el desfase horario); §3: las escrituras dicen su tipo.
        import datetime as dt
        api = restep.connect(MOCK_URL)
        when = dt.datetime(2026, 11, 21, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        req = api["events"].list_request(filter={"at": {"ge": when}})
        self.assertIn("filter%5Bat%5D%5Bge%5D=2026-11-21T10%3A00%3A00%2B02%3A00", req.url)
        self.assertEqual(self.api["books"].update_request("7", {"x": 1}, None).headers["content-type"], "application/merge-patch+json")
        self.assertEqual(self.api["books"].create_request({"title": "x"}, None).headers["content-type"], "application/json")
        self.assertEqual(api["orders"].action_request("5", "cancel", {"why": "x"}, None).headers["content-type"], "application/json")

    def test_links_and_origins(self) -> None:
        # §3: no se sigue un enlace con otro esquema; §4.3: credenciales y cabeceras propias, solo al origen.
        api = restep.connect(REF_URL, token="t", headers={"X-Tenant": "a"})
        for link in ("file:///etc/passwd", "data:text/plain,hola", "ftp://x.example/"):
            with self.assertRaises(ValueError):
                api._core.build("GET", api._core.url(link))
        other = api._core.build("GET", "https://otro.example/x").headers
        self.assertNotIn("x-tenant", other)
        self.assertNotIn("authorization", other)
        self.assertEqual(api._core.build("GET", api._core.url("/books")).headers["x-tenant"], "a")

    def test_core_only_rejects_query(self) -> None:
        api = restep.connect(f"http://localhost:{MOCK2}/b/")
        self.assertIsNone(api.discovery)
        self.assertEqual(api.profiles, {"core"})
        things = api.collection("things")
        with self.assertRaises(restep.UnsupportedParameter) as cm:
            things.list(limit=5)
        self.assertEqual(cm.exception.parameter, "limit")
        self.assertEqual(things.list().items, [{"id": "x"}])


class Writes(unittest.TestCase):
    api: restep.Api

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = restep.connect(REF_URL)

    def test_crud_with_etags(self) -> None:
        books = self.api["books"]
        book = books.create({"title": "Prueba", "price": "9.90", "category": "poesia"})
        self.assertIn("_etag", book)
        changed = books.update(book["id"], {"price": "10.50"}, if_match=book)
        self.assertEqual(changed["price"], "10.50")
        with self.assertRaises(restep.PreconditionFailed):
            books.update(book["id"], {"price": "11.00"}, if_match=book)  # la ETag ya no vale
        with self.assertRaises(restep.PreconditionFailed):
            books.delete(book["id"], if_match=book["_etag"])
        books.delete(book["id"], if_match=changed)
        with self.assertRaises(restep.NotFound):
            books.get(book["id"])

    def test_idempotency_key(self) -> None:
        books = self.api["books"]
        body = {"title": "Una vez", "price": "5.00", "category": "ensayo"}
        a = books.create(body, idempotency_key="clave-unica-py-1")
        b = books.create(body, idempotency_key="clave-unica-py-1")
        self.assertEqual(a["id"], b["id"])
        with self.assertRaises(restep.IdempotencyMismatch):
            books.create({**body, "price": "6.00"}, idempotency_key="clave-unica-py-1")
        c = books.create(body)  # AUTO: una clave nueva, así que otro elemento
        self.assertNotEqual(a["id"], c["id"])
        books.delete(a["id"])
        books.delete(c["id"])

    def test_invalid_body(self) -> None:
        with self.assertRaises(restep.Invalid) as cm:
            self.api["books"].create({"title": "Sin precio"})
        self.assertTrue(cm.exception.errors)
        self.assertTrue(all("pointer" in e for e in cm.exception.errors))
        with self.assertRaises(restep.Invalid) as cm:
            self.api["books"].create({"id": "1", "title": "x", "price": "1"})
        self.assertTrue(cm.exception.local)

    def test_inherited_is_read_only(self) -> None:
        products = self.api["products"]
        pid = products.list(limit=1).items[0]["id"]
        with self.assertRaises(restep.Invalid) as cm:
            products.update(pid, {"tax_rate": "99"})
        self.assertEqual(cm.exception.errors[0]["code"], "read_only")

    def test_id_from(self) -> None:
        # §5.1: con "either" el cliente puede elegir el id; con "client" debe; con uno desconocido, no lo manda.
        import uuid
        cats = self.api["categories"]
        self.assertEqual(cats.id_from, "either")
        cid = f"py-{uuid.uuid4()}"
        made = cats.create({"id": cid, "name": "Prueba"}, idempotency_key=None)
        try:
            self.assertEqual(made["id"], cid)
            self.assertTrue(cats.create_request({"id": "x", "name": "y"}, None).retry_safe)  # §5.7
        finally:
            cats.delete(cid)
        mock = restep.connect(MOCK_URL)
        with self.assertRaises(restep.Invalid) as cm:
            mock["tags"].create({"name": "x"})
        self.assertEqual(cm.exception.errors[0], {"pointer": "/id", "code": "required"})
        with self.assertRaises(restep.Invalid):
            mock["slugs"].create({"id": "a", "name": "x"})
        with self.assertRaises(restep.Invalid):
            self.api["books"].create({"id": "a", "title": "x", "price": "1"})

    def test_inherited(self) -> None:
        products = self.api["products"]
        item = products.get("1")
        self.assertEqual(products.inherited(item, "tax_rate"), "default")
        self.assertEqual(products.inherited({"_inherited": {}}, "tax_rate"), "own")
        self.assertIsNone(products.inherited({}, "tax_rate"))
        self.assertEqual(products.inherited({"_inherited": {"tax_rate": {"from": {"href": "/categories/2"}}}}, "tax_rate"), {"href": "/categories/2"})
        # §11.6: un from desconocido es "hidden".
        self.assertEqual(products.inherited({"_inherited": {"tax_rate": {"from": "abuela"}}}, "tax_rate"), "hidden")
        self.assertEqual(products.inherited({"_inherited": {"tax_rate": {"from": {"path": "/x"}}}}, "tax_rate"), "hidden")

    def test_method_not_declared(self) -> None:
        api = restep.connect(MOCK_URL)
        with self.assertRaises(restep.MethodNotAllowed) as cm:
            api["orders"].create({"x": 1})
        self.assertTrue(cm.exception.local)
        self.assertEqual(cm.exception.status, 405)


class Relations(unittest.TestCase):
    """Contra la referencia: subcolecciones, include con profundidad, relaciones con href y
    polimórficas, y las tres clases de acción (§5.1, §9, §11.1)."""
    api: restep.Api

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = restep.connect(REF_URL)

    def test_subcollection_with_parent(self) -> None:
        reviews = self.api["reviews"]
        self.assertEqual((reviews.parent, reviews.variables), ("books", ["book_id"]))
        page = reviews.at(book_id=7).list(sort="-id")
        self.assertTrue(page.items and all("_etag" in r for r in page.items))
        with self.assertRaises(restep.NotFound):  # el padre no existe
            reviews.at(book_id="no-existe").list()
        book = self.api["books"].create({"title": "Con reseña", "price": "3.00", "category": "poesia"})
        mine = reviews.at(book_id=book["id"])
        r = mine.create({"rating": 4, "text": "Corta."})
        self.assertEqual(mine.get(r["id"])["rating"], 4)
        self.api["books"].delete(book["id"])
        with self.assertRaises(restep.NotFound):  # se borra con el libro
            mine.get(r["id"])

    def test_include_depth_two(self) -> None:
        item = self.api["products"].list(include="category.parent", limit=5).items
        nested = [p for p in item if isinstance(p["category"].get("parent"), dict)]
        self.assertTrue(nested and "name" in nested[0]["category"]["parent"], item)
        with self.assertRaises(restep.UnsupportedParameter) as cm:  # include_depth es 2
            self.api["products"].list(include="category.parent.parent")
        self.assertTrue(cm.exception.local)

    def test_relation_with_href(self) -> None:
        book = self.api["books"].get("7", include="highlight")
        hl = book["highlight"]
        self.assertEqual(hl["href"], "/books/7/reviews/" + hl["id"])
        self.assertEqual(self.api["reviews"].at(book_id="7").ref(hl["id"]), {"id": hl["id"], "href": hl["href"]})
        self.assertEqual(self.api["reviews"].at(book_id="7").get(hl["id"])["text"], hl["text"])

    def test_polymorphic_relation(self) -> None:
        cats = self.api["categories"].list(include="showcase").items
        shown = [c["showcase"] for c in cats if c.get("showcase")]
        self.assertTrue(shown and {s["type"] for s in shown} <= {"books", "products"}, shown)
        for s in shown:  # el type dice en qué colección está
            self.assertEqual(self.api[s["type"]].get(s["id"])["id"], s["id"])

    def test_actions(self) -> None:
        products = self.api["products"]
        low = products.action(None, "low_stock")  # GET de colección
        self.assertTrue(all(p["stock"] < 5 for p in low["items"]))
        before = products.get("1")["stock"]
        after = products.action("1", "restock", {"quantity": 3})  # POST de elemento con input
        self.assertEqual(after["stock"], before + 3)
        with self.assertRaises(restep.Invalid):
            products.action("1", "restock", {"quantity": 0})
        authors = self.api["authors"]
        a = authors.action(None, "find_or_create", {"name": "Autora de restep-py"})  # idempotente
        b = authors.action(None, "find_or_create", {"name": "Autora de restep-py"})
        self.assertEqual(a["id"], b["id"])
        with self.assertRaises(TypeError):
            authors.action("1", "find_or_create", {"name": "x"})
        cat = self.api["categories"].create({"name": "Cat restep-py", "tax_rate_own": "7"})
        self.assertIsNone(self.api["categories"].action(cat["id"], "inherit_tax")["tax_rate_own"])


class OAuth(unittest.TestCase):
    url = f"http://localhost:{REF_TOKEN}/"

    def test_without_token(self) -> None:
        with self.assertRaises(restep.Unauthenticated) as cm:
            restep.connect(self.url)
        self.assertEqual(cm.exception.resource_metadata, f"http://localhost:{REF_TOKEN}/.well-known/oauth-protected-resource")

    def test_metadata(self) -> None:
        meta = restep.oauth_metadata(self.url)
        self.assertEqual(meta["resource"], self.url)
        self.assertEqual(restep.oauth_metadata_url("https://example.com/api/"),
                         "https://example.com/.well-known/oauth-protected-resource/api/")
        self.assertEqual(restep.oauth_metadata_url("https://api.example.com/"),
                         "https://api.example.com/.well-known/oauth-protected-resource")

    def test_with_token(self) -> None:
        api = restep.connect(self.url, token=TOKEN)
        self.assertIn("oauth", api.extensions)
        self.assertTrue(api["books"].list(limit=1).items)

    def test_token_only_to_api_origin(self) -> None:
        api = restep.connect(self.url, token=TOKEN)
        self.assertIn("authorization", api._core.build("GET", api._core.url("/books")).headers)
        self.assertNotIn("authorization", api._core.build("GET", "https://otro.example/x").headers)


# ── Contra los servidores de pega ────────────────────────────────────────────
class Mocked(unittest.TestCase):
    def setUp(self) -> None:
        Mock.log.clear()
        Mock.fails.clear()

    def test_actions(self) -> None:
        orders = restep.connect(MOCK_URL)["orders"]
        self.assertEqual(orders.action("5", "cancel"), {"id": "5", "status": "cancelled"})
        sent = Mock.log[-1]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/orders/5:cancel"))
        self.assertRegex(sent["headers"].get("Idempotency-Key", ""), r'^"[0-9a-f-]{36}"$')
        self.assertEqual(orders.action("5", "preview"), {"preview": True})
        self.assertEqual(Mock.log[-1]["method"], "GET")
        self.assertIsNone(orders.action(None, "import", {"rows": []}))
        self.assertNotIn("Idempotency-Key", Mock.log[-1]["headers"])  # idempotente: no hace falta
        with self.assertRaises(restep.NotFound):
            orders.action("5", "refund")
        with self.assertRaises(TypeError):
            orders.action(None, "cancel")
        with self.assertRaises(TypeError):
            orders.action("5", "preview", {"x": 1})

    def test_subcollection(self) -> None:
        reviews = restep.connect(MOCK_URL)["reviews"]
        self.assertEqual(reviews.parent, "books")
        with self.assertRaises(TypeError):
            reviews.list()
        self.assertEqual(reviews.at(book_id=7).list(sort="-id").items, [{"id": "1"}])
        self.assertEqual(Mock.log[-1]["path"], "/books/7/reviews?sort=-id")
        # §5.1: un elemento de una subcolección se enlaza por su href.
        self.assertEqual(reviews.at(book_id=7).ref("3"), {"id": "3", "href": "/books/7/reviews/3"})
        self.assertEqual(restep.connect(MOCK_URL)["notes"].ref("3"), {"id": "3"})

    def test_anonymous_keys(self) -> None:
        # §7.1: con "anonymous": false, sin credenciales no se manda Idempotency-Key.
        api = restep.connect(f"http://localhost:{MOCK}/anon/")
        Mock.log.clear()
        api["notes"].create({"text": "x"})
        self.assertNotIn("Idempotency-Key", Mock.log[-1]["headers"])
        with self.assertRaises(restep.BadRequest) as cm:
            api["notes"].create({"text": "x"}, idempotency_key="k-1")
        self.assertTrue(cm.exception.local)
        restep.connect(f"http://localhost:{MOCK}/anon/", token="t")["notes"].create({"text": "x"})
        self.assertIn("Idempotency-Key", Mock.log[-1]["headers"])

    def test_retry_429(self) -> None:
        api = restep.connect(MOCK_URL, retries=2)
        Mock.log.clear()
        Mock.fails["GET /orders"] = [(429, {"Retry-After": "0"}), (429, {"Retry-After": "0"})]
        self.assertEqual(api["orders"].list().items, [{"id": "1"}])
        self.assertEqual(len(Mock.log), 3)

    def test_retry_gives_up(self) -> None:
        api = restep.connect(MOCK_URL, retries=1)
        Mock.log.clear()
        Mock.fails["GET /orders"] = [(503, {"Retry-After": "0"})] * 3
        with self.assertRaises(restep.Unavailable) as cm:
            api["orders"].list()
        self.assertEqual(cm.exception.retry_after, 0)
        self.assertEqual(len(Mock.log), 2)

    def test_503_post_retried_only_with_key(self) -> None:
        api = restep.connect(MOCK_URL, retries=3)
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(503, {"Retry-After": "0"})]
        with self.assertRaises(restep.Unavailable):
            api["notes"].create({"text": "x"}, idempotency_key=None)
        self.assertEqual(len(Mock.log), 1)
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(503, {"Retry-After": "0"})]
        self.assertEqual(api["notes"].create({"text": "x"})["text"], "x")
        keys = {r["headers"]["Idempotency-Key"] for r in Mock.log}
        self.assertEqual(len(Mock.log), 2)
        self.assertEqual(len(keys), 1)  # el reintento lleva la misma clave

    def test_request_in_progress(self) -> None:
        # Sin reintentos: la excepción propia, que no es Conflict, con su Retry-After.
        api = restep.connect(MOCK_URL)
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(409, {"Retry-After": "0"})]
        with self.assertRaises(restep.RequestInProgress) as cm:
            api["notes"].create({"text": "x"})
        self.assertNotIsInstance(cm.exception, restep.Conflict)
        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.retry_after), (409, "request_in_progress", 0))
        # Con reintentos: se repite con la misma clave después de Retry-After.
        api = restep.connect(MOCK_URL, retries=2)
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(409, {"Retry-After": "0"})]
        self.assertEqual(api["notes"].create({"text": "x"})["text"], "x")
        self.assertEqual(len(Mock.log), 2)
        self.assertEqual(len({r["headers"]["Idempotency-Key"] for r in Mock.log}), 1)
        # Un 409 conflict no se reintenta nunca.
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(409, {})]
        Mock.conflict = True
        try:
            with self.assertRaises(restep.Conflict):
                api["notes"].create({"text": "x"})
        finally:
            Mock.conflict = False
        self.assertEqual(len(Mock.log), 1)
        self.assertIsInstance(restep.error_from_problem(409, {"code": "request_in_progress"}), restep.RequestInProgress)

    def test_no_retry_by_default(self) -> None:
        api = restep.connect(MOCK_URL)
        Mock.log.clear()
        Mock.fails["GET /orders"] = [(429, {"Retry-After": "0"})]
        with self.assertRaises(restep.RateLimited):
            api["orders"].list()
        self.assertEqual(len(Mock.log), 1)

    def test_deprecation_warning(self) -> None:
        api = restep.connect(MOCK_URL)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            api["old"].list()
        self.assertTrue(any(issubclass(w.category, restep.RestepDeprecationWarning) for w in caught))

    def test_service_desc(self) -> None:
        api = restep.connect(f"http://localhost:{MOCK2}/a/")
        self.assertEqual(set(api.collections), {"things"})
        self.assertEqual(api["things"].list().items, [{"id": "x"}])

    def test_unknown_code_by_status(self) -> None:
        err = restep.error_from_problem(409, {"code": "algo_nuevo"})
        self.assertIsInstance(err, restep.Conflict)
        self.assertIsInstance(restep.error_from_problem(418, {"code": "teapot"}), restep.ClientError)
        self.assertIsInstance(restep.error_from_problem(502, {"code": "bad_gateway"}), restep.ServerError)
        self.assertIsInstance(restep.error_from_problem(400, {"code": "unsupported_parameter"}), restep.UnsupportedParameter)
        # §5.4: un reason desconocido se trata como su code.
        err = restep.error_from_problem(409, {"code": "conflict", "reason": "sin_existencias"})
        self.assertIsInstance(err, restep.Conflict)
        self.assertEqual(err.reason, "sin_existencias")


# ── Asíncrono ─────────────────────────────────────────────────────────────────
class _FakeHeaders:
    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def multi_items(self) -> list[tuple[str, str]]:
        return self._pairs


class _FakeResponse:
    def __init__(self, status: int, headers: list[tuple[str, str]], content: bytes, url: str) -> None:
        self.status_code, self.headers, self.content, self.url = status, _FakeHeaders(headers), content, url


class FakeAsyncClient:
    """Un cliente con la forma de httpx.AsyncClient hecho con urllib, para probar restep.aio sin httpx."""

    async def request(self, method: str, url: str, headers: dict[str, str], content: bytes | None) -> _FakeResponse:
        def go() -> _FakeResponse:
            req = urllib.request.Request(url, data=content, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req) as f:
                    return _FakeResponse(f.status, list(f.headers.items()), f.read(), url)
            except urllib.error.HTTPError as e:
                with e:
                    return _FakeResponse(e.code, list(e.headers.items()), e.read(), url)
        return await asyncio.to_thread(go)

    async def aclose(self) -> None:
        pass


class Async(unittest.TestCase):
    async def scenario(self, client: Any) -> None:
        api = await restep.aio.connect(REF_URL, language="es", client=client)
        books = api["books"]
        page = await books.list(sort="price", limit=3, count=True)
        self.assertEqual(len(page), 3)
        ids = [b["id"] async for b in books.iter(limit=4)]
        self.assertEqual(len(ids), await books.count())
        book = await books.create({"title": "Asíncrono", "price": "3.00", "category": "infantil"})
        book = await books.update(book["id"], {"price": "3.50"}, if_match=book)
        with self.assertRaises(restep.UnsupportedParameter):
            await books.list(filter={"nope": 1})
        await books.delete(book["id"], if_match=book)
        with self.assertRaises(restep.NotFound):
            await books.get(book["id"])
        self.assertEqual(api.version, "1.0-draft.6")
        shop = await api.singletons["shop"].get()
        self.assertIn("_etag", shop)
        await api.aclose()

    def test_with_fake_client(self) -> None:
        asyncio.run(self.scenario(FakeAsyncClient()))

    @unittest.skipUnless(restep.aio.HAS_HTTPX, "httpx no está instalado")
    def test_with_httpx(self) -> None:
        asyncio.run(self.scenario(None))

    @unittest.skipIf(restep.aio.HAS_HTTPX, "httpx está instalado")
    def test_without_httpx(self) -> None:
        with self.assertRaises(ImportError):
            asyncio.run(restep.aio.connect(REF_URL))


if __name__ == "__main__":
    unittest.main(verbosity=2)
