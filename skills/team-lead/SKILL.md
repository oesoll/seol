---
name: team-lead
description: Use only when the person asks you to orchestrate — lead a team of agents (your harness's subagents and seol sessions on any harness) and decide who does each piece of work, on which model, paid from which subscription. Not for ordinary peer use.
---

# Team lead — who does the work

You lead. Workers do the hands-on work; judgment stays with you: the direction, how the work is split, and whether a result is good.

This is an opt-in add-on to seol. When someone just wants to talk to one other agent, use peer as usual and leave all of this out. How to start and message agents is in the `seol` and `peer` skills; this skill is only about who gets the work.

## This setup

What differs from person to person lives in `~/.config/seol/team.md`: their preferences, which subscription accounts they have on which machine and how to see what is left, how their lead is configured, notes from past runs. Read it first and let it override the defaults below.

If it does not exist, ask the person a few short questions before building the team (which subscriptions, how they want them spent, engines they prefer or avoid), write the answers there in their language, and go on. Leave out what you can see for yourself: machines (`peer --all`, `~/.config/peer/hosts.json`), harnesses (`sm spawn --help`), remaining quota a harness already shows.

A worker is three choices: a **harness** (claude, codex, … — whatever `sm spawn --kind` offers), a **model and effort**, and a **pool** (the subscription account that pays for it).

## 1. Your own subagent, or a seol session

Your harness's subagents are the default when they can do it: almost free to start, the result comes back as one summary, gone when done. A seol session starts a whole harness, reading its system prompt and tools again each time.

Use a seol session (`sm spawn`) when any of these holds:
- **The model is outside your harness** — another company's model cannot be your subagent. Using another pool and cross-engine review both go through here.
- **It must outlive you.** A subagent dies with your session; a seol session comes back by name.
- **The person may want to watch or step in.** A subagent can only be waited on or cancelled.
- **Workers need to talk to each other directly**, or the work must run on **another machine or account**.

Otherwise, a subagent: finding, reading, a one-off review, a summary.

Always say the model and effort; harness defaults are often lower than the job needs. `--worker` starts a worker without its harness's lead-only features (Claude: the advisor), so the decisions stay with you.

```sh
sm spawn w-impl   --kind codex  --worker -- -m <model> -c model_reasoning_effort=high
sm spawn w-review --kind claude --worker -- --model <model> --effort high
sm spawn sub-lead --kind claude -- --model <model> --effort xhigh     # another lead: no --worker
```

For a harness you have not used yet, find its model and effort options in its `--help`, spawn, check the header with `sm tail NAME`, and add the working line to `team.md`.

## 2. Which pool pays

Subscription windows reset and what is left unused is lost. So efficiency comes from sending work where allowance remains, not from downgrading models.

Pools belong to the logged-in account, and different machines may be logged into different accounts. Before spawning, look at every machine's pool (`team.md` says how for this setup); when one runs out, run the work on another machine (`--host`).

Your own context spends your pool too. Hand the context-heavy work — big reads, browsing, repeated runs — to workers on a pool with room, and keep your context for judgment. Tell workers in the brief not to fan out subagents of their own.

## 3. Which model

`models.md` has the public benchmarks and what each model is for; open it when choosing. The unit is a (model, effort) pair: effort moves a model about as much as switching models does. Choose the engine by the nature of the work first, then the model by how costly a mistake would be and whether it can be checked: work a test can verify can go to a mid-tier model; direction, design and anything that goes out under the person's name gets the top tier.

## 4. Review

- **Another company's model reviews.** A worker's own tests only cover the cases it thought of, and models from one company tend to miss the same things. Harness does not matter here: the same company's model in another harness is the same engine.
- **Use the surface.** Open the app, click through, look at the output. A worker's report, a process exit or "tests pass" is not evidence. Keep it cheap: check what changed, hand broad checks to a worker, look at the few that matter.
- **Leave results where the person can open them**, since that is how they will judge.
- **An advisor is not a review.** It reads your whole conversation, so it leans your way.

## 5. Who talks to whom

- Inside a team, workers hand off to each other with `peer say`; if every message goes through you, your context fills first.
- Across teams, only leads talk.
- A shared owner (deployment, say) gets one request from the lead with the results gathered, not a report from each worker.
- Keep the plan and progress in a file, not only in the conversation, so you can pick it up after compaction.

## 6. Record what happened

The strengths and weaknesses in `models.md` are only as good as real runs. After you review a worker's result, add one line to the log `team.md` names (default `~/.local/state/seol/observations.jsonl`, one per machine):

```sh
mkdir -p ~/.local/state/seol && echo '{"at":"YYYY-MM-DD","harness":"codex","model":"<model>","effort":"high","via":"peer","task":"implement|review|research|writing|design","verdict":"pass|fixed|redo","note":"what went well or wrong, one phrase"}' >> ~/.local/state/seol/observations.jsonl
```

When the observations start to say something, move it into `team.md`.
