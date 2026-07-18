I could finally SEE my AI agents. So naturally, I wanted to DRIVE them.

A few weeks ago I built a read-only dashboard that watches my fleet of Claude Code agents by reading their transcript files from disk. Beautiful window. No door. Once I could see them sitting at permission prompts, waiting on me, I couldn't stand having no way to act.

So Part 2 turns that monitor into a control plane — drive a whole fleet of AI coding agents without touching a terminal.

The thesis: once you can see your agents, your own attention becomes the bottleneck. Judgment, not information, is the scarce resource. Every feature in this round is about spending human attention more cheaply.

What I shipped:

🔁 tmux as the API — one invariant ties it together: the tmux session name equals the Claude session ID. Every ID is both a file path (for reading) and a live terminal target (for writing). Capture the screen, parse the permission prompt, send the keystroke answer.

📨 Slack approval gates — permission prompts post to Slack with tap-to-approve buttons. Socket Mode, no public URL, no infra. Approve from your phone while the agent keeps working.

🚦 Triage view — one priority-sorted column, only sessions that need a human, longest-waiting first. Stop scanning the fleet; just clear the queue.

🚀 Dispatch — spawn new agent sessions from a modal: pick project, type the task, choose the model.

🎛️ Autonomy dial — per-session trust levels: manual, auto-safe (auto-approve read-only, escalate writes), or yolo. The keyword classifier fails toward asking, never toward acting.

🤝 Session-to-session relay — a file-based message bus so one agent can hand context to another. Early primitive toward multi-agent orchestration.

🗣️ Voice — browser speech-to-text with a wake word ("send"/"submit") for hands-free driving.

Boring tech, on purpose: Python, FastAPI, plain HTML/CSS/JS, no build step, no database. Centralize authority — one thread owns auto-approval, one function owns status — and safety through paranoia: kill switches, confirmations, fail-toward-asking.

My favorite design problem: the read-only status heuristic (idle time → status decay) broke the moment live sessions were real. A session blocked at a permission prompt is alive but aging toward "ENDED." Fix: while a session's tmux is alive, its status can't decay past WAITING. A new door changed what the old walls meant.

And yes — the ouroboros is real. I built agent-management tools using the agents being managed. The fleet supervised its own supervisor.

Full write-up (Part 2 of the series): https://medium.com/artificial-intelligence-and-just-all-about-it/i-could-see-my-ai-agents-so-now-i-wanted-to-drive-them-154cffd9966f

Code: github.com/fernandokarnagi/claude.sessions

#AI #ClaudeCode #SoftwareEngineering #AIAgents #DeveloperTools #Automation #LLMOps #AgenticAI #Python #FastAPI