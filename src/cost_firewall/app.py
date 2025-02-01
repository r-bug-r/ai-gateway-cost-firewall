"""HTTP surface: OpenAI-compatible chat completions plus operational endpoints."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from cost_firewall.config import Config
from cost_firewall.errors import (
    GatewayError,
    PayloadTooLarge,
    UpstreamTimeout,
    ValidationError,
)
from cost_firewall.gateway import Gateway, chat_completion_body
from cost_firewall.ledger import now_ms, utc_month_start_ms

logger = logging.getLogger("cost_firewall.http")

_MAX_REPORT_WINDOW_DAYS = 366


class BodyReader:
    """Reads and size-limits the raw request body before JSON parsing."""

    def __init__(self, request: Request, max_body_bytes: int):
        self._request = request
        self._max = max_body_bytes

    async def read_json(self) -> object:
        declared = self._request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max:
                    raise PayloadTooLarge(f"Request body exceeds {self._max} bytes.")
            except ValueError as exc:
                raise PayloadTooLarge("Invalid Content-Length header.") from exc
        chunks: list[bytes] = []
        total = 0
        async for chunk in self._request.stream():
            total += len(chunk)
            if total > self._max:
                raise PayloadTooLarge(f"Request body exceeds {self._max} bytes.")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw:
            raise ValidationError("Request body is empty.")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Request body is not valid JSON: {exc.msg}") from exc


def _error_response(error: GatewayError) -> JSONResponse:
    headers: dict[str, str] = {}
    if error.retry_after is not None:
        headers["retry-after"] = str(error.retry_after)
    return JSONResponse(
        status_code=error.status_code,
        content=error.envelope().as_dict(),
        headers=headers or None,
    )


def create_app(config: Config, gateway: Gateway) -> FastAPI:
    app = FastAPI(
        title="AI Gateway Cost Firewall",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    max_body = config.server.max_body_bytes

    @app.middleware("http")
    async def request_log_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Access log: method, path, status, latency. Never prompt text."""
        started = time.perf_counter()
        response = await call_next(request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "%s %s -> %s in %dms",
            request.method,
            request.url.path,
            response.status_code,
            latency_ms,
        )
        return response

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, GatewayError)
        return _error_response(exc)

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled gateway failure")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Internal gateway error.",
                    "type": "api_error",
                    "param": None,
                    "code": "internal_error",
                }
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        try:
            with gateway.ledger.connection() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:  # pragma: no cover - exercised by the fail-closed test
            return {"status": "degraded", "ledger": "unavailable"}
        return {"status": "ok", "ledger": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        return gateway.metrics.render()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        authorization = request.headers.get("authorization")
        payload = await BodyReader(request, max_body).read_json()
        # `gateway.chat` is synchronous (SQLite + provider HTTP). Run it in a worker
        # thread so one slow provider call cannot stall the event loop.
        result = await run_in_threadpool(gateway.chat, authorization, payload)
        body = chat_completion_body(result)
        return JSONResponse(status_code=200, content=body)

    @app.post("/v1/gateway/dry-run")
    async def dry_run(request: Request) -> Response:
        authorization = request.headers.get("authorization")
        payload = await BodyReader(request, max_body).read_json()
        decision = await run_in_threadpool(gateway.dry_run, authorization, payload)
        return JSONResponse(status_code=200, content=decision)

    @app.get("/v1/gateway/usage")
    async def usage(
        request: Request, project: str | None = None, days: int | None = None
    ) -> Response:
        selected = _authorize_report(gateway, request.headers.get("authorization"), project)
        since_ms = None
        if days is not None:
            if days < 1 or days > _MAX_REPORT_WINDOW_DAYS:
                raise ValidationError(
                    f"'days' must be between 1 and {_MAX_REPORT_WINDOW_DAYS}.",
                    param="days",
                )
            since_ms = now_ms() - days * 86_400_000
        return JSONResponse(status_code=200, content=gateway.usage(selected, since_ms=since_ms))

    @app.get("/v1/gateway/budget")
    async def budget(request: Request, project: str | None = None) -> Response:
        selected = _authorize_report(gateway, request.headers.get("authorization"), project)
        return JSONResponse(status_code=200, content=gateway.budget_report(selected))

    @app.get("/v1/gateway/requests/{request_id}")
    async def request_trace(request_id: str, request: Request) -> Response:
        selected = _authorize_report(gateway, request.headers.get("authorization"), None)
        trace = gateway.request_trace(request_id)
        if trace is None or trace["project_id"] != selected.id:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": "Request not found.",
                        "type": "invalid_request_error",
                        "param": "request_id",
                        "code": "request_not_found",
                    }
                },
            )
        return JSONResponse(status_code=200, content=trace)

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        project = gateway.authenticator.resolve(request.headers.get("authorization"))
        allowed = [
            route.model
            for route in config.routes
            if not project.allowed_models or route.model in project.allowed_models
        ]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": [{"id": model, "object": "model"} for model in allowed],
            },
        )

    return app


def _authorize_report(gateway: Gateway, authorization: str | None, project_id: str | None) -> Any:
    from cost_firewall.config import ProjectConfig

    project: ProjectConfig = gateway.authenticator.resolve(authorization)
    if project_id is None or project_id == project.id:
        return project
    # Cross-project reads require a key that belongs to the requested project.
    target = gateway.config.project(project_id)
    if target is None:
        raise ValidationError(f"Unknown project '{project_id}'.", param="project")
    # Reading another project's ledger requires holding a key for that project too:
    # the credential proves authorization for the data being read.
    for candidate in target.api_key_hashes:
        if candidate in project.api_key_hashes:
            return target
    raise GatewayError("This API key cannot read another project's usage.", param="project")


def month_window_start() -> int:
    return utc_month_start_ms(now_ms())


async def lifespan_shim(_: FastAPI) -> AsyncIterator[None]:  # pragma: no cover
    yield


__all__ = ["UpstreamTimeout", "create_app", "month_window_start"]
