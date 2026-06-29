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

// Autonomy level → small badge (nothing for the default "manual").
const AUTONOMY_META = {
  "manual":    { icon: "🙋", label: "Manual" },
  "auto-safe": { icon: "🤖", label: "Auto-safe" },
  "yolo":      { icon: "🚀", label: "YOLO" },
};
function autonomyBadge(level) {
  if (!level || level === "manual") return "";
  const m = AUTONOMY_META[level] || { icon: "🤖", label: level };
  return `<span class="auto-badge ${level}" title="Autonomy: ${esc(m.label)}">${m.icon} ${esc(m.label)}</span>`;
}

// ---- Dashboard ---------------------------------------------------------------

const Dashboard = {
  FAST_MS: 1000,   // poll rate while any session is live/THINKING
  SLOW_MS: 5000,   // poll rate when everything is idle (or backgrounded)
  timer: null,
  mode: "all",            // "all" | "attention"
  notifyOn: false,
  prevStatus: {},         // session_id -> last seen status (for transition detection)
  baselineSet: false,

  init() {
    document.getElementById("limit").addEventListener("change", () => this.tick());
    document.getElementById("attentionToggle").addEventListener("click", () => {
      this.mode = this.mode === "attention" ? "all" : "attention";
      this.updateToggles();
      this.tick();
    });
    document.getElementById("notifyToggle").addEventListener("click", () => this.toggleNotify());
    this.notifyOn = localStorage.getItem("notifyOn") === "1"
      && ("Notification" in window) && Notification.permission === "granted";
    this.updateToggles();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.tick(); });
    this.tick();
  },

  updateToggles() {
    document.getElementById("attentionToggle").classList.toggle("active", this.mode === "attention");
    document.getElementById("limitLabel").style.display = this.mode === "attention" ? "none" : "";
    const nt = document.getElementById("notifyToggle");
    nt.classList.toggle("active", this.notifyOn);
    nt.textContent = this.notifyOn ? "🔔 Notifying" : "🔔 Notify";
  },

  async toggleNotify() {
    if (!("Notification" in window)) {
      alert("This browser doesn't support the Notification API.");
      return;
    }
    if (!window.isSecureContext) {
      alert("Notifications need a secure context. Open the dashboard via http://127.0.0.1 or http://localhost (not a LAN IP).");
      return;
    }
    if (this.notifyOn) {
      this.notifyOn = false; localStorage.setItem("notifyOn", "0");
      this.updateToggles();
      return;
    }
    let perm = Notification.permission;
    if (perm === "default") perm = await Notification.requestPermission();
    if (perm !== "granted") {
      alert("Notifications are blocked for this site.\n\nEnable them in your browser's site settings (click the icon left of the URL), and make sure your OS allows notifications for the browser (macOS: System Settings → Notifications → your browser; turn off Do Not Disturb/Focus).");
      this.updateToggles();
      return;
    }
    this.notifyOn = true; localStorage.setItem("notifyOn", "1");
    this.updateToggles();
    // Immediate confirmation so you know the pipeline works without waiting for
    // a real THINKING->WAITING transition.
    try {
      const n = new Notification("✅ Notifications enabled", {
        body: "You'll be alerted here when a session starts waiting for you.",
      });
      n.onclick = () => window.focus();
      setTimeout(() => n.close(), 5000);
    } catch (e) {
      alert("Permission is granted, but the browser couldn't show a notification. Check your OS notification settings for this browser (and Do Not Disturb / Focus).");
    }
  },

  async tick() {
    clearTimeout(this.timer);
    let active = false;
    try {
      const url = this.mode === "attention"
        ? "/api/sessions?status=attention&limit=all"
        : `/api/sessions?limit=${document.getElementById("limit").value}`;
      const data = await getJSON(url);
      this.detectTransitions(data.sessions);
      this.render(data);
      active = data.sessions.some((s) => s.status === "THINKING" || s.live);
    } catch (e) {
      document.getElementById("meta").textContent = "error: " + e.message;
    }
    // Keep polling in the background only when notifications are on (so we can
    // alert you while away); otherwise pause to save resources.
    if (document.hidden) {
      if (this.notifyOn) this.timer = setTimeout(() => this.tick(), this.SLOW_MS);
      return;
    }
    this.timer = setTimeout(() => this.tick(), active ? this.FAST_MS : this.SLOW_MS);
  },

  // Fire a notification when a session goes THINKING -> WAITING.
  detectTransitions(sessions) {
    const cur = {};
    for (const s of sessions) cur[s.session_id] = s.status;
    if (this.baselineSet && this.notifyOn) {
      for (const s of sessions) {
        if (this.prevStatus[s.session_id] === "THINKING" && s.status === "WAITING") {
          this.notify(s);
        }
      }
    }
    Object.assign(this.prevStatus, cur);
    this.baselineSet = true;
  },

  notify(s) {
    try {
      const n = new Notification("⏳ Session waiting for you", {
        body: `${s.title}\n${s.project || ""}`,
        tag: s.session_id,          // collapse repeats per session
        renotify: false,
      });
      n.onclick = () => {
        window.focus();
        location.href = `/session.html?id=${encodeURIComponent(s.session_id)}`;
      };
    } catch (e) { /* notifications best-effort */ }
  },

  render(data) {
    const grid = document.getElementById("grid");
    const empty = document.getElementById("empty");
    const meta = document.getElementById("meta");
    const t = new Date().toLocaleTimeString();
    if (this.mode === "attention") {
      meta.textContent = `${data.total} waiting on you · updated ${t}`;
      empty.textContent = "🎉 Nothing needs your attention right now.";
    } else {
      meta.textContent = `${data.sessions.length} of ${data.total} sessions · updated ${t}`;
      empty.textContent = "No sessions found.";
    }
    empty.style.display = data.sessions.length ? "none" : "block";
    grid.innerHTML = data.sessions.map((s) => this.card(s)).join("");
  },

  card(s) {
    const acts = (s.last_activities || []).map((a) =>
      `<div class="act"><span class="k ${a.kind}">${esc(a.kind)}</span>` +
      `<span class="t">${esc(a.text)}</span></div>`
    ).join("") || `<div class="act"><span class="t">No activity yet</span></div>`;

    return `
    <a class="card${s.pending_approval ? " gated" : ""}" href="/session.html?id=${encodeURIComponent(s.session_id)}">
      <div class="title">${esc(s.title)}${s.pending_approval ? `<span class="appr-badge">⚠ approve</span>` : ""}</div>
      <div class="project">${esc(s.project)}</div>
      <div class="row">
        ${originBadge(s)}
        <span class="pill ${s.status}">${s.status}</span>
        ${autonomyBadge(s.autonomy)}
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

// ---- Board (Kanban) — the home page ------------------------------------------

const Board = {
  FAST_MS: 1500,
  SLOW_MS: 5000,
  timer: null,
  notifyOn: false,
  prevStatus: {},
  baselineSet: false,
  lastSessions: [],
  limits: {},
  DEFAULT_LIMIT: "25",
  // THINKING first, then WAITING, then the rest. Archived has its own page.
  LANES: [
    { key: "THINKING", label: "💭 Thinking" },
    { key: "WAITING", label: "⏳ Waiting" },
    { key: "SITTING", label: "🪑 Sitting" },
    { key: "SLEEPING", label: "😴 Sleeping" },
    { key: "ENDED", label: "🏁 Ended" },
  ],

  init() {
    this.LANES.forEach((l) => {
      this.limits[l.key] = localStorage.getItem("laneLimit:" + l.key) || this.DEFAULT_LIMIT;
    });
    this.renderShell();
    this.notifyOn = localStorage.getItem("notifyOn") === "1"
      && ("Notification" in window) && Notification.permission === "granted";
    this.updateNotifyBtn();
    document.getElementById("notifyToggle").addEventListener("click", () => this.toggleNotify());
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.tick(); });
    this.tick();
  },

  renderShell() {
    const opts = (sel) => ["10", "25", "50", "all"]
      .map((v) => `<option value="${v}"${v === sel ? " selected" : ""}>${v === "all" ? "All" : v}</option>`).join("");
    document.getElementById("board").innerHTML = this.LANES.map((l) => `
      <section class="lane" data-key="${l.key}">
        <div class="lane-head">
          <span class="lane-label">${l.label}</span>
          <span class="lane-tools">
            <select class="lane-limit" data-key="${l.key}" title="Sessions to show in this lane">${opts(this.limits[l.key])}</select>
            <span class="lane-count" id="count-${l.key}">0</span>
          </span>
        </div>
        <div class="lane-body" id="lane-${l.key}"></div>
      </section>`).join("");
    document.querySelectorAll(".lane-limit").forEach((sel) => {
      sel.addEventListener("change", () => {
        this.limits[sel.dataset.key] = sel.value;
        localStorage.setItem("laneLimit:" + sel.dataset.key, sel.value);
        this.distribute(this.lastSessions);   // re-render with the new cap
      });
    });
  },

  async tick() {
    clearTimeout(this.timer);
    let active = false;
    try {
      const data = await getJSON("/api/sessions?limit=all");  // archived excluded by default
      this.lastSessions = data.sessions;
      this.detectTransitions(data.sessions);
      this.distribute(data.sessions);
      document.getElementById("meta").textContent =
        `${data.total} sessions · updated ${new Date().toLocaleTimeString()}`;
      active = data.sessions.some((s) => s.status === "THINKING" || s.live);
    } catch (e) {
      document.getElementById("meta").textContent = "error: " + e.message;
    }
    if (document.hidden) {                       // keep polling in background only to notify
      if (this.notifyOn) this.timer = setTimeout(() => this.tick(), this.SLOW_MS);
      return;
    }
    this.timer = setTimeout(() => this.tick(), active ? this.FAST_MS : this.SLOW_MS);
  },

  distribute(sessions) {
    const buckets = {};
    this.LANES.forEach((l) => (buckets[l.key] = []));
    for (const s of sessions) {
      if (buckets[s.status]) buckets[s.status].push(s);
      else buckets.ENDED.push(s);
    }
    for (const l of this.LANES) {
      const body = document.getElementById("lane-" + l.key);
      const keepScroll = body.scrollTop;
      const items = buckets[l.key];
      document.getElementById("count-" + l.key).textContent = items.length;
      const lim = this.limits[l.key] === "all" ? Infinity : parseInt(this.limits[l.key], 10);
      const shown = items.slice(0, lim);
      let html = shown.map((s) => Dashboard.card(s)).join("");
      if (items.length > shown.length) {
        html += `<div class="lane-more">+ ${items.length - shown.length} more — raise the limit</div>`;
      }
      body.innerHTML = html || `<div class="lane-empty">—</div>`;
      body.scrollTop = keepScroll;
    }
  },

  // notify when a session goes THINKING -> WAITING
  detectTransitions(sessions) {
    const cur = {};
    for (const s of sessions) cur[s.session_id] = s.status;
    if (this.baselineSet && this.notifyOn) {
      for (const s of sessions) {
        if (this.prevStatus[s.session_id] === "THINKING" && s.status === "WAITING") this.notify(s);
      }
    }
    Object.assign(this.prevStatus, cur);
    this.baselineSet = true;
  },

  notify(s) {
    try {
      const n = new Notification("⏳ Session waiting for you", {
        body: `${s.title}\n${s.project || ""}`, tag: s.session_id,
      });
      n.onclick = () => { window.focus(); location.href = `/session.html?id=${encodeURIComponent(s.session_id)}`; };
    } catch (e) { /* best-effort */ }
  },

  updateNotifyBtn() {
    const nt = document.getElementById("notifyToggle");
    nt.classList.toggle("active", this.notifyOn);
    nt.textContent = this.notifyOn ? "🔔 Notifying" : "🔔 Notify";
  },

  async toggleNotify() {
    if (!("Notification" in window)) { alert("This browser doesn't support the Notification API."); return; }
    if (!window.isSecureContext) { alert("Notifications need a secure context — open via http://127.0.0.1 or http://localhost."); return; }
    if (this.notifyOn) { this.notifyOn = false; localStorage.setItem("notifyOn", "0"); this.updateNotifyBtn(); return; }
    let perm = Notification.permission;
    if (perm === "default") perm = await Notification.requestPermission();
    if (perm !== "granted") {
      alert("Notifications are blocked. Enable them in your browser's site settings and your OS (macOS: System Settings → Notifications; turn off Focus/Do Not Disturb).");
      this.updateNotifyBtn(); return;
    }
    this.notifyOn = true; localStorage.setItem("notifyOn", "1"); this.updateNotifyBtn();
    try {
      const n = new Notification("✅ Notifications enabled", { body: "You'll be alerted when a session starts waiting for you." });
      n.onclick = () => window.focus(); setTimeout(() => n.close(), 5000);
    } catch (e) {
      alert("Permission granted, but the browser couldn't show a notification. Check your OS notification settings / Focus.");
    }
  },
};

// ---- Archived page -----------------------------------------------------------

const Archived = {
  init() {
    this.load();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.load(); });
  },
  async load() {
    try {
      const data = await getJSON("/api/sessions?archived=only&limit=all");
      const grid = document.getElementById("grid");
      const empty = document.getElementById("empty");
      document.getElementById("meta").textContent =
        `${data.total} archived · updated ${new Date().toLocaleTimeString()}`;
      grid.innerHTML = data.sessions.map((s) => Dashboard.card(s)).join("");
      empty.textContent = "No archived sessions. Archive one from its detail page.";
      empty.style.display = data.sessions.length ? "none" : "block";
    } catch (e) {
      document.getElementById("meta").textContent = "error: " + e.message;
    }
  },
};

// ---- World (anime-RPG view) --------------------------------------------------
//
// Every session is a little character living in the zone that matches its
// status. When a session changes status, its character WALKS to the new zone
// (the move is a CSS transform transition). Characters are emoji avatars chosen
// deterministically from the session id — no external art, fully offline.

const World = {
  FAST_MS: 1500,
  SLOW_MS: 5000,
  timer: null,
  actors: {},        // session_id -> { el, zone, x }
  lastSessions: [],
  ZONE_CAP: 30,      // most-recent N characters per zone (keeps the scene readable)

  // Ended sessions are not shown — a character leaves the world when its
  // session ends (it poofs out).
  ZONES: [
    { key: "THINKING", name: "⚒️ The Forge",   sub: "hard at work",        prop: "⚒️" },
    { key: "WAITING",  name: "📜 Quest Board",  sub: "awaiting your reply",  prop: "📜" },
    { key: "SITTING",  name: "🏕️ The Campfire", sub: "resting a while",      prop: "🔥" },
    { key: "SLEEPING", name: "🏠 The Inn",      sub: "fast asleep",          prop: "🌙" },
  ],

  // Original pixel-art RPG cast (generated by scripts/gen_heroes.py — no
  // third-party art). Deterministic per session, so a character keeps its look.
  AVATARS: [
    "knight-red", "knight-blue", "knight-green", "mage-purple", "mage-blue",
    "archer", "rogue", "barbarian", "skeleton", "goblin", "paladin",
  ],
  EMOTES: { THINKING: "💭", WAITING: "❗", SITTING: "☕", SLEEPING: "💤", ENDED: "🪦" },

  init() {
    this.renderShell();
    window.addEventListener("resize", () => this.layout(this.lastSessions));
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.tick(); });
    this.tick();
  },

  hash(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) | 0;
    return Math.abs(h);
  },

  avatarFor(id) { return this.AVATARS[this.hash(id) % this.AVATARS.length]; },

  renderShell() {
    document.getElementById("zones").innerHTML = this.ZONES.map((z) => `
      <section class="zone zone-${z.key}" data-key="${z.key}">
        <div class="zone-head">
          <span class="zone-name">${z.name}</span>
          <span class="zone-count" id="zc-${z.key}">0</span>
        </div>
        <div class="zone-sub">${z.sub}</div>
        <div class="zone-prop">${z.prop}</div>
        <div class="zone-floor" id="floor-${z.key}"></div>
      </section>`).join("");
  },

  async tick() {
    clearTimeout(this.timer);
    let active = false;
    try {
      const data = await getJSON("/api/sessions?limit=all");  // archived excluded by default
      this.lastSessions = data.sessions;
      this.layout(data.sessions);
      document.getElementById("meta").textContent =
        `${data.total} adventurers · updated ${new Date().toLocaleTimeString()}`;
      active = data.sessions.some((s) => s.status === "THINKING" || s.live);
    } catch (e) {
      document.getElementById("meta").textContent = "error: " + e.message;
    }
    if (document.hidden) return;
    this.timer = setTimeout(() => this.tick(), active ? this.FAST_MS : this.SLOW_MS);
  },

  layout(sessions) {
    if (!sessions) return;
    const stage = document.getElementById("world");
    const stageRect = stage.getBoundingClientRect();

    // bucket sessions by zone; statuses without a zone (ENDED) are skipped
    const buckets = {};
    this.ZONES.forEach((z) => (buckets[z.key] = []));
    for (const s of sessions) if (buckets[s.status]) buckets[s.status].push(s);

    // cap each zone to the most-recent N (sessions arrive sorted newest-first)
    const shownIds = new Set();
    const overflow = {};
    for (const z of this.ZONES) {
      const all = buckets[z.key];
      overflow[z.key] = Math.max(0, all.length - this.ZONE_CAP);
      buckets[z.key] = all.slice(0, this.ZONE_CAP);
      buckets[z.key].forEach((s) => shownIds.add(s.session_id));
    }

    // retire characters that are no longer shown (gone, archived, or capped out)
    for (const id of Object.keys(this.actors)) {
      if (!shownIds.has(id)) {
        const gone = this.actors[id];
        gone.el.classList.add("poof");
        setTimeout(() => { gone.el.remove(); }, 600);
        delete this.actors[id];
      }
    }

    for (const z of this.ZONES) {
      const items = buckets[z.key];
      const total = items.length + overflow[z.key];
      const cnt = document.getElementById("zc-" + z.key);
      cnt.textContent = overflow[z.key] ? `${items.length}+${overflow[z.key]}` : total;
      cnt.title = overflow[z.key] ? `showing ${items.length} of ${total} (newest first)` : `${total}`;
      const floor = document.getElementById("floor-" + z.key).getBoundingClientRect();
      const ax0 = floor.left - stageRect.left;
      const ay0 = floor.top - stageRect.top;
      const aw = Math.max(floor.width, 1);
      const ah = Math.max(floor.height, 1);

      const n = items.length;
      const cols = Math.max(1, Math.min(n || 1, Math.floor(aw / 84)));
      const rows = Math.max(1, Math.ceil(n / cols));
      const cellW = aw / cols;

      // CH = full height above the anchor: emote(~22) + headroom(~26) + avatar(~65)
      // + name label below the avatar — so the top row's bubble clears the header.
      const GAP = 18, CH = 118;
      const span = Math.max(0, ah - GAP - CH);
      const spacing = rows > 1 ? Math.min(CH, span / (rows - 1)) : 0;

      items.forEach((s, i) => {
        const c = i % cols, r = Math.floor(i / cols);
        const x = ax0 + cellW * c + cellW / 2;
        // fill top rows first, growing downward (y is the character's feet line)
        const y = ay0 + GAP + r * spacing + CH;
        this.place(s, z.key, x, y);
      });
    }
  },

  place(s, zoneKey, x, y) {
    let a = this.actors[s.session_id];
    if (!a) {
      const el = document.createElement("a");
      el.className = "actor spawn";
      el.href = `/session.html?id=${encodeURIComponent(s.session_id)}`;
      el.style.transform = `translate(${x}px, ${y}px)`;   // spawn in place (no slide)
      el.innerHTML = `
        <span class="toon">
          <span class="emote"></span>
          <img class="avatar" src="/static/sprites/${this.avatarFor(s.session_id)}.png" alt="" draggable="false">
          <span class="name"></span>
        </span>`;
      document.getElementById("actors").appendChild(el);
      // drop the spawn flag next frame so future moves animate
      requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("spawn")));
      a = this.actors[s.session_id] = { el, zone: null, x };
    }

    const el = a.el;
    // facing: flip when walking left
    if (x < a.x - 2) el.classList.add("face-left");
    else if (x > a.x + 2) el.classList.remove("face-left");
    a.x = x;

    if (a.zone !== zoneKey) {
      el.classList.remove("z-THINKING", "z-WAITING", "z-SITTING", "z-SLEEPING", "z-ENDED");
      el.classList.add("z-" + zoneKey);
      if (a.zone !== null) {            // an actual move — play the walk wobble
        el.classList.add("walking");
        setTimeout(() => el.classList.remove("walking"), 950);
      }
      a.zone = zoneKey;
      el.querySelector(".emote").textContent = this.EMOTES[zoneKey] || "";
    }

    el.style.transform = `translate(${x}px, ${y}px)`;
    el.style.zIndex = String(1000 + Math.round(y));   // lower = in front
    el.querySelector(".name").textContent =
      s.title && s.title.length > 22 ? s.title.slice(0, 21) + "…" : (s.title || "session");
    el.title = `${s.title}\n${s.status} · ${modelShort(s.model)} · ${fmtNum(s.tokens.total)} tok\n${s.project || ""}`;
  },
};

// ---- Detail ------------------------------------------------------------------

const Detail = {
  id: null,
  busy: false,
  activeModel: null,
  pollTimer: null,
  offset: 0,
  FAST_MS: 1500,
  SLOW_MS: 5000,

  init() {
    this.id = new URLSearchParams(location.search).get("id");
    if (!this.id) { document.getElementById("header").textContent = "Missing session id"; return; }
    this.load().then(() => this.poll());   // start polling only after first load
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) this.poll();
    });
  },

  // lightweight status refresh so the detail header tracks live CLI activity
  async poll() {
    clearTimeout(this.pollTimer);
    let active = false;
    try {
      const s = await getJSON(`/api/sessions/${encodeURIComponent(this.id)}/status`);
      active = s.status === "THINKING" || s.live;
      this.applyStatus(s);
      await this.tailNew();   // stream any newly-written history
      await this.refreshApproval();   // live tmux permission gate
    } catch (e) { /* keep last known state */ }
    if (document.hidden) return;
    this.pollTimer = setTimeout(() => this.poll(), active ? this.FAST_MS : this.SLOW_MS);
  },

  applyStatus(s) {
    // don't disturb an in-progress title edit
    if (document.getElementById("titleInput")) return;
    const prev = this.detail ? this.detail.status : null;
    // merge fresh status fields onto the loaded detail (keep activities)
    this.detail = Object.assign({}, this.detail || {}, s);
    this.renderHeader(this.detail);
    // when a session transitions into a waiting state, fetch its summary
    const WAIT = ["WAITING", "SITTING", "SLEEPING"];
    if (WAIT.includes(s.status) && !WAIT.includes(prev)) this.loadSummary(this.detail);
  },

  async load() {
    try {
      const d = await getJSON(`/api/sessions/${encodeURIComponent(this.id)}`);
      document.title = d.title;
      this.offset = d.file_size || 0;   // start tailing from here
      this.renderHeader(d);
      this.renderChat(d);
      this.renderEvents(d.activities || []);
      this.loadSummary(d);
      this.refreshApproval();
    } catch (e) {
      document.getElementById("header").textContent = "Error: " + e.message;
    }
  },

  // ---- live tmux permission gate (Yes/No/...) -------------------------------
  async refreshApproval() {
    const box = document.getElementById("approval");
    if (!box) return;
    let prompt = null;
    try {
      const r = await getJSON(`/api/sessions/${encodeURIComponent(this.id)}/tmux`);
      prompt = r.prompt;
      this.applyTmux(!!r.has_tmux);
    } catch (e) { return; /* keep last render */ }

    if (!prompt) {
      // Don't wipe an in-flight answer the user is typing into.
      if (this._answering) return;
      box.innerHTML = "";
      this._promptSig = null;
      return;
    }
    const sig = prompt.question + "|" + (prompt.context || "") + "|" + prompt.options.map((o) => o.num + o.label).join("|");
    if (sig === this._promptSig) return;   // unchanged — avoid re-render churn
    this._promptSig = sig;
    this._answering = false;

    const ctx = prompt.context
      ? `<pre class="approval-ctx">${esc(prompt.context)}</pre>` : "";

    const btns = prompt.options.map((o) => {
      const needsText = /tell claude|differently|what to do/i.test(o.label);
      const tone = o.num === 1 ? "yes" : (/^no\b/i.test(o.label) ? "no" : "");
      return `<button class="appr-btn ${tone}" data-num="${o.num}" data-text="${needsText ? 1 : 0}">
                <b>${o.num}</b> ${esc(o.label)}</button>`;
    }).join("");

    box.innerHTML = `
      <div class="approval-panel">
        <div class="approval-head">⚠ Needs your approval</div>
        ${ctx}
        <div class="approval-q">${esc(prompt.question)}</div>
        <div class="approval-opts">${btns}</div>
        <div class="approval-text" id="apprText" hidden>
          <input id="apprTextInput" placeholder="Tell Claude what to do differently…" />
          <button id="apprTextSend">Send</button>
        </div>
      </div>`;

    box.querySelectorAll(".appr-btn").forEach((b) => {
      b.addEventListener("click", () => {
        const num = parseInt(b.dataset.num, 10);
        if (b.dataset.text === "1") {
          this._answering = true;
          const tw = box.querySelector("#apprText");
          tw.hidden = false;
          const inp = box.querySelector("#apprTextInput");
          inp.focus();
          const go = () => this.answerPrompt(num, inp.value.trim());
          box.querySelector("#apprTextSend").onclick = go;
          inp.onkeydown = (e) => { if (e.key === "Enter") go(); };
        } else {
          this.answerPrompt(num, "");
        }
      });
    });
  },

  async answerPrompt(choice, text) {
    const box = document.getElementById("approval");
    box.innerHTML = `<div class="approval-panel"><div class="approval-head">sending answer…</div></div>`;
    this._promptSig = null;
    this._answering = false;
    try {
      const resp = await fetch(`/api/sessions/${encodeURIComponent(this.id)}/answer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choice, text: text || "" }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
    } catch (e) {
      box.innerHTML = `<div class="approval-panel"><div class="approval-head">⚠ ${esc(e.message)}</div></div>`;
      return;
    }
    // Give the REPL a moment to advance, then re-probe.
    setTimeout(() => this.refreshApproval(), 700);
  },

  // "What's expected from you" — generated lazily for idle/waiting sessions
  async loadSummary(d) {
    const box = document.getElementById("summary");
    const WAITING = ["WAITING", "SITTING", "SLEEPING"];
    if (!WAITING.includes(d.status)) { box.innerHTML = ""; return; }
    if (this._summaryBusy) return;   // avoid concurrent generation
    this._summaryBusy = true;

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
    } finally {
      this._summaryBusy = false;
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
          <button class="hdr-btn" id="archiveBtn">${d.archived ? "↩ Unarchive" : "📦 Archive"}</button>
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
    document.getElementById("archiveBtn").addEventListener("click", () => this.toggleArchive(d.archived));
  },

  async toggleArchive(isArchived) {
    const msg = isArchived
      ? "Unarchive this session? It will return to the dashboard and its status lane on the board."
      : "Archive this session? It will be hidden from the dashboard and moved to the board's Archived lane.";
    if (!confirm(msg)) return;
    try {
      await fetch(`/api/sessions/${encodeURIComponent(this.id)}/archive`,
        { method: isArchived ? "DELETE" : "POST" });
    } catch (e) { alert("Archive action failed: " + e.message); return; }
    this.load();
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
        <button id="spawnBtn" class="hdr-btn" title="Start a live tmux session (resume) so messages & approvals run in a live REPL">▶ Start live session</button>
        <select id="autoSel" title="Autonomy: how this session's permission gates are answered">
          ${["manual", "auto-safe", "yolo"].map((l) =>
            `<option value="${l}"${(d.autonomy || "manual") === l ? " selected" : ""}>auto: ${l}</option>`).join("")}
        </select>
        <span class="live-tmux" id="liveTmux"></span>
        <span class="now-model" id="nowModel"></span>
      </div>
      <div id="attach" class="attach"></div>
      <div class="chat-input">
        <textarea id="msg" rows="2" placeholder="Resume this session — type a message (paste an image to attach) and press ⌘/Ctrl+Enter…"></textarea>
        <button id="send">Send</button>
      </div>
      <div id="stream" class="stream"></div>`;
    this.attachments = [];
    this.renderAttach();

    const ta = document.getElementById("msg");
    const btn = document.getElementById("send");
    btn.addEventListener("click", () => this.send());
    ta.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); this.send(); }
    });
    ta.addEventListener("paste", (e) => this.handlePaste(e, ta));
    document.getElementById("spawnBtn").addEventListener("click", () => this.spawn());
    document.getElementById("autoSel").addEventListener("change", (e) => this.setAutonomy(e.target.value));
  },

  // Reflect whether a live tmux session exists; toggle the Start button.
  applyTmux(hasTmux) {
    const btn = document.getElementById("spawnBtn");
    const ind = document.getElementById("liveTmux");
    if (!btn || !ind) return;
    btn.style.display = hasTmux ? "none" : "";
    ind.textContent = hasTmux ? "● live tmux" : "";
  },

  async spawn() {
    const btn = document.getElementById("spawnBtn");
    btn.disabled = true; btn.textContent = "Starting…";
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(this.id)}/spawn`, { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      this.applyTmux(true);
      await this.refreshApproval();
    } catch (e) {
      btn.textContent = "▶ Start live session";
      btn.disabled = false;
      alert("Could not start live session: " + e.message);
    }
  },

  // Paste an image: upload it, show it as a thumbnail chip. Its saved path is
  // appended to the message on send (Claude Code reads image paths).
  async handlePaste(e, ta) {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    const img = [...items].find((it) => it.type && it.type.startsWith("image/"));
    if (!img) return;   // normal text paste — leave it alone
    e.preventDefault();
    const file = img.getAsFile();
    if (!file) return;

    const dataUrl = await new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(r.result); r.onerror = rej;
      r.readAsDataURL(file);
    });
    const chip = { id: "a" + Date.now(), path: null, dataUrl, busy: true, err: null };
    (this.attachments = this.attachments || []).push(chip);
    this.renderAttach();
    try {
      const resp = await fetch(`/api/sessions/${encodeURIComponent(this.id)}/paste`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data: dataUrl, mime: file.type }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const r = await resp.json();
      chip.path = r.path; chip.busy = false;
    } catch (err) {
      chip.busy = false; chip.err = err.message;
    }
    this.renderAttach();
    ta.focus();
  },

  renderAttach() {
    const box = document.getElementById("attach");
    if (!box) return;
    const list = this.attachments || [];
    if (!list.length) { box.innerHTML = ""; return; }
    box.innerHTML = list.map((a) => `
      <span class="chip ${a.err ? "err" : ""}" title="${esc(a.err || a.path || "uploading…")}">
        <img src="${a.dataUrl}" alt="" />
        <span class="chip-label">${a.busy ? "⏳" : a.err ? "⚠ failed" : "image"}</span>
        <button class="chip-x" data-id="${a.id}" aria-label="remove">✕</button>
      </span>`).join("");
    box.querySelectorAll(".chip-x").forEach((b) =>
      b.addEventListener("click", () => this.removeAttach(b.dataset.id)));
  },

  removeAttach(id) {
    this.attachments = (this.attachments || []).filter((a) => a.id !== id);
    this.renderAttach();
  },

  async send() {
    if (this.busy) return;
    const ta = document.getElementById("msg");
    const typed = ta.value.trim();
    const paths = (this.attachments || []).filter((a) => a.path).map((a) => a.path);
    if (!typed && !paths.length) return;
    // Append image paths so the live REPL / headless resume can read them.
    const text = paths.length ? (typed ? typed + "\n" + paths.join("\n") : paths.join("\n")) : typed;
    const perm = document.getElementById("perm").value;
    const stream = document.getElementById("stream");
    const btn = document.getElementById("send");

    this.busy = true; btn.disabled = true; btn.textContent = "Running…";
    stream.innerHTML = `<div class="event user"><div class="hd"><span class="kind">you</span></div><pre>${esc(text)}</pre></div>`;
    ta.value = "";
    this.attachments = [];
    this.renderAttach();

    // If this session is running live in tmux, type into the live REPL (one
    // continuous conversation) instead of forking a headless `claude --resume`.
    let live = false;
    try {
      const t = await getJSON(`/api/sessions/${encodeURIComponent(this.id)}/tmux`);
      live = !!t.has_tmux;
    } catch (e) { /* fall back to headless */ }

    try {
      if (live) {
        const resp = await fetch(`/api/sessions/${encodeURIComponent(this.id)}/say`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        stream.insertAdjacentHTML("beforeend",
          `<div class="result-line">→ sent to live tmux session · reply streams into History below</div>`);
      } else {
        const resp = await fetch(`/api/sessions/${encodeURIComponent(this.id)}/send`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, permission_mode: perm }),
        });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        await this.consume(resp, stream);
      }
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

  eventHTML(e, isNew) {
    const cls = "event " + e.kind + (e.is_error ? " error" : "") + (isNew ? " flash" : "");
    const label = e.name ? `${e.kind} · ${esc(e.name)}` : e.kind;
    return `
      <div class="${cls}">
        <div class="hd"><span class="kind">${label}</span><span class="ts">${fmtTime(e.ts)}</span></div>
        <pre>${esc(e.text)}</pre>
      </div>`;
  },

  renderEvents(events) {
    const wrap = document.getElementById("events");
    if (!events.length) { wrap.innerHTML = `<div class="empty">No activity.</div>`; return; }
    wrap.innerHTML = events.map((e) => this.eventHTML(e, false)).join("");
  },

  // append newly-written events to the TOP of the history (newest first)
  prependEvents(acts) {
    const wrap = document.getElementById("events");
    const placeholder = wrap.querySelector(".empty");
    if (placeholder) placeholder.remove();
    // acts are chronological (old->new); reverse so the newest ends up at top
    const html = acts.slice().reverse().map((e) => this.eventHTML(e, true)).join("");
    wrap.insertAdjacentHTML("afterbegin", html);
  },

  async tailNew() {
    if (this.busy) return;  // chat turn streams into #stream; load() resyncs after
    try {
      const r = await getJSON(`/api/sessions/${encodeURIComponent(this.id)}/tail?offset=${this.offset || 0}`);
      if (r.activities && r.activities.length) {
        this.offset = r.offset;
        this.prependEvents(r.activities);
      } else if (typeof r.offset === "number") {
        this.offset = r.offset;
      }
    } catch (e) { /* keep last offset; retry next poll */ }
  },

  // ---- autonomy selector ----------------------------------------------------
  async setAutonomy(level) {
    try {
      await fetch(`/api/sessions/${encodeURIComponent(this.id)}/autonomy`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level }),
      });
      if (this.detail) this.detail.autonomy = level;
    } catch (e) { alert("Could not set autonomy: " + e.message); }
  },
};

// ---- Triage (the inbox of sessions needing you) ------------------------------
//
// One column, longest-waiting on top. Each row shows what the session is blocked
// on — a permission gate (answer inline) or "waiting for your reply" — plus a
// quick autonomy dial. The whole point: clear the queue without opening tabs.

const Triage = {
  FAST_MS: 2000,
  SLOW_MS: 5000,
  timer: null,
  paused: false,
  _answering: {},      // sid -> true while an answer is in flight (don't churn)

  init() {
    document.getElementById("pauseToggle").addEventListener("click", () => this.togglePause());
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.tick(); });
    this.tick();
  },

  async tick() {
    clearTimeout(this.timer);
    try {
      const data = await getJSON("/api/triage");
      this.paused = data.autonomy_paused;
      this.render(data);
    } catch (e) {
      document.getElementById("meta").textContent = "error: " + e.message;
    }
    if (document.hidden) return;
    this.timer = setTimeout(() => this.tick(), this.FAST_MS);
  },

  render(data) {
    const list = document.getElementById("triage");
    const empty = document.getElementById("empty");
    document.getElementById("meta").textContent =
      `${data.total} need you · updated ${new Date().toLocaleTimeString()}`;
    const pt = document.getElementById("pauseToggle");
    pt.classList.toggle("active", this.paused);
    pt.textContent = this.paused ? "⏸ Autonomy paused" : "⏯ Autonomy live";
    empty.style.display = data.sessions.length ? "none" : "block";
    list.innerHTML = data.sessions.map((s) => this.row(s)).join("");
    list.querySelectorAll("[data-answer]").forEach((b) =>
      b.addEventListener("click", () => this.answer(b.dataset.sid, parseInt(b.dataset.answer, 10), b.dataset.needs === "1")));
    list.querySelectorAll("[data-auto]").forEach((sel) =>
      sel.addEventListener("change", () => this.setAutonomy(sel.dataset.sid, sel.value)));
  },

  row(s) {
    const wait = relTime(s.updated_at);
    const href = `/session.html?id=${encodeURIComponent(s.session_id)}`;
    const autoSel = `
      <select class="tri-auto" data-sid="${s.session_id}" title="Autonomy level">
        ${["manual", "auto-safe", "yolo"].map((l) =>
          `<option value="${l}"${s.autonomy === l ? " selected" : ""}>${l}</option>`).join("")}
      </select>`;

    let body;
    if (s.prompt) {
      const ctx = s.prompt.context
        ? `<pre class="approval-ctx">${esc(s.prompt.context)}</pre>` : "";
      const btns = s.prompt.options.map((o) => {
        const needsText = /tell claude|differently|what to do/i.test(o.label);
        const tone = o.num === 1 ? "yes" : (/^no\b/i.test(o.label) ? "no" : "");
        return `<button class="appr-btn ${tone}" data-sid="${s.session_id}" data-answer="${o.num}" data-needs="${needsText ? 1 : 0}"><b>${o.num}</b> ${esc(o.label)}</button>`;
      }).join("");
      body = `
        <div class="tri-gate">
          <div class="approval-head">⚠ Needs approval</div>
          ${ctx}
          <div class="approval-q">${esc(s.prompt.question)}</div>
          <div class="approval-opts">${btns}</div>
        </div>`;
    } else {
      body = `<div class="tri-wait">⏳ Waiting for your reply — <a href="${href}">open to respond</a></div>`;
    }

    return `
      <div class="tri-card${s.prompt ? " gated" : ""}">
        <div class="tri-head">
          <a class="tri-title" href="${href}">${esc(s.title)}</a>
          <span class="tri-meta">
            ${autonomyBadge(s.autonomy)}
            <span class="badge">${esc(modelShort(s.model))}</span>
            <span class="badge">${wait}</span>
            ${autoSel}
          </span>
        </div>
        <div class="tri-project">${esc(s.project || "")}</div>
        ${body}
      </div>`;
  },

  async answer(sid, choice, needsText) {
    if (needsText) {   // free-text follow-up belongs on the detail page
      location.href = `/session.html?id=${encodeURIComponent(sid)}`;
      return;
    }
    if (this._answering[sid]) return;
    this._answering[sid] = true;
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/answer`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choice, text: "" }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
    } catch (e) { alert("Answer failed: " + e.message); }
    finally {
      delete this._answering[sid];
      setTimeout(() => this.tick(), 600);   // let the REPL advance, then refresh
    }
  },

  async setAutonomy(sid, level) {
    try {
      await fetch(`/api/sessions/${encodeURIComponent(sid)}/autonomy`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level }),
      });
    } catch (e) { alert("Could not set autonomy: " + e.message); }
    this.tick();
  },

  async togglePause() {
    try {
      const r = await fetch("/api/autonomy/pause", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paused: !this.paused }),
      });
      const d = await r.json();
      this.paused = d.paused;
    } catch (e) { alert("Could not toggle: " + e.message); }
    this.tick();
  },
};

// ---- Dispatch (spawn a brand-new session for a task) -------------------------
//
// A modal that starts a fresh `claude --session-id <new>` in tmux, rooted at a
// project dir, and seeds it with the task prompt. Optionally sets the new
// session's autonomy so it can run hands-off.

const Dispatch = {
  open() {
    if (document.getElementById("dispatchModal")) return;
    const last = localStorage.getItem("dispatchCwd") || "";
    const el = document.createElement("div");
    el.id = "dispatchModal";
    el.className = "modal-backdrop";
    el.innerHTML = `
      <div class="modal">
        <div class="modal-head">✨ Dispatch a new session</div>
        <label class="modal-label">Project directory</label>
        <input id="dispCwd" class="modal-input" placeholder="/path/to/project" value="${esc(last)}" />
        <label class="modal-label">Task</label>
        <textarea id="dispPrompt" class="modal-input" rows="4" placeholder="What should the new session do?"></textarea>
        <div class="modal-row">
          <span>
            <label class="modal-label">Model</label>
            <select id="dispModel" class="modal-input">
              <option value="opus" selected>opus</option>
              <option value="sonnet">sonnet</option>
            </select>
          </span>
          <span>
            <label class="modal-label">Autonomy</label>
            <select id="dispAuto" class="modal-input">
              <option value="manual" selected>manual</option>
              <option value="auto-safe">auto-safe</option>
              <option value="yolo">yolo</option>
            </select>
          </span>
        </div>
        <div class="modal-status" id="dispStatus"></div>
        <div class="modal-actions">
          <button id="dispCancel" class="hdr-btn">Cancel</button>
          <button id="dispGo" class="hdr-btn primary">Dispatch ▶</button>
        </div>
      </div>`;
    document.body.appendChild(el);
    el.addEventListener("click", (e) => { if (e.target === el) this.close(); });
    document.getElementById("dispCancel").addEventListener("click", () => this.close());
    document.getElementById("dispGo").addEventListener("click", () => this.go());
  },

  close() {
    const el = document.getElementById("dispatchModal");
    if (el) el.remove();
  },

  async go() {
    const cwd = document.getElementById("dispCwd").value.trim();
    const prompt = document.getElementById("dispPrompt").value.trim();
    const model = document.getElementById("dispModel").value;
    const autonomy = document.getElementById("dispAuto").value;
    const status = document.getElementById("dispStatus");
    if (!cwd || !prompt) { status.textContent = "Project directory and task are required."; return; }
    localStorage.setItem("dispatchCwd", cwd);
    const go = document.getElementById("dispGo");
    go.disabled = true; status.textContent = "Starting session… (waiting for the REPL, ~10s)";
    try {
      const r = await fetch("/api/dispatch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cwd, prompt, model, autonomy }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status));
      status.innerHTML = `✅ Dispatched. <a href="/session.html?id=${encodeURIComponent(d.session_id)}">Open session →</a>`;
      go.textContent = "Dispatched";
    } catch (e) {
      status.textContent = "Dispatch failed: " + e.message;
      go.disabled = false;
    }
  },
};
