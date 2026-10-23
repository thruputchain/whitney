"""Build archive: cache lookup, build, verification, and upload."""
from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from whitney.config import Scenario
from whitney.proc import Exec

REPO = Path(__file__).resolve().parents[1]
BUILD_SH = REPO / "build" / "build.sh"
PRESIGN_SECONDS = 12 * 3600


@dataclass(frozen=True)
class Archive:
    path: Path
    sha256: str
    manifest: dict


def archive_name(key: str) -> str:
    return f"whitney-{key}.tar.gz"


def ensure_build(s: Scenario, cache: Path, ex: Exec, *, arch: str = "znver5") -> Archive:
    """Return the cached archive for the scenario's hogd_ref, or build it.

    build.sh --print-key gives the cache key (hogd_ref, whitney source hash,
    arch), so Python never re-implements the hash.
    """
    key_argv = [str(BUILD_SH), "--print-key", "--hogd-ref", s.hogd_ref, "--arch", arch]
    key = ex(key_argv).stdout.strip()
    if not key:
        raise RuntimeError("build.sh --print-key printed no key")
    path = cache / archive_name(key)
    if not (path.is_file() and Path(f"{path}.sha256").is_file()):
        cache.mkdir(parents=True, exist_ok=True)
        path = run_build(s.hogd_ref, arch, cache, ex)
        if path.name != archive_name(key):
            raise RuntimeError(f"build.sh wrote {path.name}, expected {archive_name(key)}")
    return load_archive(path, s)


def run_build(hogd_ref: str, arch: str, out: Path, ex: Exec) -> Path:
    """Run build.sh and return the archive path it prints on its last stdout line."""
    cp = ex([str(BUILD_SH), "--hogd-ref", hogd_ref, "--arch", arch, "--out", str(out)])
    lines = cp.stdout.strip().splitlines()
    if not lines:
        raise RuntimeError("build.sh printed no archive path")
    return Path(lines[-1])


def load_archive(path: Path, s: Scenario) -> Archive:
    """Verify an archive against its .sha256 file and the scenario's hogd_ref."""
    sha_file = Path(f"{path}.sha256")
    if not path.is_file():
        raise ValueError(f"no archive at {path}")
    if not sha_file.is_file():
        raise ValueError(f"no {sha_file.name} next to {path}")
    fields = sha_file.read_text().split()
    want = fields[0].lower() if fields else ""
    got = _sha256(path)
    if got != want:
        raise ValueError(f"archive sha256 mismatch for {path}: {sha_file.name} says {want}, file is {got}")
    manifest = _read_manifest(path)
    if manifest.get("hogd_ref") != s.hogd_ref:
        raise ValueError(
            f"archive hogd_ref {manifest.get('hogd_ref')} != scenario hogd_ref {s.hogd_ref} ({path})")
    return Archive(path=path, sha256=got, manifest=manifest)


def s3_key(a: Archive) -> str:
    """whitney/<archive stem>-<sha256[:12]>.tar.gz.

    The hash is in the key because two builds of one cache key differ in
    bytes (built_at): a second upload must not replace the object a booting
    cluster is about to fetch and check.
    """
    stem = a.path.name.removesuffix(".tar.gz")
    return f"whitney/{stem}-{a.sha256[:12]}.tar.gz"


def upload_archive(a: Archive, bucket: str, ex: Exec, *, region: str) -> str:
    """Copy the archive to s3://<bucket>/<s3_key> and return a 12 h presigned URL.

    region is the scenario (and bucket) region. Without it the aws CLI signs
    for its default region, and hosts in another region get a URL S3 rejects.
    """
    s3 = f"s3://{bucket}/{s3_key(a)}"
    ex(["aws", "s3", "cp", str(a.path), s3, "--region", region])
    url = ex(["aws", "s3", "presign", s3, "--expires-in", str(PRESIGN_SECONDS),
              "--region", region]).stdout.strip()
    if not url.startswith("https://"):
        raise RuntimeError("aws s3 presign did not print an https URL")
    return url


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_manifest(path: Path) -> dict:
    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1:
            raise ValueError(f"archive {path} must have one top dir, has {sorted(tops)}")
        member = f"{tops.pop()}/manifest.json"
        if member not in names:
            raise ValueError(f"archive {path} has no {member}")
        f = tar.extractfile(member)
        if f is None:
            raise ValueError(f"archive {path}: {member} is not a file")
        return json.load(f)
