# 0005 — No streaming in v1

- **Status:** accepted
- **Date:** 2026-10-20

## Context

`POST /v1/chat/completions` with `stream: true` is the default for many chat clients.
Supporting it means:

- holding the reservation open for the duration of the stream;
- deciding what to do when the stream breaks after the provider has emitted tokens (the
  provider will bill, but the client received a partial answer);
- forwarding server-sent events through the proxy without buffering, which changes the
  transport shape from "request/response" to a long-lived connection;
- a separate settlement path, since usage arrives in a final event that may never come.

## Decision

Reject `stream: true` with `400 invalid_request` and `param: "stream"`. Document it in the
README, the API reference, and the changelog. Do not pretend to support it by buffering the
stream and returning it as one body — that would break the caller's event parsing while
looking like it worked.

## Consequences

- Settlement is exact: one request, one response, one usage record.
- Timeouts stay meaningful: a request either completed or did not.
- Clients that require streaming cannot use the gateway. This is a real limitation and the
  most likely reason someone chooses a different tool.
- The reservation TTL covers the whole non-streaming request, which is bounded by
  `request_timeout_seconds`; no long-lived reservation exists to leak.
- Adding streaming later requires a design for partial-output billing. The current schema
  can record a partial attempt (`output_tokens` with a `status` of `partial`), but that
  status is not implemented and would be a schema-visible change.
