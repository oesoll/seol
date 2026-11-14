#!/bin/sh
# Build peer-hold for this machine and put it next to peer (bin/peer-hold, not in git: per-arch binary).
set -e
here=$(cd "$(dirname "$0")" && pwd)
cd "$here" && cargo build --release --quiet
dest=${1:-"$here/../bin"}
install -m 0755 "$here/target/release/peer-hold" "$dest/peer-hold.new" && mv -f "$dest/peer-hold.new" "$dest/peer-hold"
echo "installed $dest/peer-hold"
