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

function ageString(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    return "-";
  }
  if (seconds < 60) return Math.floor(seconds) + "s";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m";
  return Math.floor(seconds / 3600) + "h";
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
  return html`
    <tr>
      <td>${ageString(row.age_seconds)}</td>
      <td>${String(row.container || "")}</td>
      <td>
        ${hostPort}
        ${row.host_is_ip
          ? html`<span
              class="badge"
              title="Approving records the decision; the CIDR must reach the manifest by hand."
              >IP</span
            >`
          : null}
      </td>
      <td>${String(row.uid == null ? "" : row.uid)}</td>
      <td>${String(row.comm || "")}</td>
      <td>${String(row.hit_count == null ? "" : row.hit_count)}</td>
      <td>${String(row.reason || "")}</td>
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
        ${message
          ? html`<div class=${message.type === "error" ? "chip error" : "chip"}>${message.text}</div>`
          : null}
      </td>
    </tr>
  `;
}

function HostGroup({ host, rows, armValue, onArmChange, onGlobalDeny, message }) {
  if (rows.length <= 1) return null;
  let totalHits = 0;
  for (const row of rows) totalHits += Number(row.hit_count || 0);
  return html`
    <tr class="host-group">
      <td colspan="8">
        ${host + " - " + rows.length + " request(s), " + totalHits + " hits"}
        <span> </span>
        <input
          type="text"
          maxlength="200"
          placeholder="Type host to arm global deny"
          value=${armValue}
          onInput=${(event) => onArmChange(host, event.currentTarget.value)}
        />
        <button
          type="button"
          class="error"
          style="margin-left: 8px;"
          onClick=${() => onGlobalDeny(host, rows, armValue)}
        >
          Deny always (global)
        </button>
        ${message
          ? html`<span class=${message.type === "error" ? "chip error" : "chip"} style="margin-left: 8px;"
              >${message.text}</span
            >`
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
  onGroupArmChange,
  onDecide,
  onGroupGlobalDeny,
}) {
  const openRows = Array.isArray(data.open) ? data.open.slice() : [];
  openRows.sort((a, b) => {
    const aa = typeof a.age_seconds === "number" ? a.age_seconds : 0;
    const bb = typeof b.age_seconds === "number" ? b.age_seconds : 0;
    return bb - aa;
  });

  const groups = new Map();
  for (const row of openRows) {
    const key = String(row.host || "");
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
  for (const host of groups.keys()) {
    const rows = groups.get(host);
    const groupKey = "__group__:" + host;
    bodyRows.push(html`<${HostGroup}
      host=${host}
      rows=${rows}
      armValue=${globalArmByKey[groupKey] || ""}
      onArmChange=${onGroupArmChange}
      onGlobalDeny=${onGroupGlobalDeny}
      message=${rowMessage[groupKey] || null}
    />`);
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
            <th>Age</th>
            <th>Container</th>
            <th>Host:port</th>
            <th>UID</th>
            <th>Comm</th>
            <th>Hits</th>
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

  const onGroupArmChange = useCallback((host, value) => {
    const key = "__group__:" + host;
    setGlobalArmByKey((prev) => ({ ...prev, [key]: value }));
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

  const onGroupGlobalDeny = useCallback(
    (host, rows, armValue) => {
      const first = rows[0];
      if (!first) return;
      onDecide(
        { request_id: "__group__:" + host, host: host, container: first.container },
        "deny_global",
        "",
        armValue,
        "__group__:" + host
      );
    },
    [onDecide]
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
        onGroupArmChange=${onGroupArmChange}
        onDecide=${onDecide}
        onGroupGlobalDeny=${onGroupGlobalDeny}
      />
    </main>
    <footer class="small">
      Shows open approval requests (decisions), not currently-permitted hosts — the ipset allowlist is the authority.
    </footer>
  `;
}

render(html`<${App} />`, document.getElementById("appMount"));
