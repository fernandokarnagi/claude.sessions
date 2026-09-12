"""Full-text search over every transcript the board can see.

One SQLite file beside the server (`server/.search.db`) holding an FTS5 table
of messages plus a ledger of what has been indexed. It is derived state: delete
it and the next refresh rebuilds it from the transcripts, which are never
touched.

Why a separate store at all: the four providers keep their history in four
different shapes (JSONL per session, a JSON directory per session, one SQLite
file per conversation, one shared SQLite file for everything). Grepping them
live on every keystroke would mean re-reading gigabytes; FTS5 ships with CPython
and answers in milliseconds.

The refresh is incremental. Each session carries a stamp — its (mtime, size),
or for opencode, whose sessions all share one database file, its own last-update
time and part count. A session whose stamp has not moved is skipped entirely;
one that has moved is deleted from the index and re-read whole. Re-reading a
changed session beats appending because a transcript can be rewritten, not only
extended, and a wrong index is worse than a slow one.
"""

import glob
import json
import os
import sqlite3
import threading
import time

from . import agyparser, grokparser, opencodeparser, parser

# The index lives beside the server, next to the other gitignored state files.
DB_PATH = os.environ.get(
    "SEARCH_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".search.db"))

# A single message longer than this is truncated before indexing.
MAX_TEXT = 20_000

# Tool calls and their results get a tighter cap. They are three quarters of
# the raw text on a real fleet — mostly file dumps and diffs — and past a few
# thousand characters they stop being something an operator searches for and
# start being the reason the index is hundreds of megabytes.
MAX_TOOL_TEXT = 4_000

# Snippet markers. Control characters, because anything printable (**, [[ ]])
# can occur in a transcript and would light up as a false highlight in the UI.
MARK_OPEN = "\x02"
MARK_CLOSE = "\x03"

# How long a refresh is trusted before a search kicks another one off.
REFRESH_SECS = float(os.environ.get("SEARCH_REFRESH_SECS", "60"))

_lock = threading.Lock()        # held while the index is being written
_gate = threading.Lock()        # guards the background-refresh bookkeeping
_last_refresh = 0.0
_refreshing = False

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS msgs USING fts5(
    session_id UNINDEXED,
    provider   UNINDEXED,
    seq        UNINDEXED,
    role       UNINDEXED,
    ts         UNINDEXED,
    text,
    tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS sources (
    session_id TEXT PRIMARY KEY,
    provider   TEXT,
    path       TEXT,
    mtime      REAL,
    size       INTEGER,
    msgs       INTEGER,
    indexed_at REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def db_path() -> str:
    return DB_PATH


def _connect() -> sqlite3.Connection:
    """Open the index, creating it if this is the first run."""
    conn = sqlite3.connect(db_path(), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # WAL so a search during a refresh reads the previous state instead of
    # blocking; the board polls while the index is being built.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    return conn


# ---- what there is to index --------------------------------------------------

def _claude_sources() -> dict:
    out = {}
    for path in glob.glob(os.path.join(parser.PROJECTS_DIR, "*", "*.jsonl")):
        sid = os.path.splitext(os.path.basename(path))[0]
        try:
            st = os.stat(path)
        except OSError:
            continue
        out[sid] = {"provider": "claude", "path": path,
                    "mtime": st.st_mtime, "size": st.st_size}
    return out


def _agy_sources() -> dict:
    out = {}
    for path in glob.glob(os.path.join(agyparser.CONV_DIR, "*.db")):
        sid = os.path.splitext(os.path.basename(path))[0]
        try:
            st = os.stat(path)
        except OSError:
            continue
        out[sid] = {"provider": "agy", "path": path,
                    "mtime": st.st_mtime, "size": st.st_size}
    return out


def _grok_sources() -> dict:
    """Grok keeps a directory per session, two levels under the sessions root;
    the chat log inside it is the stamp."""
    out = {}
    for sid, d in grokparser._session_dirs().items():
        try:
            st = os.stat(os.path.join(d, "chat_history.jsonl"))
        except OSError:
            continue
        out[sid] = {"provider": "grok", "path": d,
                    "mtime": st.st_mtime, "size": st.st_size}
    return out


def _opencode_sources() -> dict:
    """One shared database, so the per-file stamp would mark every session dirty
    on any write. The session's own updated time and part count stand in."""
    conn = opencodeparser._connect()
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT s.id AS id, s.time_updated AS updated, "
            "  (SELECT COUNT(*) FROM part p WHERE p.session_id = s.id) AS parts "
            "FROM session s").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {r["id"]: {"provider": "opencode", "path": opencodeparser.db_path(),
                      "mtime": float(r["updated"] or 0), "size": int(r["parts"] or 0)}
            for r in rows}


def sources() -> dict:
    """Every session the index should cover, keyed by session id."""
    out = {}
    for fn in (_claude_sources, _agy_sources, _grok_sources, _opencode_sources):
        try:
            out.update(fn())
        except Exception:
            # A provider that isn't installed, or whose store is mid-write,
            # must not stop the other three from being indexed.
            continue
    return out


# ---- reading one session's messages -----------------------------------------

def _clip(text, kind: str = "") -> str:
    text = (text or "").strip()
    # The snippet markers are these two control characters. A transcript that
    # contained one would light up as a false highlight in the UI, so they never
    # reach the index.
    if MARK_OPEN in text or MARK_CLOSE in text:
        text = text.replace(MARK_OPEN, " ").replace(MARK_CLOSE, " ")
    cap = MAX_TOOL_TEXT if kind in ("tool", "result", "tool_result") else MAX_TEXT
    return text[:cap] if len(text) > cap else text


def _claude_messages(path: str) -> list[dict]:
    """The same render path the history view uses, so what you search is what
    you would have read on the session page."""
    out, seq = [], 0
    try:
        events = list(parser._iter_events(path))
    except OSError:
        return []
    for evt in events:
        for block in parser.render_blocks(evt):
            kind = block.get("kind") or "assistant"
            text = _clip(block.get("text"), kind)
            if not text:
                continue
            role = kind
            if block.get("name"):
                role = f"tool:{block['name']}"
            out.append({"seq": seq, "role": role, "ts": block.get("ts"), "text": text})
            seq += 1
    return out


def _from_activities(acts: list, chronological: bool = True) -> list[dict]:
    if not chronological:
        acts = list(reversed(acts))
    out, seq = [], 0
    for a in acts:
        role = a.get("role") or a.get("kind") or "assistant"
        text = _clip(a.get("text"), role)
        if not text:
            continue
        if a.get("name"):
            role = f"tool:{a['name']}"
        out.append({"seq": seq, "role": role, "ts": a.get("ts"), "text": text})
        seq += 1
    return out


def messages(provider: str, sid: str, path: str) -> list[dict]:
    """Every readable message of one session, oldest first."""
    if provider == "claude":
        return _claude_messages(path)
    if provider == "grok":
        return _from_activities(grokparser._activities(path, 0, with_ts=True))
    if provider == "opencode":
        return _from_activities(opencodeparser._activities(sid, 0))
    if provider == "agy":
        detail = agyparser.get_conversation(sid)
        # agy's detail view is newest-first; the index stores reading order.
        return _from_activities((detail or {}).get("activities") or [],
                                chronological=False)
    return []


def _indexable(provider: str, sid: str, path: str) -> bool:
    """Skip the dashboard's own throwaway summarizer runs — they live in the
    Claude projects directory but never appear on the board."""
    if provider != "claude":
        return True
    s = parser._summary_for(path)
    return bool(s) and s.get("cwd") != parser.SUMMARIZER_CWD


# ---- building ----------------------------------------------------------------

def refresh(force: bool = False, limit: int | None = None) -> dict:
    """Bring the index level with the transcripts.

    Only sessions whose stamp has moved are re-read. `force` re-reads
    everything; `limit` caps how many sessions are re-read in one pass, so a
    first build on a large fleet can be done in slices without holding the lock
    for a minute.
    """
    started = time.time()
    found = sources()
    with _lock:
        conn = _connect()
        try:
            known = {r["session_id"]: r for r in
                     conn.execute("SELECT session_id, mtime, size FROM sources")}
            gone = [sid for sid in known if sid not in found]
            for sid in gone:
                conn.execute("DELETE FROM msgs WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sources WHERE session_id = ?", (sid,))

            stale = []
            for sid, src in found.items():
                row = known.get(sid)
                if (force or row is None
                        or row["mtime"] != src["mtime"] or row["size"] != src["size"]):
                    stale.append((sid, src))
            pending = max(0, len(stale) - limit) if limit else 0
            if limit:
                stale = stale[:limit]

            indexed = written = 0
            for sid, src in stale:
                if not _indexable(src["provider"], sid, src["path"]):
                    conn.execute("DELETE FROM msgs WHERE session_id = ?", (sid,))
                    conn.execute("DELETE FROM sources WHERE session_id = ?", (sid,))
                    continue
                try:
                    msgs = messages(src["provider"], sid, src["path"])
                except Exception:
                    # One unreadable session must not abort the whole pass.
                    continue
                conn.execute("DELETE FROM msgs WHERE session_id = ?", (sid,))
                conn.executemany(
                    "INSERT INTO msgs (session_id, provider, seq, role, ts, text) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(sid, src["provider"], m["seq"], m["role"], m["ts"], m["text"])
                     for m in msgs])
                conn.execute(
                    "INSERT OR REPLACE INTO sources "
                    "(session_id, provider, path, mtime, size, msgs, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sid, src["provider"], src["path"], src["mtime"], src["size"],
                     len(msgs), time.time()))
                indexed += 1
                written += len(msgs)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         ("built_at", str(time.time())))
            conn.commit()
        finally:
            conn.close()
    return {"indexed": indexed, "removed": len(gone), "messages": written,
            "sessions": len(found), "pending": pending,
            "took": round(time.time() - started, 3)}


def ensure_fresh(force: bool = False) -> bool:
    """Kick a refresh in the background if the index may be behind.

    Never blocks: a search answers from whatever is already indexed and the
    next one sees the new text. A first build on a large fleet takes tens of
    seconds, and a search box that hangs for that long is worse than one that
    is briefly incomplete.

    Returns True if this call started a pass.
    """
    global _last_refresh, _refreshing
    with _gate:
        if _refreshing:
            return False
        if not force and (time.time() - _last_refresh) < REFRESH_SECS:
            return False
        _refreshing = True

    def run():
        global _last_refresh, _refreshing
        try:
            refresh()
        except Exception:
            pass
        finally:
            with _gate:
                _last_refresh = time.time()
                _refreshing = False

    threading.Thread(target=run, name="search-index", daemon=True).start()
    return True


def indexing() -> bool:
    """True while a background pass is running."""
    with _gate:
        return _refreshing


def stats() -> dict:
    """What the index currently holds — for the UI's 'indexed N sessions' line."""
    try:
        conn = _connect()
    except sqlite3.Error:
        return {"sessions": 0, "messages": 0, "bytes": 0,
                "path": db_path(), "built_at": None}
    try:
        sess, msgs = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(msgs), 0) FROM sources").fetchone()
        row = conn.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
    finally:
        conn.close()
    try:
        size = os.path.getsize(db_path())
    except OSError:
        size = 0
    built = None
    if row:
        try:
            built = parser._iso(float(row[0]))
        except (TypeError, ValueError):
            built = None
    return {"sessions": int(sess or 0), "messages": int(msgs or 0),
            "bytes": size, "path": db_path(), "built_at": built,
            "indexing": indexing()}


# ---- searching ---------------------------------------------------------------

def _quoted(q: str) -> str:
    """Every word as a quoted term, ANDed. The fallback for a query FTS5 can't
    parse — an operator typing `don't` or `foo(bar)` means the literal text."""
    words = [w for w in "".join(c if c.isalnum() else " " for c in q).split() if w]
    return " AND ".join('"%s"' % w for w in words)


def search(q: str, limit: int = 40, per_session: int = 5,
           session_ids: set | None = None) -> dict:
    """Matching messages, grouped by session, best session first.

    Returns {hits: {session_id: [{seq, role, ts, snippet, provider}]},
             order: [session_id...], sessions, matches, query}.
    Snippets carry MARK_OPEN/MARK_CLOSE around each hit; the UI escapes the text
    first and turns the markers into <mark> after, so escaping stays the single
    XSS boundary.
    """
    q = (q or "").strip()
    empty = {"hits": {}, "order": [], "sessions": 0, "matches": 0,
             "query": q, "error": None}
    if not q:
        return empty
    try:
        conn = _connect()
    except sqlite3.Error:
        return empty

    sql = ("SELECT session_id, provider, seq, role, ts, "
           "       snippet(msgs, 5, ?, ?, '…', 16) AS snip, bm25(msgs) AS score "
           "FROM msgs WHERE msgs MATCH ? ORDER BY score LIMIT ?")
    # Over-fetch: the rows are per message, and the answer is per session.
    cap = max(limit * per_session * 4, 200)
    rows, error = [], None
    try:
        try:
            rows = conn.execute(sql, (MARK_OPEN, MARK_CLOSE, q, cap)).fetchall()
        except sqlite3.OperationalError:
            fallback = _quoted(q)
            if not fallback:
                return empty
            rows = conn.execute(sql, (MARK_OPEN, MARK_CLOSE, fallback, cap)).fetchall()
    except sqlite3.Error as exc:
        error = str(exc)
    finally:
        conn.close()
    if error:
        out = dict(empty)
        out["error"] = error
        return out

    hits: dict[str, list] = {}
    order: list[str] = []
    matched = 0
    for r in rows:
        sid = r["session_id"]
        if session_ids is not None and sid not in session_ids:
            continue
        if sid not in hits:
            if len(order) >= limit:
                continue
            hits[sid] = []
            order.append(sid)
        matched += 1
        if len(hits[sid]) >= per_session:
            continue
        hits[sid].append({"seq": r["seq"], "role": r["role"], "ts": r["ts"],
                          "snippet": r["snip"], "provider": r["provider"]})
    return {"hits": hits, "order": order, "sessions": len(order),
            "matches": matched, "query": q, "error": None}


def drop() -> None:
    """Delete the index. It is derived state; the next refresh rebuilds it."""
    global _last_refresh
    with _gate:
        _last_refresh = 0.0
    with _lock:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path() + suffix)
            except OSError:
                pass
