const state = {
  capture: null,
  kind: "rrc",
  query: "",
  offset: 0,
  limit: 40,
  total: 0,
};

const filters = [
  ["rrc", "RRC"],
  ["mac-ul", "MAC UL"],
  ["mac-dl", "MAC DL"],
  ["uci", "UCI"],
  ["rlc", "RLC"],
  ["bsr", "Buffer"],
  ["phy", "PHY"],
  ["nas", "5GMM"],
  ["lte", "LTE"],
  ["all", "All"],
];

const codeKind = {
  "0xB821": "rrc",
  "0xB881": "mac-ul",
  "0xB888": "mac-dl",
  "0xB89B": "uci",
  "0xB84D": "rlc",
  "0xB873": "bsr",
  "0xB97F": "phy",
  "0xB80C": "nas",
  "0xB0C0": "lte",
  "0xB0ED": "lte",
  "0xB063": "lte",
  "0xB064": "lte",
  "0xB18A": "lte",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[char]));
}

function tone(value) {
  const text = String(value ?? "").toLowerCase();
  if (["up", "registered", "yes", "1"].includes(text)) return "good";
  if (["down", "searching", "no", "0"].includes(text)) return "bad";
  if (text === "na" || text === "" || text === "null" || text === "none") return "warn";
  return "";
}

function card(label, value) {
  const shown = value === null || value === undefined || value === "" ? "—" : value;
  return `<article class="card"><span>${esc(label)}</span><strong class="${tone(shown)}">${esc(shown)}</strong></article>`;
}

function showBanner(text) {
  const banner = document.querySelector("#banner");
  banner.hidden = !text;
  banner.textContent = text || "";
}

async function loadLive() {
  const live = await fetch("/api/live").then((response) => response.json());
  const radio = live.radio || {};
  const link = live.link || {};
  if (!live.ok) {
    document.querySelector("#status").innerHTML = card("Cache", live.error || "unavailable");
    return;
  }
  document.querySelector("#status").innerHTML = [
    card("Status", live.status),
    card("Dongle", live.dongle),
    card("Registration", radio.reg),
    card("Data", radio.data),
    card("RSRP", radio.rsrp === null ? "—" : `${radio.rsrp} dBm`),
    card("RSRQ", radio.rsrq),
    card("SINR", radio.sinr),
    card("Band", radio.band),
    card("PCI", radio.pci),
    card("ARFCN", radio.arfcn),
    card("Internet loss", link.inet_loss === null || link.inet_loss === undefined ? "—" : `${link.inet_loss}%`),
    card("Cache age", live.age_s === null ? "—" : `${live.age_s}s`),
  ].join("");
  if (live.stale) showBanner("The monitor cache is stale. Status numbers may be from the previous reading.");
  else if ((live.errors || []).length) showBanner(live.errors.join(" "));
  else showBanner("");
}

async function loadCaptures() {
  const captures = await fetch("/api/captures").then((response) => response.json());
  const box = document.querySelector("#captures");
  if (!captures.length) {
    box.innerHTML = `<p class="empty">No captures yet. Run python3 trace_drop.py from this folder.</p>`;
    return;
  }
  if (!state.capture) state.capture = captures[0].id;
  box.innerHTML = captures.map((item) => {
    const codes = item.codes || {};
    return `<button type="button" class="capture${item.id === state.capture ? " active" : ""}" data-id="${item.id}">
      <strong>${esc(item.id)}</strong>
      <small>class ${esc(item.class || "—")} · usable ${esc(item.usable || "—")} · RRC ${codes["0xB821"] ?? 0} · 5GMM ${codes["0xB80C"] ?? 0} · PHY ${codes["0xB97F"] ?? 0}</small>
    </button>`;
  }).join("");
  box.querySelectorAll(".capture").forEach((button) => {
    button.addEventListener("click", () => {
      state.capture = button.dataset.id;
      state.offset = 0;
      loadCaptures();
      loadLog();
    });
  });
}

function renderCoverage(coverage) {
  const host = document.querySelector("#coverage");
  if (!host) return;
  const rows = (coverage && coverage.rows) || [];
  if (!rows.length) {
    host.innerHTML = `<p class="empty">This capture has no raw code counts. New traces subscribe every row in the lab table.</p>`;
    return;
  }
  host.innerHTML = `<table><thead><tr><th>Code</th><th>Row</th><th>Packets</th><th>Decoder</th></tr></thead><tbody>`
    + rows.map((row) => `<tr class="pick" data-kind="${codeKind[row.code] || "all"}">
      <td>${esc(row.code)}</td>
      <td>${esc(row.name)}</td>
      <td class="${row.count ? "good" : "warn"}">${esc(row.count)}</td>
      <td>${esc(row.decoder)}</td>
    </tr>`).join("")
    + `</tbody></table>`;
  host.querySelectorAll(".pick").forEach((row) => {
    row.addEventListener("click", () => {
      state.kind = row.dataset.kind;
      state.offset = 0;
      loadLog();
    });
  });
}

function renderTimeline(rows) {
  const box = document.querySelector("#timeline");
  if (!rows.length) {
    box.innerHTML = `<p class="empty">This capture has no timeline.csv.</p>`;
    return;
  }
  const head = "<tr><th>Time</th><th>Reg</th><th>Data</th><th>RSRP</th><th>Loss</th></tr>";
  const body = rows.map((row) => `<tr>
    <td>${esc((row.timestamp || "").slice(11, 19))}</td>
    <td class="${tone(row.reg)}">${esc(row.reg || "—")}</td>
    <td class="${tone(row.data)}">${esc(row.data || "—")}</td>
    <td>${esc(row.rsrp || "—")}</td>
    <td>${row.inet_loss === undefined ? "—" : `${esc(row.inet_loss)}%`}</td>
  </tr>`).join("");
  box.innerHTML = `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

function renderFilters(counts) {
  document.querySelector("#filters").innerHTML = filters.map(([kind, label]) => {
    const count = kind === "all" ? Object.values(counts).reduce((sum, value) => sum + value, 0) : (counts[kind] || 0);
    return `<button type="button" data-kind="${kind}" aria-selected="${state.kind === kind}">${label} ${count}</button>`;
  }).join("");
  document.querySelectorAll("#filters button").forEach((button) => {
    button.addEventListener("click", () => {
      state.kind = button.dataset.kind;
      state.offset = 0;
      loadLog();
    });
  });
}

function renderLog(payload) {
  renderTimeline(payload.timeline || []);
  renderCoverage(payload.coverage);
  renderFilters(payload.counts || {});
  const notes = payload.notes || {};
  const codes = notes.codes || {};
  const missing = ["0xB821", "0xB80C", "0xB97F"].filter((code) => !codes[code]);
  document.querySelector("#log-meta").textContent = missing.length
    ? `Class ${notes.class || "—"}. Usable ${notes.usable || "no"}. Missing ${missing.join(", ")}.`
    : `Class ${notes.class || "—"}. Usable ${notes.usable || "no"}. RRC, 5GMM, and PHY are all present.`;
  const log = document.querySelector("#log");
  if (!payload.events.length) {
    const why = {
      rrc: "No NR RRC packets in this capture.",
      nas: "No 5GMM state packets. The modem did not emit 0xB80C in this window.",
      "mac-ul": "No NR MAC uplink stats in this capture.",
      "mac-dl": "No NR MAC downlink stats in this capture.",
      uci: "No NR UCI packets in this capture.",
      rlc: "No NR RLC packets in this capture.",
      bsr: "No uplink buffer reports. That row appears under load.",
      lte: "No LTE packets. This Pegatron capture is 5G only.",
      phy: "No NR measurement packets in this capture.",
    }[state.kind] || "Nothing in this filter.";
    log.innerHTML = `<p class="empty">${why}</p>`;
  } else {
    log.innerHTML = payload.events.map((event) => `<details class="event">
      <summary>
        <span class="tag ${esc(event.kind)}">${esc(event.kind)}</span>
        <span class="time">${esc(event.time.startsWith("20") ? event.time.slice(11, 19) : (event.code || event.kind))}</span>
        <span>${esc(event.summary)}</span>
      </summary>
      <pre>${esc(event.text)}</pre>
    </details>`).join("");
  }
  const page = Math.floor(state.offset / state.limit) + 1;
  const pages = Math.max(1, Math.ceil(payload.total / state.limit));
  document.querySelector("#page").textContent = `${page} / ${pages}`;
  document.querySelector("#prev").disabled = state.offset === 0;
  document.querySelector("#next").disabled = state.offset + state.limit >= payload.total;
  state.total = payload.total;
}

async function loadLog() {
  if (!state.capture) return;
  const params = new URLSearchParams({
    kind: state.kind,
    q: state.query,
    offset: String(state.offset),
    limit: String(state.limit),
  });
  const payload = await fetch(`/api/captures/${state.capture}?${params}`).then((response) => response.json());
  renderLog(payload);
}

document.querySelector("#refresh").addEventListener("click", loadLive);
document.querySelector("#read-dongle").addEventListener("click", async () => {
  const button = document.querySelector("#read-dongle");
  button.disabled = true;
  showBanner("Opening the dongle DIAG port for 20 seconds. AT ports stay with ModemManager.");
  const response = await fetch("/api/capture", { method: "POST" });
  const body = await response.json();
  if (!response.ok) {
    showBanner(body.message || "Could not start the dongle read.");
    button.disabled = false;
    return;
  }
  const timer = setInterval(async () => {
    const status = await fetch("/api/capture").then((item) => item.json());
    if (!status.running) {
      clearInterval(timer);
      button.disabled = false;
      showBanner(status.message || "Dongle read finished.");
      state.capture = null;
      state.kind = "rrc";
      state.offset = 0;
      await loadCaptures();
      await loadLog();
    }
  }, 2000);
});
document.querySelector("#search").addEventListener("input", (event) => {
  state.query = event.target.value.trim();
  state.offset = 0;
  loadLog();
});
document.querySelector("#prev").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  loadLog();
});
document.querySelector("#next").addEventListener("click", () => {
  if (state.offset + state.limit < state.total) {
    state.offset += state.limit;
    loadLog();
  }
});

loadLive();
loadCaptures().then(loadLog);
