# I Could See My AI Agents — So Now I Wanted to Drive Them

### Part 2: how a read-only dashboard for Claude Code turned into a control plane for a fleet of AI coding agents

---

In [Part 1](https://medium.com/artificial-intelligence-and-just-all-about-it/i-couldnt-see-what-my-ai-coding-agent-was-doing-so-i-built-a-dashboard-for-it-e52116b5faa8), I built a dashboard because I couldn't see what my AI coding agent was doing. It read Claude Code's session transcripts off disk, inferred a status for each one (THINKING, WAITING, SITTING, SLEEPING, ENDED), and gave me a live grid — mission control for my AI pair programmer. It was strictly read-only. It watched. It never touched anything.

That solved the first problem: *I couldn't see them.*

It immediately created the second problem: **now that I could see them, I couldn't stand not being able to do anything about them.**

I'd be looking at the board, watching a session sit in WAITING with a bright badge that said "needs your approval," and my only move was to alt-tab into the right terminal, find the tmux pane, read the prompt again, and type `1`. The dashboard had turned a dozen invisible sessions into a dozen visible ones — and in doing so, it made the friction of *acting* on them impossible to ignore. I'd built a beautiful window and no door.

Part 2 is the story of adding the doors. It's how the dashboard went from a **monitor** to a **control plane** — a place where I approve, redirect, spawn, relay, and shut down a fleet of agents without ever touching a terminal. And, spoiler, the most important thing I learned building it isn't about any one feature. It's this:

> Once you can see your agents, **your own attention becomes the bottleneck.** Every feature in Part 2 is really an attempt to spend that attention more cheaply.

Let me walk through what I added, why, how it's wired, and what each piece bought me.

---

## The hinge: from reading files to driving a live REPL

Part 1's superpower was also its cage. Everything worked by reading `~/.claude/projects/**/*.jsonl` — the transcript files Claude Code writes as it goes. Reading files is safe, simple, and completely one-directional. You cannot *answer* a permission prompt by reading a log.

To act, I needed a live handle on the running session, not its transcript. And it turned out I already had one, sitting in plain sight.

Every one of my Claude Code sessions is launched inside a **detached tmux session**, and — this is the crucial invariant — **the tmux session's name is the Claude session id.** That one convention is the whole foundation of Part 2. It means a session id is simultaneously:

- a filename (the transcript, for reading — Part 1), and
- a tmux target (the live REPL, for writing — Part 2).

So I wrote a small module, `tmuxio.py`, that does three things against a live pane:

- **`capture_pane(id)`** — grab the current terminal screen as text.
- **`parse_prompt(text)`** — detect a pending permission prompt (the numbered `❯ 1. Yes / 2. No…` menu) and pull out the question, the options, and the command it's asking to run.
- **`answer(id, choice, text)`** — send the keystrokes to pick an option (and type follow-up guidance for the "No, and tell Claude what to do differently" case).

That's it. No API, no plugin, no hook into Claude Code's internals. I read the screen and I type on the keyboard, exactly like I would by hand — just programmatically. It's a little bit of a hack and I love it, because it's *robust*: it doesn't depend on any internal format that could change out from under me. tmux is the API.

**The benefit:** the dashboard could now show a permission gate the instant it appeared, with the command it wanted to run, and let me click **Yes / No / option 3** right there. The alt-tab tax was gone. That single change is what made everything after it possible.

---

## Getting the approvals off the screen entirely: Slack

Answering gates in the dashboard was great when I was looking at the dashboard. But the whole reason agents are useful is that I *walk away* from them. The gate would fire, and the session would sit blocked until I happened to glance back.

So the next door led out of the app entirely, into **Slack**.

I run a background watcher thread that polls for pending gates. When a new one appears, it posts a message to a Slack channel — the session title, the command being requested, and a button per option. I tap **Yes** on my phone, the watcher receives the action and calls `tmuxio.answer`, and the session unblocks. When the gate resolves, the message updates in place so I'm never looking at a stale button. There's a `/pending` slash command to list everything currently blocked, and the "tell Claude what to do differently" option opens a little modal so I can type redirection from Slack.

Architecturally it's Socket Mode (a WebSocket, so no public URL, no inbound webhook to expose from my laptop) plus a bit of Web API polling for reliability. The whole thing is off by default — if the Slack env vars aren't set, the watcher just doesn't start and the dashboard runs exactly as before.

There was one genuinely infuriating gotcha I'll pass on to save you the afternoon I lost: **the Slack bot stayed completely silent, no error, nothing.** The cause was mundane and cruel — the process serving the dashboard didn't have the Slack tokens in its environment, so the bot's `enabled()` check quietly returned false and it never even tried to start. The fix was to source a gitignored `.env.slack` file from the launch script and set `PYTHONUNBUFFERED=1` so I could actually *see* the logs. "Check whether the feature is even turned on" is embarrassingly far down everyone's debugging list, mine included.

**The benefit:** approvals stopped being tied to a screen. My agents could now interrupt me *wherever I was*, and I could unblock them with a thumb. The leash got a lot longer.

---

## One inbox instead of a wall: the Triage view

Here's what happens when the fleet grows: the board is a gorgeous grid, but a grid is a *spatial* layout, and "what needs me right now" is a *priority* question. I found myself scanning columns like I was looking for my keys.

So I added **Triage** — a single, opinionated column. It shows only the sessions that need a human: the ones with an open permission gate, and the ones sitting in WAITING for my reply. Longest-waiting on top, because the session that's been blocked for eight minutes deserves my attention before the one that just paused. Gated rows carry their answer buttons inline, so triage isn't a list of links to go click elsewhere — it's the place you *clear the queue*.

It's deliberately boring. No spatial reasoning, no hunting. Top to bottom, answer, done.

**The benefit:** it reframed my relationship with the fleet. The board is for *browsing*; Triage is for *working*. When I sit down to "deal with my agents," I open Triage, clear it, and close it. It turned a wall of status into a to-do list.

---

## Starting work, not just reacting to it: Dispatch

Everything so far is reactive — the agents do things, I respond. But a control plane that can only respond isn't really in control. I wanted to *start* work from the dashboard.

**Dispatch** is a button and a modal: pick a project directory, type a task, choose a model, hit go. Under the hood it mirrors exactly how my normal launcher starts a session — generate a fresh UUID, create a detached tmux session named after that UUID, run `claude --session-id <uuid>` inside it, wait for the REPL to come up, then type the task in and submit. Because it preserves the sacred name-equals-id invariant, the new session immediately shows up on the board and works with every other feature — gates, Slack, kill, all of it — for free.

**The benefit:** the dashboard became the front door as well as the window. "I should have an agent look at that" went from *open terminal, cd, launch, paste, wait* to *one modal*. Reducing the activation energy of starting a session sounds trivial until you notice you're starting three times as many of them.

---

## The feature I was slightly afraid of: the Autonomy dial

Now the honest part. Approving gates by hand — in the app, in Slack — is *better*, but it's still me, in the loop, one gate at a time. And a lot of those gates are things like "may I read this file?" that I approve reflexively every single time. I was a rubber stamp with a pulse.

So I built an **autonomy dial**: a per-session trust level with three settings.

- **manual** (the default) — nothing is auto-answered. Exactly Part 1's world.
- **auto-safe** — automatically approve *read-only* gates (reads, searches, listing files) and escalate anything that writes, runs a command, or is ambiguous back to a human.
- **yolo** — approve everything.

An always-on watcher thread is the single authority that acts on these. For each open gate it looks up the session's level and decides. `yolo` clicks yes on anything. `auto-safe` scans the gate's text and options for markers: hit an *unsafe* word (`bash`, `write`, `rm`, `git`, `push`, `install`, `sudo`, …) and it refuses to act and leaves it for me; hit only a *safe* word (`read`, `view`, `search`, …) and it approves; match neither and it — importantly — **escalates.** The classifier fails toward asking, never toward acting. The worst case is that auto-safe bugs me too often, not that it does something I didn't want.

Two things I insisted on, because handing a keyboard to a robot deserves paranoia:

1. **Kill switches.** A global pause toggle right in the Triage header, and an `AUTONOMY_DISABLED=1` environment variable that hard-disables the whole thing regardless of any per-session setting. When I say stop, everything stops.
2. **A single choke point.** Exactly one thread is allowed to auto-answer. Not the Slack handler, not the UI, not three racing timers. One authority, one place, deduped by gate signature so the same prompt can't be answered twice.

The levels persist to a gitignored JSON file (absent means manual — the safe default wins even if the file vanishes), and the dial shows up as a selector on both the detail page and each Triage row, with a badge on the board so I can see at a glance which sessions are running hot.

**The benefit:** this is the feature that changes what the tool *is*. Manual is a dashboard. auto-safe is a *supervisor* — the reflexive yes-clicks disappear and I only see the decisions that actually need judgment. yolo is a leap of faith I take only for throwaway sessions. Being able to slide each session independently along that spectrum is the difference between watching a fleet and *running* one.

If I sound cautious about it, that's the point. The keyword classifier is coarse, and I know it. The safety of the design isn't in the classifier being smart — it's in the classifier being *allowed to be dumb*, because ambiguity escalates and there's a kill switch two inches from my cursor.

---

## Letting the agents talk to each other: session-to-session relay

Once I had multiple live sessions and a way to type into any of them, an obvious question fell out: could one session hand work to another?

I already had a little file-based message bus lying around — a script that writes a message into a target session's inbox and nudges its REPL with a notification line so it knows to go read it. The nice property is that it's *addressed*: session A sends to session B, and B knows the message came from A, so B can reply straight back into A's pane. It's a real conversation, not a one-way poke.

I surfaced it as a **↔ Relay** button on each Triage row. You pick a *source* session, type (or dictate — more on that in a second) a message, and send. The target gets nudged; its reply routes home.

I want to be honest about the altitude here: this is early. It's a primitive, not a workflow engine. But it's the primitive that everything interesting is built out of — the day I want "when session A finishes, hand its result to session B and unblock it," this is the piece I'll build that on.

One small UX decision I'm quietly proud of: the relay composer is a **modal**, not an inline box on the row. Triage auto-refreshes every couple of seconds, and if the compose box lived in the row, a refresh mid-sentence would wipe what I was typing. The modal floats above the churn. It's a tiny thing that would have been a daily papercut.

**The benefit:** the fleet stopped being a set of isolated conversations with me at the center of every one. Agents can now pass context sideways. It's the first crack in the door toward genuine multi-agent orchestration.

---

## Talking to my agents, literally: speech-to-text

This one started as a lark and became something I use constantly.

Typing tasks and redirections is fine, but a lot of what I say to an agent is conversational — "check the last comment Derek left and summarize it," "re-run that but skip the tests." That's *talking*, and I have a perfectly good mouth. So I wired the browser's built-in Web Speech API — zero backend, zero cost, no model to install — into every text input the app has: the send box, the redirect field, Dispatch, and Relay. A little 🎤 button; click it, talk, the text fills in.

Then came the request that made it genuinely hands-free. Dictating still meant reaching for the mouse to hit *Send*. So I added a spoken **wake word**: end your sentence with "send" (or "submit," or "send it"), and the app strips the wake word and fires the message. Now I can dictate a whole instruction and launch it without touching anything.

The engineering care here is all in the edges. The wake word only triggers on a *finalized* speech result, never on the interim guesses the recognizer streams while you're mid-word — otherwise it'd fire early. A bare "send" with no message is ignored. And it's a graceful no-op in browsers that don't support the API, so the button simply doesn't appear rather than breaking. There's a real trade-off I accepted knowingly: a message that legitimately ends with the word "send" will submit itself. For how I talk to agents, that's a fine trade.

**The benefit:** it lowered the cost of a single interaction to almost nothing. When telling an agent what to do is as cheap as saying it out loud, you delegate more, and you delegate smaller things. The interface got out of the way.

---

## The quality-of-life pass: the things that make it livable

Not every addition is a headline. A cluster of smaller changes did more for the day-to-day feel than some of the big ones.

**Markdown rendering.** The assistant's messages were being shown as raw escaped text in a `<pre>` block, so every `**bold**`, list, table, and code fence showed up as literal markup. I wrote a tiny, dependency-free Markdown-to-HTML converter — escape first (so it's XSS-safe), *then* apply a safe subset — and used it only for assistant messages. Suddenly the history read like a conversation instead of a diff.

**A history filter.** A long session logs *everything* — every tool call, every tool result, every thinking block. One of my sessions had nearly 2,000 events, and rendering all of them made the page enormous and slow. So the detail view now defaults to showing only the **user and assistant messages** — the actual conversation — with a toggle to reveal the tool/thinking noise when I'm debugging. On that 2,000-event session it renders about a third of the nodes. The page got dramatically faster and, more importantly, *readable*.

**A kill switch for the REPL.** Sometimes a session is wedged, or done, or I just want the tmux gone. There's now a guarded "⏻ Kill tmux" button on the detail page that ends the live REPL — with a confirmation, because it's irreversible — while leaving the transcript untouched. The conversation stays in history; only the running process dies.

**A "live tmux" badge on the board.** With sessions being spawned and killed, I wanted to know at a glance which ones actually have a running REPL versus which are just transcripts on disk. A small green badge, driven by a single `tmux list-sessions` call per refresh.

None of these are impressive on their own. Together they're the difference between a demo and a tool I reach for every day.

---

## The most interesting design problem this time: a live session that looks dead

Part 1 had a great puzzle — *there is no "ended" event*, so status had to be inferred from how long ago the transcript was last written. That heuristic worked beautifully for read-only monitoring.

Part 2 broke it.

Here's the bug. A session's status decays with idle time: no writes for a while and it slides from WAITING to SITTING to SLEEPING to, eventually, ENDED. That's correct for a transcript nobody's touching. But now I had sessions with a **live REPL sitting at a permission prompt, waiting for me.** The process is alive. It's *literally blocked on my input.* And because it wasn't writing to its transcript while it waited, the heuristic happily aged it into SLEEPING and then ENDED — the dashboard was declaring dead the very sessions that most needed me.

The fix is a clamp, and I like it because it expresses the actual rule in one sentence: **while a session's tmux is alive, its status can't decay past WAITING.** THINKING still shows through — if it's genuinely working, I want to see that — but idle-decay is capped. Only once the tmux is *killed* is the session allowed to age into SITTING, SLEEPING, and ENDED. Alive-but-idle means waiting on you, and waiting on you means WAITING, full stop.

I put the clamp in exactly one place — the function every endpoint already runs to decorate a session before returning it — so the board, the detail view, the status poll, search, and Triage all inherited the corrected behavior with no extra code. And because it's computed from a single per-request snapshot of live tmux sessions, the batch endpoints don't pay for it per-session.

The lesson echoes Part 1's: **when you add a capability, re-examine every assumption the old design was allowed to make.** "Status equals idle time" was true right up until "a session can be idle *and* alive at the same time." A new door changed what the old walls meant.

---

## The architecture, one level up

If Part 1's diagram was "read files, infer status, render grid," Part 2 adds a second half:

```
                    ┌─────────────────────────────┐
   READ (Part 1)    │   transcripts on disk        │
   ───────────────▶ │   ~/.claude/projects/**.jsonl │
                    └─────────────────────────────┘
                                  │  parse + status heuristic
                                  ▼
   ┌──────────────────────────────────────────────────────┐
   │                    FastAPI backend                     │
   │  board · detail · triage · search · dispatch · relay   │
   └──────────────────────────────────────────────────────┘
                    ▲                         │
   ACT (Part 2)     │ capture_pane / answer   │  spawn / say / kill
                    ▼                         ▼
   ┌─────────────────────────────┐   ┌──────────────────────┐
   │  live tmux panes             │   │  autonomy watcher    │
   │  (name == session id)        │◀──│  (single authority)  │
   └─────────────────────────────┘   └──────────────────────┘
                    ▲
                    │  buttons
   ┌────────────────┴───────────────┐
   │  Slack bot (Socket Mode)        │
   └────────────────────────────────┘
```

The philosophy from Part 1 held all the way through: **boring tech ships.** It's still Python and FastAPI. Still plain static HTML/CSS/JS with no build step — I version the assets with a `?v=N` query string and a `no-store` header, and that's my entire "deploy." Still flat, gitignored JSON files for state; there's no database because there's nothing here a database earns. The new dependencies are a single Slack SDK and, for speech, *nothing at all* — it's built into the browser.

The one architectural principle I'd add to Part 1's list, learned the hard way this time: **when you let software take actions, centralize the authority to act.** One watcher owns auto-approval. One function owns status. One invariant (name == id) ties reading and writing together. Every time I was tempted to sprinkle a capability across three call sites, the bug I'd have shipped was already visible.

---

## What I gained (again)

Part 1 taught me that observability becomes the interface when work moves to agents. Part 2 taught me the sequel to that:

**Once you can see the work, the scarce resource is no longer information — it's your judgment.** The dashboard could already tell me everything about all my sessions. What it couldn't do was let me spend my attention *efficiently*. Every Part 2 feature is a different lever on the same problem:

- Triage spends attention in priority order instead of spatial order.
- Slack lets me spend it from anywhere.
- The autonomy dial spends *zero* attention on the decisions that never needed it.
- Speech and the wake word make each unit of attention cheaper to express.
- Dispatch and Relay let a little attention start a lot of work.

The read-only dashboard made me a better *observer* of my agents. The control plane made me a *manager* of them — and the uncomfortable, interesting truth is that managing a fleet of AI coders is a real skill, with real ergonomics, that almost none of our tools are designed for yet. We're all about to need mission control, and most of us are still alt-tabbing between terminals.

There's still that pleasing ouroboros from Part 1, by the way — I built all of this *using* Claude Code, watching the very sessions doing the building show up on the board, sometimes approving their own gates through the feature they were in the middle of writing. At one point a session I'd set to auto-safe approved a file read for the code that implemented auto-safe. The tool ate its own tail and kept going.

---

## What's next

The primitives are in place; now they want to be composed.

- **Autonomy policy rules** — replace the coarse keyword classifier with per-project allow/deny rules, so "auto-approve reads under this repo, never `git push`, always ask outside the working directory" is a config, not a guess.
- **Learn-from-me approvals** — notice that I've approved the same gate three times and *offer* to automate it. Let the tool learn my judgment instead of me hand-configuring it.
- **Session choreography** — chain Dispatch and Relay into a real flow: when A finishes, hand its output to B and unblock it. The multi-agent workflow that today's relay button only hints at.
- **A context-budget meter** — show how full each session's context window is and warn before compaction quietly eats state. Purely a Claude-native concern, and increasingly the thing that bites at scale.

---

## Try it / steal the ideas

Same offer as last time. If you run Claude Code inside tmux, the whole thing rests on one convention — **name your tmux session after the Claude session id** — and everything here becomes possible. Read the screen, type on the keyboard, centralize the authority to act, and put a kill switch where your cursor already is.

Part 1 was about being able to *see* your agents. Part 2 is about being able to *run* them. If the last two years were about making AI that can code, the next few are going to be about the unglamorous, essential work of learning to supervise it — at fleet scale, without losing the thread, without alt-tabbing your life away.

I built the mission control I wished I had. Go build yours, and tell me what you added that I didn't think of.

---

*Part 1: [I Couldn't See What My AI Coding Agent Was Doing — So I Built a Dashboard For It](https://medium.com/artificial-intelligence-and-just-all-about-it/i-couldnt-see-what-my-ai-coding-agent-was-doing-so-i-built-a-dashboard-for-it-e52116b5faa8)*
