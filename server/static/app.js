"use strict";

// ---- shared helpers ----------------------------------------------------------

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function fmtNum(n) { return (n || 0).toLocaleString(); }

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function relTime(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return Math.floor(diff) + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

function modelShort(m) {
  if (!m) return "?";
  return m.replace(/^claude-/, "").replace(/-\d{8}$/, "");
}

function originBadge(s) {
  const origin = s.origin || "cli";
  const label = origin === "web" ? "WEB"
    : origin === "claude-vscode" ? "VSCODE"
    : "CLI";
  const dot = s.live ? `<span class="live-dot"></span>` : "";
  return `<span class="origin ${origin === "web" ? "web" : "cli"}">${dot}${label}</span>`;
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status + " " + url);
  return r.json();
}

// ---- Dashboard ---------------------------------------------------------------

const Dashboard = {
  FAST_MS: 1000,   // poll rate while any session is live/THINKING
  SLOW_MS: 5000,   // poll rate when everything is idle
  timer: null,

  init() {
    document.getElementById("limit").addEventListener("change", () => this.tick());
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) this.tick();  // refresh immediately when tab refocused
    });
    this.tick();
  },

  // self-pacing loop: fetch, render, then schedule the next tick based on activity
  async tick() {
    clearTimeout(this.timer);
    const limit = document.getElementById("limit").value;
    let active = false;
    try {
      const data = await getJSON(`/api/sessions?limit=${limit}`);
      this.render(data);
      active = data.sessions.some((s) => s.status === "THINKING" || s.live);
    } catch (e) {
      document.getElementById("meta").textContent = "error: " + e.message;
    }
    if (document.hidden) return;  // pause polling while tab is hidden
    this.timer = setTimeout(() => this.tick(), active ? this.FAST_MS : this.SLOW_MS);
  },

  render(data) {
    const grid = document.getElementById("grid");
    const empty = document.getElementById("empty");
    const meta = document.getElementById("meta");
    meta.textContent =
      `${data.sessions.length} of ${data.total} sessions · updated ${new Date().toLocaleTimeString()}`;

    empty.style.display = data.sessions.length ? "none" : "block";
    grid.innerHTML = data.sessions.map((s) => this.card(s)).join("");
  },

  card(s) {
    const acts = (s.last_activities || []).map((a) =>
      `<div class="act"><span class="k ${a.kind}">${esc(a.kind)}</span>` +
      `<span class="t">${esc(a.text)}</span></div>`
    ).join("") || `<div class="act"><span class="t">No activity yet</span></div>`;

    return `
    <a class="card" href="/session.html?id=${encodeURIComponent(s.session_id)}">
      <div class="title">${esc(s.title)}</div>
      <div class="project">${esc(s.project)}</div>
      <div class="row">
        ${originBadge(s)}
        <span class="pill ${s.status}">${s.status}</span>
        <span class="badge">${esc(modelShort(s.model))}</span>
        <span class="badge tokens"><b>${fmtNum(s.tokens.total)}</b> tok</span>
        <span class="badge" title="${esc(s.created_at || "")}">${relTime(s.updated_at)}</span>
      </div>
      <div class="activities">${acts}</div>
    </a>`;
  },
};

// ---- Search ------------------------------------------------------------------

const Search = {
  timer: null,

  init() {
    const input = document.getElementById("q");
    // prefill from ?q= if present
    const pre = new URLSearchParams(location.search).get("q");
    if (pre) { input.value = pre; }
    input.addEventListener("input", () => {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.run(), 200); // debounce
    });
    if (input.value.trim()) this.run();
  },

  async run() {
    const q = document.getElementById("q").value.trim();
    const grid = document.getElementById("grid");
    const empty = document.getElementById("empty");
    const meta = document.getElementById("meta");

    if (!q) {
      grid.innerHTML = "";
      empty.style.display = "block";
      empty.textContent = "Type to search by session ID or project path.";
      meta.textContent = "";
      return;
    }
    try {
      const data = await getJSON(`/api/search?q=${encodeURIComponent(q)}`);
      meta.textContent = `${data.total} match${data.total === 1 ? "" : "es"}`;
      grid.innerHTML = data.sessions.map((s) => Dashboard.card(s)).join("");
      empty.style.display = data.sessions.length ? "none" : "block";
      if (!data.sessions.length) empty.textContent = `No sessions match “${q}”.`;
    } catch (e) {
      meta.textContent = "error: " + e.message;
    }
  },
};

// ---- Detail ------------------------------------------------------------------

const Detail = {
  id: null,
  busy: false,
  activeModel: null,

  init() {
    this.id = new URLSearchParams(location.search).get("id");
    if (!this.id) { document.getElementById("header").textContent = "Missing session id"; return; }
    this.load();
  },

  async load() {
    try {
      const d = await getJSON(`/api/sessions/${encodeURIComponent(this.id)}`);
      document.title = d.title;
      this.renderHeader(d);
      this.renderChat(d);
      this.renderEvents(d.activities || []);
      this.loadSummary(d);
    } catch (e) {
      document.getElementById("header").textContent = "Error: " + e.message;
    }
  },

  // "What's expected from you" — generated lazily for idle/waiting sessions
  async loadSummary(d) {
    const box = document.getElementById("summary");
    const WAITING = ["WAITING", "SITTING", "SLEEPING"];
    if (!WAITING.includes(d.status)) { box.innerHTML = ""; return; }

    box.innerHTML = `
      <div class="summary-panel">
        <div class="summary-head">📋 What's expected from you
          <span class="summary-status">generating…</span>
        </div>
        <div class="summary-body"><span class="spin">●</span> reading the last message…</div>
      </div>`;
    try {
      const r = await getJSON(`/api/sessions/${encodeURIComponent(this.id)}/summary`);
      const head = box.querySelector(".summary-status");
      const body = box.querySelector(".summary-body");
      if (r.summary) {
        head.textContent = r.cached ? "cached" : "";
        body.textContent = r.summary;
      } else {
        head.textContent = "";
        body.innerHTML = `<span class="muted">No summary (${esc(r.reason || "unavailable")}).</span>`;
      }
    } catch (e) {
      box.querySelector(".summary-body").textContent = "Could not generate summary: " + e.message;
    }
  },

  renderHeader(d) {
    const t = d.tokens;
    this.detail = d;
    document.getElementById("meta").textContent = d.session_id;
    const resetBtn = d.renamed
      ? `<button class="icon-btn" id="resetTitle" title="Revert to original: ${esc(d.default_title)}">↺</button>`
      : "";
    document.getElementById("header").innerHTML = `
      <div class="detail-header">
        <h2 class="sess-title">
          <span id="titleText">${esc(d.title)}</span>
          <button class="icon-btn" id="editTitle" title="Rename">✎</button>
          ${resetBtn}
        </h2>
        <div class="stats">
          ${originBadge(d)}
          <span class="pill ${d.status}">${d.status}</span>
          <span>model <b>${esc(modelShort(d.model))}</b></span>
          <span>created <b>${fmtTime(d.created_at)}</b></span>
          <span>updated <b>${fmtTime(d.updated_at)}</b></span>
          <span>${esc(d.project)}</span>
        </div>
        <div class="token-grid">
          <div><span>total</span><b>${fmtNum(t.total)}</b></div>
          <div><span>input</span>${fmtNum(t.input)}</div>
          <div><span>output</span>${fmtNum(t.output)}</div>
          <div><span>cache read</span>${fmtNum(t.cache_read)}</div>
          <div><span>cache create</span>${fmtNum(t.cache_creation)}</div>
        </div>
      </div>`;

    document.getElementById("editTitle").addEventListener("click", () => this.startEdit());
    const rb = document.getElementById("resetTitle");
    if (rb) rb.addEventListener("click", () => this.resetTitle());
  },

  startEdit() {
    const span = document.getElementById("titleText");
    const current = this.detail.title;
    const h2 = span.closest(".sess-title");
    h2.innerHTML = `
      <input id="titleInput" class="title-input" value="${esc(current)}" />
      <button class="icon-btn" id="saveTitle" title="Save (Enter)">✓</button>
      <button class="icon-btn" id="cancelTitle" title="Cancel (Esc)">✕</button>`;
    const input = document.getElementById("titleInput");
    input.focus(); input.select();
    document.getElementById("saveTitle").addEventListener("click", () => this.saveTitle(input.value));
    document.getElementById("cancelTitle").addEventListener("click", () => this.renderHeader(this.detail));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); this.saveTitle(input.value); }
      if (e.key === "Escape") { e.preventDefault(); this.renderHeader(this.detail); }
    });
  },

  async saveTitle(value) {
    try {
      await fetch(`/api/sessions/${encodeURIComponent(this.id)}/title`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: value }),
      });
    } catch (e) { /* ignore; reload reflects truth */ }
    this.load();
  },

  async resetTitle() {
    try {
      await fetch(`/api/sessions/${encodeURIComponent(this.id)}/title`, { method: "DELETE" });
    } catch (e) { /* ignore */ }
    this.load();
  },

  renderChat(d) {
    const cli = d.status === "THINKING" && d.origin !== "web";
    const warn = cli
      ? `<div class="chat-warn">⚠ This session looks live in the CLI right now. Resuming from the web forks the conversation — wait until it's idle.</div>`
      : "";
    document.getElementById("chat").innerHTML = `
      ${warn}
      <div class="chat-row">
        <select id="perm" title="Permission mode for tools">
          <option value="acceptEdits" selected>acceptEdits</option>
          <option value="plan">plan</option>
          <option value="bypassPermissions">bypassPermissions</option>
          <option value="default">default</option>
        </select>
        <span class="now-model" id="nowModel"></span>
      </div>
      <div class="chat-input">
        <textarea id="msg" rows="2" placeholder="Resume this session — type a message and press ⌘/Ctrl+Enter…"></textarea>
        <button id="send">Send</button>
      </div>
      <div id="stream" class="stream"></div>`;

    const ta = document.getElementById("msg");
    const btn = document.getElementById("send");
    btn.addEventListener("click", () => this.send());
    ta.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); this.send(); }
    });
  },

  async send() {
    if (this.busy) return;
    const ta = document.getElementById("msg");
    const text = ta.value.trim();
    if (!text) return;
    const perm = document.getElementById("perm").value;
    const stream = document.getElementById("stream");
    const btn = document.getElementById("send");

    this.busy = true; btn.disabled = true; btn.textContent = "Running…";
    stream.innerHTML = `<div class="event user"><div class="hd"><span class="kind">you</span></div><pre>${esc(text)}</pre></div>`;
    ta.value = "";

    try {
      const resp = await fetch(`/api/sessions/${encodeURIComponent(this.id)}/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, permission_mode: perm }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      await this.consume(resp, stream);
    } catch (e) {
      stream.insertAdjacentHTML("beforeend",
        `<div class="event result error"><pre>${esc(e.message)}</pre></div>`);
    } finally {
      this.busy = false; btn.disabled = false; btn.textContent = "Send";
      this.load(); // refresh persisted history + header
    }
  },

  // read an SSE-style stream from a fetch response
  async consume(resp, stream) {
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (chunk.startsWith("data: ")) {
          let evt; try { evt = JSON.parse(chunk.slice(6)); } catch { continue; }
          this.handleEvent(evt, stream);
        }
      }
    }
  },

  handleEvent(evt, stream) {
    const add = (html) => { stream.insertAdjacentHTML("beforeend", html); stream.scrollTop = stream.scrollHeight; };

    if (evt.type === "system" && evt.subtype === "init") {
      this.activeModel = evt.model;
      document.getElementById("nowModel").innerHTML =
        `now using <b>${esc(modelShort(evt.model))}</b>`;
      return;
    }
    if (evt.type === "assistant" && evt.message) {
      for (const b of evt.message.content || []) {
        if (b.type === "text") add(`<div class="event assistant"><pre>${esc(b.text)}</pre></div>`);
        else if (b.type === "thinking") add(`<div class="event thinking"><pre>${esc(b.thinking)}</pre></div>`);
        else if (b.type === "tool_use")
          add(`<div class="event tool"><div class="hd"><span class="kind">tool · ${esc(b.name)}</span></div><pre>${esc(JSON.stringify(b.input, null, 2))}</pre></div>`);
      }
      return;
    }
    if (evt.type === "user" && evt.message) {
      for (const b of evt.message.content || []) {
        if (b.type === "tool_result") {
          let c = b.content;
          if (Array.isArray(c)) c = c.map((x) => x.text || "").join("\n");
          add(`<div class="event result${b.is_error ? " error" : ""}"><pre>${esc(c)}</pre></div>`);
        }
      }
      return;
    }
    if (evt.type === "result") {
      const u = evt.usage || {};
      const cost = evt.total_cost_usd != null ? ` · $${evt.total_cost_usd.toFixed(4)}` : "";
      add(`<div class="result-line">✔ done · ${fmtNum((u.input_tokens || 0) + (u.output_tokens || 0))} tok${cost} · ${evt.duration_ms || 0}ms</div>`);
      return;
    }
    if (evt.type === "error") {
      add(`<div class="event result error"><pre>${esc(evt.message)}</pre></div>`);
    }
  },

  renderEvents(events) {
    const wrap = document.getElementById("events");
    if (!events.length) { wrap.innerHTML = `<div class="empty">No activity.</div>`; return; }
    wrap.innerHTML = events.map((e) => {
      const cls = "event " + e.kind + (e.is_error ? " error" : "");
      const label = e.name ? `${e.kind} · ${esc(e.name)}` : e.kind;
      return `
      <div class="${cls}">
        <div class="hd"><span class="kind">${label}</span><span class="ts">${fmtTime(e.ts)}</span></div>
        <pre>${esc(e.text)}</pre>
      </div>`;
    }).join("");
  },
};
