#!/usr/bin/env bash
# Build hogd at a ref with whitney-runner, then package the archive.
#
# usage:
#   build/build.sh --hogd-ref <tag|commit> --out <dir> [--hogd-src <path>] [--arch znver5|znver4] [--test]
#   build/build.sh --hogd-ref <tag|commit> --print-key [--arch znver5|znver4]
#
# Output: <out>/whitney-<key>.tar.gz and <out>/whitney-<key>.tar.gz.sha256.
# stdout carries only the archive path; logs go to stderr. <out> must not be
# under runner/ or build/ of this repo (their files are in the key).
#
# --test also builds whitney_runner_test and runs it in the toolchain
# container. The build fails if a test fails or no test ran. Run --test only
# on a CPU that supports --arch (a Zen 4 host cannot run znver5 code).
#
# Key: <hogd_ref>-<whitney_src_hash[:12]>-<arch>, for example
#   v0.1.32-0123456789ab-znver5
# --print-key prints the key and exits. It needs only git and this repo.
#
# whitney_src_hash: SHA-256 of a byte stream with one record per file in
#   `git ls-files --cached --others --exclude-standard runner build`
# (sorted, C locale), "<path>\0<sha256 of file content>\n" ("deleted" in
# place of the content hash for a tracked file that is gone), then one
# record per hogd build flag, "flag\0<flag>\n". A change to any runner or
# build file, or to --arch, changes the key.
#
# Without --hogd-src, hogd is cloned to <out>/src/hogd-<ref> from
# $WHITNEY_HOGD_REPO (default git@github.com:thruputchain/hogd.git). Set
# GITHUB_TOKEN to clone over HTTPS with a token; otherwise SSH is used.
#
# The build runs in the hogd toolchain image (.devcontainer/Dockerfile of
# that checkout), tagged whitney-toolchain:<hogd_commit>. These env vars are
# passed to it for scripts/fetch.sh, when set:
#   CARGO_REGISTRIES_GROUNDHOG_TOKEN, CARGO_REGISTRIES_GROUNDHOG_LEGACY_TOKEN
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TARGETS=(control_server control_cli hog-exec hog-db hog-event hog-indexer whitney-runner)
DEFAULT_HOGD_REPO=git@github.com:thruputchain/hogd.git

usage() {
  sed -n '4,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}
die() {
  echo "build.sh: $*" >&2
  exit 1
}
log() { echo "== $*" >&2; }

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  else
    shasum -a 256 | cut -d' ' -f1
  fi
}

hogd_ref=
out=
hogd_src=
arch=znver5
print_key=0
run_test=0
while [[ $# -gt 0 ]]; do
  case "$1" in
  --hogd-ref) hogd_ref="${2:?}"; shift 2 ;;
  --out) out="${2:?}"; shift 2 ;;
  --hogd-src) hogd_src="${2:?}"; shift 2 ;;
  --arch) arch="${2:?}"; shift 2 ;;
  --print-key) print_key=1; shift ;;
  --test) run_test=1; shift ;;
  -h | --help) usage ;;
  *) echo "build.sh: unknown argument: $1" >&2; usage ;;
  esac
done
[[ -n "$hogd_ref" ]] || usage
[[ "$hogd_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
  die "--hogd-ref must be a tag or commit ([A-Za-z0-9._-]): $hogd_ref"
case "$arch" in
znver5 | znver4) ;;
*) die "--arch must be znver5 or znver4: $arch" ;;
esac

BUILD_FLAGS=(-t Release -c gcc
  -x -DBUILD_BENCHMARKS=ON
  -x "-DBUILD_ARCHITECTURE=$arch" -x "-DRUST_BUILD_CPU=$arch"
  -x -DHOGD_EXTRA_BENCH_DIRS=/whitney/runner)

whitney_src_hash() {
  local path
  {
    while IFS= read -r -d '' path; do
      if [[ -f "$REPO/$path" ]]; then
        printf '%s\0%s\n' "$path" "$(sha256 <"$REPO/$path")"
      else
        printf '%s\0deleted\n' "$path"
      fi
    done < <(git -C "$REPO" ls-files -z --cached --others --exclude-standard -- runner build |
      LC_ALL=C sort -z -u)
    for path in "${BUILD_FLAGS[@]}"; do
      printf 'flag\0%s\n' "$path"
    done
  } | sha256
}

src_hash="$(whitney_src_hash)"
key="$hogd_ref-${src_hash:0:12}-$arch"
if [[ "$print_key" == 1 ]]; then
  echo "$key"
  exit 0
fi

[[ -n "$out" ]] || usage

# abs_path prints the absolute, symlink-free form of $1. Path parts that do
# not exist yet are kept as given.
abs_path() {
  local p="$1" rest=""
  [[ "$p" == /* ]] || p="$PWD/$p"
  while [[ ! -d "$p" ]]; do
    rest="/$(basename "$p")$rest"
    p="$(dirname "$p")"
  done
  echo "$(cd "$p" && pwd -P)$rest"
}
# The key hashes untracked files under runner/ and build/, so build output
# there would change the key of the next build.
check_out() {
  case "$1/" in
  "$REPO/runner/"* | "$REPO/build/"*)
    die "--out must not be under $REPO/runner or $REPO/build: $1" ;;
  esac
}
check_out "$(abs_path "$out")"
mkdir -p "$out"
out="$(cd "$out" && pwd -P)"
check_out "$out"

# 1. hogd source at the ref.
if [[ -n "$hogd_src" ]]; then
  [[ -d "$hogd_src" ]] || die "no such dir: $hogd_src"
  src="$(cd "$hogd_src" && pwd -P)"
else
  src="$out/src/hogd-$hogd_ref"
  if [[ ! -d "$src/.git" ]]; then
    log "clone hogd@$hogd_ref into $src"
    gitcfg="$(mktemp)"
    trap 'rm -f "$gitcfg"' EXIT
    chmod 600 "$gitcfg"
    for c in "${XDG_CONFIG_HOME:-$HOME/.config}/git/config" "$HOME/.gitconfig"; do
      if [[ -f "$c" ]]; then
        git config --file "$gitcfg" --add include.path "$c"
      fi
    done
    export GIT_CONFIG_GLOBAL="$gitcfg"
    hogd_repo="${WHITNEY_HOGD_REPO:-$DEFAULT_HOGD_REPO}"
    if [[ -n "${GITHUB_TOKEN:-}" && "$hogd_repo" =~ ^git@github\.com:(.+)$ ]]; then
      git config --file "$gitcfg" --add \
        "url.https://x-access-token:${GITHUB_TOKEN}@github.com/${BASH_REMATCH[1]}.insteadOf" "$hogd_repo"
    fi
    rm -rf "$src.tmp"
    git clone --quiet --no-checkout --filter=blob:none "$hogd_repo" "$src.tmp"
    git -C "$src.tmp" -c advice.detachedHead=false checkout --quiet "$hogd_ref"
    # Private submodules: the checkout's own helper maps them to the token
    # (or to SSH without one). Never trace this: it writes the token.
    if [[ -f "$src.tmp/.github/actions/private-sources/git-config.sh" ]]; then
      bash "$src.tmp/.github/actions/private-sources/git-config.sh" "${GITHUB_TOKEN:-}" "$gitcfg" >&2
    fi
    if ! grep -q HOGD_EXTRA_BENCH_DIRS "$src.tmp/hogd/hog_exec/bench/CMakeLists.txt"; then
      die "hogd@$hogd_ref has no HOGD_EXTRA_BENCH_DIRS hook (left in $src.tmp)"
    fi
    git -C "$src.tmp" submodule update --quiet --init --recursive
    mv "$src.tmp" "$src"
    rm -f "$gitcfg"
    unset GIT_CONFIG_GLOBAL
  fi
fi

git -C "$src" rev-parse --git-dir >/dev/null 2>&1 || die "$src is not a git checkout"
hogd_commit="$(git -C "$src" rev-parse HEAD)"
ref_commit="$(git -C "$src" rev-parse --verify --quiet "$hogd_ref^{commit}")" ||
  die "$src does not know ref $hogd_ref"
[[ "$ref_commit" == "$hogd_commit" ]] ||
  die "$src HEAD is $hogd_commit, but $hogd_ref is $ref_commit"
if git -C "$src" show-ref --verify --quiet "refs/tags/$hogd_ref"; then
  ref_is_tag=true
else
  ref_is_tag=false
fi
grep -q HOGD_EXTRA_BENCH_DIRS "$src/hogd/hog_exec/bench/CMakeLists.txt" ||
  die "hogd@$hogd_ref has no HOGD_EXTRA_BENCH_DIRS hook"
[[ -z "$(git -C "$src" status --porcelain)" ]] ||
  die "hogd checkout $src is not clean before the build"

whitney_commit="$(git -C "$REPO" rev-parse HEAD)"
if [[ -n "$(git -C "$REPO" status --porcelain -- runner build)" ]]; then
  whitney_commit="$whitney_commit+dirty"
fi

# 2. Toolchain image.
image="whitney-toolchain:$hogd_commit"
log "build toolchain image $image"
# No provenance attestation: it carries a timestamp, so the image digest
# would change on every (fully cached) rebuild.
docker build --provenance=false -t "$image" -f "$src/.devcontainer/Dockerfile" "$src/.devcontainer" >&2
image_digest="$(docker image inspect --format '{{.Id}}' "$image")"

# 3. fetch + build inside the image. The build dir is gitignored (build/**).
build_rel="build/whitney-$arch"
cache="$out/toolcache"
mkdir -p "$cache/ccache" "$cache/cargo-registry" "$cache/cargo-git"
targets=("${TARGETS[@]}")
if [[ "$run_test" == 1 ]]; then
  targets+=(whitney_runner_test)
fi
log "build hogd@$hogd_ref ($hogd_commit) + whitney-runner, $arch"
# shellcheck disable=SC2016 # expanded inside the container
inner='
set -euo pipefail
./scripts/fetch.sh
./scripts/build.sh -d "$BUILD_REL" "$@"
cxx="$(sed -n "s/^CMAKE_CXX_COMPILER:[A-Z]*=//p" "$BUILD_REL/CMakeCache.txt")"
jq -n \
  --arg gcc "$("$cxx" -dumpfullversion)" \
  --arg clang "$(clang -dumpversion)" \
  --arg rustc "$(cd hogd/rust && rustc --version | cut -d" " -f2)" \
  --arg cmake "$(cmake --version | sed -n "1s/.* //p")" \
  "{gcc: \$gcc, clang: \$clang, rustc: \$rustc, cmake: \$cmake}" >"$BUILD_REL/whitney-toolchain.json"
if [[ "$RUN_TEST" == 1 ]]; then
  echo "== run whitney_runner_test"
  "$BUILD_REL/whitney/whitney_runner_test" --gtest_brief=1 |
    tee /tmp/whitney_runner_test.log
  ran="$(grep -oE "^\[==========\] [0-9]+ tests? from" /tmp/whitney_runner_test.log |
    grep -oE "[0-9]+" | head -n1 || true)"
  if [[ "${ran:-0}" -eq 0 ]]; then
    echo "build.sh: whitney_runner_test ran zero tests" >&2
    exit 1
  fi
fi
'
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$src:/hogd" -v "$REPO:/whitney:ro" \
  -v "$cache/ccache:/ccache" \
  -v "$cache/cargo-registry:/usr/local/cargo/registry" \
  -v "$cache/cargo-git:/usr/local/cargo/git" \
  -e HOME=/tmp -e CCACHE_DIR=/ccache -e CARGO_NET_GIT_FETCH_WITH_CLI=true \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  -e BUILD_REL="$build_rel" -e RUN_TEST="$run_test" \
  -e CARGO_REGISTRIES_GROUNDHOG_TOKEN -e CARGO_REGISTRIES_GROUNDHOG_LEGACY_TOKEN \
  -w /hogd "$image" \
  bash -c "$inner" inner "${BUILD_FLAGS[@]}" "${targets[@]}" >&2

dirty="$(git -C "$src" status --porcelain)"
[[ -z "$dirty" ]] || die "the build changed the hogd checkout:"$'\n'"$dirty"

# 4. Package.
meta="$(mktemp)"
trap 'rm -f "$meta"' EXIT
build_flags_json="$(printf '%s\n' "${BUILD_FLAGS[@]}" | jq -R . | jq -s -c .)"
jq -n \
  --arg hogd_ref "$hogd_ref" \
  --argjson hogd_ref_is_tag "$ref_is_tag" \
  --arg hogd_commit "$hogd_commit" \
  --arg whitney_src_hash "$src_hash" \
  --arg whitney_commit "$whitney_commit" \
  --arg toolchain_image_digest "$image_digest" \
  --arg arch "$arch" \
  --argjson build_flags "$build_flags_json" \
  --slurpfile toolchain "$src/$build_rel/whitney-toolchain.json" \
  '{hogd_ref: $hogd_ref, hogd_ref_is_tag: $hogd_ref_is_tag, hogd_commit: $hogd_commit,
    whitney_src_hash: $whitney_src_hash, whitney_commit: $whitney_commit,
    build_flags: $build_flags, toolchain_image_digest: $toolchain_image_digest,
    toolchain: $toolchain[0], arch: $arch}' >"$meta"
"$REPO/build/package.sh" --build-dir "$src/$build_rel" --meta "$meta" --key "$key" --out "$out"
