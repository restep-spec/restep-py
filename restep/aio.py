"""Cliente asíncrono, con httpx. La misma API que el síncrono, con ``await`` y ``async for``.

httpx es opcional: este módulo se importa sin él, pero :func:`connect` lo necesita salvo que se
le pase ``client``, un objeto con ``async request(method, url, headers=..., content=...)`` que
devuelva algo con ``status_code``, ``headers`` y ``content`` (la forma de ``httpx.AsyncClient``).
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Iterator, Mapping, Sequence

from . import errors as E
from ._core import (AUTO, ApiCore, CollectionCore, Filter, IdempotencyKey, Item, PageData, Request, Response,
                    SingletonCore, Total, is_discovery, merge_headers, with_etag)

try:  # import condicional: httpx no es una dependencia
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
                raise ImportError("restep.aio necesita httpx (pip install httpx) o un client compatible.")
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
        resp, doc = await self._call(self._core.build("GET", self._core.root))
        where = self._core.root
        if not is_discovery(doc):
            for target in self._core.service_desc(resp):
                try:
                    _, doc = await self._call(self._core.build("GET", target))
                except (E.RestepError, ValueError):
                    continue
                if is_discovery(doc):
                    where = target
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
            names = ", ".join(self._collections) or "ninguna"
            raise KeyError(f"El descubrimiento no declara la colección «{name}»; declara: {names}.") from None

    def __contains__(self, name: object) -> bool:
        return name in self._collections

    def collection(self, name: str, path: str | None = None) -> "AsyncCollection":
        if path is None and name in self._collections:
            return self._collections[name]
        return AsyncCollection(self, name, {"path": path or f"/{name}"})

    async def status(self) -> bool:
        target = (self.discovery or {}).get("status")
        if not isinstance(target, str):
            raise LookupError("El descubrimiento no declara status (§4.4); no hay que suponer una ruta.")
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

    async def count(self, *, filter: Filter | None = None, named: Mapping[str, Any] | None = None) -> Total:
        page = await self.list(filter=filter, named=named, limit=1, count=True)
        if page.total is None:
            raise E.RestepError(200, {"detail": "Se pidió count=true y la respuesta no trae total (§6.5)."}, local=True)
        return page.total

    async def get(self, id: Any, *, fields: str | Sequence[str] | None = None,
                  include: str | Sequence[str] | None = None) -> Item:
        resp, body = await self._client._call(self.get_request(id, fields, include))
        return body if fields or include else with_etag(resp, body)

    async def create(self, data: Mapping[str, Any], *, idempotency_key: IdempotencyKey = AUTO) -> Item:
        resp, body = await self._client._call(self.create_request(data, idempotency_key))
        return with_etag(resp, body if isinstance(body, dict) else {})

    async def update(self, id: Any, patch: Mapping[str, Any], *, if_match: str | Mapping[str, Any] | None = None) -> Item:
        resp, body = await self._client._call(self.update_request(id, patch, if_match))
        return with_etag(resp, body if isinstance(body, dict) else {})

    async def delete(self, id: Any, *, if_match: str | Mapping[str, Any] | None = None) -> None:
        await self._client._call(self.delete_request(id, if_match))

    async def action(self, id: Any, name: str, data: Any = None, *, idempotency_key: IdempotencyKey = AUTO) -> Any:
        _, body = await self._client._call(self.action_request(id, name, data, idempotency_key))
        return body


class AsyncSingleton(SingletonCore):
    def __init__(self, api: AsyncApi, name: str, decl: Mapping[str, Any]) -> None:
        super().__init__(api._core, name, decl)
        self._client = api

    async def get(self, *, fields: str | Sequence[str] | None = None) -> Item:
        resp, body = await self._client._call(self.get_request(fields))
        return body if fields else with_etag(resp, body)

    async def update(self, patch: Mapping[str, Any], *, if_match: str | Mapping[str, Any] | None = None) -> Item:
        resp, body = await self._client._call(self.update_request(patch, if_match))
        return with_etag(resp, body if isinstance(body, dict) else {})


async def connect(url: str, token: str | None = None, language: str | None = None, *,
                  headers: Mapping[str, str] | None = None, validate: bool = True, retries: int = 0,
                  max_retry_wait: float = 60.0, timeout: float = 30.0, client: Any = None) -> AsyncApi:
    """Como :func:`restep.connect`, asíncrono. Ciérralo con ``await api.aclose()`` o úsalo con ``async with``."""
    api = AsyncApi(url, token=token, language=language, headers=headers, validate=validate, retries=retries,
                   max_retry_wait=max_retry_wait, timeout=timeout, client=client)
    try:
        await api._discover()
    except BaseException:
        await api.aclose()
        raise
    return api
