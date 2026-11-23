"""Excepciones tipadas a partir de los errores problem+json de Restep (§5.4).

Cada ``code`` de la tabla de §5.4 tiene su clase. Un ``code`` desconocido se trata según la
clase del estado (§5.4: «El cliente DEBE tratar un code que no conoce según la clase del
status»): la clase por defecto de ese estado si no es ambigua, si no ``ClientError`` o
``ServerError``.

Los errores que el cliente detecta antes de enviar la petición (validación local contra el
descubrimiento) son de las mismas clases, con ``local = True``, para que el código que los
captura no tenga que distinguir quién los ha detectado.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = [
    "RestepError", "ClientError", "ServerError",
    "BadRequest", "InvalidParameter", "UnsupportedParameter", "IdempotencyKeyRequired",
    "Unauthenticated", "Forbidden", "NotFound", "MethodNotAllowed", "NotAcceptable", "Conflict",
    "RequestInProgress", "Gone", "StateExpired", "Retired", "PreconditionFailed", "ContentTooLarge", "UriTooLong",
    "UnsupportedMediaType", "Invalid", "IdempotencyMismatch", "NotExecuted", "PreconditionRequired",
    "RateLimited", "Internal", "ServerNotImplemented", "Unavailable",
    "RestepDeprecationWarning", "UnsupportedVersion", "ExperimentalNotUsable", "error_from_problem",
]


class RestepError(Exception):
    """Un error de una API Restep. ``problem`` es el objeto problem+json entero."""

    code: str | None = None  # el code que representa la clase; la instancia lleva el que llegó

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
        origin = " [detectado en el cliente]" if self.local else ""
        return f"{self.status} {self.code or ''}{where}: {text}{origin}".replace("  ", " ")

    @property
    def retry_after(self) -> float | None:
        """Segundos que pide esperar ``Retry-After``, si llegó (en segundos o como fecha HTTP)."""
        return parse_retry_after(self.headers.get("retry-after"))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status}, code={self.code!r}, parameter={self.parameter!r}, detail={self.detail!r})"


class ClientError(RestepError):
    """Un 4xx: hay que cambiar la petición."""


class ServerError(RestepError):
    """Un 5xx: el fallo es del servidor."""


# ── 4xx ───────────────────────────────────────────────────────────────────────
class BadRequest(ClientError):
    code = "bad_request"


class InvalidParameter(BadRequest):
    """Un parámetro admitido con un valor erróneo: corrige el valor y reintenta (§5.3)."""
    code = "invalid_parameter"


class UnsupportedParameter(BadRequest):
    """El recurso no ofrece lo que pide el parámetro: no reintentes, renuncia a la capacidad (§5.3)."""
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
        """La URL de los metadatos de recurso protegido del reto, si la trae (§11.5, RFC 9728)."""
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


class Conflict(ClientError):
    """Choca con el estado actual: un duplicado, una transición imposible. Repetirla no sirve."""
    code = "conflict"


class RequestInProgress(ClientError):
    """La misma petición con la misma ``Idempotency-Key`` sigue en curso (§7.1): se puede volver a
    enviar con la misma clave, después de ``retry_after`` si llegó. No es un ``Conflict``."""
    code = "request_in_progress"


class Gone(ClientError):
    """Un 410: base de ``StateExpired`` y ``Retired``."""


class StateExpired(Gone):
    code = "state_expired"


class Retired(Gone):
    code = "retired"


class PreconditionFailed(ClientError):
    """``If-Match`` no coincide: alguien cambió el elemento; vuelve a leerlo (§7.2)."""
    code = "precondition_failed"


class ContentTooLarge(ClientError):
    code = "content_too_large"


class UriTooLong(ClientError):
    code = "uri_too_long"


class UnsupportedMediaType(ClientError):
    code = "unsupported_media_type"


class Invalid(ClientError):
    """El cuerpo se entiende pero no es válido; el detalle va en ``errors`` (§5.4)."""
    code = "invalid"


class IdempotencyMismatch(ClientError):
    code = "idempotency_mismatch"


class NotExecuted(ClientError):
    code = "not_executed"


class PreconditionRequired(ClientError):
    code = "precondition_required"


class RateLimited(ClientError):
    code = "rate_limited"


# ── 5xx ───────────────────────────────────────────────────────────────────────
class Internal(ServerError):
    code = "internal"


class ServerNotImplemented(ServerError):
    code = "not_implemented"


class Unavailable(ServerError):
    code = "unavailable"


class RestepDeprecationWarning(DeprecationWarning):
    """La respuesta llevaba ``Deprecation`` (§5.5): lo que se ha usado se va a retirar."""


class UnsupportedVersion(ValueError):
    """El documento de descubrimiento declara en ``restep`` algo que no es una versión de Restep 1:
    no sigue la gramática de §1.1 o su versión mayor no es 1. Un cliente de Restep 1 no lo trata
    como descubrimiento ni supone por eso solo ``core`` (§1.1): esa API no es Restep 1."""

    def __init__(self, version: Any, url: str | None = None) -> None:
        self.version = version
        self.url = url
        where = f" en {url}" if url else ""
        super().__init__(f"El descubrimiento{where} declara restep={version!r}, que no es una versión de Restep 1 (§1.1).")


class ExperimentalNotUsable(LookupError):
    """Una parte experimental (§1.2) que el servidor no anuncia, o que anuncia bajo una versión cuyo
    texto este cliente no implementa: no se usa (§1.2)."""


_BY_CODE: dict[str, type[RestepError]] = {
    cls.code: cls for cls in (
        BadRequest, InvalidParameter, UnsupportedParameter, IdempotencyKeyRequired, Unauthenticated, Forbidden,
        NotFound, MethodNotAllowed, NotAcceptable, Conflict, RequestInProgress, StateExpired, Retired, PreconditionFailed,
        ContentTooLarge, UriTooLong, UnsupportedMediaType, Invalid, IdempotencyMismatch, NotExecuted,
        PreconditionRequired, RateLimited, Internal, ServerNotImplemented, Unavailable)
    if cls.code
}

# Para un code desconocido: la clase del estado cuando solo hay una candidata.
_BY_STATUS: dict[int, type[RestepError]] = {
    400: BadRequest, 401: Unauthenticated, 403: Forbidden, 404: NotFound, 405: MethodNotAllowed,
    406: NotAcceptable, 409: Conflict, 410: Gone, 412: PreconditionFailed, 413: ContentTooLarge,
    414: UriTooLong, 415: UnsupportedMediaType, 424: NotExecuted, 428: PreconditionRequired,
    429: RateLimited, 500: Internal, 501: ServerNotImplemented, 503: Unavailable,
}

# Estados que la tabla de §5.4 asocia a cada code: si llegan juntos de forma incoherente, manda el estado.
_STATUS_OF: dict[str, int] = {
    "bad_request": 400, "invalid_parameter": 400, "unsupported_parameter": 400, "idempotency_key_required": 400,
    "unauthenticated": 401, "forbidden": 403, "not_found": 404, "method_not_allowed": 405, "not_acceptable": 406,
    "conflict": 409, "request_in_progress": 409, "state_expired": 410, "retired": 410, "precondition_failed": 412, "content_too_large": 413,
    "uri_too_long": 414, "unsupported_media_type": 415, "invalid": 422, "idempotency_mismatch": 422,
    "not_executed": 424, "precondition_required": 428, "rate_limited": 429, "internal": 500,
    "not_implemented": 501, "unavailable": 503,
}


def error_class(status: int, code: str | None) -> type[RestepError]:
    if code in _BY_CODE and _STATUS_OF.get(code) == status:
        return _BY_CODE[code]
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    return ServerError if status >= 500 else ClientError


def error_from_problem(status: int, problem: Mapping[str, Any], **kwargs: Any) -> RestepError:
    """La excepción que corresponde a un problem+json con ese estado."""
    return error_class(status, _str(problem.get("code")))(status, problem, **kwargs)


def local_error(cls: type[RestepError], detail: str, *, parameter: str | None = None,
                status: int = 400, **extra: Any) -> RestepError:
    """Un error detectado en el cliente, con la misma forma que el que daría el servidor."""
    code = cls.code or "bad_request"
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
