# Setting up seol

This page is for the coding agent (Claude Code, Codex, or another) that a person has asked to set up seol on their machine. When you finish, their agents run as named sessions that survive restarts, talk to each other by name, and (on a Mac) show up in the sm and Fine apps.

seol has three parts, each its own repository linked here as a submodule: **peer** (agents message each other by name), **sm** (the Mac app that lays sessions out as workspaces, tabs and panes), **Fine** (a Mac window of conversations). The session layer in this repository (`bin/`, `hold/`) is what keeps agents alive under all three.

## 1. Check what the machine has

- macOS 14+ on Apple Silicon for the apps, or Linux for the session layer and peer alone.
- `git`, `python3` 3.9+, and Rust (`cargo`) to build `peer-hold`.
- On a Mac, the apps need **Xcode** (the App Store app, selected with `sudo xcode-select -s /Applications/Xcode.app`). The Command Line Tools alone cannot build them: the SwiftUI macros they use ship only with Xcode. Without Xcode, install with `--no-apps`; the session layer and peer work the same and `sm` is the session CLI.
- Claude Code and/or Codex installed and logged in. seol starts them as they are.

Install anything missing that the person's package manager can install without questions (for example `brew install rust`). Ask before anything that needs their password.

## 2. Ask the choices that are theirs

Ask these in one short message and wait. Recommend yes to both.

- **Idle Claude sessions take messages at once** (`--accept-inbound`): sets `"crossSessionInbound": "accept"` in `~/.claude/settings.json`.
- **Plain `codex` starts reachable** (`--codex-alias`): adds `alias codex='peer run codex'` to their shell rc.

## 3. Install

```sh
git clone --recursive https://github.com/oesoll/seol ~/.local/share/seol
sh ~/.local/share/seol/install.sh --accept-inbound --codex-alias   # only the flags they agreed to
```

On a Mac this builds the two apps (a few minutes, the first build downloads the terminal engine). `--no-apps` skips them. Read every `✗` line in the output and fix it yourself; `·` lines are optional.

If Codex is installed, tell them to open `/hooks` once in a Codex session and trust the new hooks. Only the person can do that.

## 4. Show it working

```sh
sm spawn hello --kind claude --cwd ~
peer                    # hello is listed
peer say hello "say hi back to me with peer say"
```

On a Mac, open SpaceManager and Fine once so macOS asks for their permissions; `sm show hello` puts the session in front of them. Then `sm stop hello`.

Done means: `peer doctor` shows no `✗`, the person saw a message arrive, and they know `sm` and `peer` exist (`sm --help`, `peer -h`).

## Updating

```sh
git -C ~/.local/share/seol pull --recurse-submodules && sh ~/.local/share/seol/install.sh
```

It relinks and rebuilds; running agents are restarted into the new version by sm once they are idle.
