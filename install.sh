#!/bin/sh
# Install seol: the session layer, peer, and on a Mac the sm and Fine apps. Safe to run again (it updates).
#   sh install.sh [--no-apps] [--no-services] [peer install flags, e.g. --accept-inbound --codex-alias]
# Everything lands where the apps look for it: ~/.smux/bin (session layer + peer), ~/.local/bin (CLIs),
# /Applications (apps; SEOL_APPS overrides), ~/Library/LaunchAgents or ~/.config/systemd/user (restore, sleep).
set -eu
case "$0" in
    *install.sh) here=$(cd "$(dirname "$0")" && pwd) ;;
    *) here="" ;;   # piped from curl
esac
if [ ! -f "$here/bin/sm-session" ]; then
    dest="${SEOL_HOME:-$HOME/.local/share/seol}"
    if [ -d "$dest/.git" ]; then git -C "$dest" pull --ff-only --quiet
    else git clone --quiet https://github.com/oesoll/seol "$dest"; fi
    exec sh "$dest/install.sh" "$@"
fi

apps=1; services=1
for a in "$@"; do case "$a" in --no-apps) apps=0 ;; --no-services) services=0 ;; esac; done
peer_flags=$(printf '%s\n' "$@" | grep -v '^--no-' | tr '\n' ' ' || true)
mac=0; [ "$(uname -s)" = Darwin ] && mac=1
smux="$HOME/.smux"
step() { printf '\n== %s\n' "$1"; }

step "sources"
git -C "$here" submodule update --init --quiet

step "session layer and peer → $smux/bin"
# Copied into one folder, not linked: the session layer and peer import each other from the folder they run in.
mkdir -p "$smux/bin" "$HOME/.local/bin"
for f in "$here"/bin/* "$here"/peer/bin/*; do
    [ -f "$f" ] && install -m "$( [ -x "$f" ] && echo 0755 || echo 0644 )" "$f" "$smux/bin/$(basename "$f")"
done
# This machine's name in peer and sm. The sm app names the Mac it runs on "mac".
config="${PEER_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/peer}"; mkdir -p "$config"
[ -s "$config/hostname" ] || { if [ $mac = 1 ]; then echo mac; else hostname -s | tr 'A-Z' 'a-z'; fi; } > "$config/hostname"
ln -sfn "$smux/bin/peer" "$HOME/.local/bin/peer"
[ $mac = 1 ] || ln -sfn "$smux/bin/sm-session" "$HOME/.local/bin/sm"   # on Linux `sm` is the session CLI itself

step "peer-hold (keeps each agent's terminal alive)"
cargo=$(command -v cargo || true); [ -n "$cargo" ] || { [ -x "$HOME/.cargo/bin/cargo" ] && cargo="$HOME/.cargo/bin/cargo"; }
if [ -n "$cargo" ]; then
    PATH="$(dirname "$cargo"):$PATH" sh "$here/hold/install.sh" "$smux/bin"
else
    echo "✗ needs Rust: install it (macOS: brew install rust; Linux: https://rustup.rs) and run this again"
fi

step "hooks for Claude Code and Codex"
# shellcheck disable=SC2086
"$smux/bin/peer" install $peer_flags
python3 "$here/hooks/install-attention-hooks.py" --host local
for skills in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    [ -d "$(dirname "$skills")" ] || continue
    mkdir -p "$skills"
    rm -rf "$skills/peer" "$skills/seol"
    cp -R "$here/peer/skills/peer" "$here/skills/seol" "$skills/"
done

if [ $mac = 1 ] && [ $apps = 1 ]; then
    step "apps (first build downloads the terminal engine, about 35 MB)"
    to="${SEOL_APPS:-/Applications}"
    app=$(zsh "$here/sm/scripts/package-app.sh" release | tail -1)
    ditto "$app" "$to/SpaceManager.app"
    install -m 0755 "$here/sm/.build/release/sm" "$HOME/.local/bin/sm"
    app=$(zsh "$here/fine/scripts/package-app.sh" release | tail -1)
    ditto "$app" "$to/Fine.app"
    install -m 0755 "$here/fine/.build/release/fine-cli" "$HOME/.local/bin/fine"
    echo "SpaceManager.app and Fine.app in $to; sm and fine in ~/.local/bin"
fi

step "restore after reboot, sleep idle agents"
if [ $services = 0 ]; then
    echo "skipped (--no-services)"
elif [ $mac = 1 ]; then
    agents="$HOME/Library/LaunchAgents"; mkdir -p "$agents"
    for p in dev.sm.restore dev.sm.sleep; do
        cp "$here/service/$p.plist" "$agents/$p.plist"
        launchctl bootout "gui/$(id -u)/$p" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$agents/$p.plist" 2>/dev/null || echo "· launchctl could not load $p (load it after login)"
    done
elif command -v systemctl >/dev/null 2>&1; then
    units="$HOME/.config/systemd/user"; mkdir -p "$units"
    cp "$here"/service/sm-restore.service "$here"/service/sm-sleep.service "$here"/service/sm-sleep.timer "$units/"
    systemctl --user daemon-reload && systemctl --user enable --now sm-restore.service sm-sleep.timer >/dev/null 2>&1 \
        || echo "· systemd --user is not running here; enable sm-restore.service and sm-sleep.timer later"
fi

step "check"
"$smux/bin/peer" doctor || true
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to PATH so you and your agents can run sm, peer and fine." ;; esac
