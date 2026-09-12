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
- `files` — `path, mtime, size, session_id`, the incremental ledger.

`refresh()` stats every transcript across all four providers, reparses only
files whose `(mtime, size)` moved, and replaces that file's rows in one
transaction. A deleted transcript drops its rows. Text comes from the same
render path the history view uses, so a snippet reads like the UI.

`search(q, limit, per_session)` returns hits carrying `snippet()` output,
role, timestamp and seq.

### 7.2 `server/app.py`

`/api/search` gains `mode=meta|text|both` (default `both`). Metadata matching
is untouched; text hits merge in, and every session still passes through
`_decorate`, so a result arrives with status, model, tokens, cost, projects and
task count. The response adds `hits: {session_id: [...]}`.

`POST /api/search/reindex` and `GET /api/search/index` (rows, files, last
build, whether a build is running).

### 7.3 UI

Result rows keep the full session header, then list each matching message with
highlighted context and an `open session →` link anchored to the match.

Snippets escape first, then the FTS5 markers become `<mark>`. The markers are
sentinels that cannot survive escaping as markup, so `esc` stays the single XSS
boundary it is everywhere else in the frontend.

### 7.4 Tests

`test_index.py` — incremental skip, deletion, all four providers, snippet
shape. `test_search_text_api.py` — mode switching, archived exclusion, hits
keyed by session.
