"""restep · cliente genérico para APIs Restep 1, guiado por el documento de descubrimiento.

    import restep
    api = restep.connect("http://localhost:8080/")
    page = api["books"].list(filter={"price": {"le": "20"}}, sort="-rating", limit=10)

Especificación: https://restep.wubytes.com/1/ (borrador 6).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ._core import AUTO, IMPLEMENTED_VERSIONS, Item, Total, parse_version
from ._sync import Api, Collection, Page, Singleton, connect
from .errors import *  # noqa: F401,F403
from .errors import RestepError, __all__ as _errors

__version__ = "0.2.0"

__all__ = ["connect", "Api", "Collection", "Singleton", "Page", "AUTO", "Item", "Total", "IMPLEMENTED_VERSIONS", "parse_version", "oauth_metadata_url",
           "oauth_metadata", "__version__", *_errors]


def oauth_metadata_url(root: str) -> str:
    """La URL de los metadatos de recurso protegido de una raíz de API (§11.5, RFC 9728 3.1):
    el sufijo well-known va entre el host y la ruta, que conserva su barra final; si la raíz es
    el host a secas, su barra desaparece."""
    p = urlsplit(root)
    path = "" if p.path in ("", "/") else p.path
    return urlunsplit((p.scheme, p.netloc, "/.well-known/oauth-protected-resource" + path, "", ""))


def oauth_metadata(root: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Los metadatos de recurso protegido de la API (§11.5), pedidos sin credenciales. Comprueba
    que ``resource`` sea la raíz, como exige el RFC 9728 (3.3)."""
    if not root.endswith("/"):
        root += "/"
    url = oauth_metadata_url(root)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"accept": "application/json"}), timeout=timeout) as f:
            meta = json.loads(f.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RestepError(e.code, {"detail": f"No hay metadatos de recurso protegido en {url}."}, url=url) from None
    if not isinstance(meta, dict) or meta.get("resource") != root:
        raise RestepError(200, {"detail": f"Los metadatos de {url} no son de {root} (RFC 9728, 3.3)."}, url=url, local=True)
    return meta
