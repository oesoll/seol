#!/bin/zsh
# Build the Mac pieces from the pinned sources and publish them as a GitHub release, so installing needs no
# Xcode and no Rust. Usage: scripts/release.sh vX.Y.Z   (then commit RELEASE and push)
#
# RELEASE records which sm, Fine and hold sources the archive was built from. install.sh uses the archive only
# while the checkout still matches; after any change to those, it builds from source until the next release.
set -euo pipefail
here=${0:A:h:h}
tag=${1:?usage: scripts/release.sh vX.Y.Z}
[[ $(uname -m) == arm64 ]] || { echo "build on Apple Silicon" >&2; exit 1; }
cd $here
git submodule update --init --quiet
[[ -z $(git status --porcelain -- bin hold sm fine peer | grep -v '^ ?') ]] || { echo "commit first: the release must match the pins" >&2; exit 1; }

out=$here/.build/release; rm -rf $out; mkdir -p $out/seol/bin
(cd hold && cargo build --release --quiet)
install -m 0755 hold/target/release/peer-hold $out/seol/bin/peer-hold
zsh sm/scripts/package-app.sh release > /dev/null
zsh fine/scripts/package-app.sh release > /dev/null
ditto sm/.build/SpaceManager.app $out/seol/SpaceManager.app
ditto fine/.build/Fine.app $out/seol/Fine.app
install -m 0755 sm/.build/release/sm $out/seol/bin/sm
install -m 0755 fine/.build/release/fine-cli $out/seol/bin/fine
# One signature for every binary: the macOS grants (Screen Recording, Accessibility) then survive updates.
identity="${SEOL_SIGN_IDENTITY:-$(security find-identity -v -p codesigning 2>/dev/null | awk -F'"' '/Apple Development/ { print $2; exit }')}"
for b in $out/seol/bin/*; do codesign --force -s "${identity:--}" "$b"; done
asset=$out/seol-macos-arm64.zip
ditto -c -k --keepParent $out/seol $asset

{
    echo "$tag"
    echo "sm $(git -C sm rev-parse HEAD)"
    echo "fine $(git -C fine rev-parse HEAD)"
    echo "hold $(git rev-parse HEAD:hold)"
} > RELEASE
gh release create "$tag" "$asset" --repo oesoll/seol --title "seol $tag" \
    --notes "Prebuilt for macOS on Apple Silicon: SpaceManager.app (sm), Fine.app, and the sm, fine and peer-hold binaries. install.sh downloads this; no Xcode or Rust needed."
echo "released $tag; now: git add RELEASE && git commit -m 'Release $tag' && git push"
