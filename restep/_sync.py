"""Synchronous client, with urllib from the standard library."""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any, Iterator, Mapping, Sequence

from . import errors as E
from ._core import (AUTO, ActionResult, ApiCore, CollectionCore, Filter, IdempotencyKey, Item, PageData, Request, Response,
                    SingletonCore, Total, action_result, is_safe_action, merge_headers, with_etag, written)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A 3xx from the API is returned as is: following it would resend credentials uncontrolled.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Api:
    """A connected Restep API. Create it with :func:`restep.connect`."""

    def __init__(self, url: str, *, token: str | None = None, language: str | None = None,
                 headers: Mapping[str, str] | None = None, validate: bool = True, retries: int = 0,
                 max_retry_wait: float = 60.0, timeout: float = 30.0) -> None:
        self._core = ApiCore(url, token=token, language=language, headers=headers, validate=validate,
                             retries=retries, max_retry_wait=max_retry_wait, timeout=timeout)
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._core.new_connection_per_request = True  # urllib opens a connection per request (§5.7, 421)
        self._collections: dict[str, Collection] = {}
        self._singletons: dict[str, Singleton] = {}

    # Transport
    def _send_once(self, req: Request) -> Response:
        r = urllib.request.Request(req.url, data=req.body, method=req.method, headers=req.headers)
        try:
            with self._opener.open(r, timeout=self._core.timeout) as f:
                return Response(f.status, merge_headers(f.headers.items()), f.read(), f.url)
        except urllib.error.HTTPError as e:
            with e:
                return Response(e.code, merge_headers(e.headers.items()), e.read(), req.url)

    def _send(self, req: Request) -> Response:
        attempt = 0
        req.first_sent = time.monotonic()
        while True:
            resp = self._send_once(req)
            wait = self._core.retry_delay(req, resp, attempt)
            if wait is None:
                return resp
            time.sleep(wait)
            attempt += 1

    def _call(self, req: Request) -> tuple[Response, Any]:
        resp = self._send(req)
        return resp, self._core.parse(req, resp)

    def _discover(self) -> None:
        resp = self._send(self._core.build("GET", self._core.root))
        doc, core_only = self._core.discovery_response(resp)
        where = self._core.root
        if core_only:
            # §4.2: the document may be linked with service-desc; the first target that is a
            # discovery document of Restep 1 counts. Without one, the API is core only.
            for target in self._core.service_desc(resp):
                try:
                    found, _ = self._core.discovery_response(self._send(self._core.build("GET", target)))
                except (E.RestepError, ValueError):
                    continue
                if self._core.restep1(found):
                    doc, where = found, target
                    break
        self._core.set_discovery(doc, where)
        self._collections = {n: Collection(self, n, d) for n, d in self._core.declarations.items()}
        self._singletons = {n: Singleton(self, n, d) for n, d in self._core.singleton_declarations.items()}

    # What discovery says
    @property
    def root(self) -> str:
        return self._core.root

    @property
    def discovery(self) -> dict[str, Any] | None:
        """The discovery document (§4.2), or ``None`` if the server does not serve it (core only)."""
        return self._core.discovery

    @property
    def profiles(self) -> frozenset[str]:
        return self._core.profiles

    @property
    def extensions(self) -> frozenset[str]:
        return self._core.extensions

    @property
    def version(self) -> str | None:
        """The ``restep`` the server declares (§1.1), or ``None`` without discovery."""
        return self._core.restep_version

    @property
    def default_language(self) -> str | None:
        """The API's default language (§5.6), from discovery, or ``None`` if it does not declare one."""
        return self._core.default_language

    @property
    def pre_release(self) -> str | None:
        """``"draft.7"``, ``"rc.1"``… if it declares a draft or a candidate, which must not be assumed
        to have the guarantees of a published version (§1.1); ``None`` if it is a published one."""
        return self._core.pre_release

    def usable(self, part: str) -> bool:
        """Whether a profile or an extension can be used: announced and, if experimental, under a
        version whose text this client implements (§1.2)."""
        return self._core.usable(part)

    @property
    def deprecations(self) -> list[dict[str, Any]]:
        """The retirement notices in discovery (§5.5), with ``announced``, ``sunset`` and ``where``."""
        return self._core.deprecations()

    @property
    def collections(self) -> Mapping[str, "Collection"]:
        return dict(self._collections)

    @property
    def singletons(self) -> Mapping[str, "Singleton"]:
        """The declared singletons (§5.9), without those with a template or with ``parent``, which are ignored."""
        return dict(self._singletons)

    def __getitem__(self, name: str) -> "Collection":
        try:
            return self._collections[name]
        except KeyError:
            names = ", ".join(self._collections) or "none"
            raise KeyError(f"The discovery document does not declare the collection '{name}'; it declares: {names}.") from None

    def __contains__(self, name: object) -> bool:
        return name in self._collections

    def collection(self, name: str, path: str | None = None) -> "Collection":
        """A declared collection, or one that discovery does not declare (a core-only server),
        at ``path`` or at ``/name``."""
        if path is None and name in self._collections:
            return self._collections[name]
        return Collection(self, name, {"path": path or f"/{name}"})

    def status(self) -> bool:
        """Whether the API can serve requests, according to the declared ``status`` resource (§4.4)."""
        target = (self.discovery or {}).get("status")
        if not isinstance(target, str):
            raise LookupError("The discovery document does not declare status (§4.4); a path must not be assumed.")
        req = self._core.build("GET", self._core.url(target))
        resp = self._send(req)
        if resp.status in (200, 503):
            return resp.status == 200
        self._core.parse(req, resp)
        return False

    def __repr__(self) -> str:
        return f"<restep.Api {self.root} profiles={sorted(self.profiles)} collections={list(self._collections)}>"


class Page:
    """A page of a collection (§5.2). It iterates as its ``items``."""

    def __init__(self, collection: "Collection", data: PageData) -> None:
        self._collection = collection
        self.items: list[Item] = data.items
        self.next: str | None = data.next
        self.total: Total | None = data.total
        self.raw: dict[str, Any] = data.raw

    def next_page(self) -> "Page | None":
        """The next page, following ``next`` as is (§5.2), or ``None`` if there are no more."""
        return self._collection._follow(self.next) if self.next else None

    def __iter__(self) -> Iterator[Item]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return f"<restep.Page {self._collection.name} items={len(self.items)} next={'yes' if self.next else 'no'}>"


class Collection(CollectionCore):
    """A collection from discovery. Operations it does not declare (§4.3) fail without calling
    the server, as do the parameters it does not offer (§5.3)."""

    def __init__(self, api: Api, name: str, decl: Mapping[str, Any] | None, path_vars: Mapping[str, str] | None = None) -> None:
        super().__init__(api._core, name, decl, path_vars)
        self._client = api

    def at(self, **path_vars: Any) -> "Collection":
        """The subcollection with its template variables filled: ``api['reviews'].at(id='7')``."""
        return Collection(self._client, self.name, self.declaration, {**self._vars, **{k: str(v) for k, v in path_vars.items()}})

    def _follow(self, url: str) -> Page:
        _, body = self._client._call(self._api.build("GET", self._api.url(url)))
        return Page(self, PageData.from_body(body))

    def list(self, *, filter: Filter | None = None, named: Mapping[str, Any] | None = None, sort: str | Sequence[str] | None = None,
             fields: str | Sequence[str] | None = None, include: str | Sequence[str] | None = None,
             limit: int | None = None, count: bool = False) -> Page:
        """A page of the collection (§5.2, §6)."""
        _, body = self._client._call(self.list_request(filter=filter, named=named, sort=sort, fields=fields, include=include,
                                                       limit=limit, count=count))
        return Page(self, PageData.from_body(body))

    def iter(self, *, filter: Filter | None = None, named: Mapping[str, Any] | None = None, sort: str | Sequence[str] | None = None,
             fields: str | Sequence[str] | None = None, include: str | Sequence[str] | None = None,
             limit: int | None = None) -> Iterator[Item]:
        """All the items, page by page; ``limit`` is the size of each page."""
        page: Page | None = self.list(filter=filter, named=named, sort=sort, fields=fields, include=include, limit=limit)
        while page is not None:
            yield from page.items
            page = page.next_page()

    def copy(self, *, limit: int | None = None) -> Iterator[Item]:
        """Every item the client can see: the request for a full copy, with every default filter
        turned off and no other filter (§5.2). A collection with a required filter has none."""
        query = self.copy_query() + self.read_query(limit=limit, item=True)
        _, body = self._client._call(self._api.build("GET", self._url(), query=query))
        page: Page | None = Page(self, PageData.from_body(body))
        while page is not None:
            yield from page.items
            page = page.next_page()

    def count(self, *, filter: Filter | None = None, named: Mapping[str, Any] | None = None) -> Total:
        """How many items there are (§6.5). ``Total.estimated`` says whether the number is approximate."""
        page = self.list(filter=filter, named=named, limit=1, count=True)
        if page.total is None:
            raise E.RestepError(200, {"detail": "count=true was requested and the response has no total (§6.5)."}, local=True)
        return page.total

    def get(self, id: Any, *, fields: str | Sequence[str] | None = None,
            include: str | Sequence[str] | None = None) -> Item:
        """An item. It carries its item ETag in ``_etag`` (§7.2)."""
        resp, body = self._client._call(self.get_request(id, fields, include))
        return body if fields or include else with_etag(resp, body)

    def create(self, data: Mapping[str, Any], *, idempotency_key: IdempotencyKey = AUTO) -> Item:
        """Creates an item (§5.1). With ``writes``, it carries a new ``Idempotency-Key`` unless another
        or ``None`` is passed (§7.1); retries use the same one."""
        resp, body = self._client._call(self.create_request(data, idempotency_key))
        return written(resp, body, self._api.root)

    def update(self, id: Any, patch: Mapping[str, Any], *, if_match: str | Mapping[str, Any] | None = None,
               if_none_match: str | None = None) -> Item:
        """Updates with JSON Merge Patch (§5.1). ``if_match`` is an ETag or an item that was read (§7.2).
        In a collection that declares ``upsert``, a free ``id`` creates the item (``201``), and
        ``if_none_match="*"`` makes it succeed only if it creates (§5.1)."""
        resp, body = self._client._call(self.update_request(id, patch, if_match, if_none_match))
        return written(resp, body, self._api.root)

    def delete(self, id: Any, *, if_match: str | Mapping[str, Any] | None = None) -> None:
        """Deletes an item (§5.1)."""
        self._client._call(self.delete_request(id, if_match))

    def action(self, id: Any, name: str, data: Any = None, *, params: Mapping[str, Any] | None = None,
               idempotency_key: IdempotencyKey = AUTO, full: bool = False) -> Any:
        """Runs a declared action (§11.1): on an item with its id, on the collection with ``None``.
        A ``GET`` action takes its declared ``params``; a ``POST`` one, its input in ``data``.
        Returns the body (the item's new representation, with ``_etag``, when it came with
        ``Content-Location``), or ``None`` (204); with ``full=True``, an :class:`ActionResult` whose
        ``stale`` says that a stored copy of the item must be revalidated (§11.1)."""
        resp, body = self._client._call(self.action_request(id, name, data, idempotency_key, params))
        decl = (self.declaration.get("actions") or {}).get(name)
        result = action_result(self._api, resp, body, self._item_url(id) if id is not None else None, is_safe_action(decl))
        return result if full else body

    def new(self, **params: Any) -> Any:
        """The starting values and effective schema of a new item for this principal: the safe
        collection action ``new`` (§11.1), ``{"values": {…}, "schema": {…}}``. Advisory: the ``POST`` decides."""
        return self.action(None, "new", params=params or None)

    def upload(self, data: bytes, media_type: str, *, filename: str | None = None,
               idempotency_key: IdempotencyKey = AUTO) -> Item:
        """Creates an item from the bytes of a file, in a collection that declares ``upload`` (§5.1).
        The new item carries ``_content`` with the link to the bytes, their media type and size."""
        resp, body = self._client._call(self.upload_request(data, media_type, filename, idempotency_key))
        return written(resp, body, self._api.root)

    def __repr__(self) -> str:
        return f"<restep.Collection {self.name} {self.path_template}>"


class Singleton(SingletonCore):
    """A singleton from discovery (§5.9): ``api.singletons["shop"].get()``."""

    def __init__(self, api: Api, name: str, decl: Mapping[str, Any], path_vars: Mapping[str, str] | None = None) -> None:
        super().__init__(api._core, name, decl, path_vars)
        self._client = api

    def at(self, **path_vars: Any) -> "Singleton":
        """A singleton that hangs from an item, with the variables of its template filled (§5.9):
        ``api.singletons["settings"].at(reader_id="7")``."""
        return Singleton(self._client, self.name, self.declaration, {**self._vars, **{k: str(v) for k, v in path_vars.items()}})

    def action(self, name: str, data: Any = None, *, params: Mapping[str, Any] | None = None,
               idempotency_key: IdempotencyKey = AUTO, full: bool = False) -> Any:
        """Runs an action of the singleton, at ``{path}:{name}`` (§11.1); as :meth:`Collection.action`."""
        resp, body = self._client._call(self.action_request(name, data, idempotency_key, params))
        decl = (self.declaration.get("actions") or {}).get(name)
        result = action_result(self._api, resp, body, self._url(), is_safe_action(decl))
        return result if full else body

    def get(self, *, fields: str | Sequence[str] | None = None) -> Item:
        """Its representation; without ``fields``, with its ETag in ``_etag`` (§7.2)."""
        resp, body = self._client._call(self.get_request(fields))
        return body if fields else with_etag(resp, body)

    def update(self, patch: Mapping[str, Any], *, if_match: str | Mapping[str, Any] | None = None,
               if_none_match: str | None = None) -> Item:
        """Updates it with JSON Merge Patch (§5.9, §5.1); ``if_match`` as in collections."""
        resp, body = self._client._call(self.update_request(patch, if_match, if_none_match))
        return with_etag(resp, body if isinstance(body, dict) else {})

    def __repr__(self) -> str:
        return f"<restep.Singleton {self.name} {self.path}>"


def connect(url: str, token: str | None = None, language: str | None = None, *,
            headers: Mapping[str, str] | None = None, validate: bool = True, retries: int = 0,
            max_retry_wait: float = 60.0, timeout: float = 30.0) -> Api:
    """Connects to a Restep API by reading its discovery document (§4.2). If it declares a
    version that is not Restep 1 (§1.1), it raises ``restep.UnsupportedVersion``.

    - ``token``: sent as ``Authorization: Bearer`` only to the API's origin (§4.3).
    - ``language``: ``Accept-Language`` for the API's texts (errors, descriptions, §5.6).
    - ``validate``: checks the parameters against discovery before sending.
    - ``retries``: how many times to repeat on ``429`` or ``503`` (the latter only if repeating is safe),
      waiting what ``Retry-After`` says up to ``max_retry_wait`` seconds.
    """
    api = Api(url, token=token, language=language, headers=headers, validate=validate, retries=retries,
              max_retry_wait=max_retry_wait, timeout=timeout)
    api._discover()
    return api
