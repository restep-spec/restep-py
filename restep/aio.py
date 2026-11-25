"""Asynchronous client, with httpx. The same API as the synchronous one, with ``await`` and ``async for``.

httpx is optional: this module imports without it, but :func:`connect` needs it unless it is
given ``client``, an object with ``async request(method, url, headers=..., content=...)`` that
returns something with ``status_code``, ``headers`` and ``content`` (the shape of ``httpx.AsyncClient``).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Iterator, Mapping, Sequence

from . import errors as E
from ._core import (AUTO, ActionResult, ApiCore, CollectionCore, Filter, IdempotencyKey, Item, PageData, Request, Response,
                    SingletonCore, Total, action_result, is_safe_action, merge_headers, with_etag, written)

try:  # conditional import: httpx is not a dependency
    import httpx as _httpx
except ImportError:  # pragma: no cover
    _httpx = None

HAS_HTTPX = _httpx is not None

__all__ = ["connect", "AsyncApi", "AsyncCollection", "AsyncSingleton", "AsyncPage", "HAS_HTTPX"]


def _header_pairs(headers: Any) -> list[tuple[str, str]]:
    multi = getattr(headers, "multi_items", None)
    return list(multi()) if callable(multi) else list(dict(headers).items())


class AsyncApi:
    def __init__(self, url: str, *, token: str | None = None, language: str | None = None,
                 headers: Mapping[str, str] | None = None, validate: bool = True, retries: int = 0,
                 max_retry_wait: float = 60.0, timeout: float = 30.0, client: Any = None) -> None:
        self._core = ApiCore(url, token=token, language=language, headers=headers, validate=validate,
                             retries=retries, max_retry_wait=max_retry_wait, timeout=timeout)
        self._own_client = client is None
        if client is None:
            if _httpx is None:
                raise ImportError("restep.aio needs httpx (pip install httpx) or a compatible client.")
            client = _httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._client = client
        self._collections: dict[str, AsyncCollection] = {}
        self._singletons: dict[str, AsyncSingleton] = {}

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def __aenter__(self) -> "AsyncApi":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _send_once(self, req: Request) -> Response:
        r = await self._client.request(req.method, req.url, headers=req.headers, content=req.body)
        return Response(r.status_code, merge_headers(_header_pairs(r.headers)), r.content, str(getattr(r, "url", req.url)))

    async def _send(self, req: Request) -> Response:
        attempt = 0
        req.first_sent = time.monotonic()
        while True:
            resp = await self._send_once(req)
            wait = self._core.retry_delay(req, resp, attempt)
            if wait is None:
                return resp
            await asyncio.sleep(wait)
            attempt += 1

    async def _call(self, req: Request) -> tuple[Response, Any]:
        resp = await self._send(req)
        return resp, self._core.parse(req, resp)

    async def _discover(self) -> None:
        resp = await self._send(self._core.build("GET", self._core.root))
        doc, core_only = self._core.discovery_response(resp)
        where = self._core.root
        if core_only:
            # §4.2: the document may be linked with service-desc; the first target that is a
            # discovery document of Restep 1 counts. Without one, the API is core only.
            for target in self._core.service_desc(resp):
                try:
                    found, _ = self._core.discovery_response(await self._send(self._core.build("GET", target)))
                except (E.RestepError, ValueError):
                    continue
                if self._core.restep1(found):
                    doc, where = found, target
                    break
        self._core.set_discovery(doc, where)
        self._collections = {n: AsyncCollection(self, n, d) for n, d in self._core.declarations.items()}
        self._singletons = {n: AsyncSingleton(self, n, d) for n, d in self._core.singleton_declarations.items()}

    @property
    def root(self) -> str:
        return self._core.root

    @property
    def discovery(self) -> dict[str, Any] | None:
        return self._core.discovery

    @property
    def profiles(self) -> frozenset[str]:
        return self._core.profiles

    @property
    def extensions(self) -> frozenset[str]:
        return self._core.extensions

    @property
    def version(self) -> str | None:
        return self._core.restep_version

    @property
    def default_language(self) -> str | None:
        """The API's default language (§5.6), from discovery, or ``None`` if it does not declare one."""
        return self._core.default_language

    @property
    def pre_release(self) -> str | None:
        return self._core.pre_release

    def usable(self, part: str) -> bool:
        return self._core.usable(part)

    @property
    def deprecations(self) -> list[dict[str, Any]]:
        return self._core.deprecations()

    @property
    def collections(self) -> Mapping[str, "AsyncCollection"]:
        return dict(self._collections)

    @property
    def singletons(self) -> Mapping[str, "AsyncSingleton"]:
        return dict(self._singletons)

    def __getitem__(self, name: str) -> "AsyncCollection":
        try:
            return self._collections[name]
        except KeyError:
            names = ", ".join(self._collections) or "none"
            raise KeyError(f"The discovery document does not declare the collection '{name}'; it declares: {names}.") from None

    def __contains__(self, name: object) -> bool:
        return name in self._collections

    def collection(self, name: str, path: str | None = None) -> "AsyncCollection":
        if path is None and name in self._collections:
            return self._collections[name]
        return AsyncCollection(self, name, {"path": path or f"/{name}"})

    async def status(self) -> bool:
        target = (self.discovery or {}).get("status")
        if not isinstance(target, str):
            raise LookupError("The discovery document does not declare status (§4.4); a path must not be assumed.")
        req = self._core.build("GET", self._core.url(target))
        resp = await self._send(req)
        if resp.status in (200, 503):
            return resp.status == 200
        self._core.parse(req, resp)
        return False


class AsyncPage:
    def __init__(self, collection: "AsyncCollection", data: PageData) -> None:
        self._collection = collection
        self.items: list[Item] = data.items
        self.next: str | None = data.next
        self.total: Total | None = data.total
        self.raw: dict[str, Any] = data.raw

    async def next_page(self) -> "AsyncPage | None":
        return await self._collection._follow(self.next) if self.next else None

    def __iter__(self) -> Iterator[Item]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


class AsyncCollection(CollectionCore):
    def __init__(self, api: AsyncApi, name: str, decl: Mapping[str, Any] | None, path_vars: Mapping[str, str] | None = None) -> None:
        super().__init__(api._core, name, decl, path_vars)
        self._client = api

    def at(self, **path_vars: Any) -> "AsyncCollection":
        return AsyncCollection(self._client, self.name, self.declaration, {**self._vars, **{k: str(v) for k, v in path_vars.items()}})

    async def _follow(self, url: str) -> AsyncPage:
        _, body = await self._client._call(self._api.build("GET", self._api.url(url)))
        return AsyncPage(self, PageData.from_body(body))

    async def list(self, *, filter: Filter | None = None, named: Mapping[str, Any] | None = None, sort: str | Sequence[str] | None = None,
                   fields: str | Sequence[str] | None = None, include: str | Sequence[str] | None = None,
                   limit: int | None = None, count: bool = False) -> AsyncPage:
        _, body = await self._client._call(self.list_request(filter=filter, named=named, sort=sort, fields=fields, include=include,
                                                             limit=limit, count=count))
        return AsyncPage(self, PageData.from_body(body))

    async def iter(self, *, filter: Filter | None = None, named: Mapping[str, Any] | None = None, sort: str | Sequence[str] | None = None,
                   fields: str | Sequence[str] | None = None, include: str | Sequence[str] | None = None,
                   limit: int | None = None) -> AsyncIterator[Item]:
        page: AsyncPage | None = await self.list(filter=filter, named=named, sort=sort, fields=fields, include=include, limit=limit)
        while page is not None:
            for item in page.items:
                yield item
            page = await page.next_page()

    async def copy(self, *, limit: int | None = None) -> AsyncIterator[Item]:
        """Every item the client can see: the request for a full copy, with every default filter
        turned off and no other filter (§5.2). A collection with a required filter has none."""
        query = self.copy_query() + self.read_query(limit=limit, item=True)
        _, body = await self._client._call(self._api.build("GET", self._url(), query=query))
        page: AsyncPage | None = AsyncPage(self, PageData.from_body(body))
        while page is not None:
            for item in page.items:
                yield item
            page = await page.next_page()

    async def count(self, *, filter: Filter | None = None, named: Mapping[str, Any] | None = None) -> Total:
        page = await self.list(filter=filter, named=named, limit=1, count=True)
        if page.total is None:
            raise E.RestepError(200, {"detail": "count=true was requested and the response has no total (§6.5)."}, local=True)
        return page.total

    async def get(self, id: Any, *, fields: str | Sequence[str] | None = None,
                  include: str | Sequence[str] | None = None) -> Item:
        resp, body = await self._client._call(self.get_request(id, fields, include))
        return body if fields or include else with_etag(resp, body)

    async def create(self, data: Mapping[str, Any], *, idempotency_key: IdempotencyKey = AUTO) -> Item:
        resp, body = await self._client._call(self.create_request(data, idempotency_key))
        return written(resp, body, self._api.root)

    async def update(self, id: Any, patch: Mapping[str, Any], *, if_match: str | Mapping[str, Any] | None = None,
                     if_none_match: str | None = None) -> Item:
        resp, body = await self._client._call(self.update_request(id, patch, if_match, if_none_match))
        return written(resp, body, self._api.root)

    async def delete(self, id: Any, *, if_match: str | Mapping[str, Any] | None = None) -> None:
        await self._client._call(self.delete_request(id, if_match))

    async def action(self, id: Any, name: str, data: Any = None, *, params: Mapping[str, Any] | None = None,
               idempotency_key: IdempotencyKey = AUTO, full: bool = False) -> Any:
        """Runs a declared action (§11.1): on an item with its id, on the collection with ``None``.
        A ``GET`` action takes its declared ``params``; a ``POST`` one, its input in ``data``.
        Returns the body (the item's new representation, with ``_etag``, when it came with
        ``Content-Location``), or ``None`` (204); with ``full=True``, an :class:`ActionResult` whose
        ``stale`` says that a stored copy of the item must be revalidated (§11.1)."""
        resp, body = await self._client._call(self.action_request(id, name, data, idempotency_key, params))
        decl = (self.declaration.get("actions") or {}).get(name)
        result = action_result(self._api, resp, body, self._item_url(id) if id is not None else None, is_safe_action(decl))
        return result if full else body

    async def new(self, **params: Any) -> Any:
        """The starting values and effective schema of a new item for this principal: the safe
        collection action ``new`` (§11.1), ``{"values": {…}, "schema": {…}}``. Advisory: the ``POST`` decides."""
        return await self.action(None, "new", params=params or None)

    async def upload(self, data: bytes, media_type: str, *, filename: str | None = None,
               idempotency_key: IdempotencyKey = AUTO) -> Item:
        """Creates an item from the bytes of a file, in a collection that declares ``upload`` (§5.1).
        The new item carries ``_content`` with the link to the bytes, their media type and size."""
        resp, body = await self._client._call(self.upload_request(data, media_type, filename, idempotency_key))
        return written(resp, body, self._api.root)


class AsyncSingleton(SingletonCore):
    def __init__(self, api: AsyncApi, name: str, decl: Mapping[str, Any], path_vars: Mapping[str, str] | None = None) -> None:
        super().__init__(api._core, name, decl, path_vars)
        self._client = api

    def at(self, **path_vars: Any) -> "AsyncSingleton":
        """A singleton that hangs from an item, with the variables of its template filled (§5.9):
        ``api.singletons["settings"].at(reader_id="7")``."""
        return AsyncSingleton(self._client, self.name, self.declaration, {**self._vars, **{k: str(v) for k, v in path_vars.items()}})

    async def action(self, name: str, data: Any = None, *, params: Mapping[str, Any] | None = None,
               idempotency_key: IdempotencyKey = AUTO, full: bool = False) -> Any:
        """Runs an action of the singleton, at ``{path}:{name}`` (§11.1); as :meth:`AsyncCollection.action`."""
        resp, body = await self._client._call(self.action_request(name, data, idempotency_key, params))
        decl = (self.declaration.get("actions") or {}).get(name)
        result = action_result(self._api, resp, body, self._url(), is_safe_action(decl))
        return result if full else body

    async def get(self, *, fields: str | Sequence[str] | None = None) -> Item:
        resp, body = await self._client._call(self.get_request(fields))
        return body if fields else with_etag(resp, body)

    async def update(self, patch: Mapping[str, Any], *, if_match: str | Mapping[str, Any] | None = None,
                     if_none_match: str | None = None) -> Item:
        resp, body = await self._client._call(self.update_request(patch, if_match, if_none_match))
        return with_etag(resp, body if isinstance(body, dict) else {})


async def connect(url: str, token: str | None = None, language: str | None = None, *,
                  headers: Mapping[str, str] | None = None, validate: bool = True, retries: int = 0,
                  max_retry_wait: float = 60.0, timeout: float = 30.0, client: Any = None) -> AsyncApi:
    """Like :func:`restep.connect`, asynchronous. Close it with ``await api.aclose()`` or use it with ``async with``."""
    api = AsyncApi(url, token=token, language=language, headers=headers, validate=validate, retries=retries,
                   max_retry_wait=max_retry_wait, timeout=timeout, client=client)
    try:
        await api._discover()
    except BaseException:
        await api.aclose()
        raise
    return api
