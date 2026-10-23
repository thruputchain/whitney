#!/usr/bin/env bash
# Check a Whitney archive made by build/build.sh.
#
# usage: build/test-archive.sh <whitney-KEY.tar.gz>
#
# Fails unless:
#   - the archive has one top dir with every binary, executable;
#   - bin/contracts has simple_wallet, itc_erc20 and genesis_root .rv32a;
#   - manifest.json parses and has every manifest key, and hogd_git_hash is
#     a clean commit (no +dirty);
#   - <tarball>.sha256, if present, matches;
#   - in ubuntu:26.04 with only RUNTIME_PACKAGES (libjemalloc2 libatomic1
#     libsnappy1v5) installed, ldd bin/* has no "not found";
#   - znver4 archives only: bin/whitney-runner with no args prints "Usage:"
#     and exits non-zero. A znver5 archive is not executed here, because
#     the builder is Zen 4; it first runs on the Zen 5 EC2 hosts.
set -euo pipefail

BINARIES=(whitney-runner control_server control_cli hog-exec hog-db hog-event hog-indexer)
CONTRACTS=(simple_wallet itc_erc20 genesis_root)
MANIFEST_KEYS=(hogd_ref hogd_ref_is_tag hogd_commit whitney_src_hash hogd_git_hash
  whitney_commit build_flags toolchain_image_digest toolchain built_at arch)
TOOLCHAIN_KEYS=(gcc clang rustc cmake)
TEST_IMAGE=ubuntu:26.04
# Runtime packages a host installs on top of the base image.
RUNTIME_PACKAGES="libjemalloc2 libatomic1 libsnappy1v5"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[[ $# -eq 1 ]] || {
  echo "usage: $0 <whitney-KEY.tar.gz>" >&2
  exit 2
}
tarball="$1"
[[ -f "$tarball" ]] || fail "no such archive: $tarball"

if [[ -f "$tarball.sha256" ]]; then
  (cd "$(dirname "$tarball")" && sha256sum --quiet -c "$(basename "$tarball").sha256") ||
    fail "sha256 mismatch for $tarball"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar -C "$work" -xzf "$tarball" || fail "cannot extract $tarball"

shopt -s nullglob
tops=("$work"/*)
shopt -u nullglob
[[ ${#tops[@]} -eq 1 && -d "${tops[0]}" ]] || fail "archive must hold exactly one top dir"
root="${tops[0]}"

for b in "${BINARIES[@]}"; do
  [[ -f "$root/bin/$b" && -x "$root/bin/$b" ]] || fail "missing or not executable: bin/$b"
done
for c in "${CONTRACTS[@]}"; do
  [[ -f "$root/bin/contracts/$c.rv32a" ]] || fail "missing contract: bin/contracts/$c.rv32a"
done

manifest="$root/manifest.json"
jq -e 'type == "object"' "$manifest" >/dev/null 2>&1 || fail "manifest.json missing or not a JSON object"
for k in "${MANIFEST_KEYS[@]}"; do
  jq -e --arg k "$k" 'has($k)' "$manifest" >/dev/null || fail "manifest.json has no key: $k"
done
for k in "${TOOLCHAIN_KEYS[@]}"; do
  jq -e --arg k "$k" '.toolchain | has($k)' "$manifest" >/dev/null || fail "manifest.json toolchain has no key: $k"
done
jq -e '.hogd_ref_is_tag | type == "boolean"' "$manifest" >/dev/null || fail "hogd_ref_is_tag is not a bool"
jq -e '.build_flags | type == "array"' "$manifest" >/dev/null || fail "build_flags is not a list"
git_hash="$(jq -r .hogd_git_hash "$manifest")"
[[ "$git_hash" =~ ^[0-9a-f]{40}$ ]] || fail "hogd_git_hash is not a clean commit: $git_hash"
arch="$(jq -r .arch "$manifest")"
case "$arch" in
znver4) run_exec=1 ;;
znver5) run_exec=0 ;;
*) fail "unknown arch in manifest: $arch" ;;
esac

# Inside the target OS image: dynamic deps resolve, and (znver4) the runner runs.
# shellcheck disable=SC2016 # expanded inside the container
in_image='
set -eu
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends $RUNTIME_PACKAGES >/dev/null
status=0
for f in /w/bin/*; do
  [ -f "$f" ] || continue
  out="$(ldd "$f" 2>&1)" || { echo "ldd failed: $f"; echo "$out"; status=1; continue; }
  if echo "$out" | grep -q "not found"; then
    echo "unresolved libs in $f:"
    echo "$out" | grep "not found"
    status=1
  fi
done
[ "$status" -eq 0 ] || exit 10
echo "ldd: ok"
if [ "$RUN_EXEC" = 1 ]; then
  rc=0
  out="$(/w/bin/whitney-runner 2>&1)" || rc=$?
  [ "$rc" -ne 0 ] || { echo "whitney-runner exited 0 with no args"; exit 11; }
  echo "$out" | grep "Usage:" || { echo "no Usage: line"; echo "$out" | tail -5; exit 12; }
  echo "whitney-runner usage: ok (rc=$rc)"
else
  echo "skip: run check ($ARCH archive needs a Zen 5 host)"
fi
'
docker run --rm -e DEBIAN_FRONTEND=noninteractive -e RUNTIME_PACKAGES="$RUNTIME_PACKAGES" \
  -e RUN_EXEC="$run_exec" -e ARCH="$arch" -v "$root:/w:ro" "$TEST_IMAGE" sh -c "$in_image" ||
  fail "checks inside $TEST_IMAGE failed"

echo "PASS: $tarball ($arch)"
