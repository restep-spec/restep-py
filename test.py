"""Pruebas de restep-py. Desde restep/:  python3 tools/restep-py/test.py

Arranca el servidor de referencia en 8890 (y con RESTEP_TOKEN en 8891), y dos servidores de
pega en Python (8892 y 8893) para lo que el de referencia no tiene: acciones, subcolecciones,
reintentos, service-desc y un servidor solo core.
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
MOCK_DISCOVERY = {
    "restep": "1.0", "profiles": ["core", "query", "writes"], "extensions": ["actions"],
    "collections": {
        "orders": {"path": "/orders", "methods": ["GET"], "item_methods": ["GET"],
                   "actions": {"cancel": {"on": "item", "safe": False, "idempotent": False},
                               "preview": {"on": "item", "safe": True, "idempotent": True, "method": "GET"},
                               "import": {"on": "collection", "safe": False, "idempotent": True}}},
        "notes": {"path": "/notes", "methods": ["GET", "POST"], "item_methods": ["GET"]},
        "reviews": {"path": "/books/{id}/reviews", "sort": ["id"], "max_limit": 10},
        "old": {"path": "/old"},
    },
}


class Mock(BaseHTTPRequestHandler):
    log: list[dict[str, Any]] = []
    fails: dict[str, list[tuple[int, dict[str, str]]]] = {}  # "MÉTODO ruta" → respuestas de error pendientes

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
            code = {429: "rate_limited", 503: "unavailable"}.get(status, "internal")
            return self.reply(status, {"type": "about:blank", "title": code, "status": status, "code": code}, headers)
        path = self.path.split("?")[0]
        if path == "/":
            return self.reply(200, MOCK_DISCOVERY)
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
        self.assertEqual(api.profiles, {"core", "query", "writes"})
        self.assertEqual(set(api.collections), {"books", "products", "categories"})
        self.assertIn("books", api)
        self.assertEqual(api["books"].path_template, "/books")
        with self.assertRaises(KeyError):
            api["nada"]

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

    def test_unsupported(self) -> None:
        self.same(restep.UnsupportedParameter, "filter[nope]", filter={"nope": "1"})
        self.same(restep.UnsupportedParameter, "filter[price][ne]", filter={"price": {"ne": "1"}})
        self.same(restep.UnsupportedParameter, "sort", sort="-nope")
        self.same(restep.UnsupportedParameter, "include", include="author")

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

    def test_method_not_declared(self) -> None:
        api = restep.connect(MOCK_URL)
        with self.assertRaises(restep.MethodNotAllowed) as cm:
            api["orders"].create({"x": 1})
        self.assertTrue(cm.exception.local)
        self.assertEqual(cm.exception.status, 405)


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
        with self.assertRaises(TypeError):
            reviews.list()
        self.assertEqual(reviews.at(id=7).list(sort="-id").items, [{"id": "1"}])
        self.assertEqual(Mock.log[-1]["path"], "/books/7/reviews?sort=-id")

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
