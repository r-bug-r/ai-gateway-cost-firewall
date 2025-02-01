"""Gateway orchestration: auth, budget reservation, routing, fallback, settlement.

Ordering is deliberate and mirrors the documented data flow:

    auth -> validation -> rate limit -> concurrency -> budget reservation
         -> redaction -> provider attempts -> settle -> ledger

Budget reservation happens *before* any provider call, inside a serialized SQLite
transaction, so concurrent requests cannot both spend the same headroom.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cost_firewall.auth import Authenticator
from cost_firewall.config import Config, ProjectConfig, RouteConfig, TargetConfig
from cost_firewall.errors import (
    BudgetExhausted,
    BudgetStateUnavailable,
    ConcurrencyLimited,
    ModelNotFound,
    PerRequestLimitExceeded,
    ProviderError,
    RateLimited,
    UpstreamFailure,
    UpstreamTimeout,
)
from cost_firewall.ledger import (
    BudgetPolicy,
    BudgetSnapshot,
    BudgetUnavailable,
    Ledger,
    ReservationDenied,
)
from cost_firewall.limits import ConcurrencyLimiter, RateLimiter
from cost_firewall.metrics import Metrics
from cost_firewall.models import (
    AttemptOutcome,
    ChatRequest,
    ChatResult,
    Message,
    parse_chat_request,
)
from cost_firewall.money import format_usd, micros_to_usd, price_micros
from cost_firewall.providers import ProviderAdapter, build_adapter
from cost_firewall.redaction import LogRedactor, Redactor
from cost_firewall.tokens import estimate_message_tokens

logger = logging.getLogger("cost_firewall.gateway")


@dataclass(frozen=True)
class TargetPlan:
    provider: str
    model: str
    estimated_cost_micros: int


@dataclass(frozen=True)
class RoutingPlan:
    route_model: str
    targets: tuple[TargetPlan, ...]
    reservation_micros: int
    estimated_input_tokens: int
    fallback_allowed: bool
    reason: str


@dataclass
class _Context:
    project: ProjectConfig
    request: ChatRequest
    request_id: str
    plan: RoutingPlan
    reserved: bool
    attempts: list[AttemptOutcome] = field(default_factory=list)


class Gateway:
    def __init__(
        self,
        config: Config,
        ledger: Ledger,
        *,
        adapters: dict[str, ProviderAdapter] | None = None,
        metrics: Metrics | None = None,
    ):
        self.config = config
        self.ledger = ledger
        self.metrics = metrics or Metrics()
        self.authenticator = Authenticator(config)
        self.redactor = Redactor(config.redaction)
        self.log_redactor = LogRedactor(self.redactor)
        self._adapters: dict[str, ProviderAdapter] = adapters or {
            provider.id: build_adapter(provider) for provider in config.providers
        }
        self._rate_limiters = {
            project.id: RateLimiter(limit=project.requests_per_minute)
            for project in config.projects
        }
        self._concurrency = ConcurrencyLimiter(
            {project.id: project.max_concurrency for project in config.projects}
        )
        self._sync_projects()

    def _sync_projects(self) -> None:
        self.ledger.sync_projects(
            [
                BudgetPolicy(
                    project_id=project.id,
                    name=project.name,
                    monthly_budget_micros=project.monthly_budget_micros,
                    daily_budget_micros=project.daily_budget_micros,
                    per_request_max_micros=project.per_request_max_micros,
                )
                for project in self.config.projects
            ]
        )

    # --------------------------------------------------------------- planning

    def _resolve_route(self, project: ProjectConfig, model: str) -> RouteConfig:
        if project.allowed_models and model not in project.allowed_models:
            raise ModelNotFound(
                f"Model '{model}' is not allowed for project '{project.id}'. "
                f"Allowed: {', '.join(project.allowed_models)}.",
                param="model",
            )
        route = self.config.route(model)
        if route is None:
            raise ModelNotFound(
                f"Model '{model}' has no route in this gateway. "
                f"Configured models: {', '.join(r.model for r in self.config.routes)}.",
                param="model",
            )
        return route

    @staticmethod
    def estimate_cost_micros(request: ChatRequest, target: TargetConfig) -> int:
        input_tokens = estimate_message_tokens(request.messages)
        return price_micros(input_tokens, target.input_usd_per_1m) + price_micros(
            request.max_tokens, target.output_usd_per_1m
        )

    def plan(self, project: ProjectConfig, request: ChatRequest) -> RoutingPlan:
        route = self._resolve_route(project, request.model)
        targets = tuple(
            TargetPlan(
                provider=target.provider,
                model=target.model,
                estimated_cost_micros=self.estimate_cost_micros(request, target),
            )
            for target in route.targets
        )
        # Reserve the worst case across the whole route: a fallback to a pricier
        # target must never be able to exceed the reservation.
        reservation = max(target.estimated_cost_micros for target in targets)
        fallback_allowed = request.idempotent and len(targets) > 1
        if len(targets) == 1:
            reason = "single target configured; no fallback available"
        elif not request.idempotent:
            reason = "request is not idempotent; fallback is not permitted"
        else:
            reason = "fallback permitted for classified transient failures only"
        return RoutingPlan(
            route_model=route.model,
            targets=targets,
            reservation_micros=reservation,
            estimated_input_tokens=estimate_message_tokens(request.messages),
            fallback_allowed=fallback_allowed,
            reason=reason,
        )

    def dry_run(self, authorization: str | None, payload: object) -> dict[str, Any]:
        project = self.authenticator.resolve(authorization)
        request = self._parse(payload)
        plan = self.plan(project, request)
        snapshot = self.ledger.snapshot(self._budget_policy(project))
        per_request_ok = plan.reservation_micros <= project.per_request_max_micros
        budget_reason = Ledger._denial_reason(snapshot, plan.reservation_micros)
        allowed = per_request_ok and budget_reason is None
        blocking = "per_request_limit_exceeded" if not per_request_ok else budget_reason
        return {
            "object": "gateway.routing_decision",
            "project": project.id,
            "model": plan.route_model,
            "targets": [
                {
                    "provider": target.provider,
                    "model": target.model,
                    "estimated_cost_micros": target.estimated_cost_micros,
                    "estimated_cost_usd": format_usd(target.estimated_cost_micros),
                }
                for target in plan.targets
            ],
            "reservation_micros": plan.reservation_micros,
            "reservation_usd": format_usd(plan.reservation_micros),
            "estimated_input_tokens": plan.estimated_input_tokens,
            "max_output_tokens": request.max_tokens,
            "fallback_allowed": plan.fallback_allowed,
            "fallback_reason": plan.reason,
            "allowed": allowed,
            "blocking_reason": blocking,
            "budget": self._budget_dict(snapshot),
            "per_request_max_usd": format_usd(project.per_request_max_micros),
            "note": "Cost figures are estimates derived from configured prices, not invoices.",
        }

    # ------------------------------------------------------------------ chat

    def chat(self, authorization: str | None, payload: object) -> ChatResult:
        project = self.authenticator.resolve(authorization)
        request = self._parse(payload)
        self._log_request(project, request)
        plan = self.plan(project, request)

        if plan.reservation_micros > project.per_request_max_micros:
            self._reject(project, request, "per_request_limit_exceeded")
            raise PerRequestLimitExceeded(
                "Estimated maximum cost "
                f"{format_usd(plan.reservation_micros)} exceeds the per-request limit "
                f"{format_usd(project.per_request_max_micros)}. "
                "Lower 'max_tokens' or raise the project limit.",
                param="max_tokens",
            )

        retry_after = self._rate_limiters[project.id].acquire(project.id)
        if retry_after is not None:
            self._reject(project, request, "rate_limited")
            raise RateLimited(
                f"Rate limit of {project.requests_per_minute} requests/minute exceeded. "
                f"Retry in {retry_after}s.",
                retry_after=retry_after,
            )

        if not self._concurrency.try_acquire(project.id):
            self._reject(project, request, "concurrency_limit")
            raise ConcurrencyLimited(
                f"Concurrency limit of {project.max_concurrency} in-flight requests exceeded "
                "for this project."
            )

        context = _Context(
            project=project,
            request=request,
            request_id=f"req_{uuid.uuid4().hex[:24]}",
            plan=plan,
            reserved=False,
        )
        try:
            self._reserve(context)
            return self._execute(context)
        finally:
            self._concurrency.release(project.id)

    def _log_request(self, project: ProjectConfig, request: ChatRequest) -> None:
        """Metadata only by default. Prompt text requires `log_prompts = true`."""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        if self.config.server.log_prompts:
            contents = self.log_redactor.redact_value(
                [message.content for message in request.messages]
            )
            logger.debug(
                "chat project=%s model=%s max_tokens=%d messages=%s",
                project.id,
                request.model,
                request.max_tokens,
                contents,
            )
        else:
            logger.debug(
                "chat project=%s model=%s max_tokens=%d messages=%d input_chars=%d",
                project.id,
                request.model,
                request.max_tokens,
                len(request.messages),
                sum(len(message.content) for message in request.messages),
            )

    def _reserve(self, context: _Context) -> None:
        policy = self._budget_policy(context.project)
        try:
            self.ledger.reserve(
                project=policy,
                request_id=context.request_id,
                route_model=context.plan.route_model,
                reserved_micros=context.plan.reservation_micros,
                ttl_seconds=self.config.limits.reservation_ttl_seconds,
                idempotent=context.request.idempotent,
                decision=json.dumps(
                    {
                        "targets": [t.provider for t in context.plan.targets],
                        "fallback_allowed": context.plan.fallback_allowed,
                    }
                ),
            )
            context.reserved = True
            self.metrics.inc("gateway_reservations_total", (context.project.id, "granted"))
        except ReservationDenied as denied:
            self.metrics.inc(
                "gateway_reservations_total", (context.project.id, f"denied_{denied.reason}")
            )
            self._reject(context.project, context.request, denied.reason)
            raise BudgetExhausted(
                f"Budget exhausted ({denied.reason}). "
                f"Remaining this month: {format_usd(denied.snapshot.month_remaining_micros)}; "
                f"this reservation needs {format_usd(denied.requested_micros)}.",
                retry_after=60,
            ) from denied
        except BudgetUnavailable as exc:
            if self.config.server.fail_closed:
                self._reject(context.project, context.request, "budget_state_unavailable")
                raise BudgetStateUnavailable(
                    "Budget state is unavailable and the gateway is configured to fail closed. "
                    "Request not forwarded."
                ) from exc
            # Explicitly configured fail-open: forward, but record why nothing was reserved.
            self.ledger.audit(
                event="budget_fail_open",
                detail="Ledger unavailable; request forwarded without reservation.",
                project_id=context.project.id,
                request_id=context.request_id,
            )

    def _execute(self, context: _Context) -> ChatResult:
        project = context.project
        request = context.request
        forward_request = request
        if self.config.redaction.redact_before_forward:
            forward_request = _redacted_request(request, self.redactor)

        last_error: ProviderError | None = None
        for index, target in enumerate(context.plan.targets):
            attempt_number = index + 1
            provider_config = self.config.provider(target.provider)
            adapter = self._adapters[target.provider]
            timeout = min(
                self.config.server.request_timeout_seconds,
                provider_config.timeout_seconds if provider_config else 30.0,
            )
            started = time.perf_counter()
            try:
                response = adapter.complete(forward_request, target.model, timeout)
            except ProviderError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                # `exc` is deleted when the handler exits, so keep the error itself.
                last_error = exc
                self._record_attempt(
                    context,
                    attempt_number=attempt_number,
                    provider=target.provider,
                    provider_model=target.model,
                    status=exc.error_class,
                    error_class=exc.error_class,
                    http_status=exc.status_code,
                    input_tokens=0,
                    output_tokens=0,
                    cost_micros=0,
                    latency_ms=latency_ms,
                )
                self.metrics.inc(
                    "gateway_attempts_total",
                    (target.provider, target.model, f"error_{exc.error_class}"),
                )
                can_fallback = (
                    exc.transient and request.idempotent and index + 1 < len(context.plan.targets)
                )
                if can_fallback:
                    continue
                break

            latency_ms = int((time.perf_counter() - started) * 1000)
            route = self.config.route(context.plan.route_model)
            assert route is not None
            price = next(
                target_config
                for target_config in route.targets
                if target_config.provider == target.provider and target_config.model == target.model
            )
            cost = price_micros(response.input_tokens, price.input_usd_per_1m) + price_micros(
                response.output_tokens, price.output_usd_per_1m
            )
            self._record_attempt(
                context,
                attempt_number=attempt_number,
                provider=target.provider,
                provider_model=target.model,
                status="success",
                error_class=None,
                http_status=200,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_micros=cost,
                latency_ms=latency_ms,
                provider_request_id=response.provider_request_id,
            )
            self.metrics.inc("gateway_attempts_total", (target.provider, target.model, "success"))
            self.metrics.inc("gateway_tokens_total", (project.id, "input"), response.input_tokens)
            self.metrics.inc("gateway_tokens_total", (project.id, "output"), response.output_tokens)
            self.metrics.inc("gateway_cost_micros_total", (project.id, target.provider), cost)
            if context.reserved:
                if cost > context.plan.reservation_micros:
                    self.ledger.audit(
                        event="settlement_overage",
                        detail=(
                            "Provider-reported usage exceeded the reservation: "
                            f"settled {cost} micros vs reserved "
                            f"{context.plan.reservation_micros} micros."
                        ),
                        project_id=project.id,
                        request_id=context.request_id,
                    )
                self.ledger.settle(request_id=context.request_id, cost_micros=cost)
            self.metrics.inc("gateway_requests_total", (project.id, request.model, "success"))
            return ChatResult(
                request_id=context.request_id,
                response=response,
                provider=target.provider,
                upstream_model=target.model,
                cost_micros=cost,
                attempts=tuple(context.attempts),
            )

        # No attempt produced a billable success.
        error = last_error or ProviderError(
            "unknown", "No provider target available", error_class="permanent"
        )
        timed_out = error.status_code is None and "Timeout" in error.message
        if context.reserved:
            if timed_out:
                # Ambiguous outcome: the provider may have billed without returning
                # usage evidence. Release rather than charge, and record the release.
                self.ledger.release(request_id=context.request_id, error_code="upstream_timeout")
            else:
                self.ledger.fail(
                    request_id=context.request_id,
                    error_code=f"upstream_{error.error_class}",
                )
        self.metrics.inc(
            "gateway_requests_total", (project.id, request.model, f"error_{error.error_class}")
        )
        if timed_out:
            raise UpstreamTimeout(
                "Provider timed out and returned no usage evidence. "
                "The reservation was released; the request was not charged by this gateway."
            ) from error
        raise UpstreamFailure(
            f"All provider attempts failed ({error.error_class}): {error.message}"
        ) from error

    def _record_attempt(
        self,
        context: _Context,
        *,
        attempt_number: int,
        provider: str,
        provider_model: str,
        status: str,
        error_class: str | None,
        http_status: int | None,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        latency_ms: int,
        provider_request_id: str | None = None,
    ) -> None:
        context.attempts.append(
            AttemptOutcome(
                attempt=attempt_number,
                provider=provider,
                upstream_model=provider_model,
                status=status,
                success=status == "success",
                error_class=error_class,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=cost_micros,
                latency_ms=latency_ms,
            )
        )
        if context.reserved:
            self.ledger.record_attempt(
                request_id=context.request_id,
                attempt_number=attempt_number,
                provider=provider,
                provider_model=provider_model,
                status=status,
                error_class=error_class,
                http_status=http_status,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=cost_micros,
                latency_ms=latency_ms,
                provider_request_id=provider_request_id,
            )

    # ------------------------------------------------------------------ helpers

    def _parse(self, payload: object) -> ChatRequest:
        limits = self.config.limits
        return parse_chat_request(
            payload,
            max_messages=limits.max_messages,
            max_input_chars=limits.max_input_chars,
            max_output_tokens=limits.max_output_tokens,
            default_max_output_tokens=limits.default_max_output_tokens,
        )

    def _budget_policy(self, project: ProjectConfig) -> BudgetPolicy:
        return BudgetPolicy(
            project_id=project.id,
            name=project.name,
            monthly_budget_micros=project.monthly_budget_micros,
            daily_budget_micros=project.daily_budget_micros,
            per_request_max_micros=project.per_request_max_micros,
        )

    def _budget_dict(self, snapshot: BudgetSnapshot) -> dict[str, Any]:
        # Micros are the exact integer source of truth; the `_usd` strings are the
        # display form and always agree with them.
        return {
            "monthly_budget_micros": snapshot.monthly_budget_micros,
            "daily_budget_micros": snapshot.daily_budget_micros,
            "month_settled_micros": snapshot.month_settled_micros,
            "month_reserved_micros": snapshot.month_reserved_micros,
            "month_remaining_micros": snapshot.month_remaining_micros,
            "day_settled_micros": snapshot.day_settled_micros,
            "day_reserved_micros": snapshot.day_reserved_micros,
            "day_remaining_micros": snapshot.day_remaining_micros,
            "month_committed_micros": snapshot.month_settled_micros
            + snapshot.month_reserved_micros,
            "monthly_budget_usd": format_usd(snapshot.monthly_budget_micros),
            "daily_budget_usd": (
                None
                if snapshot.daily_budget_micros is None
                else format_usd(snapshot.daily_budget_micros)
            ),
            "month_settled_usd": format_usd(snapshot.month_settled_micros),
            "month_reserved_usd": format_usd(snapshot.month_reserved_micros),
            "month_remaining_usd": format_usd(snapshot.month_remaining_micros),
            "day_settled_usd": format_usd(snapshot.day_settled_micros),
            "day_reserved_usd": format_usd(snapshot.day_reserved_micros),
            "day_remaining_usd": (
                None
                if snapshot.day_remaining_micros is None
                else format_usd(snapshot.day_remaining_micros)
            ),
        }

    def _reject(self, project: ProjectConfig, request: ChatRequest, reason: str) -> None:
        self.metrics.inc("gateway_rejections_total", (reason,))
        self.metrics.inc(
            "gateway_requests_total", (project.id, request.model, f"rejected_{reason}")
        )

    # -------------------------------------------------------------- reporting

    def usage(
        self, project: ProjectConfig | None, *, since_ms: int | None = None
    ) -> dict[str, Any]:
        report = self.ledger.usage_report(
            project_id=project.id if project else None, since_ms=since_ms
        )
        report["settled_usd"] = format_usd(report["settled_micros"])
        return report

    def budget_report(self, project: ProjectConfig) -> dict[str, Any]:
        snapshot = self.ledger.snapshot(self._budget_policy(project))
        return {"project": project.id, **self._budget_dict(snapshot)}

    def request_trace(self, request_id: str) -> dict[str, Any] | None:
        trace = self.ledger.request_trace(request_id)
        if trace is None:
            return None
        trace["settled_usd"] = format_usd(trace["settled_micros"])
        trace["attempts"] = [
            {**attempt, "cost_usd": format_usd(attempt["cost_micros"])}
            for attempt in trace["attempts"]
        ]
        return trace


def _redacted_request(request: ChatRequest, redactor: Redactor) -> ChatRequest:
    messages = tuple(
        Message(role=message.role, content=redactor.redact(message.content).text)
        for message in request.messages
    )
    return ChatRequest(
        model=request.model,
        messages=messages,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        idempotent=request.idempotent,
    )


def chat_completion_body(result: ChatResult, *, created: int | None = None) -> dict[str, Any]:
    """OpenAI-compatible response body with an extra `gateway` evidence block."""
    timestamp = created if created is not None else int(datetime.now(tz=UTC).timestamp())
    usage = {
        "prompt_tokens": result.response.input_tokens,
        "completion_tokens": result.response.output_tokens,
        "total_tokens": result.response.input_tokens + result.response.output_tokens,
    }
    return {
        "id": f"chatcmpl-{result.request_id}",
        "object": "chat.completion",
        "created": timestamp,
        "model": result.upstream_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.response.content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
        "gateway": {
            "request_id": result.request_id,
            "provider": result.provider,
            "provider_model": result.upstream_model,
            "provider_request_id": result.response.provider_request_id,
            "estimated_cost_micros": result.cost_micros,
            "estimated_cost_usd": str(micros_to_usd(result.cost_micros)),
            "cost_basis": "configured_price_table",
            "attempts": [
                {
                    "attempt": attempt.attempt,
                    "provider": attempt.provider,
                    "model": attempt.upstream_model,
                    "status": attempt.status,
                    "error_class": attempt.error_class,
                    "cost_micros": attempt.cost_micros,
                    "latency_ms": attempt.latency_ms,
                }
                for attempt in result.attempts
            ],
        },
    }
