# Cost Plane and Full-Text Search — Implementation Plan

> Written: 2026-09-12
> Branch: `feat/cost-and-search`
> Scope: two independent features, built in order. Item 1 first (its `cost`
> field feeds the search result header), item 7 second.

---

## Global constraints

- Transcripts stay read-only. Every new file is app-side state beside the
  server, gitignored, one file per concern — the pattern `pins.py`,
  `attention.py` and `descriptions.py` already follow.
- No new runtime dependency. SQLite FTS5 ships with CPython.
- The board poll must not get slower. Anything expensive is cached by
  `(path, mtime, size)`, the same key `parser._summary_cache` uses, and the
  board only ever reads values already in a summary.
- `?v=N` bumps on every HTML page whenever `app.js` or `style.css` changes.
- Tests run with `.venv/bin/python -m pytest tests/ -q`; the suite is green
  before each commit.

---

## Item 1 — Cost plane

### 1.1 `server/prices.py`

A rate table and a classifier. Rates are US dollars per million tokens.

Four billing classes, because a fleet that mixes providers cannot be priced
with one rule:

| class | what it covers | dollars shown |
|---|---|---|
| `metered` | Anthropic API models | real, from the table |
| `subscription` | ollama cloud (`:cloud`/`-cloud`), free tiers (`:free`) | none — tokens only |
| `local` | on-machine models (mlx, gguf, quantised tags) | none — electricity is not billed here |
| `unknown` | anything unrecognised | none — never a guessed number |

Classification order: exact table hit, then a longest-prefix hit (so
`claude-haiku-4-5-20251001` resolves to the `claude-haiku-4-5` row), then the
suffix rules, then the local markers, else unknown.

`server/.prices.json` (gitignored) is merged over the built-in table at load,
so a rate change costs an edit, not a release.

API:

- `rate(model) -> dict | None` — the four per-MTok rates.
- `classify(model) -> str` — one of the four classes.
- `cost(model, tokens) -> dict` — `{usd, priced, billing, per_bucket}`.
  `usd` is `0.0` and `priced` is `False` for every class but `metered`.

### 1.2 `server/ledger.py`

- `for_session(summary)` — cost for one session from the token counts already
  in its summary. Cached on `(session_id, mtime)`; a board poll over 250
  sessions is dictionary lookups, no file work.
- `daily(path)` — tokens and dollars bucketed by calendar date, from a pass
  over the transcript's `usage` blocks (each carries a timestamp). Cached on
  `(path, mtime, size)`. Only `/cost.html` asks for it.
- `rollup(sessions)` — totals by project, by model and by day, plus burn rate
  (`usd / wall-clock hours`, wall clock being `created_at` to `mtime`).

### 1.3 `server/budgets.py` + `server/.budgets.json`

`get(sid)`, `set(sid, usd)`, `clear(sid)`, `all()`, `fleet_cap()`,
`set_fleet_cap(usd)`, `over(sid, spent)`. Absent means no cap. Same shape as
the other small state modules.

### 1.4 `server/autonomy.py` — caps as a control

In `_watch`, before any auto-answer:

1. A session over its own cap is forced to `manual`, logged, and announced
   through the existing `_hook` so Slack says why. The gate stays for a human.
2. Fleet spend over `budgets.fleet_cap()` calls `set_paused(True)` once.
3. `BUDGETS_DISABLED=1` switches the whole check off, matching
   `AUTONOMY_DISABLED`.

The check reads cached ledger values only — no subprocess, no file pass inside
the 2-second loop.

### 1.5 `server/app.py`

- `_decorate` gains `cost`, so board, search, triage and world inherit it.
- `GET /api/cost?group=project|model|day&days=N`
- `GET|PUT /api/sessions/{id}/budget`, `GET|PUT /api/budget/fleet`
- route `/cost.html`

### 1.6 UI

`cost.html` plus a `Cost` controller: fleet totals for today, 7 days and 30
days, burn rate, and bar tables by project, model and day. A `$` badge joins
the token count on the board card, and the detail header carries the session's
own cost and cap.

### 1.7 Tests

`test_prices.py`, `test_ledger.py`, `test_budgets.py`, `test_cost_api.py` —
unknown models stay unpriced, subscription and local never show a dollar, the
dated Haiku id resolves by prefix, a session over cap is forced to manual, a
fleet over cap pauses autonomy.

---

## Item 7 — Full-text search

### 7.1 `server/index.py`

SQLite FTS5 at `server/.search.db` (gitignored).

- `msgs` — FTS5 virtual table over `session_id, provider, seq, role, ts, text`.
- `sources` — `session_id, provider, path, mtime, size, msgs, indexed_at`, the
  incremental ledger.

**Built as:** the ledger is keyed by session, not by file. opencode keeps every
session in one database, so a per-file stamp would mark the whole fleet dirty
on any write; its sessions are stamped by their own `time_updated` and part
count instead. The other three keep a file (or directory) per session and stamp
as `(mtime, size)`.

`refresh()` stats every transcript across all four providers, reparses only
sessions whose stamp moved, and replaces that session's rows in one
transaction. A deleted transcript drops its rows. Text comes from the same
render path the history view uses, so a snippet reads like the UI. Tool calls
and their results are capped at 4k characters (prose at 20k) — they are three
quarters of the raw text on a real fleet and the tail of a file dump is not
what anyone searches for.

`ensure_fresh()` kicks a pass in a background thread and returns at once: a
first build over a thousand sessions takes tens of seconds, and a search box
that hangs that long is worse than one that is briefly incomplete.

`search(q, limit, per_session)` returns hits carrying `snippet()` output,
role, timestamp and seq.

### 7.2 `server/app.py`

`/api/search` gains `mode=meta|text|both` (default `both`). Metadata matching
is untouched; text hits merge in, and every session still passes through
`_decorate`, so a result arrives with status, model, tokens, cost, projects and
task count. The response adds `hits: {session_id: [...]}`.

`POST /api/search/reindex` (`?rebuild=true` throws the index away first) and
`GET /api/search/index` (sessions, messages, bytes, last build, whether a build
is running).

The root route was renamed `serve_index` — `index` is now a module.

### 7.3 UI

Result rows keep the full session header, then list each matching message with
highlighted context and an `open session →` link anchored to the match.

Snippets escape first, then the FTS5 markers become `<mark>`. The markers are
sentinels that cannot survive escaping as markup, so `esc` stays the single XSS
boundary it is everywhere else in the frontend.

### 7.4 Tests

`test_index.py` — incremental skip, deletion, clipping, snippet shape.
`test_index_providers.py` — one session built from scratch in each of the four
stores, including opencode's shared database and grok's two-level directory.
`test_search_text_api.py` — mode switching, archived exclusion, hits keyed by
session, the full session header on every result.

`tests/conftest.py` points the index at a scratch file and disarms the
background refresh for the whole suite, so no test can spend half a minute
re-reading the operator's transcripts or write over the running server's index.
