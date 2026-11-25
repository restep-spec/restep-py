"""Tests for restep-py. From restep/:  python3 tools/restep-py/test.py

Starts the reference server (and another with RESTEP_TOKEN), against which it also tests
subcollections, include, relations with href and polymorphic ones, and actions (class Relations), and
two stub servers in Python for what the reference lacks or does not trigger:
actions with unknown values, retries, service-desc and a core-only server.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
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

def free_port() -> int:
    """A free random high port, so parallel runs never clash on a fixed one."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


REF, REF_TOKEN, MOCK, MOCK2 = (free_port() for _ in range(4))
TOKEN = "test-secret"
procs: list[subprocess.Popen[bytes]] = []
servers: list[ThreadingHTTPServer] = []


def wait_port(port: int, seconds: float = 10) -> None:
    end = time.time() + seconds
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"Port {port} does not open.")


def start_ref(port: int, token: str | None = None) -> None:
    env = {**os.environ, "PORT": str(port)}
    if token:
        env["RESTEP_TOKEN"] = token
    server = os.environ.get("RESTEP_SERVER") or str(HERE / "vendor/restep-server.mjs")  # another copy of the reference
    procs.append(subprocess.Popen(["node", server], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    wait_port(port)


# ── Stub server with actions, subcollections and on-demand failures ─────────
AT = "2026-12-01T00:00:00Z"
MOCK_DISCOVERY = {
    "restep": "1.0-draft.7", "profiles": ["core", "query", "writes"], "extensions": ["actions", "inherit"],
    "default_language": "es-es", "idempotency": {"ttl": 3600},
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
        # Draft 7: filters with nin, prefix, contains and quantifier, default and required filters,
        # localized fields, local times, effects, upload, reusable ids, upsert and action params.
        "shelves": {
            "path": "/shelves", "id_from": "client", "reusable_ids": True, "upsert": True,
            "filter": {"tags": {"type": "string", "ops": ["eq", "in", "nin", "ne", "prefix", "future"], "quantifier": "some"},
                       "title": {"type": "string", "ops": ["eq", "in", "nin", "prefix", "contains"]},
                       "reviews.rating": {"type": "integer", "ops": ["ge"], "quantifier": "most"},
                       "length": {"type": "duration", "ops": ["le", "ge", "contains"]},
                       "opens_at": {"type": "localdatetime", "ops": ["ge"]},
                       "zone": {"type": "timezone", "ops": ["eq"]},
                       # Council round 1, decision 1: null on a to-many relationship and a list field,
                       # without quantifier; decision 3: an integer field.
                       "reviews": {"type": "string", "ops": ["null"]},
                       # Council round 4, decision 5a: a path through the to-many relationship.
                       "reviews.id": {"type": "string", "ops": ["null"]},
                       "formats": {"type": "string", "ops": ["null"]},
                       "pages": {"type": "integer", "ops": ["eq", "ge", "in"]},
                       # Council round 3: a number field (decision 7); null alone with "some" (decision 8).
                       "weight": {"type": "number", "ops": ["ge"]},
                       "labels": {"type": "string", "ops": ["null"], "quantifier": "some"}},
            "relations": {"reviews": {"to": "readers", "many": True}},
            "named_filters": {"active": {"default": True, "description": "Not archived."},
                              "featured": {"description": "Chosen by the shop."}},
            "language_maps": {"name_i18n": {}},
            "localized": {"name": {"source": "name_i18n"}, "broken": {"from": "x"}},
            "local_times": {"opens_at": {"zone": "store.time_zone"}, "meets_at": {}},
            "method_effects": {"POST": ["external"]},
            "item_method_effects": {"PATCH": [], "DELETE": ["destroys", "explodes"]},
            "upload": {"media_types": ["image/*", "application/pdf"], "max_size": 10},
            "schemas": {"create": {"type": "object"}},  # it also creates items from JSON (§5.1)
            "actions": {"compare": {"on": "item", "safe": True, "idempotent": True, "method": "GET",
                                    "params": {"with": {"type": "string", "required": True},
                                               "ids": {"type": "integer", "list": True}},
                                    "output": {"type": "object"}},
                        "touch": {"on": "item", "safe": False, "idempotent": True, "effects": ["charges"]},
                        "poke": {"on": "item", "safe": False, "idempotent": False, "effects": ["teleports"]},
                        "new": {"on": "collection", "safe": True, "idempotent": True, "method": "GET"},
                        "changes": {"on": "collection", "safe": True, "idempotent": True, "method": "GET"},
                        "audit": {"on": "item", "safe": True, "idempotent": True, "method": "GET", "effects": ["teleports"]}}},
        "uploads": {"path": "/uploads", "upload": {"media_types": ["image/*", "application/pdf"]}},
        # Council round 3, decisions 1 and 2: upload and create, with multipart.
        "docs": {"path": "/docs", "upload": {"media_types": ["application/json", "text/*"], "multipart": True},
                 "schemas": {"create": {"type": "object"}}},
        # Council round 4, decisions 1a and 4a: only uploads, and application/* admits a JSON file.
        "blobs": {"path": "/blobs", "upload": {"media_types": ["application/*"]}},
        "forms": {"path": "/forms", "actions": {"new": {"on": "item", "safe": True, "idempotent": True, "method": "GET"}}},
        "isbns": {"path": "/isbns", "filter": {"isbn": {"type": "string", "ops": ["eq"], "required": True}}},
        "readers": {"path": "/readers"},
        "products": {"path": "/products", "relations": {"category": {"to": "categories"}},
                     "inherit": {"tax_rate": {"own": "tax_rate_own", "from": ["category"], "x-acme-note": "ok"},
                                 "hours": {"own": "hours_own", "from": ["category"], "combine": "fields"},
                                 "odd": {"own": "odd_own", "from": ["category"], "merge": "deep"}}},
    },
    "singletons": {
        "me": {"path": "/me", "methods": ["GET", "PATCH"], "method_effects": {"PATCH": ["external"], "DELETE": []},
               "actions": {"verify": {"safe": False, "idempotent": False}, "bad": {"on": "item", "safe": False, "idempotent": False}}},
        "templated": {"path": "/me/{x}"},
        "child": {"path": "/kid", "parent": "me"},
        "settings": {"path": "/readers/{reader_id}/settings", "parent": "readers"},
        "orphan": {"path": "/nobody/{x}/settings", "parent": "nobody"},
    },
}
ANON_DISCOVERY = {"restep": "1.0-draft.7", "profiles": ["core", "writes"], "idempotency": {"ttl": 86400, "anonymous": False},
                  "collections": {"notes": {"path": "/notes"}}}


class Mock(BaseHTTPRequestHandler):
    log: list[dict[str, Any]] = []
    fails: dict[str, list[tuple[int, dict[str, str]]]] = {}  # "METHOD path" → pending error responses
    conflict = False  # a pending 409 gives conflict instead of request_in_progress
    plain = False  # pending errors come without a problem+json body, as from an intermediary

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
            code = {408: "request_timeout", 409: "conflict" if Mock.conflict else "request_in_progress", 421: "misdirected_request",
                    425: "too_early", 429: "rate_limited", 502: "bad_gateway", 503: "unavailable", 504: "gateway_timeout"}.get(status, "internal")
            if Mock.plain:
                data = b"Service Unavailable"
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
                return None
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
        if path == "/me:verify":
            return self.reply(204)
        if path == "/readers/7/settings":
            return self.reply(200, {"theme": "dark"}, {"ETag": '"s-1"'})
        if path == "/shelves" and self.command == "GET":
            return self.reply(200, {"items": [{"id": "1"}], "next": None})
        if path == "/shelves" and self.command == "POST":
            return self.reply(201, {"id": "f1", "_content": {"href": "/files/f1", "media_type": self.headers.get("Content-Type"),
                                                            "size": len(body)}}, {"Location": "/shelves/f1"})
        if path == "/shelves/new1" and self.command == "PATCH":
            return self.reply(201, {"id": "new1", **json.loads(body or b"{}")}, {"Location": "/shelves/new1", "ETag": '"n-1"'})
        if path == "/shelves/1:touch":
            return self.reply(200, {"id": "1", "touched": True}, {"Content-Location": "/shelves/1", "ETag": '"t-2"'})
        if path == "/shelves/2:touch":  # a replay of a 200 to an action: no Content-Location or ETag (§7.1)
            return self.reply(200, {"id": "2", "touched": True}, {"Idempotent-Replayed": "true"})
        if path == "/blobs" and self.command == "POST":  # a replayed 201 without body (§7.1)
            return self.reply(201, None, {"Location": "/blobs/b1", "Idempotent-Replayed": "true"})
        if path == "/uploads" and self.command == "POST":
            return self.reply(415, {"type": "about:blank", "title": "Unsupported", "status": 415, "code": "unsupported_media_type"},
                              {"Accept-Post": "image/*, application/pdf"})
        if path == "/shelves/1:poke":
            return self.reply(200, {"poked": True})
        if path == "/shelves/1:compare":
            return self.reply(200, {"fields": []})
        if path == "/shelves:new":
            return self.reply(200, {"values": {"title": ""}, "schema": {"type": "object"}})
        if path == "/isbns":
            return self.reply(200, {"items": [], "next": None})
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
    """/a/: the root is not the discovery document and links it with service-desc. /b/: a core-only server."""

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        headers: dict[str, str] = {}
        if path == "/a/":
            body: Any = {"hello": "this is not Restep"}
            headers["Link"] = '</docs/openapi.yaml>; rel="service-desc"; type="application/yaml", </a/restep.json>; rel="service-desc"; type="application/json"'
        elif path == "/a/restep.json":
            body = {"restep": "1.0", "profiles": ["core"], "collections": {"things": {"path": "/things"}}}
        elif path == "/b/":
            body = {"hello": "no discovery"}
        elif path == "/v2/":
            body = {"restep": "2.0", "profiles": ["core"], "collections": {}}
        elif path == "/bad/":
            body = {"restep": "1.00", "collections": {}}
        elif path == "/d5/":
            body = {"restep": "1.0-draft.5", "profiles": ["core"], "extensions": ["inherit"], "collections": {
                "products": {"path": "/d5/products", "relations": {"category": {"to": "categories"}},
                             "inherit": {"tax_rate": {"own": "tax_rate_own", "from": ["category"]}}}}}
        elif path == "/c/":
            body = {"type": "about:blank", "title": "Not Found", "status": 404, "code": "not_found"}
            headers["Link"] = '</c/v2.json>; rel="service-desc"; type="application/json", </c/restep.json>; rel="service-desc"; type="application/json"'
        elif path == "/c/v2.json":
            body = {"restep": "2.0", "profiles": ["core"], "collections": {"wrong": {"path": "/wrong"}}}
        elif path == "/c/restep.json":
            body = {"restep": "1.0-draft.7", "profiles": ["core"], "collections": {"things": {"path": "/c/things"}}}
        elif path == "/d/":
            body = {"type": "about:blank", "title": "Not Found", "status": 404, "code": "not_found"}
        elif path == "/e/":
            body = {"type": "about:blank", "title": "Unauthorized", "status": 401, "code": "unauthenticated"}
            headers["WWW-Authenticate"] = 'Bearer realm="x"'
        elif path == "/f/":
            body = {"type": "about:blank", "title": "Unavailable", "status": 503, "code": "unavailable"}
        elif path in ("/b/things", "/a/things"):
            body = {"items": [{"id": "x"}], "next": None}
        else:
            body = {"type": "about:blank", "title": "No", "status": 404, "code": "not_found"}
        data = json.dumps(body).encode()
        self.send_response(body.get("status", 404) if "code" in body else 200)
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


# ── Against the reference server ─────────────────────────────────────────────
class Discovery(unittest.TestCase):
    def test_connect(self) -> None:
        api = restep.connect(REF_URL)
        # What the reference announces: cache is a profile of its own, apart from writes (§8).
        self.assertTrue({"core", "query", "writes", "cache"} <= api.profiles)
        self.assertEqual(api.profiles, frozenset(api.discovery["profiles"]))
        # The reference moves from draft to draft: what counts is that version and pre_release agree with it.
        self.assertEqual(api.version, api.discovery["restep"])
        self.assertEqual(api.pre_release, restep.parse_version(api.version)[2])
        # Collections are read from discovery: the reference keeps adding some (authors, reviews…).
        self.assertEqual(set(api.collections), set(api.discovery["collections"]))
        self.assertTrue({"books", "products", "categories", "authors", "reviews"} <= set(api.collections))
        self.assertIn("books", api)
        self.assertEqual(api["books"].path_template, "/books")
        with self.assertRaises(KeyError):
            api["nada"]

    def test_versions(self) -> None:
        # §1.1: neither another major version nor something outside the grammar is Restep 1, nor "core only".
        for path, version in (("v2", "2.0"), ("bad", "1.00")):
            with self.assertRaises(restep.UnsupportedVersion) as cm:
                restep.connect(f"http://localhost:{MOCK2}/{path}/")
            self.assertEqual(cm.exception.version, version)
        self.assertEqual(restep.parse_version("1.0-rc.1"), (1, 0, "rc.1"))
        for bad in ("1", "01.0", "1.0-draft", "1.0-beta.1", "1.0-draft.06", 1.0):
            self.assertIsNone(restep.parse_version(bad), bad)

    def test_experimental_needs_known_version(self) -> None:
        # §1.2: inherit is experimental; with a draft whose text it does not implement, it is not used.
        api = restep.connect(REF_URL)
        self.assertEqual(api.usable("inherit"), api.version in restep.IMPLEMENTED_VERSIONS)
        self.assertTrue(restep.connect(MOCK_URL).usable("inherit"))
        # The text of 1.0 does not exist yet: only draft 7 counts as implemented.
        self.assertEqual(restep.IMPLEMENTED_VERSIONS, frozenset({"1.0-draft.7"}))
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
        # §5.9 (draft 7): a template without parent, or a parent that names nothing, is ignored;
        # a singleton that hangs from a collection or from another singleton is not.
        mock = restep.connect(MOCK_URL)
        self.assertEqual(set(mock.singletons), {"me", "child", "settings"})
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
        # 6.3: fields keeps id and the _ members (_etag, and _actions where the server lists them).
        self.assertEqual({k for k in part if not k.startswith("_")}, {"id", "title"})
        self.assertIn("_etag", part)
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
    """What discovery says would fail is detected before sending, with the same exception."""
    api: restep.Api
    raw: restep.Api

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = restep.connect(REF_URL)
        cls.raw = restep.connect(REF_URL, validate=False)

    def same(self, exc: type[restep.RestepError], parameter: str, **params: Any) -> None:
        """Local validation and the server give the same class of error and the same parameter."""
        with self.assertRaises(exc) as local:
            self.api["books"].list(**params)
        with self.assertRaises(exc) as server:
            self.raw["books"].list(**params)
        self.assertTrue(local.exception.local)
        self.assertFalse(server.exception.local)
        self.assertEqual(local.exception.parameter, parameter)
        self.assertEqual(server.exception.parameter, parameter)

    def test_named_filters(self) -> None:
        # §6.1: a switch goes with True and no value; one with a type, with a value of its type.
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
        # books already declares author with include: true; a relation that does not exist still gives unsupported.
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
        # §4.2: a filter with an unknown type, an unknown count or an action with an unknown on or method
        # are treated as undeclared.
        api = restep.connect(MOCK_URL)
        with self.assertRaises(restep.UnsupportedParameter) as cm:
            api["events"].list(filter={"mood": "feliz"})
        self.assertTrue(cm.exception.local)
        with self.assertRaises(restep.UnsupportedParameter):
            api["events"].list(count=True)
        for name in ("zap", "weird"):
            with self.assertRaises(restep.NotFound):
                api["orders"].action("5", name)
        # §6.1: a collation that is not a string or is not known is "server".
        events = api["events"]
        self.assertEqual([events.collation(f) for f in ("title", "name", "code", "other")], ["server", "server", "codepoint", "codepoint"])
        self.assertEqual(self.api["categories"].collation("name"), "server")
        # §5.5: retirement notices with announced; those with an unknown kind do not appear.
        self.assertEqual([(d["where"], d["kind"]) for d in api.deprecations], [("", "version"), ("old", "field")])

    def test_plus_and_media_types(self) -> None:
        # §5.2: a literal + goes as %2B (the time offset); §3: writes state their type.
        import datetime as dt
        api = restep.connect(MOCK_URL)
        when = dt.datetime(2026, 11, 21, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        req = api["events"].list_request(filter={"at": {"ge": when}})
        self.assertIn("filter%5Bat%5D%5Bge%5D=2026-11-21T10%3A00%3A00%2B02%3A00", req.url)
        self.assertEqual(self.api["books"].update_request("7", {"x": 1}, None).headers["content-type"], "application/merge-patch+json")
        self.assertEqual(self.api["books"].create_request({"title": "x"}, None).headers["content-type"], "application/json")
        self.assertEqual(api["orders"].action_request("5", "cancel", {"why": "x"}, None).headers["content-type"], "application/json")

    def test_links_and_origins(self) -> None:
        # §3: a link with another scheme is not followed; §4.3: credentials and own headers, only to the origin.
        api = restep.connect(REF_URL, token="t", headers={"X-Tenant": "a"})
        for link in ("file:///etc/passwd", "data:text/plain,hello", "ftp://x.example/"):
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
            books.update(book["id"], {"price": "11.00"}, if_match=book)  # the ETag is no longer valid
        with self.assertRaises(restep.PreconditionFailed):
            books.delete(book["id"], if_match=book["_etag"])
        books.delete(book["id"], if_match=changed)
        with self.assertRaises(restep.NotFound):
            books.get(book["id"])

    def test_idempotency_key(self) -> None:
        books = self.api["books"]
        body = {"title": "Una vez", "price": "5.00", "category": "ensayo"}
        a = books.create(body, idempotency_key="unique-key-py-1")
        b = books.create(body, idempotency_key="unique-key-py-1")
        self.assertEqual(a["id"], b["id"])
        with self.assertRaises(restep.IdempotencyMismatch):
            books.create({**body, "price": "6.00"}, idempotency_key="unique-key-py-1")
        c = books.create(body)  # AUTO: a new key, so another item
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
        # §5.1: with "either" the client may choose the id; with "client" it must; with an unknown one, it does not send it.
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
        if not self.api.usable("inherit"):
            # §1.2: the reference declares a draft whose text of inherit this client does not implement.
            with self.assertRaises(restep.ExperimentalNotUsable):
                products.inherited(item, "tax_rate")
            products = restep.connect(MOCK_URL)["products"]
            item = {"_inherited": {"tax_rate": {"from": "default"}}}
        self.assertEqual(products.inherited(item, "tax_rate"), "default")
        self.assertEqual(products.inherited({"_inherited": {}}, "tax_rate"), "own")
        self.assertIsNone(products.inherited({}, "tax_rate"))
        self.assertEqual(products.inherited({"_inherited": {"tax_rate": {"from": {"href": "/categories/2"}}}}, "tax_rate"), {"href": "/categories/2"})
        # §11.6: an unknown from is "hidden".
        self.assertEqual(products.inherited({"_inherited": {"tax_rate": {"from": "abuela"}}}, "tax_rate"), "hidden")
        self.assertEqual(products.inherited({"_inherited": {"tax_rate": {"from": {"path": "/x"}}}}, "tax_rate"), "hidden")

    def test_method_not_declared(self) -> None:
        api = restep.connect(MOCK_URL)
        with self.assertRaises(restep.MethodNotAllowed) as cm:
            api["orders"].create({"x": 1})
        self.assertTrue(cm.exception.local)
        self.assertEqual(cm.exception.status, 405)


class Relations(unittest.TestCase):
    """Against the reference: subcollections, include with depth, relations with href and
    polymorphic ones, and the three kinds of action (§5.1, §9, §11.1)."""
    api: restep.Api

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = restep.connect(REF_URL)

    def test_subcollection_with_parent(self) -> None:
        reviews = self.api["reviews"]
        self.assertEqual((reviews.parent, reviews.variables), ("books", ["book_id"]))
        page = reviews.at(book_id=7).list(sort="-id")
        self.assertTrue(page.items and all("_etag" in r for r in page.items))
        with self.assertRaises(restep.NotFound):  # the parent does not exist
            reviews.at(book_id="no-existe").list()
        book = self.api["books"].create({"title": "Con reseña", "price": "3.00", "category": "poesia"})
        mine = reviews.at(book_id=book["id"])
        r = mine.create({"rating": 4, "text": "Corta."})
        self.assertEqual(mine.get(r["id"])["rating"], 4)
        self.api["books"].delete(book["id"])
        with self.assertRaises(restep.NotFound):  # it is deleted with the book
            mine.get(r["id"])

    def test_include_depth_two(self) -> None:
        item = self.api["products"].list(include="category.parent", limit=5).items
        nested = [p for p in item if isinstance(p["category"].get("parent"), dict)]
        self.assertTrue(nested and "name" in nested[0]["category"]["parent"], item)
        with self.assertRaises(restep.UnsupportedParameter) as cm:  # include_depth is 2
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
        for s in shown:  # the type says which collection it is in
            self.assertEqual(self.api[s["type"]].get(s["id"])["id"], s["id"])

    def test_actions(self) -> None:
        products = self.api["products"]
        low = products.action(None, "low_stock")  # collection GET
        self.assertTrue(all(p["stock"] < 5 for p in low["items"]))
        before = products.get("1")["stock"]
        after = products.action("1", "restock", {"quantity": 3})  # item POST with input
        self.assertEqual(after["stock"], before + 3)
        with self.assertRaises(restep.Invalid):
            products.action("1", "restock", {"quantity": 0})
        authors = self.api["authors"]
        # 11.1 (draft 7): an unsafe collection action answers 201 with Location or 204, never 200.
        name = f"Autora de restep-py {time.time_ns()}"
        a = authors.action(None, "find_or_create", {"name": name}, full=True)  # idempotente
        b = authors.action(None, "find_or_create", {"name": name}, full=True)
        self.assertEqual(a.status, 201)
        self.assertRegex(a.location or "", r"/authors/[^/]+$")
        self.assertIn(b.status, (201, 204))
        if b.status == 201:
            self.assertEqual(b.location, a.location)
        self.assertFalse(a.stale or b.stale)
        with self.assertRaises(TypeError):
            authors.action("1", "find_or_create", {"name": "x"})
        cat = self.api["categories"].create({"name": "Cat restep-py", "tax_rate_own": "7"})
        self.assertIsNone(self.api["categories"].action(cat["id"], "inherit_tax")["tax_rate_own"])


class ReferenceDraft7(unittest.TestCase):
    """Against the reference, what draft 7 adds (skipped while it declares another version)."""
    api: restep.Api

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = restep.connect(REF_URL)
        if cls.api.version != "1.0-draft.7":
            raise unittest.SkipTest(f"the reference declares {cls.api.version}")

    def test_filters_and_copy(self) -> None:
        books = self.api["books"]
        titles = [b["title"] for b in books.list(filter={"title": {"prefix": "La "}}, fields="title")]
        self.assertTrue(titles and all(t.startswith("La ") for t in titles))
        rest = books.list(filter={"title": {"nin": titles}}, fields="title")
        self.assertTrue(all(not b["title"].startswith("La ") for b in rest))
        orders = self.api["orders"]
        self.assertEqual(orders.default_filters, ["active"])
        everything = list(orders.copy())
        self.assertEqual(len(everything), orders.count(named={"active": False}))
        self.assertGreaterEqual(len(everything), orders.count())
        fresh = orders.new()
        self.assertIn("values", fresh)
        self.assertEqual(orders.effects("collect"), {"charges"})
        self.assertEqual(self.api["payments"].effects("POST"), {"charges"})
        self.assertEqual(self.api.default_language, "en")

    def test_localized_upload_reuse_settings_upsert(self) -> None:
        import base64
        genres = self.api["genres"]
        self.assertEqual(genres.localized, {"name": "name_i18n"})
        self.assertTrue(all(g["name"] is None or isinstance(g["name"], str) for g in genres.list()))
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        files = self.api["files"]
        f = files.upload(png, "image/png", filename="pixel.png")
        self.assertEqual(f["_content"]["size"], len(png))
        files.delete(f["id"])
        subs = self.api["subscriptions"].at(reader_id="2")
        taken = {s["id"] for s in subs.list()}
        author = next(a["id"] for a in self.api["authors"].list() if a["id"] not in taken)
        first = subs.create({"id": author, "frequency": "weekly"})
        subs.delete(author, if_match=first)
        again = subs.create({"id": author})
        self.assertNotEqual(again["_etag"], first["_etag"])  # a new incarnation (5.1)
        subs.delete(author, if_match=again)
        settings = self.api.singletons["settings"].at(reader_id="1").get()
        self.assertIsInstance(settings["newsletter"], bool)
        book = self.api["books"].create({"title": "restep-py drafts", "price": "1.00"})
        try:
            draft = self.api["drafts"].at(book_id=book["id"]).update("current", {}, if_none_match="*")
            self.assertEqual(draft["id"], "current")
        finally:
            self.api["books"].delete(book["id"])


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


# ── Against the stub servers ─────────────────────────────────────────────────
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
        self.assertNotIn("Idempotency-Key", Mock.log[-1]["headers"])  # idempotent: not needed
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
        # §5.1: an item of a subcollection is linked by its href.
        self.assertEqual(reviews.at(book_id=7).ref("3"), {"id": "3", "href": "/books/7/reviews/3"})
        self.assertEqual(restep.connect(MOCK_URL)["notes"].ref("3"), {"id": "3"})

    def test_anonymous_keys(self) -> None:
        # §7.1: with "anonymous": false, no Idempotency-Key is sent without credentials.
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
        # §5.7 (draft 7): a 503 with problem+json comes from the API, which never gives it to a request
        # that had an effect, so it is repeated whatever the method; without that body it may come
        # from an intermediary, and a POST without key is not repeated.
        api = restep.connect(MOCK_URL, retries=3)
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(503, {"Retry-After": "0"})]
        self.assertEqual(api["notes"].create({"text": "x"}, idempotency_key=None)["text"], "x")
        self.assertEqual(len(Mock.log), 2)
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(503, {"Retry-After": "0"})]
        Mock.plain = True
        try:
            with self.assertRaises(restep.Unavailable):
                api["notes"].create({"text": "x"}, idempotency_key=None)
        finally:
            Mock.plain = False
        self.assertEqual(len(Mock.log), 1)
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(503, {"Retry-After": "0"})]
        self.assertEqual(api["notes"].create({"text": "x"})["text"], "x")
        keys = {r["headers"]["Idempotency-Key"] for r in Mock.log}
        self.assertEqual(len(Mock.log), 2)
        self.assertEqual(len(keys), 1)  # the retry carries the same key

    def test_request_in_progress(self) -> None:
        # Without retries: its own exception, which is not a Conflict, with its Retry-After.
        api = restep.connect(MOCK_URL)
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(409, {"Retry-After": "0"})]
        with self.assertRaises(restep.RequestInProgress) as cm:
            api["notes"].create({"text": "x"})
        self.assertNotIsInstance(cm.exception, restep.Conflict)
        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.retry_after), (409, "request_in_progress", 0))
        # With retries: it is repeated with the same key after Retry-After.
        api = restep.connect(MOCK_URL, retries=2)
        Mock.log.clear()
        Mock.fails["POST /notes"] = [(409, {"Retry-After": "0"})]
        self.assertEqual(api["notes"].create({"text": "x"})["text"], "x")
        self.assertEqual(len(Mock.log), 2)
        self.assertEqual(len({r["headers"]["Idempotency-Key"] for r in Mock.log}), 1)
        # A 409 conflict is never retried.
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
        # §5.4: an unknown reason is treated as its code.
        err = restep.error_from_problem(409, {"code": "conflict", "reason": "sin_existencias"})
        self.assertIsInstance(err, restep.Conflict)
        self.assertEqual(err.reason, "sin_existencias")


# ── Draft 7 ───────────────────────────────────────────────────────────────────
def query_of(req: Any) -> str:
    from urllib.parse import unquote, urlsplit
    return unquote(urlsplit(req.url).query)


class Draft7(unittest.TestCase):
    """What draft 7 changes for a client (C-numbers of BORRADOR-7.md)."""
    api: restep.Api

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = restep.connect(MOCK_URL)

    def setUp(self) -> None:
        Mock.log.clear()
        Mock.fails.clear()

    def test_codes_match_problem_schema(self) -> None:
        # C-56, C-63: the closed table of §5.4 is the enumeration of problem.json, with the same statuses.
        schema = json.loads((HERE / "schema/problem.json").read_text())
        enum = schema["properties"]["code"]["anyOf"][0]["enum"]
        self.assertEqual(set(restep.CODES), set(enum))
        for rule in schema["allOf"]:
            cond = rule.get("if", {}).get("properties", {}).get("code", {})
            status = rule.get("then", {}).get("properties", {}).get("status", {}).get("const")
            for code in cond.get("enum", [cond.get("const")] if "const" in cond else []):
                self.assertEqual(restep.CODES[code], status, code)
        for status, cls in ((408, restep.RequestTimeout), (421, restep.MisdirectedRequest), (425, restep.TooEarly),
                            (502, restep.BadGateway), (504, restep.GatewayTimeout)):
            err = restep.error_from_problem(status, {"code": restep.code_for_status(status)})
            self.assertIsInstance(err, cls)
        # A status the table does not list has http_ and its digits, handled by its class.
        self.assertEqual(restep.code_for_status(431), "http_431")
        self.assertIsNone(restep.code_for_status(409))
        err = restep.error_from_problem(431, {"code": "http_431"})
        self.assertIs(type(err), restep.ClientError)
        self.assertIs(type(restep.error_from_problem(507, {"code": "http_507"})), restep.ServerError)

    def test_durations(self) -> None:
        # C-15: the grammar of §3, fractions only in the seconds and up to nine digits, any case.
        import datetime as dt
        for good in ("PT1.5S", "PT0.250S", "pt1h30m", "P1W", "P1Y2M3DT4H5M6.123456789S", "P3DT5M"):
            self.assertTrue(restep.is_duration(good), good)
        for bad in ("PT1.5H", "P1Y2D", "PT1H30S", "P1W1D", "PT0.1234567890S", "P", "PT", "-PT1S", "P1.5D"):
            self.assertFalse(restep.is_duration(bad), bad)
        self.assertEqual(restep.duration_seconds("PT1.5S"), restep.duration_seconds("PT1.500S"))
        self.assertEqual(restep.iso_duration(dt.timedelta(hours=1, seconds=5)), "PT1H0M5S")
        self.assertEqual(restep.iso_duration(dt.timedelta(milliseconds=250)), "PT0.25S")
        shelves = self.api["shelves"]
        self.assertEqual(query_of(shelves.list_request(filter={"length": {"le": "PT0.250S"}})),
                         "filter[length][le]=PT0.250S")
        self.assertEqual(query_of(shelves.list_request(filter={"length": {"ge": dt.timedelta(seconds=90)}})), "filter[length][ge]=PT1M30S")
        for bad in ("PT1.5H", "P1Y", "P2M"):
            with self.assertRaises(restep.InvalidParameter):
                shelves.list_request(filter={"length": {"le": bad}})

    def test_local_times(self) -> None:
        # C-15, C-29: localdatetime and timezone filters; conversion with the rules of RFC 5545.
        import datetime as dt
        shelves = self.api["shelves"]
        self.assertEqual(shelves.local_times, {"opens_at": "store.time_zone", "meets_at": None})
        self.assertEqual(query_of(shelves.list_request(filter={"opens_at": {"ge": dt.datetime(2027, 3, 30, 19)}})),
                         "filter[opens_at][ge]=2027-03-30T19:00:00")
        self.assertEqual(query_of(shelves.list_request(filter={"zone": "Europe/Madrid"})), "filter[zone]=Europe/Madrid")
        for bad in ("2027-03-30T19:00:00Z", dt.datetime(2027, 3, 30, 19, tzinfo=dt.timezone.utc)):
            with self.assertRaises(restep.InvalidParameter):
                shelves.list_request(filter={"opens_at": {"ge": bad}})
        utc = dt.timezone.utc
        # A gap takes the offset from before it; a time that occurs twice is the first of them.
        self.assertEqual(restep.local_to_instant("2027-03-28T02:30:00", "Europe/Madrid"), dt.datetime(2027, 3, 28, 1, 30, tzinfo=utc))
        self.assertEqual(restep.local_to_instant("2027-10-31T02:30:00", "Europe/Madrid"), dt.datetime(2027, 10, 31, 0, 30, tzinfo=utc))
        item = {"opens_at": "2027-03-30T19:00:00", "store": {"id": "1", "time_zone": "Europe/Madrid"}, "meets_at": "2027-01-01T10:00:00"}
        self.assertEqual(shelves.instant(item, "opens_at"), dt.datetime(2027, 3, 30, 17, tzinfo=utc))
        with self.assertRaises(LookupError):
            shelves.instant(item, "meets_at")  # a floating local time denotes no instant
        with self.assertRaises(LookupError):
            shelves.instant({"opens_at": "2027-03-30T19:00:00"}, "opens_at")

    def test_default_language_and_localized(self) -> None:
        # C-69: default_language; C-19: a localized field is read-only, the client writes its map.
        self.assertEqual(self.api.default_language, "es-es")
        self.assertIsNone(restep.connect(f"http://localhost:{MOCK2}/a/").default_language)
        shelves = self.api["shelves"]
        self.assertEqual(shelves.localized, {"name": "name_i18n"})
        # A write that carries a localized field is not rejected locally: the server ignores it with the
        # value the item has, or answers 422 read_only (§5.1).
        for req in (shelves.create_request({"id": "s1", "name": "Novela"}, None), shelves.update_request("s1", {"name": "Novela"}, None)):
            self.assertEqual(json.loads(req.body), {**({"id": "s1"} if req.method == "POST" else {}), "name": "Novela"})
        shelves.update_request("s1", {"name_i18n": {"es-es": "Novela"}}, None)

    def test_filter_operators(self) -> None:
        # C-82, C-83, C-80, C-25: nin, prefix and contains; an unknown operator is ignored on its own;
        # no ne or nin under quantifier; an unknown quantifier leaves the filter out.
        shelves = self.api["shelves"]
        self.assertEqual(query_of(shelves.list_request(filter={"title": {"nin": ["a", "b"], "prefix": "La "}})),
                         "filter[title][nin]=a,b&filter[title][prefix]=La ")
        self.assertEqual(shelves.filter_ops("tags"), ["eq", "in", "prefix"])
        self.assertEqual(shelves.filter_ops("length"), ["le", "ge"])
        for cond, exc in (({"tags": {"nin": ["x"]}}, restep.UnsupportedParameter), ({"tags": {"ne": "x"}}, restep.UnsupportedParameter),
                          ({"tags": {"future": "x"}}, restep.UnsupportedParameter), ({"reviews.rating": {"ge": 4}}, restep.UnsupportedParameter),
                          ({"title": {"contains": ""}}, restep.InvalidParameter), ({"title": {"nin": []}}, restep.InvalidParameter),
                          ({"title": {"nin": ["a,b"]}}, restep.InvalidParameter), ({"title": {"in": [""]}}, restep.InvalidParameter)):
            with self.assertRaises(exc, msg=cond) as cm:
                shelves.list_request(filter=cond)
            self.assertTrue(cm.exception.local)

    def test_default_and_required_filters(self) -> None:
        # C-86: false turns a default filter off; a full copy turns every one off and has no other
        # filter; a required filter is required, and its collection has no full copy.
        shelves = self.api["shelves"]
        self.assertEqual(shelves.default_filters, ["active"])
        self.assertEqual(query_of(shelves.list_request(named={"active": False, "featured": False})), "filter[active]=false")
        self.assertEqual(list(shelves.copy(limit=None)), [{"id": "1"}])
        self.assertEqual(Mock.log[-1]["path"], "/shelves?filter%5Bactive%5D=false")
        isbns = self.api["isbns"]
        self.assertEqual(isbns.required_filters, ["filter[isbn]"])
        with self.assertRaises(restep.InvalidParameter) as cm:
            isbns.list()
        self.assertEqual(cm.exception.parameter, "filter[isbn]")
        self.assertEqual(isbns.list(filter={"isbn": "978"}).items, [])
        with self.assertRaises(restep.InvalidParameter):
            list(isbns.copy())

    def test_effects(self) -> None:
        # C-114 (11.1), 4.3: declared effects; without a declaration, or with an unknown value, all three.
        shelves, all3 = self.api["shelves"], frozenset({"destroys", "external", "charges"})
        self.assertEqual(shelves.effects("POST"), {"external"})
        self.assertEqual(shelves.effects("PATCH", item=True), frozenset())
        self.assertEqual(shelves.effects("DELETE", item=True), all3)
        self.assertEqual(self.api["notes"].effects("DELETE", item=True), {"destroys"})
        self.assertEqual(self.api["notes"].effects("PATCH", item=True), all3)
        self.assertEqual(shelves.effects("GET"), frozenset())
        self.assertEqual(shelves.effects("touch"), {"charges"})
        self.assertEqual(shelves.effects("poke"), all3)
        # §11.1 (author's decision): a safe action without effects has none; an unknown value is all three.
        self.assertEqual(shelves.effects("compare"), frozenset())
        self.assertEqual(self.api["orders"].effects("preview"), frozenset())
        self.assertEqual(restep.effects_of(["teleports"], safe=True), all3)
        self.assertEqual(self.api.singletons["me"].effects("verify"), all3)

    def test_actions(self) -> None:
        # C-113: params of a GET action; C-116: new; reserved names; C-114, C-96: the copy after an action.
        shelves = self.api["shelves"]
        self.assertEqual(shelves.action("1", "compare", params={"with": "2", "ids": [3, 4]}), {"fields": []})
        self.assertEqual(Mock.log[-1]["path"], "/shelves/1:compare?with=2&ids=3,4")
        for params, exc in (({}, restep.InvalidParameter), ({"with": "2", "other": "x"}, restep.UnsupportedParameter),
                            ({"with": "2", "ids": []}, restep.InvalidParameter), ({"with": "2", "ids": ["x"]}, restep.InvalidParameter)):
            with self.assertRaises(exc, msg=params) as cm:
                shelves.action("1", "compare", params=params)
            self.assertTrue(cm.exception.local)
        with self.assertRaises(TypeError):
            shelves.action("1", "touch", {"x": 1}, params={"with": "2"})
        self.assertEqual(shelves.new(), {"values": {"title": ""}, "schema": {"type": "object"}})
        self.assertEqual(Mock.log[-1]["path"], "/shelves:new")
        with self.assertRaises(restep.NotFound):
            shelves.action(None, "changes")  # reserved for sync
        with self.assertRaises(restep.NotFound):
            self.api["forms"].action("1", "new")  # new is only the safe GET collection action
        touched = shelves.action("1", "touch", full=True)
        self.assertEqual(touched.item, {"id": "1", "touched": True, "_etag": '"t-2"'})
        self.assertEqual((touched.status, touched.content_location, touched.stale), (200, "/shelves/1", False))
        poked = shelves.action("1", "poke", full=True)
        self.assertEqual((poked.item, poked.stale), (None, True))
        self.assertFalse(shelves.action("1", "compare", params={"with": "2"}, full=True).stale)

    def test_council_round_1(self) -> None:
        # Decisions of the council (round 1) that touch a client: 1, 2, 3, 6, 8, 9 and 12.
        shelves, all3 = self.api["shelves"], frozenset({"destroys", "external", "charges"})
        # 1 (§6.1): null on a to-many relationship or a list field, declared without quantifier.
        self.assertEqual(shelves.filter_ops("reviews"), ["null"])
        self.assertEqual(query_of(shelves.list_request(filter={"reviews": {"null": True}, "formats": {"null": False}})),
                         "filter[reviews][null]=true&filter[formats][null]=false")
        with self.assertRaises(restep.UnsupportedParameter):
            shelves.list_request(filter={"reviews": "3"})
        # 3 (§6.1): 20.0 is the same operand as 20 for an integer field; 20.5 gives invalid_parameter.
        from decimal import Decimal
        self.assertEqual(query_of(shelves.list_request(filter={"pages": {"ge": 20.0, "in": [Decimal("20.0"), "20.0", "2e1", 3]}})),
                         "filter[pages][ge]=20&filter[pages][in]=20,20.0,2e1,3")
        for bad in (20.5, Decimal("20.5"), "20.5", "2e-1", "x"):
            with self.assertRaises(restep.InvalidParameter, msg=bad) as cm:
                shelves.list_request(filter={"pages": bad})
            self.assertTrue(cm.exception.local)
        # 6 (§11.1): a GET action without params takes no query parameters.
        with self.assertRaises(restep.UnsupportedParameter) as cm:
            self.api["orders"].action("1", "preview", params={"x": "1"})
        self.assertEqual((cm.exception.parameter, cm.exception.local), ("x", True))
        self.assertEqual(self.api["orders"].action("1", "preview"), {"preview": True})
        self.assertEqual(Mock.log[-1]["path"], "/orders/1:preview")
        # 8 (§5.9, §4.3): a singleton's PATCH has what method_effects declares, all three without it.
        self.assertEqual(self.api.singletons["me"].effects("PATCH"), {"external"})
        self.assertEqual(self.api.singletons["settings"].effects("PATCH"), all3)
        # 9 (§11.1): an unknown value of effects is all three, also on a safe action.
        self.assertEqual(shelves.effects("audit"), all3)
        # 12 (§7.1): a replay without Content-Location is not the new copy; the stored one is stale.
        replay = shelves.action("2", "touch", full=True)
        self.assertEqual((replay.item, replay.stale, replay.content_location), (None, True, None))
        # 2 (§5.1): a 415 exposes the media types of Accept-Post; so does the one found before sending.
        uploads = self.api["uploads"]
        with self.assertRaises(restep.UnsupportedMediaType) as cm:
            restep.connect(MOCK_URL, validate=False)["uploads"].create({"name": "x"}, idempotency_key=None)
        self.assertEqual((cm.exception.local, cm.exception.accept_post), (False, ["image/*", "application/pdf"]))
        with self.assertRaises(restep.UnsupportedMediaType) as cm:
            uploads.upload(b"x", "text/plain")
        self.assertEqual((cm.exception.local, cm.exception.accept_post), (True, ["image/*", "application/pdf"]))
        with self.assertRaises(restep.UnsupportedMediaType) as cm:
            self.api["notes"].upload(b"x", "image/png")
        self.assertIsNone(cm.exception.accept_post)

    def test_upload(self) -> None:
        # C-48: the bytes of a file, with their media type and name.
        shelves = self.api["shelves"]
        item = shelves.upload(b"\x89PNG....", "image/png", filename="portada ñ.png", idempotency_key=None)
        self.assertEqual(item["_content"], {"href": "/files/f1", "media_type": "image/png", "size": 8})
        sent = Mock.log[-1]
        self.assertEqual((sent["body"], sent["headers"]["Content-Type"]), (b"\x89PNG....", "image/png"))
        self.assertEqual(sent["headers"]["Content-Disposition"], "attachment; filename=\"portada _.png\"; filename*=UTF-8''portada%20%C3%B1.png")
        for data, media, exc in ((b"x", "text/plain", restep.UnsupportedMediaType), (b"x" * 11, "application/pdf", restep.ContentTooLarge)):
            with self.assertRaises(exc) as cm:
                shelves.upload(data, media)
            self.assertTrue(cm.exception.local)
        with self.assertRaises(restep.UnsupportedMediaType):
            self.api["notes"].upload(b"x", "image/png")

    def test_council_round_3(self) -> None:
        shelves, docs = self.api["shelves"], self.api["docs"]
        # 7 (§6.1, §11.1): integer and number operands follow the number grammar of RFC 8259, exponent
        # included; so do a GET action's params, each element of a list too.
        self.assertEqual(query_of(shelves.list_request(filter={"pages": {"ge": "2e1", "in": ["250e-1", "-0", "0.0E+5"]},
                                                               "weight": {"ge": "-0.5E+1"}})),
                         "filter[pages][ge]=2e1&filter[pages][in]=250e-1,-0,0.0E+5&filter[weight][ge]=-0.5E+1")
        for bad in ("020", "+20", ".5", "0x14", "20.", "1e", " 20", "", "25e-1", "2.05e1"):
            with self.assertRaises(restep.InvalidParameter, msg=bad) as cm:
                shelves.list_request(filter={"pages": {"ge": bad}})
            self.assertEqual((cm.exception.local, cm.exception.parameter), (True, "filter[pages][ge]"))
        for bad in ("020", "+20", ".5", "0x14", "20.", "1e"):
            with self.assertRaises(restep.InvalidParameter, msg=bad):
                shelves.list_request(filter={"weight": {"ge": bad}})
        req = shelves.action_request("1", "compare", None, None, params={"with": "2", "ids": ["2e1", 3]})
        self.assertTrue(req.url.endswith("/shelves/1:compare?with=2&ids=2e1,3"), req.url)
        for ids in (["020"], [1, "+2"], ["0x14"], ["1.5"]):
            with self.assertRaises(restep.InvalidParameter, msg=ids) as cm:
                shelves.action_request("1", "compare", None, None, params={"with": "2", "ids": ids})
            self.assertEqual(cm.exception.parameter, "ids")
        # 3 (§3): a local date-time has no leap second.
        with self.assertRaises(restep.InvalidParameter):
            shelves.list_request(filter={"opens_at": {"ge": "2016-12-31T23:59:60"}})
        with self.assertRaises(ValueError):
            restep.local_to_instant("2016-12-31T23:59:60", "Europe/Madrid")
        # 8 (§6.1): null alone on a group, with or without "quantifier": "some", is used alike.
        self.assertEqual(shelves.filter_ops("labels"), ["null"])
        self.assertEqual(query_of(shelves.list_request(filter={"labels": {"null": True}, "formats": {"null": True}})),
                         "filter[labels][null]=true&filter[formats][null]=true")
        # 1 (§5.1): a collection with upload and without create takes only files: no JSON create.
        self.assertEqual((self.api["uploads"].creates_from_json, docs.creates_from_json, self.api["notes"].creates_from_json),
                         (False, True, True))
        n = len(Mock.log)
        with self.assertRaises(restep.UnsupportedMediaType) as cm:
            self.api["uploads"].create({"name": "x"})
        self.assertEqual((cm.exception.local, cm.exception.accept_post, len(Mock.log)), (True, ["image/*", "application/pdf"], n))
        # 2 (§5.1): with both, a JSON POST is a creation and a JSON file goes as the file part of
        # multipart/form-data; any other file goes as its bytes.
        self.assertEqual(docs.create_request({"title": "x"}, None).headers["content-type"], "application/json")
        req = docs.upload_request(b'{"a":1}', "application/json", 'a "b".json', None)
        boundary = re.fullmatch(r"multipart/form-data; boundary=(\S+)", req.headers["content-type"]).group(1)
        self.assertNotIn("content-disposition", req.headers)
        self.assertEqual(req.body, (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a %22b%22.json"\r\n'
                                    f'Content-Type: application/json\r\n\r\n{{"a":1}}\r\n--{boundary}--\r\n').encode())
        self.assertEqual(docs.upload_request(b"hi", "text/plain", None, None).headers["content-type"], "text/plain")
        self.assertEqual(self.api["uploads"].upload_request(b"%PDF", "application/pdf", None, None).body, b"%PDF")
        with self.assertRaises(restep.UnsupportedMediaType) as cm:  # shelves creates from JSON and declares no multipart
            shelves.upload_request(b"{}", "application/json; charset=utf-8", None, None)
        self.assertTrue(cm.exception.local)

    def test_council_round_4(self) -> None:
        from decimal import Decimal
        shelves, docs, blobs = self.api["shelves"], self.api["docs"], self.api["blobs"]
        # 1a, 4a (§5.1): in a collection that only takes uploads, application/* (or */*) admits a JSON
        # file, which goes as its bytes; 1b: a +json type is a file like any other, never multipart.
        req = blobs.upload_request(b'{"a":1}', "application/json", None, None)
        self.assertEqual((req.headers["content-type"], req.body), ("application/json", b'{"a":1}'))
        wild = restep.connect(MOCK_URL)["blobs"]
        wild.declaration["upload"] = {"media_types": ["*/*"]}
        self.assertEqual(wild.upload_request(b"{}", "application/json", None, None).headers["content-type"], "application/json")
        both = restep.connect(MOCK_URL)["docs"]
        both.declaration["upload"] = {"media_types": ["application/*"], "multipart": True}
        self.assertEqual(both.upload_request(b"{}", "application/ld+json", None, None).headers["content-type"], "application/ld+json")
        self.assertTrue(both.upload_request(b"{}", "application/json", None, None).headers["content-type"].startswith("multipart/form-data"))
        # 2 (§5.1): multipart/form-data never goes to a collection without upload.multipart, even when
        # media_types admits it, also without local validation; with it, such a file is the file part.
        for api in (restep.connect(MOCK_URL), restep.connect(MOCK_URL, validate=False)):
            loose = api["blobs"]
            loose.declaration["upload"] = {"media_types": ["*/*"]}
            with self.assertRaises(restep.UnsupportedMediaType) as cm:
                loose.upload_request(b"--x--", "multipart/form-data; boundary=x", None, None)
            self.assertTrue(cm.exception.local)
        both.declaration["upload"] = {"media_types": ["*/*"], "multipart": True}
        req = both.upload_request(b"--x--", "multipart/form-data; boundary=x", None, None)
        self.assertIn(b"Content-Type: multipart/form-data; boundary=x\r\n", req.body)
        self.assertRegex(req.headers["content-type"], r"^multipart/form-data; boundary=restep-")
        # 4b (§5.1): the file part always carries a filename, "file" when there is none.
        self.assertIn(b'Content-Disposition: form-data; name="file"; filename="file"\r\n',
                      docs.upload_request(b"{}", "application/json", None, None).body)
        # 5a (§6.1): two null filters on the same to-many relationship, its own or a path's, give
        # invalid_parameter, even when their values agree; on different groups they go.
        for v in (True, False):
            with self.assertRaises(restep.InvalidParameter) as cm:
                shelves.list_request(filter={"reviews": {"null": True}, "reviews.id": {"null": v}})
            self.assertEqual((cm.exception.local, cm.exception.parameter), (True, "filter[reviews.id][null]"))
        self.assertEqual((shelves.null_group("reviews.id"), shelves.null_group("labels"), shelves.null_group("pages")),
                         ("reviews", "labels", None))
        self.assertEqual(query_of(shelves.list_request(filter={"reviews.id": {"null": True}, "labels": {"null": False}})),
                         "filter[reviews.id][null]=true&filter[labels][null]=false")
        # 6 (§6.1): a numeric operand is never replaced by another value: huge ones go as they are,
        # and an infinity or NaN is not an operand.
        self.assertEqual(query_of(shelves.list_request(filter={"pages": {"ge": "1e400"}, "weight": {"ge": "-1e400"}})),
                         "filter[pages][ge]=1e400&filter[weight][ge]=-1e400")
        for bad in (Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN"), float("inf")):
            for name in ("pages", "weight"):
                with self.assertRaises(restep.InvalidParameter, msg=(bad, name)):
                    shelves.list_request(filter={name: {"ge": bad}})
        # Location (§5.1, §7.3): kept on a 201 without body, and on one with the item.
        created = blobs.upload(b"{}", "application/json", idempotency_key="k")
        self.assertEqual((created, created.location, type(created)), ({}, f"{MOCK_URL}blobs/b1", restep.Created))
        item = shelves.upload(b"%PDF", "application/pdf", idempotency_key=None)
        self.assertEqual((item["id"], item.location), ("f1", f"{MOCK_URL}shelves/f1"))

    def test_upsert_and_reusable_ids(self) -> None:
        # C-47: a PATCH to a free id creates (201); C-46: with reusable_ids, PATCH and DELETE are safe
        # to repeat only with If-Match.
        shelves = self.api["shelves"]
        self.assertEqual(shelves.update("new1", {"title": "x"}, if_none_match="*"), {"id": "new1", "title": "x", "_etag": '"n-1"'})
        self.assertEqual(Mock.log[-1]["headers"]["If-None-Match"], "*")
        self.assertFalse(shelves.update_request("1", {}, None).retry_safe)
        self.assertTrue(shelves.update_request("1", {}, '"e"').retry_safe)
        self.assertFalse(shelves.delete_request("1", None).retry_safe)
        self.assertTrue(self.api["books"].update_request("1", {}, None).retry_safe)

    def test_singletons_with_parent_and_actions(self) -> None:
        # C-76: a singleton under an item, with its template filled; C-110: actions of a singleton.
        settings = self.api.singletons["settings"]
        self.assertEqual((settings.parent, settings.variables), ("readers", ["reader_id"]))
        with self.assertRaises(TypeError):
            settings.get()
        self.assertEqual(settings.at(reader_id="7").get(), {"theme": "dark", "_etag": '"s-1"'})
        me = self.api.singletons["me"]
        result = me.action("verify", full=True)
        self.assertEqual((result.status, result.stale), (204, True))
        self.assertEqual((Mock.log[-1]["method"], Mock.log[-1]["path"]), ("POST", "/me:verify"))
        with self.assertRaises(restep.NotFound):
            me.action("bad")  # a singleton's action carries no on

    def test_ijson(self) -> None:
        # C-08: what a client sends is I-JSON.
        for body in ({"text": "\ud800"}, {"text": "﷐"}, {"\U0010ffff": 1}, {"n": float("nan")}):
            with self.assertRaises(ValueError):
                self.api["notes"].create(body)
        self.assertEqual(Mock.log, [])

    def test_discovery_outcomes(self) -> None:
        # C-22 (4.2): core only after a 2xx without restep or a 404, never after another error; of
        # several service-desc links, the first target that is a discovery document of Restep 1.
        api = restep.connect(f"http://localhost:{MOCK2}/c/")
        self.assertEqual((set(api.collections), api.version), ({"things"}, "1.0-draft.7"))
        api = restep.connect(f"http://localhost:{MOCK2}/d/")
        self.assertEqual((api.discovery, api.profiles), (None, frozenset({"core"})))
        with self.assertRaises(restep.Unauthenticated):
            restep.connect(f"http://localhost:{MOCK2}/e/")
        with self.assertRaises(restep.Unavailable):
            restep.connect(f"http://localhost:{MOCK2}/f/")

    def test_retries(self) -> None:
        # C-72: 408 and 425 are repeated as is, 421 over another connection (urllib opens one per
        # request); 502 and 504 only where repeating is safe.
        api = restep.connect(MOCK_URL, retries=2)
        for status in (408, 421, 425):
            Mock.log.clear()
            Mock.fails["POST /notes"] = [(status, {})]
            self.assertEqual(api["notes"].create({"text": "x"}, idempotency_key=None)["text"], "x", status)
            self.assertEqual(len(Mock.log), 2)
        for status, exc in ((502, restep.BadGateway), (504, restep.GatewayTimeout)):
            Mock.log.clear()
            Mock.fails["POST /notes"] = [(status, {"Retry-After": "0"})]
            with self.assertRaises(exc):
                api["notes"].create({"text": "x"}, idempotency_key=None)
            self.assertEqual(len(Mock.log), 1)
            Mock.fails["POST /notes"] = [(status, {"Retry-After": "0"})]
            self.assertEqual(api["notes"].create({"text": "x"})["text"], "x")  # with a key
        # The async transport may reuse the connection, so it does not repeat a 421.
        import restep._core as core
        req = core.Request("GET", MOCK_URL, {}, retry_safe=True)
        self.assertIsNone(core.ApiCore(MOCK_URL, retries=1).retry_delay(req, core.Response(421, {}), 0))

    def test_key_ttl(self) -> None:
        # C-71: a POST safe to repeat only because of its key is not repeated after idempotency.ttl
        # from its first attempt (3600 here; 24 hours where it is not declared).
        import restep._core as core
        c = self.api._core
        self.assertEqual(c.key_ttl, 3600)
        self.assertEqual(core.ApiCore(MOCK_URL).key_ttl, 86400)
        c.retries = 3
        try:
            busy = core.Response(409, {"content-type": "application/problem+json", "retry-after": "1"},
                                 b'{"code": "request_in_progress"}')
            req = self.api["notes"].create_request({"text": "x"}, restep.AUTO)
            self.assertEqual(req.key_ttl, 3600)
            req.first_sent = time.monotonic() - 3598
            self.assertEqual(c.retry_delay(req, busy, 0), 1)
            req.first_sent = time.monotonic() - 3600
            self.assertIsNone(c.retry_delay(req, busy, 0))
            # The protection of a client-chosen id, and of an idempotent action, does not expire.
            self.assertIsNone(self.api["tags"].create_request({"id": "t"}, restep.AUTO).key_ttl)
            self.assertIsNone(self.api["orders"].action_request(None, "import", {}, restep.AUTO).key_ttl)
        finally:
            c.retries = 0

    def test_inherit_unknown_members(self) -> None:
        # C-131: a field of inherit whose declaration carries a member this client does not know is
        # not inherited; x- members are the server's own and are ignored. combine: fields.
        products = self.api["products"]
        self.assertEqual(products.inherited({"_inherited": {"tax_rate": {"from": "default"}}}, "tax_rate"), "default")
        with self.assertRaises(KeyError):
            products.inherited({"_inherited": {}}, "odd")  # merge: an unknown member without x-
        self.assertEqual(products.inherited({"_inherited": {}}, "tax_rate"), "own")  # x-acme-note is the server's own
        got = products.inherited({"_inherited": {"hours": {"fields": {"monday": {"from": {"href": "/chains/1"}}, "sunday": {"from": "x"}}}}}, "hours")
        self.assertEqual(got, {"fields": {"monday": {"href": "/chains/1"}, "sunday": "hidden"}})


# ── Asynchronous ─────────────────────────────────────────────────────────────
class _FakeHeaders:
    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def multi_items(self) -> list[tuple[str, str]]:
        return self._pairs


class _FakeResponse:
    def __init__(self, status: int, headers: list[tuple[str, str]], content: bytes, url: str) -> None:
        self.status_code, self.headers, self.content, self.url = status, _FakeHeaders(headers), content, url


class FakeAsyncClient:
    """A client shaped like httpx.AsyncClient made with urllib, to test restep.aio without httpx."""

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
        self.assertEqual(api.version, api.discovery["restep"])
        shop = await api.singletons["shop"].get()
        self.assertIn("_etag", shop)
        await api.aclose()

    async def draft7(self, client: Any) -> None:
        # The draft 7 operations of restep.aio: a full copy, upload, an action's result, a
        # singleton under an item, and core only after a 404.
        api = await restep.aio.connect(MOCK_URL, client=client)
        shelves = api["shelves"]
        self.assertEqual([s async for s in shelves.copy()], [{"id": "1"}])
        self.assertEqual((await shelves.upload(b"%PDF", "application/pdf"))["_content"]["size"], 4)
        self.assertTrue((await shelves.action("1", "poke", full=True)).stale)
        self.assertEqual(await shelves.new(), {"values": {"title": ""}, "schema": {"type": "object"}})
        self.assertEqual(await api.singletons["settings"].at(reader_id=7).get(), {"theme": "dark", "_etag": '"s-1"'})
        self.assertIsNone(await api.singletons["me"].action("verify"))
        self.assertEqual(api.default_language, "es-es")
        core = await restep.aio.connect(f"http://localhost:{MOCK2}/d/", client=client)
        self.assertIsNone(core.discovery)
        with self.assertRaises(restep.Unauthenticated):
            await restep.aio.connect(f"http://localhost:{MOCK2}/e/", client=client)

    def test_with_fake_client(self) -> None:
        asyncio.run(self.scenario(FakeAsyncClient()))
        asyncio.run(self.draft7(FakeAsyncClient()))

    @unittest.skipUnless(restep.aio.HAS_HTTPX, "httpx is not installed")
    def test_with_httpx(self) -> None:
        asyncio.run(self.scenario(None))

    @unittest.skipIf(restep.aio.HAS_HTTPX, "httpx is installed")
    def test_without_httpx(self) -> None:
        with self.assertRaises(ImportError):
            asyncio.run(restep.aio.connect(REF_URL))


if __name__ == "__main__":
    unittest.main(verbosity=2)
