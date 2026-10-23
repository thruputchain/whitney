"""Build cache, archive checks, and the archive upload."""
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from fakes import KEY, PRESIGNED, Reply, install_cluster, make_archive, opts
from whitney.build import ensure_build, load_archive, upload_archive
from whitney.run import run_scenario

REPO = Path(__file__).resolve().parents[1]


def build_rule(fx, cache: Path, key_of=lambda: KEY):
    def build(argv):
        return str(make_archive(cache, key_of())) + "\n"
    fx.on("--out", Reply(effect=build))


def builds(fx) -> list[int]:
    return [i for i in fx.find("build.sh") if "--print-key" not in fx.calls[i].argv]


def test_build_cached(tmp_path, fx, scenario):
    s, cache = scenario(), tmp_path / "cache"
    fx.on("--print-key", Reply(stdout=KEY + "\n"))
    build_rule(fx, cache)
    a1 = ensure_build(s, cache, fx)
    a2 = ensure_build(s, cache, fx)
    assert len(builds(fx)) == 1
    assert a1 == a2
    assert a1.path == cache / f"whitney-{KEY}.tar.gz"
    assert a1.sha256 == hashlib.sha256(a1.path.read_bytes()).hexdigest()
    assert a1.manifest["hogd_ref"] == "v0.1.31"
    key_call = fx.calls[fx.find("--print-key")[0]].argv
    assert key_call[0] == str(REPO / "build" / "build.sh")
    assert key_call[1:] == ["--print-key", "--hogd-ref", "v0.1.31", "--arch", "znver5"]


def test_build_passes_arch(tmp_path, fx, scenario):
    cache = tmp_path / "cache"
    fx.on("--print-key", Reply(stdout="v0.1.31-0123456789ab-znver4\n"))
    build_rule(fx, cache, lambda: "v0.1.31-0123456789ab-znver4")
    ensure_build(scenario(), cache, fx, arch="znver4")
    build = fx.calls[builds(fx)[0]].argv
    assert build[1:] == ["--hogd-ref", "v0.1.31", "--arch", "znver4", "--out", str(cache)]


def test_cache_key_includes_whitney_source(tmp_path, fx, scenario):
    """The key from the real `build.sh --print-key` decides cache hit or miss (R2)."""
    repo = tmp_path / "repo"
    for d in ("build", "runner"):
        shutil.copytree(REPO / d, repo / d)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    cache, keys = tmp_path / "cache", []

    def ex(argv, *, cwd=None, check=True):
        if "--print-key" in argv:
            cp = subprocess.run(["bash", str(repo / "build" / "build.sh"), *argv[1:]],
                                capture_output=True, text=True, check=True)
            keys.append(cp.stdout.strip())
            return cp
        return fx(argv, cwd=cwd, check=check)

    build_rule(fx, cache, lambda: keys[-1])
    s = scenario()
    ensure_build(s, cache, ex)
    ensure_build(s, cache, ex)
    assert len(builds(fx)) == 1
    (repo / "runner" / "whitney_runner.cc").write_text("// changed\n")
    a = ensure_build(s, cache, ex)
    assert keys[0] == keys[1] != keys[2]
    assert len(builds(fx)) == 2
    assert a.path.name == f"whitney-{keys[2]}.tar.gz"


def test_archive_tag_mismatch_rejected(tmp_path, fx, scenario):
    s, cache = scenario(), tmp_path / "cache"
    path = make_archive(cache, KEY, hogd_ref="v0.1.30")
    fx.on("--print-key", Reply(stdout=KEY + "\n"))
    msg = r"archive hogd_ref v0\.1\.30 != scenario hogd_ref v0\.1\.31"
    with pytest.raises(ValueError, match=msg):
        ensure_build(s, cache, fx)
    assert not builds(fx)
    with pytest.raises(ValueError, match=msg):  # --archive <path>
        load_archive(path, s)


def test_corrupt_archive_rejected(tmp_path, scenario):
    path = make_archive(tmp_path, KEY)
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ValueError, match="archive sha256 mismatch"):
        load_archive(path, scenario())


def test_upload_archive_presigns_for_12h(tmp_path, fx, scenario):
    a = load_archive(make_archive(tmp_path, KEY), scenario())
    fx.on("aws s3 presign", Reply(stdout=PRESIGNED + "\n"))
    assert upload_archive(a, "bkt", fx, region="eu-west-1") == PRESIGNED
    # The key carries the archive hash: two builds of one cache key differ
    # in bytes, and must not replace each other while a cluster boots.
    s3 = f"s3://bkt/whitney/whitney-{KEY}-{a.sha256[:12]}.tar.gz"
    # The presigned URL must be signed for the bucket's (scenario) region,
    # not the aws CLI default region.
    assert fx.calls[0].argv == ["aws", "s3", "cp", str(a.path), s3, "--region", "eu-west-1"]
    assert fx.calls[1].argv == ["aws", "s3", "presign", s3, "--expires-in", "43200",
                                "--region", "eu-west-1"]


def test_run_and_sweep_upload_with_scenario_region(tmp_path, fx, scenario):
    from whitney.sweep import sweep
    s = scenario("region='ap-south-1'", "availability_zone='ap-south-1a'", "n_list=[1]")
    install_cluster(fx, 1)
    run_scenario(s, 1, opts(tmp_path / "run"), fx, command=["whitney"])
    sweep(s, opts(tmp_path / "sweep"), fx, seed=1)
    ups = [c.argv for c in fx.calls if c.argv[:3] in (["aws", "s3", "cp"], ["aws", "s3", "presign"])]
    assert len(ups) == 4 and all(a[-2:] == ["--region", "ap-south-1"] for a in ups)
