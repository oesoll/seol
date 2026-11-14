---
name: seol
description: Use when you start, bring back, stop, find or arrange coding agents on this machine or another one — sm sessions (spawn, resume, stop, sleep, where, tail, keys, run, cp) and the layout the sm app shows (workspaces, tabs, panes). Talking to an agent is the peer skill.
---

# seol: where agents live

An agent is an **sm session**: a name, one Claude Code or Codex conversation behind it, and one small `peer-hold` process that keeps its terminal alive. The apps (sm, Fine) only show sessions. Quitting an app or closing a pane does not end the agent, and after a reboot it comes back by name. `peer` reaches it by that name.

## Sessions

```bash
sm spawn NAME --kind claude|codex [--cwd DIR] [--host HOST] [-- engine args or "first prompt"]
sm resume NAME[@HOST] [-- ARGS]      # same conversation; ARGS such as -m MODEL apply to it
sm stop NAME[@HOST]                  # ends it on purpose; it stays ended until resumed
sm ls [--ws WS] [--background]       sm where [NAME]      sm tail NAME [--lines N]
sm keys NAME KEY…                    # type into a screen peer cannot answer yet; read it first
sm run HOST -- CMD                   sm cp [HOST:]SRC [HOST:]DST
```

- Start agents with `sm spawn`, not a bare `claude` or `codex` in some terminal: only an sm session survives restarts, sleeps and wakes, and is reachable by name.
- The name is the session. Spawning a running name returns it; spawning an ended name resumes its conversation. `--fresh` throws the conversation away, so use `sm resume NAME -- -m MODEL` to change an option instead.
- Spawned agents start without permission prompts: nobody sits at their screen, so a prompt would stop them for good. `--ask` keeps the prompts.
- Idle past the prompt cache (65 min), a session sleeps to free memory. A message, opening its pane or `sm wake NAME` brings it back in place. `sm awake NAME` exempts one.
- When claude or codex is updated, sm restarts idle sessions into the new version with the same conversation.
- A choice shown on an agent's screen that belongs to the person stays theirs. Never type Enter into a screen you have not read.

## Layout (what the sm app shows)

Workspaces → tabs → panes, kept as data on the machine where the workspace runs. Every change prints the new shape, `*` marking what the person sees:

```
billing-api@mac  main*: [lead* | review] · workers: [(w1 | w2) / (w3 | w4)]
```

```bash
sm ws new NAME [--cwd DIR]           sm split PANE --right|--down [--spawn NAME --kind K | --attach NAME]
sm tab new --name T [--spawn NAME --kind K]       sm arrange [TAB] columns|rows|grid|main-left
sm move PANE --tab T                 sm close PANE (the agent keeps running)      sm show NAME
```

Layout commands never move the person's view or keyboard: they may be typing in that workspace. Only `--focus`, `sm focus`, `sm tab select` and `sm show NAME` do, so use them when the person asked to see something.

If sm or peer got in your way, `sm feedback "one line"` records it with the commands you ran just before.
