# Models (snapshot 2026-11-15)

Public numbers to choose workers by. They age fast: when a model is added or retired, refresh this file from the sources below. Your own runs (`team.md`, the observation log) outrank these numbers.

| Model | Harness | Intelligence (AA index, medium / high / max effort) | Terminal-Bench 4.1, max, vendor's own agent | Pool burn | Made for |
|---|---|---|---|---|---|
| Claude Fable 5.2 | claude | 56 / 62 / 65 | 65.8 (Claude Code) | fastest; on Max at most 50% of the weekly limit | hard reasoning, hours-long agent work |
| Claude Opus 5.5 | claude | 48 / 52 / 59 | 65.8 (Claude Code) | several times Sonnet | long agentic coding and knowledge work; the usual starting point |
| Claude Sonnet 5.5 | claude | 31 / 42 / 55 | 63.9 (Claude Code) | less than Opus | speed and intelligence balanced; everyday coding, tool use |
| Claude Haiku 5.5 | claude | 25 / 30 / 36 | 40.0 | very little | classification, extraction, many small subagents |
| GPT-6 Astra | codex | 52 / 55 / 57 | 60.3 (Codex) | about 3–5× Sol | the hardest end-to-end work |
| GPT-6.1 Sol | codex | 46 / 50 / 55 | 60.9 (Codex) | baseline | near-Astra results for less; long-running code and documents |
| GPT-6.1 Terra | codex | 38 / 42 / 51 | 53.3 (Codex) | about half of Sol | fast and cheap: writing, screens and images, computer use, clear-cut coding |
| GPT-6 Luna | codex | 18 / 21 / 28 | 15.2 | very little | narrow, high-volume tasks |

- **Effort moves the ranking as much as the model does.** Opus at its default medium (48) scores below Sol at high (50). Leads and judgment work run at high or above.
- **Defaults are low.** Codex starts Sol and Terra at low effort and Claude starts Opus at medium; say the effort every time.
- **Pools differ.** Claude Max has a 5-hour window, a weekly window and a separate weekly cap for Fable; the app and Claude Code share them. ChatGPT Pro's Codex currently has only a weekly window, so it absorbs a busy day better. Terra does about twice the work of Sol on the same allowance.
- **Terra is below Sol, not between Sol and Astra.** OpenAI itself says Sol and Astra remain better for the hardest coding.
- Codex in-harness context: Sol and Terra 500K by default, Astra and Luna 272K, all up to 872K. Claude models: 1M.

Sources (read 2026-11-15): [Artificial Analysis leaderboard](https://artificialanalysis.ai/leaderboards/models) and its per-model pages for effort levels · [Terminal-Bench leaderboard](https://www.tbench.ai/leaderboard) · [Anthropic models overview](https://platform.claude.com/docs/en/models/overview) · [Claude Fable on your plan](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan) · [Models, usage and limits in Claude Code](https://support.claude.com/en/articles/14552983-models-usage-and-limits-in-claude-code) · [OpenAI models](https://learn.chatgpt.com/docs/models) · [OpenAI pricing and Codex limits](https://learn.chatgpt.com/docs/pricing) · [Introducing GPT-6.1 Terra](https://openai.com/index/introducing-gpt-6-1-terra/)
