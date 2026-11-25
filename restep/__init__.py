"""restep · generic client for Restep 1 APIs, driven by the discovery document.

    import restep
    api = restep.connect("http://localhost:8080/")
    page = api["books"].list(filter={"price": {"le": "20"}}, sort="-rating", limit=10)

Specification: https://restep.wubytes.com/1/ (draft 7).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ._core import (AUTO, IMPLEMENTED_VERSIONS, ActionResult, Created, Item, Total, duration_seconds, effects_of, is_duration,
                    iso_duration, local_to_instant, parse_version)
from ._sync import Api, Collection, Page, Singleton, connect
from .errors import *  # noqa: F401,F403
from .errors import RestepError, __all__ as _errors

__version__ = "0.3.0"

__all__ = ["connect", "Api", "Collection", "Singleton", "Page", "AUTO", "Item", "Total", "ActionResult", "Created", "IMPLEMENTED_VERSIONS",
           "parse_version", "is_duration", "duration_seconds", "iso_duration", "local_to_instant", "effects_of",
           "oauth_metadata_url", "oauth_metadata", "__version__", *_errors]


def oauth_metadata_url(root: str) -> str:
    """The URL of the protected resource metadata of an API root (§11.5, RFC 9728 3.1):
    the well-known suffix goes between the host and the path, which keeps its trailing slash; if the
    root is the bare host, its slash disappears."""
    p = urlsplit(root)
    path = "" if p.path in ("", "/") else p.path
    return urlunsplit((p.scheme, p.netloc, "/.well-known/oauth-protected-resource" + path, "", ""))


def oauth_metadata(root: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """The API's protected resource metadata (§11.5), requested without credentials. Checks
    that ``resource`` is the root, as RFC 9728 (3.3) requires."""
    if not root.endswith("/"):
        root += "/"
    url = oauth_metadata_url(root)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"accept": "application/json"}), timeout=timeout) as f:
            meta = json.loads(f.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RestepError(e.code, {"detail": f"There is no protected resource metadata at {url}."}, url=url) from None
    if not isinstance(meta, dict) or meta.get("resource") != root:
        raise RestepError(200, {"detail": f"The metadata at {url} is not for {root} (RFC 9728, 3.3)."}, url=url, local=True)
    return meta
