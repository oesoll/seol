#!/bin/sh
# Install seol: the session layer, peer, and on a Mac the sm and Fine apps. Safe to run again (it updates).
#   sh install.sh [--no-apps] [--no-services] [--from-source] [peer install flags, e.g. --accept-inbound --codex-alias]
# On an Apple Silicon Mac the apps and peer-hold come prebuilt from the release named in RELEASE (no Xcode, no Rust);
# --from-source builds them here instead, as it does on Linux.
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

apps=1; services=1; from_source=0
for a in "$@"; do case "$a" in --no-apps) apps=0 ;; --no-services) services=0 ;; --from-source) from_source=1 ;; esac; done
peer_flags=$(printf '%s\n' "$@" | grep -vE '^--(no-|from-source)' | tr '\n' ' ' || true)
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

# Prebuilt Mac pieces, used only while sm, fine and hold are exactly what the release was built from.
prebuilt=""
if [ $mac = 1 ] && [ "$(uname -m)" = arm64 ] && [ $from_source = 0 ] && [ -f "$here/RELEASE" ]; then
    step "prebuilt apps and peer-hold"
    tag=$(sed -n 1p "$here/RELEASE")
    want() { sed -n "s/^$1 //p" "$here/RELEASE"; }
    if [ "$(want sm)" = "$(git -C "$here/sm" rev-parse HEAD)" ] && [ "$(want fine)" = "$(git -C "$here/fine" rev-parse HEAD)" ] \
        && [ "$(want hold)" = "$(git -C "$here" rev-parse HEAD:hold)" ]; then
        dl="$smux/release/$tag"
        if [ ! -x "$dl/seol/bin/peer-hold" ]; then
            rm -rf "$dl"; mkdir -p "$dl"
            if curl -fsSL -o "$dl/seol.zip" "https://github.com/oesoll/seol/releases/download/$tag/seol-macos-arm64.zip" \
                && ditto -x -k "$dl/seol.zip" "$dl"; then rm -f "$dl/seol.zip"
            else echo "· could not download release $tag; building from source"; rm -rf "$dl"; fi
        fi
        [ -x "$dl/seol/bin/peer-hold" ] && prebuilt="$dl/seol" && echo "release $tag"
    else
        echo "· this checkout is newer than release $tag: building from source"
    fi
fi

step "peer-hold (keeps each agent's terminal alive)"
cargo=$(command -v cargo || true); [ -n "$cargo" ] || { [ -x "$HOME/.cargo/bin/cargo" ] && cargo="$HOME/.cargo/bin/cargo"; }
if [ -n "$prebuilt" ]; then
    install -m 0755 "$prebuilt/bin/peer-hold" "$smux/bin/peer-hold" && echo "installed $smux/bin/peer-hold (prebuilt)"
elif [ -n "$cargo" ]; then
    PATH="$(dirname "$cargo"):$PATH" sh "$here/hold/install.sh" "$smux/bin"
else
    echo "✗ needs Rust: install it (macOS: brew install rust; Linux: https://rustup.rs) and run this again"
fi

step "hooks for Claude Code and Codex"
# shellcheck disable=SC2086
"$smux/bin/peer" install $peer_flags
# Attention notices (permission asked, turn failed) go to the Mac that runs the apps. On another machine that is the
# app host peer reaches (SM_APP_HOST, "mac" by default); "local" there showed them nowhere.
attention=local
if [ $mac = 0 ]; then
    app=${SM_APP_HOST:-mac}
    python3 -c 'import json, sys; sys.exit(sys.argv[2] not in json.load(open(sys.argv[1])))' "$config/hosts.json" "$app" 2>/dev/null \
        && attention=$app
fi
python3 "$here/hooks/install-attention-hooks.py" --host "$attention"
for skills in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    [ -d "$(dirname "$skills")" ] || continue
    mkdir -p "$skills"
    rm -rf "$skills/peer" "$skills/seol"
    cp -R "$here/peer/skills/peer" "$here/skills/seol" "$skills/"
done

if [ $mac = 1 ] && [ $apps = 1 ]; then
    step "apps"
    to="${SEOL_APPS:-/Applications}"
    developer=$(xcode-select -p 2>/dev/null || true)
    built=0
    [ -n "$prebuilt" ] && developer=prebuilt
    case "$developer" in
        prebuilt)
            ditto "$prebuilt/SpaceManager.app" "$to/SpaceManager.app"
            ditto "$prebuilt/Fine.app" "$to/Fine.app"
            install -m 0755 "$prebuilt/bin/sm" "$HOME/.local/bin/sm"
            install -m 0755 "$prebuilt/bin/fine" "$HOME/.local/bin/fine"
            echo "SpaceManager.app and Fine.app in $to; sm and fine in ~/.local/bin"
            built=1 ;;
        ""|*CommandLineTools*)
            # The SwiftUI macros the apps use (@State) ship with Xcode, not with the Command Line Tools (oesoll/seol#1).
            echo "✗ the apps need Xcode, not only the Command Line Tools ($developer)"
            echo "  → install Xcode from the App Store, then: sudo xcode-select -s /Applications/Xcode.app && sh $here/install.sh"
            echo "  (the session layer and peer are installed; --no-apps skips this step)" ;;
        *)
            if zsh "$here/sm/scripts/package-app.sh" release > "$smux/build-sm.log" 2>&1 \
                && zsh "$here/fine/scripts/package-app.sh" release > "$smux/build-fine.log" 2>&1; then
                ditto "$here/sm/.build/SpaceManager.app" "$to/SpaceManager.app"
                install -m 0755 "$here/sm/.build/release/sm" "$HOME/.local/bin/sm"
                ditto "$here/fine/.build/Fine.app" "$to/Fine.app"
                install -m 0755 "$here/fine/.build/release/fine-cli" "$HOME/.local/bin/fine"
                echo "SpaceManager.app and Fine.app in $to; sm and fine in ~/.local/bin"
                built=1
            else
                echo "✗ an app did not build; the first errors:"
                grep -h -m 5 'error:' "$smux/build-sm.log" "$smux/build-fine.log" 2>/dev/null || tail -5 "$smux/build-sm.log"
                echo "  full logs: $smux/build-sm.log, $smux/build-fine.log"
            fi ;;
    esac
fi
# Without the app (--no-apps, or it did not build), `sm` is the session CLI itself, as on Linux.
[ $mac = 1 ] && [ ! -x "$HOME/.local/bin/sm" ] && ln -sfn "$smux/bin/sm-session" "$HOME/.local/bin/sm"

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
