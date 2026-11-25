"""RFC 9457 problem+json errors. Every error the API returns goes through here."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.core.logging import correlation_id_var

log = logging.getLogger(__name__)
PROBLEM_BASE = "https://errors.entitlement-platform.example/"
PROBLEM_MEDIA_TYPE = "application/problem+json"


class ProblemException(Exception):
    def __init__(self, status: int, slug: str, title: str, detail: str | None = None,
                 headers: dict[str, str] | None = None, **extra):
        super().__init__(detail or title)
        self.status, self.slug, self.title, self.detail = status, slug, title, detail
        self.headers = headers or {}
        self.extra = extra

    def to_dict(self, instance: str | None = None) -> dict:
        body = {"type": PROBLEM_BASE + self.slug, "title": self.title, "status": self.status}
        if self.detail:
            body["detail"] = self.detail
        if instance:
            body["instance"] = instance
        body["correlation_id"] = correlation_id_var.get()
        body.update(self.extra)
        return body


# Small constructors keep call sites short and the error vocabulary consistent.
def not_found(what: str, ident: str) -> ProblemException:
    return ProblemException(404, "not-found", "Resource not found", f"{what} '{ident}' does not exist.")


def forbidden(detail: str) -> ProblemException:
    return ProblemException(403, "forbidden", "Forbidden", detail)


def unauthenticated(detail: str) -> ProblemException:
    return ProblemException(401, "unauthenticated", "Authentication required", detail,
                            headers={"WWW-Authenticate": 'Bearer realm="entitlement-platform"'})


def conflict(slug: str, title: str, detail: str) -> ProblemException:
    return ProblemException(409, slug, title, detail)


def upstream_unavailable(system: str, detail: str | None = None) -> ProblemException:
    return ProblemException(503, "upstream-unavailable", "Upstream system unavailable",
                            detail or f"The {system} is temporarily unavailable.",
                            headers={"Retry-After": "5"}, upstream=system)


def problem_response(exc: ProblemException, request: Request) -> JSONResponse:
    return JSONResponse(exc.to_dict(instance=request.url.path), status_code=exc.status,
                        headers=exc.headers, media_type=PROBLEM_MEDIA_TYPE)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemException)
    async def _problem(request: Request, exc: ProblemException):
        return problem_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"location": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return problem_response(ProblemException(422, "validation-error", "Request validation failed",
                                                 "One or more request parameters are invalid.", errors=errors), request)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        slug = {404: "not-found", 405: "method-not-allowed"}.get(exc.status_code, "http-error")
        return problem_response(ProblemException(exc.status_code, slug, str(exc.detail)), request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        return problem_response(ProblemException(500, "internal-error", "Internal server error",
                                                 "An unexpected error occurred. Quote the correlation_id to support."),
                                request)
