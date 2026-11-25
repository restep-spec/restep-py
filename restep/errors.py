"""Typed exceptions built from Restep's problem+json errors (§5.4).

Each ``code`` in the table of §5.4 has its class. An unknown ``code`` is handled by the
class of its status (§5.4: "The client MUST treat a code it does not know by the class of the
status"): the default class for that status if it is not ambiguous, otherwise ``ClientError`` or
``ServerError``.

Errors the client detects before sending the request (local validation against the
discovery document) are of the same classes, with ``local = True``, so that the code that
catches them does not have to tell who detected them.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = [
    "RestepError", "ClientError", "ServerError",
    "BadRequest", "InvalidParameter", "UnsupportedParameter", "IdempotencyKeyRequired",
    "Unauthenticated", "Forbidden", "NotFound", "MethodNotAllowed", "NotAcceptable", "RequestTimeout", "Conflict",
    "RequestInProgress", "Gone", "StateExpired", "Retired", "PreconditionFailed", "ContentTooLarge", "UriTooLong",
    "UnsupportedMediaType", "MisdirectedRequest", "Invalid", "IdempotencyMismatch", "NotExecuted", "TooEarly",
    "PreconditionRequired", "RateLimited", "Internal", "ServerNotImplemented", "BadGateway", "Unavailable", "GatewayTimeout",
    "CODES", "code_for_status",
    "RestepDeprecationWarning", "UnsupportedVersion", "ExperimentalNotUsable", "error_from_problem",
]


class RestepError(Exception):
    """An error from a Restep API. ``problem`` is the whole problem+json object."""

    code: str | None = None  # the code the class stands for; the instance carries the one that arrived

    def __init__(self, status: int, problem: Mapping[str, Any], *, headers: Mapping[str, str] | None = None,
                 local: bool = False, url: str | None = None, method: str | None = None) -> None:
        self.status = status
        self.problem: dict[str, Any] = dict(problem)
        self.code = _str(problem.get("code"))
        self.type = _str(problem.get("type"))
        self.title = _str(problem.get("title"))
        self.detail = _str(problem.get("detail"))
        self.instance = _str(problem.get("instance"))
        self.reason = _str(problem.get("reason"))
        self.parameter = _str(problem.get("parameter"))
        errors = problem.get("errors")
        self.errors: list[dict[str, Any]] = [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
        self.headers: dict[str, str] = {k.lower(): v for k, v in (headers or {}).items()}
        self.local = local
        self.url = url
        self.method = method
        super().__init__(self._message())

    def _message(self) -> str:
        text = self.detail or self.title or f"HTTP {self.status}"
        where = f" ({self.parameter})" if self.parameter else ""
        origin = " [detected in the client]" if self.local else ""
        return f"{self.status} {self.code or ''}{where}: {text}{origin}".replace("  ", " ")

    @property
    def retry_after(self) -> float | None:
        """Seconds that ``Retry-After`` asks to wait, if it arrived (in seconds or as an HTTP date)."""
        return parse_retry_after(self.headers.get("retry-after"))

    @property
    def accept_post(self) -> list[str] | None:
        """The media types of ``Accept-Post``, if it arrived: on a ``415`` to a ``POST``, what the
        collection accepts (§5.1)."""
        value = self.headers.get("accept-post")
        return None if value is None else [t.strip() for t in value.split(",") if t.strip()]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status}, code={self.code!r}, parameter={self.parameter!r}, detail={self.detail!r})"


class ClientError(RestepError):
    """A 4xx: the request has to change."""


class ServerError(RestepError):
    """A 5xx: the failure is the server's."""


# ── 4xx ───────────────────────────────────────────────────────────────────────
class BadRequest(ClientError):
    code = "bad_request"


class InvalidParameter(BadRequest):
    """A supported parameter with a wrong value: fix the value and retry (§5.3)."""
    code = "invalid_parameter"


class UnsupportedParameter(BadRequest):
    """The resource does not offer what the parameter asks for: do not retry, give up the capability (§5.3)."""
    code = "unsupported_parameter"


class IdempotencyKeyRequired(BadRequest):
    code = "idempotency_key_required"


class Unauthenticated(ClientError):
    code = "unauthenticated"

    @property
    def www_authenticate(self) -> str | None:
        return self.headers.get("www-authenticate")

    @property
    def resource_metadata(self) -> str | None:
        """The URL of the protected resource metadata from the challenge, if it carries one (§11.5, RFC 9728)."""
        m = re.search(r'resource_metadata="([^"]*)"', self.www_authenticate or "")
        return m.group(1) if m else None


class Forbidden(ClientError):
    code = "forbidden"


class NotFound(ClientError):
    code = "not_found"


class MethodNotAllowed(ClientError):
    code = "method_not_allowed"

    @property
    def allow(self) -> list[str]:
        return [m.strip() for m in self.headers.get("allow", "").split(",") if m.strip()]


class NotAcceptable(ClientError):
    code = "not_acceptable"


class RequestTimeout(ClientError):
    """408: the server did not get the whole request in time; it can be repeated as is (§5.7)."""
    code = "request_timeout"


class Conflict(ClientError):
    """Clashes with the current state: a duplicate, an impossible transition. Repeating it is no use."""
    code = "conflict"


class RequestInProgress(ClientError):
    """The same request with the same ``Idempotency-Key`` is still running (§7.1): it can be sent
    again with the same key, after ``retry_after`` if it arrived. It is not a ``Conflict``."""
    code = "request_in_progress"


class Gone(ClientError):
    """A 410: the base of ``StateExpired`` and ``Retired``."""


class StateExpired(Gone):
    code = "state_expired"


class Retired(Gone):
    code = "retired"


class PreconditionFailed(ClientError):
    """``If-Match`` does not match: someone changed the item; read it again (§7.2)."""
    code = "precondition_failed"


class ContentTooLarge(ClientError):
    code = "content_too_large"


class UriTooLong(ClientError):
    code = "uri_too_long"


class UnsupportedMediaType(ClientError):
    code = "unsupported_media_type"


class MisdirectedRequest(ClientError):
    """421: the request reached a server that cannot answer for this origin; it can be repeated over
    another connection (§5.7)."""
    code = "misdirected_request"


class Invalid(ClientError):
    """The body is understood but not valid; the detail is in ``errors`` (§5.4)."""
    code = "invalid"


class IdempotencyMismatch(ClientError):
    code = "idempotency_mismatch"


class NotExecuted(ClientError):
    code = "not_executed"


class TooEarly(ClientError):
    """425: the server will not process a request sent in TLS early data; it can be repeated without
    it (§5.7, RFC 8470). This client never sends early data."""
    code = "too_early"


class PreconditionRequired(ClientError):
    code = "precondition_required"


class RateLimited(ClientError):
    code = "rate_limited"


# ── 5xx ───────────────────────────────────────────────────────────────────────
class Internal(ServerError):
    code = "internal"


class ServerNotImplemented(ServerError):
    code = "not_implemented"


class BadGateway(ServerError):
    """502: a server the API depends on answered wrongly; it says nothing about the effect (§5.7)."""
    code = "bad_gateway"


class Unavailable(ServerError):
    code = "unavailable"


class GatewayTimeout(ServerError):
    """504: a server the API depends on did not answer in time; it says nothing about the effect (§5.7)."""
    code = "gateway_timeout"


class RestepDeprecationWarning(DeprecationWarning):
    """The response carried ``Deprecation`` (§5.5): what was used is going to be retired."""


class UnsupportedVersion(ValueError):
    """The discovery document declares in ``restep`` something that is not a Restep 1 version:
    it does not follow the grammar of §1.1 or its major version is not 1. A Restep 1 client does not
    treat it as discovery nor assume ``core`` only because of it (§1.1): that API is not Restep 1."""

    def __init__(self, version: Any, url: str | None = None) -> None:
        self.version = version
        self.url = url
        where = f" at {url}" if url else ""
        super().__init__(f"The discovery document{where} declares restep={version!r}, which is not a Restep 1 version (§1.1).")


class ExperimentalNotUsable(LookupError):
    """An experimental part (§1.2) that the server does not announce, or announces under a version whose
    text this client does not implement: it is not used (§1.2)."""


_BY_CODE: dict[str, type[RestepError]] = {
    cls.code: cls for cls in (
        BadRequest, InvalidParameter, UnsupportedParameter, IdempotencyKeyRequired, Unauthenticated, Forbidden,
        NotFound, MethodNotAllowed, NotAcceptable, RequestTimeout, Conflict, RequestInProgress, StateExpired, Retired,
        PreconditionFailed, ContentTooLarge, UriTooLong, UnsupportedMediaType, MisdirectedRequest, Invalid,
        IdempotencyMismatch, NotExecuted, TooEarly, PreconditionRequired, RateLimited, Internal, ServerNotImplemented,
        BadGateway, Unavailable, GatewayTimeout)
    if cls.code
}

# For an unknown code: the status's class when there is only one candidate.
_BY_STATUS: dict[int, type[RestepError]] = {
    400: BadRequest, 401: Unauthenticated, 403: Forbidden, 404: NotFound, 405: MethodNotAllowed,
    406: NotAcceptable, 408: RequestTimeout, 409: Conflict, 410: Gone, 412: PreconditionFailed, 413: ContentTooLarge,
    414: UriTooLong, 415: UnsupportedMediaType, 421: MisdirectedRequest, 424: NotExecuted, 425: TooEarly,
    428: PreconditionRequired, 429: RateLimited, 500: Internal, 501: ServerNotImplemented, 502: BadGateway,
    503: Unavailable, 504: GatewayTimeout,
}

# The closed table of §5.4 (and of problem.json): each code with its status. A status the table does
# not list has the code http_ and its three digits (http_418).
CODES: dict[str, int] = {
    "bad_request": 400, "invalid_parameter": 400, "unsupported_parameter": 400, "idempotency_key_required": 400,
    "unauthenticated": 401, "forbidden": 403, "not_found": 404, "method_not_allowed": 405, "not_acceptable": 406,
    "request_timeout": 408, "conflict": 409, "request_in_progress": 409, "state_expired": 410, "retired": 410,
    "precondition_failed": 412, "content_too_large": 413, "uri_too_long": 414, "unsupported_media_type": 415,
    "misdirected_request": 421, "invalid": 422, "idempotency_mismatch": 422, "not_executed": 424, "too_early": 425,
    "precondition_required": 428, "rate_limited": 429, "internal": 500, "not_implemented": 501, "bad_gateway": 502,
    "unavailable": 503, "gateway_timeout": 504,
}
_STATUS_OF = CODES
_TABLE_STATUSES = frozenset(CODES.values())


def code_for_status(status: int) -> str | None:
    """The code a status gets when the table of §5.4 gives it only one, ``http_`` and its digits when
    the table does not list it, or ``None`` when the table gives it several (400, 409, 410, 422)."""
    if status not in _TABLE_STATUSES:
        return f"http_{status}"
    codes = [c for c, s in CODES.items() if s == status]
    return codes[0] if len(codes) == 1 else None


def error_class(status: int, code: str | None) -> type[RestepError]:
    if code in _BY_CODE and _STATUS_OF.get(code) == status:
        return _BY_CODE[code]
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    return ServerError if status >= 500 else ClientError


def error_from_problem(status: int, problem: Mapping[str, Any], **kwargs: Any) -> RestepError:
    """The exception that corresponds to a problem+json with that status."""
    return error_class(status, _str(problem.get("code")))(status, problem, **kwargs)


def local_error(cls: type[RestepError], detail: str, *, parameter: str | None = None,
                status: int = 400, **extra: Any) -> RestepError:
    """An error detected in the client, with the same shape the server would give."""
    code = cls.code or code_for_status(status) or "bad_request"
    problem: dict[str, Any] = {"type": f"https://w3id.org/restep/1/errors#{code}", "title": detail,
                               "status": status, "code": code, "detail": detail, **extra}
    if parameter is not None:
        problem["parameter"] = parameter
    return cls(status, problem, local=True)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    from email.utils import parsedate_to_datetime
    import datetime as _dt
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds())


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) else None
