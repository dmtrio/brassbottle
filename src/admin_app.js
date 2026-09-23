import {
  html,
  render,
  useState,
  useEffect,
  useRef,
  useCallback,
} from "/vendor/htm-preact-standalone.module.js";

const POLL_MS = 2000;
const TITLE_BASE = "Egress queue - Djinn admin";

function localTimestamp(iso) {
  if (typeof iso !== "string" || !iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return (
    date.getFullYear() +
    "-" +
    pad(date.getMonth() + 1) +
    "-" +
    pad(date.getDate()) +
    " " +
    pad(date.getHours()) +
    ":" +
    pad(date.getMinutes()) +
    ":" +
    pad(date.getSeconds())
  );
}

function staleMessageWithSince(errorText, sinceDate) {
  const ts = sinceDate.toLocaleTimeString();
  if (typeof errorText === "string" && errorText) {
    return errorText + " - data stale since " + ts;
  }
  return "daemon unreachable - data stale since " + ts;
}

function StaleBanner({ message }) {
  if (!message) return null;
  return html`<div class="banner error" role="status" aria-live="polite" style="display: block;">${message}</div>`;
}

function EnableAlertsButton() {
  const onClick = useCallback(() => {
    if (!("Notification" in window)) return;
    Notification.requestPermission().catch(function () {});
  }, []);

  return html`<button id="alertsBtn" type="button" onClick=${onClick}>Enable alerts</button>`;
}

function RequestRow({
  row,
  inflight,
  reasonValue,
  globalArmValue,
  onReasonChange,
  onGlobalArmChange,
  onDecide,
  message,
}) {
  const hostPort = String(row.host || "") + ":" + String(row.port == null ? "" : row.port);
  const openedTitle = String(row.opened_at || "");
  const uidComm = [];
  if (row.uid != null) uidComm.push("uid " + String(row.uid));
  if (row.comm) uidComm.push(String(row.comm));
  const lastError =
    row.last_error && typeof row.last_error === "object" && !Array.isArray(row.last_error)
      ? row.last_error
      : null;
  return html`
    <tr>
      <td title=${openedTitle}>${localTimestamp(row.opened_at)}</td>
      <td title=${uidComm.join(" - ")}>
        ${hostPort}
        ${row.host_is_ip
          ? html`<span
              class="badge"
              title="Approving records the decision; the CIDR must reach the manifest by hand."
              >IP</span
            >`
          : null}
        ${row.hit_count == null
          ? null
          : html`<span class="badge">${String(row.hit_count)} hits</span>`}
      </td>
      <td>${row.reason ? String(row.reason) : "\u2014"}</td>
      <td>
        <div class="actions">
          <button type="button" disabled=${inflight} onClick=${() => onDecide(row, "allow_live", "", "")}>
            Allow
          </button>
          <button
            type="button"
            disabled=${inflight}
            onClick=${() => onDecide(row, "allow_manifest", "", "")}
          >
            Allow+manifest
          </button>
          <button
            type="button"
            class="warn"
            disabled=${inflight}
            onClick=${() => onDecide(row, "deny", reasonValue, "")}
          >
            Deny
          </button>
          <button
            type="button"
            class="warn"
            disabled=${inflight}
            onClick=${() => onDecide(row, "deny_bottle", reasonValue, "")}
          >
            Deny always (bottle)
          </button>
          <button
            type="button"
            class="error"
            disabled=${inflight}
            onClick=${() => onDecide(row, "deny_global", reasonValue, globalArmValue)}
          >
            Deny always (global)
          </button>
        </div>
        <input
          type="text"
          maxlength="200"
          placeholder="Optional deny reason"
          disabled=${inflight}
          value=${reasonValue}
          onInput=${(event) => onReasonChange(row.request_id, event.currentTarget.value)}
        />
        <input
          type="text"
          maxlength="200"
          placeholder="Type host for global deny"
          disabled=${inflight}
          value=${globalArmValue}
          onInput=${(event) => onGlobalArmChange(row.request_id, event.currentTarget.value)}
        />
        ${lastError
          ? html`<div class="chip error"
              >apply failed \u00d7${String(lastError.attempt == null ? "?" : lastError.attempt)}:
              ${String(lastError.reason || "")}</div
            >`
          : null}
        ${message
          ? html`<div class=${message.type === "error" ? "chip error" : "chip"}>${message.text}</div>`
          : null}
      </td>
    </tr>
  `;
}

function QueuePanel({
  data,
  inflightByKey,
  rowMessage,
  denyReasonByKey,
  globalArmByKey,
  onReasonChange,
  onGlobalArmChange,
  onDecide,
}) {
  const openRows = Array.isArray(data.open) ? data.open.slice() : [];
  openRows.sort((a, b) => {
    const aa = String(a.opened_at || "");
    const bb = String(b.opened_at || "");
    if (aa !== bb) return aa < bb ? -1 : 1;
    const ia = String(a.request_id || "");
    const ib = String(b.request_id || "");
    return ia < ib ? -1 : ia > ib ? 1 : 0;
  });

  const groups = new Map();
  for (const row of openRows) {
    const key = String(row.container || "");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }

  const generated = typeof data.generated_at === "string" ? data.generated_at : "unknown";
  const meta = "Open requests: " + openRows.length + " | generated at: " + generated;

  if (openRows.length === 0) {
    return html`
      <section class="panel">
        <div class="meta">${meta}</div>
        <${EnableAlertsButton} />
        <div class="empty">No open requests</div>
      </section>
    `;
  }

  const bodyRows = [];
  for (const container of groups.keys()) {
    const rows = groups.get(container);
    bodyRows.push(html`
      <tr class="group-row">
        <td colspan="4">${container + " - " + rows.length + " request(s)"}</td>
      </tr>
    `);
    for (const row of rows) {
      const key = String(row.request_id || "");
      bodyRows.push(html`<${RequestRow}
        row=${row}
        inflight=${Boolean(inflightByKey[key])}
        reasonValue=${denyReasonByKey[key] || ""}
        globalArmValue=${globalArmByKey[key] || ""}
        onReasonChange=${onReasonChange}
        onGlobalArmChange=${onGlobalArmChange}
        onDecide=${onDecide}
        message=${rowMessage[key] || null}
      />`);
    }
  }

  return html`
    <section class="panel">
      <div class="meta">${meta}</div>
      <${EnableAlertsButton} />
      <table>
        <thead>
          <tr>
            <th>Requested</th>
            <th>Destination</th>
            <th>Reason</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${bodyRows}
        </tbody>
      </table>
    </section>
  `;
}

function RecentRow({ row }) {
  const hostPort = String(row.host || "") + ":" + String(row.port == null ? "" : row.port);
  const outcomeBits = [String(row.status == null ? "" : row.status)];
  if (row.scope != null) outcomeBits.push(String(row.scope));
  let outcome = outcomeBits.filter(Boolean).join(" / ");
  if (row.apply_status && row.apply_status !== "applied") {
    outcome += " (apply: " + String(row.apply_status) + ")";
  }
  if (row.deny_reason) {
    outcome += " - " + String(row.deny_reason);
  }
  return html`
    <tr>
      <td title=${String(row.decided_at || "")}>${localTimestamp(row.decided_at)}</td>
      <td>${String(row.container || "")}</td>
      <td>${hostPort}</td>
      <td>${outcome}</td>
      <td>${String(row.decided_by || "")}</td>
    </tr>
  `;
}

function RecentPanel({ recent }) {
  const rows = Array.isArray(recent) ? recent : [];
  if (rows.length === 0) return null;
  return html`
    <section class="panel">
      <h2 class="recent-heading">Recent decisions (24 h)</h2>
      <table>
        <thead>
          <tr>
            <th>Decided</th>
            <th>Bottle</th>
            <th>Destination</th>
            <th>Outcome</th>
            <th>By</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row, index) => html`<${RecentRow} key=${index} row=${row} />`)}
        </tbody>
      </table>
    </section>
  `;
}

function App() {
  const [panel] = useState("queue");
  const [data, setData] = useState({ open: [], count: 0, generated_at: "-" });
  const [staleMessage, setStaleMessage] = useState("");
  const [inflightByKey, setInflightByKey] = useState({});
  const [rowMessage, setRowMessage] = useState({});
  const [denyReasonByKey, setDenyReasonByKey] = useState({});
  const [globalArmByKey, setGlobalArmByKey] = useState({});

  const seqRef = useRef(0);
  const timerRef = useRef(null);
  const staleSinceRef = useRef(null);
  const seenIdsRef = useRef(new Set());

  const setInflight = useCallback((key, value) => {
    setInflightByKey((prev) => {
      const next = { ...prev };
      if (value) next[key] = true;
      else delete next[key];
      return next;
    });
  }, []);

  const setRowChip = useCallback((key, value) => {
    setRowMessage((prev) => {
      const next = { ...prev };
      if (value) next[key] = value;
      else delete next[key];
      return next;
    });
  }, []);

  const pollNow = useCallback(() => {
    const seq = ++seqRef.current;
    fetch("/api/egress/queue", { method: "GET" })
      .then(async function (resp) {
        let body = {};
        try {
          body = await resp.json();
        } catch (_err) {}
        if (seq !== seqRef.current) return;
        if (resp.ok) {
          staleSinceRef.current = null;
          setStaleMessage("");
          setData(body);
          return;
        }
        if (!staleSinceRef.current) staleSinceRef.current = new Date();
        setStaleMessage(staleMessageWithSince(body.error, staleSinceRef.current));
      })
      .catch(function () {
        if (seq !== seqRef.current) return;
        if (!staleSinceRef.current) staleSinceRef.current = new Date();
        setStaleMessage(staleMessageWithSince("", staleSinceRef.current));
      })
      .finally(function () {
        if (seq !== seqRef.current) return;
        timerRef.current = setTimeout(pollNow, POLL_MS);
      });
  }, []);

  useEffect(() => {
    pollNow();
    return () => {
      seqRef.current += 1;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [pollNow]);

  useEffect(() => {
    const openRows = Array.isArray(data.open) ? data.open : [];
    const count = typeof data.count === "number" ? data.count : openRows.length;
    document.title = count > 0 ? "(" + count + ") " + TITLE_BASE : TITLE_BASE;

    if ("setAppBadge" in navigator) {
      Promise.resolve()
        .then(function () {
          if (count > 0) return navigator.setAppBadge(count);
          return navigator.clearAppBadge();
        })
        .catch(function () {});
    }

    if (!("Notification" in window) || Notification.permission !== "granted" || !document.hidden) return;
    for (const row of openRows) {
      const requestId = row && row.request_id;
      if (typeof requestId !== "string") continue;
      if (seenIdsRef.current.has(requestId)) continue;
      seenIdsRef.current.add(requestId);
      const note = new Notification("Egress request", {
        body: String(row.container || "") + " \u2192 " + String(row.host || "") + ":" + String(row.port || ""),
      });
      note.onclick = function () {
        window.focus();
      };
    }
  }, [data]);

  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    try {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    } catch (_err) {}
  }, []);

  const onReasonChange = useCallback((requestId, value) => {
    setDenyReasonByKey((prev) => ({ ...prev, [requestId]: value }));
  }, []);

  const onGlobalArmChange = useCallback((requestId, value) => {
    setGlobalArmByKey((prev) => ({ ...prev, [requestId]: value }));
  }, []);

  const onDecide = useCallback(
    (row, action, reason, requireHostText, messageKey) => {
      const key = String(messageKey || row.request_id || "");
      if (action === "deny_global" && requireHostText && requireHostText !== row.host) {
        setRowChip(key, { type: "error", text: "type exact host to arm global deny" });
        return;
      }

      const payload = { action: action, host: row.host };
      if (action !== "deny_global") payload.container = row.container;
      if (reason) payload.reason = reason;

      setRowChip(key, null);
      setInflight(key, true);

      fetch("/api/egress/decide", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Admin-UI": "1",
        },
        body: JSON.stringify(payload),
      })
        .then(async function (resp) {
          let body = {};
          try {
            body = await resp.json();
          } catch (_err) {}

          if (resp.ok) {
            const failures = Array.isArray(body.apply_failures) ? body.apply_failures : null;
            if (!failures) {
              setRowChip(key, {
                type: "neutral",
                text: "decision recorded (apply status unknown on this broker) - row clears once applied",
              });
            } else {
              const hit = failures.find(function (entry) {
                return entry && entry.request_id === row.request_id;
              });
              if (!hit) {
                setRowChip(key, { type: "neutral", text: "decision recorded" });
              } else if (hit.reason === "ip_requires_cidr") {
                setRowChip(key, { type: "neutral", text: "recorded - add CIDR to manifest by hand" });
              } else if (hit.reason === "apply_failed") {
                setRowChip(key, {
                  type: "error",
                  text: "decision recorded but rule install FAILED - request stays queued",
                });
              } else {
                setRowChip(key, { type: "error", text: "decision recorded with apply failure" });
              }
            }
            setStaleMessage("");
          } else if (resp.status === 400 && typeof body.error === "string") {
            setRowChip(key, { type: "error", text: body.error });
          } else if ((resp.status === 502 || resp.status === 503) && typeof body.error === "string") {
            setStaleMessage(body.error);
          } else {
            setStaleMessage("decide failed");
          }
        })
        .catch(function () {
          setStaleMessage("egress daemon unreachable");
        })
        .finally(function () {
          setInflight(key, false);
          pollNow();
        });
    },
    [pollNow, setInflight, setRowChip]
  );

  return html`
    <header>
      <h1>Djinn admin</h1>
      <nav aria-label="Panels">
        <button type="button" aria-current=${panel === "queue" ? "page" : null}>Egress queue</button>
        <button type="button" disabled title="Coming soon">Denylist</button>
        <button type="button" disabled title="Coming soon">Bottles</button>
        <button type="button" disabled title="Coming soon">Backup</button>
      </nav>
    </header>
    <main>
      <${StaleBanner} message=${staleMessage} />
      <${QueuePanel}
        data=${data}
        inflightByKey=${inflightByKey}
        rowMessage=${rowMessage}
        denyReasonByKey=${denyReasonByKey}
        globalArmByKey=${globalArmByKey}
        onReasonChange=${onReasonChange}
        onGlobalArmChange=${onGlobalArmChange}
        onDecide=${onDecide}
      />
      <${RecentPanel} recent=${data.recent} />
    </main>
    <footer class="small">
      Shows open approval requests (decisions), not currently-permitted hosts — the ipset allowlist is the authority.
    </footer>
  `;
}

render(html`<${App} />`, document.getElementById("appMount"));
