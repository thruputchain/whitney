#!/usr/bin/env bash
# Package a hogd build tree into the Whitney archive. build/build.sh calls it.
#
# usage: build/package.sh --build-dir <hogd build dir> --meta <json> --key <key> --out <dir>
#
# <meta> holds every manifest key except hogd_git_hash (read from the build's
# version.c, the value control_server checks) and built_at (now, UTC).
#
# Writes <out>/whitney-<key>.tar.gz with one top dir, whitney-<key>/:
#   bin/whitney-runner bin/control_server bin/control_cli
#   bin/hog-exec bin/hog-db bin/hog-event bin/hog-indexer
#   bin/contracts/*.rv32a        (every rv32a from <build dir>/built_contracts)
#   manifest.json
# and <out>/whitney-<key>.tar.gz.sha256 (sha256sum -c format).
set -euo pipefail

# archive name : path in the hogd build dir
BINARIES=(
  whitney-runner:hogd/hog_exec/bench/extra/runner/whitney-runner
  control_server:hogd/hog_exec/bench/control_server
  control_cli:hogd/hog_exec/bench/control_cli
  hog-exec:hogd/hog_exec/hog-exec
  hog-db:hogd/hog_db/hog-db
  hog-event:hogd/hog_event/hog-event
  hog-indexer:hogd/hog_indexer/hog-indexer
)

die() {
  echo "package.sh: $*" >&2
  exit 1
}

build_dir='' meta='' key='' out=''
while [[ $# -gt 0 ]]; do
  case "$1" in
  --build-dir) build_dir="${2:?}"; shift 2 ;;
  --meta) meta="${2:?}"; shift 2 ;;
  --key) key="${2:?}"; shift 2 ;;
  --out) out="${2:?}"; shift 2 ;;
  *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$build_dir" && -n "$meta" && -n "$key" && -n "$out" ]] ||
  die "usage: $0 --build-dir <dir> --meta <json> --key <key> --out <dir>"

name="whitney-$key"
stage="$(mktemp -d "$out/.stage.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
root="$stage/$name"
mkdir -p "$root/bin/contracts"

for entry in "${BINARIES[@]}"; do
  bin="${entry%%:*}"
  path="$build_dir/${entry#*:}"
  [[ -f "$path" && -x "$path" ]] || die "missing binary: $path"
  cp "$path" "$root/bin/$bin"
done

shopt -s nullglob
contracts=("$build_dir"/built_contracts/*.rv32a)
shopt -u nullglob
[[ ${#contracts[@]} -gt 0 ]] || die "no contracts in $build_dir/built_contracts"
cp "${contracts[@]}" "$root/bin/contracts/"

git_hash="$(sed -n 's/^const char hogd_commit\[\] = "\(.*\)";$/\1/p' "$build_dir/hogd/version.c")"
[[ -n "$git_hash" ]] || die "no hogd_commit in $build_dir/hogd/version.c"
jq --arg h "$git_hash" --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '. + {hogd_git_hash: $h, built_at: $t}' "$meta" >"$root/manifest.json"

tar -C "$stage" --owner=0 --group=0 --numeric-owner -czf "$stage/$name.tar.gz" "$name"
(cd "$stage" && sha256sum "$name.tar.gz" >"$name.tar.gz.sha256")
mv "$stage/$name.tar.gz" "$out/$name.tar.gz"
mv "$stage/$name.tar.gz.sha256" "$out/$name.tar.gz.sha256"
echo "$out/$name.tar.gz"
