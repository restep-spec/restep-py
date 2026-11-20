"""Cliente síncrono, con urllib de la biblioteca estándar."""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any, Iterator, Mapping, Sequence

from . import errors as E
from ._core import (AUTO, ApiCore, CollectionCore, Filter, IdempotencyKey, Item, PageData, Request, Response,
                    Total, is_discovery, merge_headers, with_etag)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Un 3xx de la API se devuelve tal cual: seguirlo reenviaría credenciales sin control.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Api:
    """Una API Restep conectada. Créala con :func:`restep.connect`."""

    def __init__(self, url: str, *, token: str | None = None, language: str | None = None,
                 headers: Mapping[str, str] | None = None, validate: bool = True, retries: int = 0,
                 max_retry_wait: float = 60.0, timeout: float = 30.0) -> None:
        self._core = ApiCore(url, token=token, language=language, headers=headers, validate=validate,
                             retries=retries, max_retry_wait=max_retry_wait, timeout=timeout)
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._collections: dict[str, Collection] = {}

    # Transporte
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
        req = self._core.build("GET", self._core.root)
        resp, doc = self._call(req)
        if not is_discovery(doc):
            # §4.2: si la raíz está ocupada, el documento puede estar enlazado con service-desc.
            for target in self._core.service_desc(resp):
                try:
                    _, doc = self._call(self._core.build("GET", target))
                except E.RestepError:
                    continue
                if is_discovery(doc):
                    break
        self._core.set_discovery(doc)
        self._collections = {n: Collection(self, n, d) for n, d in self._core.declarations.items()}

    # Lo que dice el descubrimiento
    @property
    def root(self) -> str:
        return self._core.root

    @property
    def discovery(self) -> dict[str, Any] | None:
        """El documento de descubrimiento (§4.2), o ``None`` si el servidor no lo sirve (solo core)."""
        return self._core.discovery

    @property
    def profiles(self) -> frozenset[str]:
        return self._core.profiles

    @property
    def extensions(self) -> frozenset[str]:
        return self._core.extensions

    @property
    def collections(self) -> Mapping[str, "Collection"]:
        return dict(self._collections)

    def __getitem__(self, name: str) -> "Collection":
        try:
            return self._collections[name]
        except KeyError:
            names = ", ".join(self._collections) or "ninguna"
            raise KeyError(f"El descubrimiento no declara la colección «{name}»; declara: {names}.") from None

    def __contains__(self, name: object) -> bool:
        return name in self._collections

    def collection(self, name: str, path: str | None = None) -> "Collection":
        """Una colección declarada, o una que el descubrimiento no declara (un servidor solo core),
        en ``path`` o en ``/name``."""
        if path is None and name in self._collections:
            return self._collections[name]
        return Collection(self, name, {"path": path or f"/{name}"})

    def status(self) -> bool:
        """Si la API puede atender peticiones, según el recurso ``status`` declarado (§4.4)."""
        target = (self.discovery or {}).get("status")
        if not isinstance(target, str):
            raise LookupError("El descubrimiento no declara status (§4.4); no hay que suponer una ruta.")
        resp = self._send(self._core.build("GET", self._core.url(target)))
        if resp.status in (200, 503):
            return resp.status == 200
        self._core.parse(Request("GET", self._core.url(target), {}), resp)
        return False

    def __repr__(self) -> str:
        return f"<restep.Api {self.root} profiles={sorted(self.profiles)} collections={list(self._collections)}>"


class Page:
    """Una página de una colección (§5.2). Se recorre como sus ``items``."""

    def __init__(self, collection: "Collection", data: PageData) -> None:
        self._collection = collection
        self.items: list[Item] = data.items
        self.next: str | None = data.next
        self.total: Total | None = data.total
        self.raw: dict[str, Any] = data.raw

    def next_page(self) -> "Page | None":
        """La página siguiente, siguiendo ``next`` tal cual (§5.2), o ``None`` si no hay más."""
        return self._collection._follow(self.next) if self.next else None

    def __iter__(self) -> Iterator[Item]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return f"<restep.Page {self._collection.name} items={len(self.items)} next={'sí' if self.next else 'no'}>"


class Collection(CollectionCore):
    """Una colección del descubrimiento. Las operaciones que no declara (§4.3) fallan sin llamar
    al servidor, igual que los parámetros que no ofrece (§5.3)."""

    def __init__(self, api: Api, name: str, decl: Mapping[str, Any] | None, path_vars: Mapping[str, str] | None = None) -> None:
        super().__init__(api._core, name, decl, path_vars)
        self._client = api

    def at(self, **path_vars: Any) -> "Collection":
        """La subcolección con las variables de su plantilla rellenas: ``api['reviews'].at(id='7')``."""
        return Collection(self._client, self.name, self.declaration, {**self._vars, **{k: str(v) for k, v in path_vars.items()}})

    def _follow(self, url: str) -> Page:
        _, body = self._client._call(self._api.build("GET", self._api.url(url)))
        return Page(self, PageData.from_body(body))

    def list(self, *, filter: Filter | None = None, sort: str | Sequence[str] | None = None,
             fields: str | Sequence[str] | None = None, include: str | Sequence[str] | None = None,
             limit: int | None = None, count: bool = False) -> Page:
        """Una página de la colección (§5.2, §6)."""
        _, body = self._client._call(self.list_request(filter=filter, sort=sort, fields=fields, include=include,
                                                       limit=limit, count=count))
        return Page(self, PageData.from_body(body))

    def iter(self, *, filter: Filter | None = None, sort: str | Sequence[str] | None = None,
             fields: str | Sequence[str] | None = None, include: str | Sequence[str] | None = None,
             limit: int | None = None) -> Iterator[Item]:
        """Todos los elementos, página a página; ``limit`` es el tamaño de cada página."""
        page: Page | None = self.list(filter=filter, sort=sort, fields=fields, include=include, limit=limit)
        while page is not None:
            yield from page.items
            page = page.next_page()

    def count(self, *, filter: Filter | None = None) -> Total:
        """Cuántos elementos hay (§6.5). ``Total.estimated`` dice si el número es aproximado."""
        page = self.list(filter=filter, limit=1, count=True)
        if page.total is None:
            raise E.RestepError(200, {"detail": "Se pidió count=true y la respuesta no trae total (§6.5)."}, local=True)
        return page.total

    def get(self, id: Any, *, fields: str | Sequence[str] | None = None,
            include: str | Sequence[str] | None = None) -> Item:
        """Un elemento. Lleva su ETag de elemento en ``_etag`` (§7.2)."""
        resp, body = self._client._call(self.get_request(id, fields, include))
        return body if fields or include else with_etag(resp, body)

    def create(self, data: Mapping[str, Any], *, idempotency_key: IdempotencyKey = AUTO) -> Item:
        """Crea un elemento (§5.1). Con ``writes``, lleva una ``Idempotency-Key`` nueva salvo que se
        pase otra o ``None`` (§7.1); los reintentos usan la misma."""
        resp, body = self._client._call(self.create_request(data, idempotency_key))
        return with_etag(resp, body if isinstance(body, dict) else {})

    def update(self, id: Any, patch: Mapping[str, Any], *, if_match: str | Mapping[str, Any] | None = None) -> Item:
        """Actualiza con JSON Merge Patch (§5.1). ``if_match`` es una ETag o un elemento leído (§7.2)."""
        resp, body = self._client._call(self.update_request(id, patch, if_match))
        return with_etag(resp, body if isinstance(body, dict) else {})

    def delete(self, id: Any, *, if_match: str | Mapping[str, Any] | None = None) -> None:
        """Borra un elemento (§5.1)."""
        self._client._call(self.delete_request(id, if_match))

    def action(self, id: Any, name: str, data: Any = None, *, idempotency_key: IdempotencyKey = AUTO) -> Any:
        """Ejecuta una acción declarada (§10.1): de elemento con su id, de colección con ``None``.
        Devuelve el cuerpo de la respuesta, o ``None`` si no lo hay (204)."""
        _, body = self._client._call(self.action_request(id, name, data, idempotency_key))
        return body

    def __repr__(self) -> str:
        return f"<restep.Collection {self.name} {self.path_template}>"


def connect(url: str, token: str | None = None, language: str | None = None, *,
            headers: Mapping[str, str] | None = None, validate: bool = True, retries: int = 0,
            max_retry_wait: float = 60.0, timeout: float = 30.0) -> Api:
    """Conecta con una API Restep leyendo su documento de descubrimiento (§4.2).

    - ``token``: se envía como ``Authorization: Bearer`` solo al origen de la API (§4.3).
    - ``language``: ``Accept-Language`` para los textos de la API (errores, descripciones, §5.6).
    - ``validate``: comprueba los parámetros contra el descubrimiento antes de enviar.
    - ``retries``: cuántas veces repetir ante ``429`` o ``503`` (este, solo si repetir es seguro),
      esperando lo que diga ``Retry-After`` hasta ``max_retry_wait`` segundos.
    """
    api = Api(url, token=token, language=language, headers=headers, validate=validate, retries=retries,
              max_retry_wait=max_retry_wait, timeout=timeout)
    api._discover()
    return api
