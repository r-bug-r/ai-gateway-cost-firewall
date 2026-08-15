// Operator dashboard for the cost firewall.
//
// Constraints this file is written against:
//  - No framework, no build step, no external asset. Everything is served by the
//    gateway process so a local-only deployment stays local.
//  - Data reaches the DOM through textContent only. Prompt text is never rendered
//    because the API never returns it.
//  - Every async outcome is announced through the reserved status region, so a
//    failure cannot silently reflow the page.

const state = {
  key: "",
  project: "",
  policy: null,
  lastDecision: null,
  requests: [],
  loading: 0,
};

// The dashboard ships `style-src 'self'`, which blocks the style *attribute*: an
// element created with setAttribute("style", ...) keeps the text but loses every
// declaration, silently collapsing computed widths to zero. Assigning through the
// CSSOM (element.style.setProperty) is not affected, so all inline styling here
// goes through this helper.
const applyStyle = (node, declarations) => {
  for (const part of String(declarations).split(";")) {
    const at = part.indexOf(":");
    if (at < 0) continue;
    const property = part.slice(0, at).trim();
    const value = part.slice(at + 1).trim();
    if (property && value) node.style.setProperty(property, value);
  }
};

const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name === "dataset") Object.assign(node.dataset, value);
    else if (name === "style") applyStyle(node, value);
    else node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
};

const $ = (id) => document.getElementById(id);

let captionSeq = 0;

// A horizontally scrollable table is only reachable from the keyboard if its
// scroll container is focusable, and a focusable div is only announced if it has
// a role and a name. The tab stop is added only while the table actually
// overflows, so a wide viewport gains no empty stops.
function tableWrap(table) {
  const wrap = el("div", { class: "table-wrap" }, [table]);
  const sync = () => {
    const overflows = wrap.scrollWidth > wrap.clientWidth + 1;
    if (!overflows) {
      wrap.removeAttribute("tabindex");
      wrap.removeAttribute("role");
      wrap.removeAttribute("aria-labelledby");
      return;
    }
    const caption = table.querySelector("caption");
    if (caption) {
      if (!caption.id) caption.id = `table-caption-${(captionSeq += 1)}`;
      wrap.setAttribute("aria-labelledby", caption.id);
    }
    wrap.setAttribute("role", "group");
    wrap.setAttribute("tabindex", "0");
  };
  new ResizeObserver(sync).observe(wrap);
  return wrap;
}

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(path, { params = {}, method = "GET", body } = {}) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: {
        authorization: `Bearer ${state.key}`,
        ...(body ? { "content-type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (error) {
    throw new ApiError(
      0,
      "network_error",
      error.name === "AbortError"
        ? "The gateway did not answer within 20 seconds."
        : "The gateway is unreachable from this browser.",
    );
  } finally {
    clearTimeout(timer);
  }
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const error = payload?.error ?? {};
    throw new ApiError(
      response.status,
      error.code ?? `http_${response.status}`,
      error.message ?? `Request failed with status ${response.status}.`,
    );
  }
  return payload;
}

// ------------------------------------------------------------------ formatting

// Money is stored as integer micro-USD. Precision follows magnitude: six
// decimals on sub-cent figures (where they are the whole signal), two above a
// dollar (where the tail is noise), four in between.
const usd = (micros) => {
  if (typeof micros !== "number") return "—";
  const value = micros / 1_000_000;
  const digits = Math.abs(value) >= 1 ? 2 : Math.abs(value) >= 0.01 ? 4 : 6;
  return `$${value.toFixed(digits)}`;
};

const count = (value) => (typeof value === "number" ? value.toLocaleString() : "—");

const percent = (part, whole) =>
  !whole ? "—" : `${((part / whole) * 100).toFixed(2)}%`;

const stamp = (ms) => {
  if (!ms) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(ms));
};

const shortId = (id) => (id.length > 18 ? `${id.slice(0, 10)}…${id.slice(-6)}` : id);

const statusTone = (status) =>
  status === "settled" || status === "succeeded"
    ? "ok"
    : status === "failed" || status === "blocked"
      ? "bad"
      : "neutral";

// --------------------------------------------------------------------- status

function setStatus(message, tone = "neutral") {
  const region = $("status");
  $("status-text").textContent = message;
  region.dataset.tone = tone;
}

function setLoading(isLoading) {
  state.loading += isLoading ? 1 : -1;
  if (state.loading < 0) state.loading = 0;
  $("connect").disabled = state.loading > 0;
  $("connect").textContent = state.loading > 0 ? "Connecting…" : "Connect";
}

let toastTimer = null;
function toast(message, tone = "ok") {
  const node = $("toast");
  node.textContent = message;
  node.dataset.tone = tone;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.hidden = true;
  }, 4000);
}

function skeleton(lines = 2) {
  return el(
    "div",
    {},
    Array.from({ length: lines }, () => el("div", { class: "skeleton" })),
  );
}

function emptyState(title, detail) {
  return el("div", { class: "state" }, [
    el("p", { class: "state__title", text: title }),
    detail ? el("p", { text: detail }) : null,
  ]);
}

function errorState(title, error) {
  const retry = el("button", { class: "btn", type: "button", text: "Retry" }, []);
  retry.addEventListener("click", () => loadAll());
  return el("div", { class: "state" }, [
    el("p", { class: "state__title", text: title }),
    el("p", { text: error.message }),
    retry,
  ]);
}

function badge(text, tone = "neutral") {
  const glyph = tone === "ok" ? "✓" : tone === "bad" ? "✕" : tone === "warn" ? "!" : "·";
  return el("span", { class: `badge badge--${tone}` }, [
    el("span", { class: "badge__glyph", "aria-hidden": "true", text: glyph }),
    text,
  ]);
}

// ------------------------------------------------------------------- control room

function budgetBar({ settled, reserved, total, label }) {
  const committed = settled + reserved;
  const settledPct = total ? Math.min((settled / total) * 100, 100) : 0;
  const reservedPct = total ? Math.min((reserved / total) * 100, 100 - settledPct) : 0;
  return el("div", {
    class: "meter__track meter__track--compact",
    role: "img",
    "aria-label": `${label}: ${percent(committed, total)} of ${usd(total)} committed. Settled ${usd(
      settled,
    )}, reserved ${usd(reserved)}.`,
  }, [
    el("div", { class: "meter__fill", style: `width: ${settledPct}%` }),
    el("div", { class: "meter__reserved", style: `width: ${reservedPct}%` }),
    el("div", { class: "meter__mark", style: "left: 80%", "aria-hidden": "true" }),
  ]);
}

function renderBudget(budget) {
  const host = $("budget-body");
  const total = budget.monthly_budget_micros;
  const settled = budget.month_settled_micros;
  const reserved = budget.month_reserved_micros;
  const committed = settled + reserved;
  const over = committed > total;
  const usedRatio = total ? Math.min(committed / total, 1) : 0;
  const settledPct = total ? Math.min((settled / total) * 100, 100) : 0;
  const reservedPct = total ? Math.min((reserved / total) * 100, 100 - settledPct) : 0;
  const tone = over || committed >= total ? "bad" : usedRatio >= 0.8 ? "warn" : "neutral";

  const track = el("div", {
    class: "meter__track",
    role: "img",
    "aria-label": `Budget used: ${percent(committed, total)} of ${usd(total)}. Settled ${usd(
      settled,
    )}, reserved ${usd(reserved)}.`,
  }, [
    el("div", { class: "meter__fill", style: `width: ${settledPct}%` }),
    el("div", { class: "meter__reserved", style: `width: ${reservedPct}%` }),
    el("div", { class: "meter__mark", style: "left: 80%", "aria-hidden": "true" }),
  ]);

  // The monthly meter alone is misleading when the daily cap binds first: a project
  // can sit at 0.3% of the month and still refuse every request. The daily row makes
  // the cap that actually stops traffic visible next to the one that does not.
  const dayTotal = budget.daily_budget_micros;
  const dayCommitted = budget.day_settled_micros + budget.day_reserved_micros;
  const dayRatio = dayTotal ? dayCommitted / dayTotal : 0;
  const dayTone = !dayTotal ? "neutral" : dayRatio >= 1 ? "bad" : dayRatio >= 0.8 ? "warn" : "neutral";
  const monthRatio = total ? committed / total : 0;
  const monthCapped = total > 0 && committed >= total;
  const dayCapped = dayTotal > 0 && dayCommitted >= dayTotal;
  // Say which cap is actually refusing traffic. Reporting only the tighter ratio
  // would claim "the monthly cap is tighter" while both caps are already reached.
  const binding = monthCapped && dayCapped
    ? "Both caps are reached; a request is refused by whichever it hits first."
    : monthCapped
      ? "The monthly cap is reached, so new requests are refused regardless of the daily figure."
      : dayCapped
        ? "The daily cap is reached; the month still has room, so traffic resumes when the day resets."
        : !dayTotal
          ? "No daily cap is configured, so only the monthly cap can refuse traffic."
          : monthRatio >= dayRatio
            ? "The monthly cap is the tighter limit right now."
            : "The daily cap is the tighter limit right now; a request can be refused while the month still looks nearly empty.";

  const dailyBlock = dayTotal
    ? el("div", { class: "meter meter--compact" }, [
        el("div", { class: "meter__readout" }, [
          el("span", { class: "meter__label", text: "Today" }),
          el("span", { class: "meter__of", text: `${usd(dayCommitted)} committed of ${usd(dayTotal)}` }),
          badge(dayRatio >= 1 ? "Daily cap reached" : `${percent(dayCommitted, dayTotal)} used`, dayTone),
        ]),
        budgetBar({
          settled: budget.day_settled_micros,
          reserved: budget.day_reserved_micros,
          total: dayTotal,
          label: "Daily budget used",
        }),
        el("div", { class: "meter__scale" }, [
          el("span", { text: "$0" }),
          el("span", { text: usd(dayTotal) }),
        ]),
      ])
    : null;

  host.replaceChildren(
    el("div", { class: "meter" }, [
      el("div", { class: "meter__readout" }, [
        el("span", { class: "meter__value", text: usd(committed) }),
        el("span", { class: "meter__of", text: `committed of ${usd(total)} this month` }),
        badge(
          over ? "Over budget" : committed >= total ? "Fully committed" : `${percent(committed, total)} used`,
          tone,
        ),
      ]),
      track,
      el("div", { class: "meter__scale" }, [
        el("span", { text: "$0" }),
        el("span", { text: usd(total) }),
      ]),
      el("ul", { class: "legend" }, [
        el("li", { class: "legend__item" }, [
          el("span", { class: "legend__swatch legend__swatch--settled", "aria-hidden": "true" }),
          "Settled",
          el("span", { class: "legend__amount", text: usd(settled) }),
        ]),
        el("li", { class: "legend__item" }, [
          el("span", { class: "legend__swatch legend__swatch--reserved", "aria-hidden": "true" }),
          "Reserved",
          el("span", { class: "legend__amount", text: usd(reserved) }),
        ]),
        el("li", { class: "legend__item" }, [
          el("span", { class: "legend__swatch", "aria-hidden": "true" }),
          "Remaining",
          el("span", { class: "legend__amount", text: usd(budget.month_remaining_micros) }),
        ]),
      ]),
      dailyBlock,
      el("p", {
        class: "field__hint",
        text: `${binding} The dashed line marks the 80% warning threshold.`,
      }),
      over
        ? el("div", { class: "callout callout--bad" }, [
            el("p", { class: "callout__title" }, [
              el("span", { "aria-hidden": "true", text: "✕" }),
              "Committed spend exceeds the monthly cap",
            ]),
            el("p", {
              text:
                "New requests are refused until reservations settle or the budget is raised. " +
                "Settled cost can exceed a reservation when a provider reports more usage than estimated.",
            }),
          ])
        : null,
    ]),
  );
  $("budget-period").textContent = `${percent(committed, total)} of ${usd(total)}`;
}

function renderActivity(usage) {
  const host = $("activity-body");
  if (!usage.requests) {
    host.replaceChildren(emptyState("No requests yet.", "Figures appear after the first request."));
    return;
  }
  const kpis = [
    ["Settled", usd(usage.settled_micros), `${count(usage.settled_requests)} settled requests`],
    ["Requests", count(usage.requests), `${count(usage.attempts)} provider attempts`],
    ["Tokens", `${count(usage.input_tokens)} in / ${count(usage.output_tokens)} out`, "Provider-reported"],
    ["Mean latency", `${count(usage.avg_latency_ms)} ms`, "Successful attempts only"],
    ["Blocked or failed", count(usage.failed_requests), "Includes budget refusals"],
    ["Released", count(usage.released_requests), "Reservations returned unused"],
  ];
  host.replaceChildren(
    el(
      "div",
      { class: "kpis" },
      kpis.map(([label, value, meta]) =>
        el("div", { class: "kpi" }, [
          el("p", { class: "kpi__label", text: label }),
          el("p", { class: "kpi__value", text: value }),
          el("p", { class: "kpi__meta", text: meta }),
        ]),
      ),
    ),
  );
}

function renderProviders(providers) {
  const host = $("providers-body");
  if (!providers.length) {
    host.replaceChildren(
      emptyState("No attempts observed.", "This window has no provider traffic for the project."),
    );
    return;
  }
  host.replaceChildren(
    tableWrap(
      el("table", { "data-stack": "" }, [
        el("caption", { text: "Observed outcomes in the last 7 days." }),
        el("thead", {}, [
          el("tr", {}, [
            el("th", { scope: "col", text: "Provider" }),
            el("th", { scope: "col", text: "Attempts" }),
            el("th", { scope: "col", text: "Failures" }),
            el("th", { scope: "col", text: "Last error" }),
          ]),
        ]),
        el(
          "tbody",
          {},
          providers.map((row) =>
            el("tr", {}, [
              el("td", { "data-label": "Provider" }, [
                el("span", { class: "mono", text: row.provider }),
              ]),
              el("td", { "data-label": "Attempts", class: "num", text: count(row.attempts) }),
              el("td", { "data-label": "Failures" }, [
                badge(count(row.failures), row.failures ? "warn" : "ok"),
              ]),
              el("td", { "data-label": "Last error" }, [
                el("span", {
                  class: "mono",
                  text: row.last_error_class ?? "none observed",
                }),
              ]),
            ]),
          ),
        ),
      ]),
    ),
  );
}

function renderBlocks(requests) {
  const blocked = requests.filter((row) => row.outcome === "blocked");
  $("blocks-count").textContent = String(blocked.length);
  const host = $("blocks-body");
  if (!blocked.length) {
    host.replaceChildren(
      emptyState("Nothing blocked.", "Requests refused by a budget, rate, or concurrency rule appear here."),
    );
    return;
  }
  host.replaceChildren(
    tableWrap(
      el("table", { "data-stack": "" }, [
        el("caption", { text: "Refusals never reserve budget and never reach a provider." }),
        el("thead", {}, [
          el("tr", {}, [
            el("th", { scope: "col", text: "Request" }),
            el("th", { scope: "col", text: "When" }),
            el("th", { scope: "col", text: "Model" }),
            el("th", { scope: "col", text: "Reason" }),
          ]),
        ]),
        el(
          "tbody",
          {},
          blocked.map((row) =>
            el("tr", {}, [
              el("td", { "data-label": "Request" }, [
                requestButton(row.request_id),
              ]),
              el("td", { "data-label": "When", class: "mono", text: stamp(row.created_at_ms) }),
              el("td", { "data-label": "Model", class: "mono", text: row.route_model }),
              el("td", { "data-label": "Reason" }, [
                badge(row.error_code ?? "unknown", "bad"),
              ]),
            ]),
          ),
        ),
      ]),
    ),
  );
}

function requestButton(id) {
  const button = el("button", { class: "row-btn mono", type: "button", text: shortId(id) });
  button.title = id;
  button.addEventListener("click", () => openRequest(id));
  return button;
}

// ---------------------------------------------------------------------- requests

function renderRequests() {
  const host = $("req-body");
  const query = $("req-search").value.trim().toLowerCase();
  const status = $("req-status").value;
  const rows = state.requests.filter((row) => {
    if (status && row.outcome !== status) return false;
    if (!query) return true;
    return [row.request_id, row.route_model, row.last_provider, row.error_code]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query));
  });
  $("req-count").textContent = `${rows.length} row${rows.length === 1 ? "" : "s"}`;

  if (!rows.length) {
    host.replaceChildren(
      state.requests.length
        ? emptyState("No rows match this filter.", "Clear the search or pick another status.")
        : emptyState("No requests yet.", "Send a request through the gateway to populate the ledger."),
    );
    return;
  }

  host.replaceChildren(
    tableWrap(
      el("table", { "data-stack": "" }, [
        el("caption", {
          text: "Message content is not stored, so it cannot appear in this table.",
        }),
        el("thead", {}, [
          el("tr", {}, [
            el("th", { scope: "col", text: "Request" }),
            el("th", { scope: "col", text: "When" }),
            el("th", { scope: "col", text: "Model" }),
            el("th", { scope: "col", text: "Provider" }),
            el("th", { scope: "col", text: "Status" }),
            el("th", { scope: "col", text: "Settled" }),
            el("th", { scope: "col", text: "Attempts" }),
          ]),
        ]),
        el(
          "tbody",
          {},
          rows.map((row) =>
            el("tr", {}, [
              el("td", { "data-label": "Request" }, [requestButton(row.request_id)]),
              el("td", { "data-label": "When", class: "mono", text: stamp(row.created_at_ms) }),
              el("td", { "data-label": "Model", class: "mono", text: row.route_model }),
              el("td", {
                "data-label": "Provider",
                class: "mono",
                text: row.last_provider ?? "none",
              }),
              el("td", { "data-label": "Status" }, [
                badge(row.error_code ?? row.outcome, statusTone(row.outcome)),
              ]),
              el("td", { "data-label": "Settled", class: "num", text: usd(row.settled_micros) }),
              el("td", { "data-label": "Attempts", class: "num", text: count(row.attempts) }),
            ]),
          ),
        ),
      ]),
    ),
  );
}

async function openRequest(id) {
  const dialog = $("request-dialog");
  $("request-dialog-title").textContent = id;
  const body = $("request-dialog-body");
  body.replaceChildren(skeleton(3));
  dialog.showModal();
  try {
    const trace = await api(`/v1/gateway/requests/${encodeURIComponent(id)}`);
    body.replaceChildren(...traceNodes(trace));
  } catch (error) {
    body.replaceChildren(errorState("Could not load this request.", error));
    setStatus(`Request ${shortId(id)}: ${error.message}`, "bad");
  }
}

function traceNodes(trace) {
  const rows = [
    ["Project", trace.project_id],
    ["Outcome", trace.outcome],
    ["Reason", trace.error_code ?? "—"],
    ["Model", trace.route_model],
    ["Created", stamp(trace.created_at_ms)],
    ["Completed", stamp(trace.completed_at_ms)],
    ["Reserved", usd(trace.reserved_micros)],
    ["Settled", usd(trace.settled_micros)],
  ];
  const definition = el("dl", { class: "dl" }, [
    ...rows.flatMap(([term, value]) => [
      el("dt", { text: term }),
      el("dd", { class: "mono", text: String(value) }),
    ]),
  ]);

  const attempts = trace.attempts.length
    ? tableWrap(
        el("table", { "data-stack": "" }, [
          el("caption", { text: "Every attempt, in order. Retries and fallbacks are separate rows." }),
          el("thead", {}, [
            el("tr", {}, [
              el("th", { scope: "col", text: "#" }),
              el("th", { scope: "col", text: "Provider" }),
              el("th", { scope: "col", text: "Model" }),
              el("th", { scope: "col", text: "Status" }),
              el("th", { scope: "col", text: "Tokens" }),
              el("th", { scope: "col", text: "Cost" }),
              el("th", { scope: "col", text: "Latency" }),
            ]),
          ]),
          el(
            "tbody",
            {},
            trace.attempts.map((attempt) =>
              el("tr", {}, [
                el("td", { "data-label": "Attempt", class: "num", text: String(attempt.attempt) }),
                el("td", { "data-label": "Provider", class: "mono", text: attempt.provider }),
                el("td", { "data-label": "Model", class: "mono", text: attempt.provider_model }),
                el("td", { "data-label": "Status" }, [
                  badge(attempt.error_class ?? attempt.status, statusTone(attempt.status)),
                ]),
                el("td", {
                  "data-label": "Tokens",
                  class: "num",
                  text: `${count(attempt.input_tokens)} / ${count(attempt.output_tokens)}`,
                }),
                el("td", { "data-label": "Cost", class: "num", text: usd(attempt.cost_micros) }),
                el("td", {
                  "data-label": "Latency",
                  class: "num",
                  text: `${count(attempt.latency_ms)} ms`,
                }),
              ]),
            ),
          ),
        ]),
      )
    : emptyState("No provider attempt.", "The request was refused before any provider call.");

  const audit = trace.audit?.length
    ? el("ul", { class: "legend legend--stacked" }, [
        ...trace.audit.map((event) =>
          el("li", {}, [
            el("span", { class: "mono", text: `${stamp(event.at_ms)} ` }),
            el("span", { class: "mono", text: event.event }),
            el("span", { text: ` — ${event.detail}` }),
          ]),
        ),
      ])
    : emptyState("No audit events.", "Nothing about this request required a note.");

  return [
    definition,
    el("div", {}, [el("h3", { text: "Attempts" }), attempts]),
    el("div", {}, [el("h3", { text: "Audit trail" }), audit]),
  ];
}

// --------------------------------------------------------------------- simulator

function simulatorPayload() {
  const content = $("sim-content").value || "";
  const messages = Math.max(1, Math.min(64, Number($("sim-messages").value) || 1));
  const messageList = Array.from({ length: messages }, (_, index) => ({
    role: index === messages - 1 ? "user" : "system",
    content,
  }));
  return {
    model: $("sim-model").value.trim(),
    messages: messageList,
    max_tokens: Number($("sim-max-tokens").value) || 0,
    idempotent: $("sim-idempotent").value === "true",
  };
}

function validateSimulator(payload) {
  const errors = [];
  if (!payload.model) errors.push(["sim-model", "Choose a model the project allows."]);
  if (!payload.max_tokens || payload.max_tokens < 1) {
    errors.push(["sim-max-tokens", "Enter at least 1 output token."]);
  }
  for (const id of ["sim-model", "sim-max-tokens"]) {
    const message = errors.find(([fieldId]) => fieldId === id);
    $(id).setAttribute("aria-invalid", message ? "true" : "false");
    $(`${id}-error`).hidden = !message;
    $(`${id}-error`).textContent = message ? message[1] : "";
  }
  return errors;
}

function setSimState(text, tone) {
  const node = $("sim-state");
  node.textContent = text;
  node.className = `badge badge--${tone}`;
}

async function simulate(event) {
  event.preventDefault();
  const payload = simulatorPayload();
  const errors = validateSimulator(payload);
  if (errors.length) {
    setStatus("Fix the highlighted fields before simulating.", "bad");
    $(errors[0][0]).focus();
    return;
  }
  const submit = $("sim-submit");
  submit.disabled = true;
  submit.textContent = "Simulating…";
  setSimState("running", "neutral");
  const host = $("sim-result-body");
  host.replaceChildren(skeleton(4));
  try {
    const decision = await api("/v1/gateway/dry-run", { method: "POST", body: payload });
    state.lastDecision = decision;
    $("sim-copy").disabled = false;
    setSimState(decision.allowed ? "allowed" : "blocked", decision.allowed ? "ok" : "bad");
    host.replaceChildren(...decisionNodes(decision));
    setStatus(
      decision.allowed
        ? `Allowed: ${decision.targets[0]?.provider ?? "no provider"} would handle ${decision.model} for an estimated ${decision.reservation_usd}.`
        : `Blocked: ${decision.blocking_reason}. Nothing was reserved or sent.`,
      decision.allowed ? "ok" : "bad",
    );
  } catch (error) {
    // A model outside the project's allowlist is a policy answer, not a broken tool:
    // the gateway refuses it at parse time with model_not_found. Showing "blocked"
    // here matches what would happen to the real request.
    if (error.code === "model_not_found") {
      setSimState("blocked", "bad");
      host.replaceChildren(
        el("div", { class: "stack" }, [
          el("p", {}, [
            badge("blocked", "bad"),
            el("span", { class: "mono", text: " model_not_found" }),
          ]),
          el("p", { text: error.message }),
          el("p", {
            class: "hint",
            text: "No budget was reserved and no provider was called. Pick a model from the policy tab.",
          }),
        ]),
      );
      setStatus(`Blocked by policy: ${error.message}`, "bad");
    } else {
      setSimState("error", "bad");
      host.replaceChildren(errorState("The simulation failed.", error));
      setStatus(`Simulation failed: ${error.message}`, "bad");
    }
  } finally {
    submit.disabled = false;
    submit.textContent = "Simulate routing";
  }
}

function decisionNodes(decision) {
  const summary = el("dl", { class: "dl" }, [
    el("dt", { text: "Decision" }),
    el("dd", {}, [badge(decision.allowed ? "allowed" : "blocked", decision.allowed ? "ok" : "bad")]),
    el("dt", { text: "Blocking reason" }),
    el("dd", { class: "mono", text: decision.blocking_reason ?? "none" }),
    el("dt", { text: "Route model" }),
    el("dd", { class: "mono", text: decision.model }),
    el("dt", { text: "Reservation" }),
    el("dd", { class: "mono", text: decision.reservation_usd }),
    el("dt", { text: "Per-request cap" }),
    el("dd", { class: "mono", text: decision.per_request_max_usd }),
    el("dt", { text: "Estimated input" }),
    el("dd", { class: "mono", text: `${count(decision.estimated_input_tokens)} tokens` }),
    el("dt", { text: "Max output" }),
    el("dd", { class: "mono", text: `${count(decision.max_output_tokens)} tokens` }),
    el("dt", { text: "Fallback" }),
    el("dd", {
      class: "mono",
      text: decision.fallback_allowed
        ? `allowed (${decision.fallback_reason})`
        : `not allowed (${decision.fallback_reason})`,
    }),
  ]);

  const path = el("ol", { class: "legend legend--stacked" }, [
    ...decision.targets.map((target) =>
      el("li", {}, [
        el("span", { class: "mono", text: `${target.provider}/${target.model}` }),
        el("span", { text: ` — estimated ${target.estimated_cost_usd}` }),
      ]),
    ),
  ]);

  return [
    summary,
    el("div", {}, [el("h3", { text: "Attempt order" }), path]),
    el("p", { class: "field__hint", text: decision.note }),
  ];
}

// ------------------------------------------------------------------------ policy

function renderPolicy(policy) {
  const host = $("policy-body");
  host.replaceChildren(
    el("dl", { class: "dl" }, [
      el("dt", { text: "Project" }),
      el("dd", { text: `${policy.name} (${policy.project})` }),
      el("dt", { text: "Monthly cap" }),
      el("dd", { class: "mono", text: policy.monthly_budget_usd }),
      el("dt", { text: "Daily cap" }),
      el("dd", { class: "mono", text: policy.daily_budget_usd ?? "none configured" }),
      el("dt", { text: "Per-request cap" }),
      el("dd", { class: "mono", text: policy.per_request_max_usd }),
      el("dt", { text: "Rate limit" }),
      el("dd", { class: "mono", text: `${count(policy.requests_per_minute)} requests/minute` }),
      el("dt", { text: "Concurrency" }),
      el("dd", { class: "mono", text: `${count(policy.max_concurrency)} in flight` }),
      el("dt", { text: "Retention" }),
      el("dd", {
        class: "mono",
        text: policy.retention_days === null ? "keep indefinitely" : `${policy.retention_days} days`,
      }),
      el("dt", { text: "Model allowlist" }),
      el("dd", {
        class: "mono",
        text: policy.allowed_models.length ? policy.allowed_models.join(", ") : "all routed models",
      }),
      el("dt", { text: "Redact before forwarding" }),
      el("dd", {}, [badge(policy.redact_before_forward ? "on" : "off", policy.redact_before_forward ? "ok" : "warn")]),
      el("dt", { text: "Redaction patterns" }),
      el("dd", {
        class: "mono",
        text: policy.redaction_patterns.length ? policy.redaction_patterns.join(", ") : "built-in defaults only",
      }),
    ]),
  );

  const routesHost = $("routes-body");
  if (!policy.routes.length) {
    routesHost.replaceChildren(emptyState("No routes.", "No route is available to this project."));
    return;
  }
  routesHost.replaceChildren(
    tableWrap(
      el("table", { "data-stack": "" }, [
        el("caption", { text: "Order matters: the first row is attempted first." }),
        el("thead", {}, [
          el("tr", {}, [
            el("th", { scope: "col", text: "Route model" }),
            el("th", { scope: "col", text: "Order" }),
            el("th", { scope: "col", text: "Provider" }),
            el("th", { scope: "col", text: "Provider model" }),
            el("th", { scope: "col", text: "Input / 1M" }),
            el("th", { scope: "col", text: "Output / 1M" }),
          ]),
        ]),
        el(
          "tbody",
          {},
          policy.routes.flatMap((route) =>
            route.targets.map((target, index) =>
              el("tr", {}, [
                el("td", { "data-label": "Route", class: "mono", text: route.model }),
                el("td", {
                  "data-label": "Order",
                  class: "num",
                  text: index === 0 ? "primary" : `fallback ${index}`,
                }),
                el("td", { "data-label": "Provider", class: "mono", text: target.provider }),
                el("td", { "data-label": "Model", class: "mono", text: target.model }),
                el("td", {
                  "data-label": "Input",
                  class: "num",
                  text: `$${target.input_usd_per_million}`,
                }),
                el("td", {
                  "data-label": "Output",
                  class: "num",
                  text: `$${target.output_usd_per_million}`,
                }),
              ]),
            ),
          ),
        ),
      ]),
    ),
  );

  const options = $("model-options");
  options.replaceChildren(
    ...policy.routes.map((route) => el("option", { value: route.model })),
  );
  if (!$("sim-model").value) $("sim-model").value = policy.routes[0]?.model ?? "";
}

// ----------------------------------------------------------------------- reports

function renderReport(usage, days) {
  const host = $("report-body");
  const windowLabel = days ? `last ${days} days` : "all time";
  const reconciled = usage.reconciled;
  const verdict = $("report-reconciled");
  verdict.className = `badge badge--${reconciled ? "ok" : "bad"}`;
  verdict.replaceChildren(
    el("span", {
      class: "badge__glyph",
      "aria-hidden": "true",
      text: reconciled ? "✓" : "✕",
    }),
    reconciled ? "reconciled" : "mismatch",
  );
  host.replaceChildren(
    el("dl", { class: "dl" }, [
      el("dt", { text: "Window" }),
      el("dd", { text: windowLabel }),
      el("dt", { text: "Settled" }),
      el("dd", { class: "mono", text: usage.settled_usd }),
      el("dt", { text: "Requests" }),
      el("dd", {
        class: "mono",
        text: `${count(usage.requests)} total, ${count(usage.settled_requests)} settled, ${count(
          usage.failed_requests,
        )} failed, ${count(usage.released_requests)} released`,
      }),
      el("dt", { text: "Attempts" }),
      el("dd", {
        class: "mono",
        text: `${count(usage.attempts)} total, ${count(usage.successful_attempts)} successful`,
      }),
      el("dt", { text: "Tokens" }),
      el("dd", {
        class: "mono",
        text: `${count(usage.input_tokens)} input, ${count(usage.output_tokens)} output`,
      }),
      el("dt", { text: "Mean latency" }),
      el("dd", { class: "mono", text: `${count(usage.avg_latency_ms)} ms` }),
      el("dt", { text: "Reconciliation" }),
      el("dd", {
        text: reconciled
          ? "Every settled request equals the sum of its successful attempts."
          : `Mismatched requests: ${usage.mismatched_request_ids.join(", ")}`,
      }),
    ]),
  );
  if (!reconciled) {
    setStatus("Report is not reconciled: settled cost differs from attempt cost.", "bad");
  }
}

function download(name, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function exportReport(kind) {
  const days = $("report-days").value;
  const params = { project: state.project, days };
  try {
    if (kind === "csv") {
      const url = new URL("/v1/gateway/usage.csv", window.location.origin);
      for (const [key, value] of Object.entries(params)) {
        if (value) url.searchParams.set(key, value);
      }
      const response = await fetch(url, {
        headers: { authorization: `Bearer ${state.key}` },
      });
      if (!response.ok) throw new ApiError(response.status, "export_failed", "Export failed.");
      download(`usage-${state.project}.csv`, await response.text(), "text/csv");
    } else {
      const usage = await api("/v1/gateway/usage", { params });
      download(
        `usage-${state.project}.json`,
        JSON.stringify({ project: state.project, days: days || null, usage }, null, 2),
        "application/json",
      );
    }
    toast(`${kind.toUpperCase()} export started.`);
    setStatus(`Exported ${kind.toUpperCase()} for ${days ? `the last ${days} days` : "all time"}.`, "ok");
  } catch (error) {
    setStatus(`Export failed: ${error.message}`, "bad");
    toast(error.message, "bad");
  }
}

// ---------------------------------------------------------------------- settings

function renderSettings(policy) {
  $("settings-body").replaceChildren(
    el("dl", { class: "dl" }, [
      el("dt", { text: "Ledger retention" }),
      el("dd", {
        text:
          policy.retention_days === null
            ? "Rows are kept indefinitely. Prompt content is still never stored."
            : `Request and attempt rows older than ${policy.retention_days} days are purged by the retention job.`,
      }),
      el("dt", { text: "Forwarding redaction" }),
      el("dd", {
        text: policy.redact_before_forward
          ? "Matches are replaced before the request leaves the gateway. Match counts are recorded per request; matched values are not."
          : "Requests are forwarded unmodified. Redaction still applies to gateway logs.",
      }),
      el("dt", { text: "Patterns" }),
      el("dd", {
        class: "mono",
        text: policy.redaction_patterns.length
          ? policy.redaction_patterns.join(", ")
          : "built-in defaults",
      }),
      el("dt", { text: "Rate and concurrency" }),
      el("dd", {
        text: `${count(policy.requests_per_minute)} requests/minute, ${count(
          policy.max_concurrency,
        )} in flight per project.`,
      }),
    ]),
  );
}

// ------------------------------------------------------------------------- wiring

function connect() {
  const key = $("api-key").value.trim();
  if (!key) {
    setStatus("Enter the project API key printed by the gateway at startup.", "bad");
    $("api-key").focus();
    return;
  }
  state.key = key;
  sessionStorage.setItem("cost-firewall-key", key);
  loadAll();
}

async function loadAll() {
  if (!state.key) return;
  setLoading(true);
  setStatus("Reading budget, usage, and ledger…");
  const budgetHost = $("budget-body");
  if (!state.policy) budgetHost.replaceChildren(skeleton(3));

  const params = { project: state.project };
  const results = await Promise.allSettled([
    api("/v1/gateway/whoami"),
    api("/v1/gateway/budget", { params }),
    api("/v1/gateway/usage", { params: { ...params, days: 31 } }),
    api("/v1/gateway/providers", { params: { ...params, days: 7 } }),
    api("/v1/gateway/requests", { params: { ...params, limit: Number($("req-limit").value) } }),
    api("/v1/gateway/policy", { params }),
  ]);
  setLoading(false);

  const [whoami, budget, usage, providers, requests, policy] = results;
  if (whoami.status === "rejected") {
    const error = whoami.reason;
    const unauthorized = error.status === 401 || error.status === 403;
    setStatus(
      unauthorized
        ? "The gateway rejected this key. Check the key and try again."
        : `Could not connect: ${error.message}`,
      "bad",
    );
    // Every panel is per-project. Keeping the previous project's numbers on
    // screen after a rejected key would attribute another tenant's spend to the
    // key the operator is now holding, so clear all of them, not just the budget.
    // Build one node per host: cloneNode would copy the markup but not the Retry
    // button's listener, leaving a button that looks live and does nothing.
    const build = () =>
      unauthorized
        ? emptyState("Key rejected.", "The key was not accepted by the gateway.")
        : errorState("Could not read this project.", error);
    for (const id of ["budget-body", "activity-body", "providers-body", "blocks-body", "req-body"]) {
      $(id).replaceChildren(build());
    }
    $("api-key").setAttribute("aria-invalid", "true");
    return;
  }
  $("api-key").removeAttribute("aria-invalid");

  const identity = whoami.value;
  if (!state.project) state.project = identity.project;
  const select = $("project-select");
  select.replaceChildren(
    ...identity.readable_projects.map((id) =>
      el("option", { value: id, selected: id === state.project, text: id }),
    ),
  );
  select.disabled = false;

  if (budget.status === "fulfilled") renderBudget(budget.value);
  else $("budget-body").replaceChildren(errorState("Could not read the budget.", budget.reason));

  if (usage.status === "fulfilled") renderActivity(usage.value);
  else $("activity-body").replaceChildren(errorState("Could not read usage.", usage.reason));

  if (providers.status === "fulfilled") renderProviders(providers.value.providers);
  else $("providers-body").replaceChildren(errorState("Could not read provider health.", providers.reason));

  if (requests.status === "fulfilled") {
    state.requests = requests.value.requests;
    renderRequests();
    renderBlocks(state.requests);
  } else {
    $("req-body").replaceChildren(errorState("Could not read the ledger.", requests.reason));
    $("blocks-body").replaceChildren(errorState("Could not read blocks.", requests.reason));
  }

  if (policy.status === "fulfilled") {
    state.policy = policy.value;
    renderPolicy(policy.value);
    renderSettings(policy.value);
  } else {
    $("policy-body").replaceChildren(errorState("Could not read policy.", policy.reason));
  }

  if (budget.status === "fulfilled" && usage.status === "fulfilled") {
    const b = budget.value;
    const committed = b.month_settled_micros + b.month_reserved_micros;
    const blocked = requests.status === "fulfilled"
      ? requests.value.requests.filter((row) => row.outcome === "blocked").length
      : 0;
    setStatus(
      `Connected to ${identity.name}. ${usd(b.month_settled_micros)} settled of ${usd(
        b.monthly_budget_micros,
      )} (${percent(committed, b.monthly_budget_micros)} committed, ${usd(
        b.month_reserved_micros,
      )} reserved). ${count(usage.value.requests)} requests, ${blocked} blocked.`,
      committed >= b.monthly_budget_micros ? "bad" : "ok",
    );
  }

  await refreshReport();
}

async function refreshReport() {
  if (!state.key) return;
  const days = $("report-days").value;
  try {
    const usage = await api("/v1/gateway/usage", {
      params: { project: state.project, days },
    });
    renderReport(usage, days ? Number(days) : null);
  } catch (error) {
    $("report-body").replaceChildren(errorState("Could not build the report.", error));
  }
}

// -------------------------------------------------------------------------- tabs

const TABS = ["control", "requests", "simulator", "policy", "reports", "settings"];

function selectTab(name) {
  for (const id of TABS) {
    const tab = $(`tab-${id}`);
    const view = $(`view-${id}`);
    const selected = id === name;
    tab.setAttribute("aria-selected", selected ? "true" : "false");
    tab.tabIndex = selected ? 0 : -1;
    view.hidden = !selected;
  }
}

function wireTabs() {
  TABS.forEach((id, index) => {
    const tab = $(`tab-${id}`);
    tab.addEventListener("click", () => selectTab(id));
    tab.addEventListener("keydown", (event) => {
      const offset =
        event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : event.key === "Home" ? -index : 0;
      let next = null;
      if (offset) next = TABS[(index + offset + TABS.length) % TABS.length];
      if (event.key === "End") next = TABS[TABS.length - 1];
      if (!next) return;
      event.preventDefault();
      selectTab(next);
      const target = $(`tab-${next}`);
      target.focus();
      // The tab strip scrolls horizontally on narrow screens. Focus alone does
      // not guarantee the tab is scrolled into view in every browser, and an
      // off-screen selected tab looks like a lost focus ring.
      target.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  });
}

function wire() {
  wireTabs();
  $("connect").addEventListener("click", connect);
  $("api-key").addEventListener("keydown", (event) => {
    if (event.key === "Enter") connect();
  });
  $("project-select").addEventListener("change", (event) => {
    state.project = event.target.value;
    loadAll();
  });
  $("refresh-control").addEventListener("click", loadAll);
  $("refresh-requests").addEventListener("click", loadAll);
  $("req-search").addEventListener("input", renderRequests);
  $("req-status").addEventListener("change", renderRequests);
  $("req-limit").addEventListener("change", loadAll);
  $("sim-form").addEventListener("submit", simulate);
  $("sim-reset").addEventListener("click", () => {
    state.lastDecision = null;
    $("sim-copy").disabled = true;
    setSimState("idle", "neutral");
    $("sim-result-body").replaceChildren(
      emptyState("No simulation yet.", "Submit the form to see the route, cost estimate, and blocking reason."),
    );
  });
  $("sim-copy").addEventListener("click", async () => {
    if (!state.lastDecision) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(state.lastDecision, null, 2));
      toast("Decision JSON copied.");
    } catch {
      toast("Clipboard is unavailable in this browser.", "bad");
    }
  });
  $("report-days").addEventListener("change", refreshReport);
  $("export-json").addEventListener("click", () => exportReport("json"));
  $("export-csv").addEventListener("click", () => exportReport("csv"));
  $("request-dialog-close").addEventListener("click", () => $("request-dialog").close());
}

wire();
const saved = sessionStorage.getItem("cost-firewall-key");
if (saved) {
  $("api-key").value = saved;
  state.key = saved;
  loadAll();
}
