#!/bin/sh
# Run the session layer's tests against the layout install.sh makes: bin/, peer/bin/ and peer-hold in one folder
# (they import each other from the folder they run in). peer-hold: $PEER_HOLD, else hold/target/release, else built.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
hold=${PEER_HOLD:-$here/hold/target/release/peer-hold}
[ -x "$hold" ] || (cd "$here/hold" && cargo build --release --quiet)
bin=$(mktemp -d "${TMPDIR:-/tmp}/seol-test.XXXXXX")
trap 'rm -rf "$bin"' EXIT
cp "$here"/bin/* "$here"/peer/bin/* "$bin/" && cp "$hold" "$bin/peer-hold"
status=0
for t in "$here"/tests/test_*.py; do
    echo "== $(basename "$t")"
    SEOL_BIN=$bin python3 "$t" || status=1
done
exit $status
