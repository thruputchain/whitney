import json
from pathlib import Path

import pytest
from make_fixtures import scenario, write_run

import whitney.publish
from whitney.cli import main
from whitney.publish import PublishError, publish, redact, scan_leftovers

ACCOUNT = "123456789012"
BUCKET = f"whitney-runs-{ACCOUNT}-us-west-2"
SWEEP = "20261021-1000-exec-scaling-v0.1.31-sweep"
RUNS = ("20261021-1001-exec-scaling-v0.1.31-n1-r1", "20261021-1002-exec-scaling-v0.1.31-n2-r1")
# Every private string the fake run dirs hold. None may be in the output.
PRIVATE = ("10.42.1.85", "54.12.34.56", "198.51.100.7", "ip-10-42-1-85", BUCKET, ACCOUNT,
           "/Users/alice", "/home/alice", "arn:aws:iam::", "alice-key", "X-Amz-Signature")


def _plant(d: Path, dup_keys: bool = False) -> None:
    """Add private details to a run dir, as a real EC2 run has them.
    dup_keys: two IP keys in one object, which redaction would merge."""
    by_ip = {"10.42.1.85": "runner", **({"54.12.34.56": "public"} if dup_keys else {})}
    (d / "roles.json").write_text(json.dumps(
        {"bin_dir": "/opt/whitney/bin", "runner_bind_ip": "10.42.1.85",
         "event": {"host": "10.42.1.86", "control_port": 11111},
         "loopback": "127.0.0.1", "any": "0.0.0.0",
         "hosts_by_ip": by_ip}, indent=2) + "\n")
    (d / "runner.log").write_text(
        "\x1b[1mI\x1b[0m connecting to control server on 10.42.1.85:11111\n"
        "local 127.0.0.1:9100 and 0.0.0.0:22\n"
        "host ip-10-42-1-85.us-west-2.compute.internal and ip-10-42-1-86\n"
        "version 1.2.3.4.5 stays\n")
    (d / "host-facts").mkdir()
    (d / "host-facts" / "exec0.json").write_text(json.dumps(
        {"role": "exec0", "uname": "Linux ip-10-42-1-85 7.0.0-1014-aws x86_64",
         "public_ip": "54.12.34.56", "meminfo": {"MemTotal": 518470972}}, indent=2) + "\n")
    (d / "host-facts" / "notes.txt").write_text("not json\n")
    (d / "command.txt").write_text(
        f"whitney: whitney sweep s.toml --bucket {BUCKET} --ssh-key-name alice-key "
        "--operator-cidr 198.51.100.7/32 --ssh-identity /Users/alice/.ssh/id_ed25519 "
        f"--archive s3://{BUCKET}/archives/a.tar.gz\n"
        "runner: timeout 3600 /opt/whitney/bin/whitney-runner\n")
    (d / "aws.json").write_text(json.dumps({"mode": "process", "caller": "user/alice"}))
    (d / "metrics.json").write_text(json.dumps({"remote_write": False}, indent=2) + "\n")
    for sub in ("sar", "daemon-logs/exec0", "metrics"):
        (d / sub).mkdir(parents=True)
    (d / "sar" / "exec0.bin").write_bytes(b"\x00 10.42.1.85")
    (d / "daemon-logs" / "exec0" / "hog-exec.log").write_text("listening on 10.42.1.85\n")
    (d / "metrics" / "exec0.ndjson.gz").write_bytes(b"\x1f\x8b")
    (d / "stray.txt").write_text("not on the allowlist\n")


def _sweep(tmp: Path, batch_sizes=(5,), dup_keys=False) -> Path:
    s = scenario(tmp, batch_sizes=list(batch_sizes), n_list=[1, 2], storage="tmpfs")
    root = tmp / "results" / SWEEP
    for name in RUNS:
        _plant(write_run(root, name, s, batch_size=batch_sizes[0],
                         tps=[100.0 + i for i in range(5)]), dup_keys)
    st = root / RUNS[1] / "status.json"
    st.write_text(json.dumps({"status": "ok", "error": None,
                              "note": f"https://{BUCKET}.s3.us-west-2.amazonaws.com/x?<redacted>",
                              "arn": "arn:aws:iam::123456789012:user/alice"}, indent=2) + "\n")
    (root / "sweep.json").write_text(json.dumps(
        {"seed": 1, "order": [[1, 2]], "aborted": True,
         "abort_reason": f"tofu -chdir=/home/alice/whitney destroy; bucket {BUCKET}"},
        indent=2) + "\n")
    (root / "summary-b5.md").write_text("old summary with 10.42.1.85\n")
    (root / "prof").mkdir()
    return root


def _all_text(root: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in root.rglob("*")
                     if p.is_file() and p.suffix != ".png")


def test_publish_sweep(tmp_path):
    src = _sweep(tmp_path)
    out = publish([src], tmp_path / "pub")
    dst = out / SWEEP
    run = dst / RUNS[0]
    assert sorted(p.relative_to(run).as_posix() for p in run.rglob("*")) == [
        "archive.sha256", "command.txt", "host-facts", "host-facts/exec0.json", "manifest.json",
        "metrics.json", "roles.json", "runner.log", "scenario.resolved.toml", "status.json",
        "trials.jsonl"]
    assert sorted(p.name for p in dst.iterdir()) == sorted(
        [*RUNS, "sweep.json", "summary-b5.csv", "summary-b5.md", "scaling-b5.svg",
         "scaling-b5.png"])
    text = _all_text(out)
    for p in PRIVATE:
        assert p not in text, p
    assert scan_leftovers(out) == []
    # The summary is made again from the published runs, not copied.
    assert "| tmpfs | 1 |" in (dst / "summary-b5.md").read_text()
    assert "old summary" not in text


def test_redacted_files(tmp_path):
    src = _sweep(tmp_path)
    run = publish([src], tmp_path / "pub") / SWEEP / RUNS[0]
    roles = json.loads((run / "roles.json").read_text())
    assert roles["runner_bind_ip"] == "<ip>" and roles["event"]["host"] == "<ip>"
    assert (roles["loopback"], roles["any"]) == ("127.0.0.1", "0.0.0.0")
    assert roles["hosts_by_ip"] == {"<ip>": "runner"}
    log = (run / "runner.log").read_text()
    assert "on <ip>:11111" in log and "127.0.0.1:9100" in log and "0.0.0.0:22" in log
    assert "host <host> and <host>\n" in log and "1.2.3.4.5" in log
    assert "\x1b[1mI" in log
    facts = json.loads((run / "host-facts" / "exec0.json").read_text())
    assert facts["uname"] == "Linux <host> 7.0.0-1014-aws x86_64"
    assert facts["meminfo"]["MemTotal"] == 518470972
    cmd = (run / "command.txt").read_text()
    assert ("--bucket <bucket> --ssh-key-name <key-name> --operator-cidr <ip>/32 "
            "--ssh-identity <home>/.ssh/id_ed25519 --archive <s3-url>") in cmd
    status = json.loads((run.parent / RUNS[1] / "status.json").read_text())
    assert status["note"] == "<s3-url>" and status["arn"] == "<arn>"
    sweep = json.loads((run.parent / "sweep.json").read_text())
    assert sweep["abort_reason"] == "tofu -chdir=<home>/whitney destroy; bucket <bucket>"


def test_json_keeps_layout_and_numbers(tmp_path):
    src = _sweep(tmp_path)
    run = publish([src], tmp_path / "pub") / SWEEP / RUNS[0]
    for f in ("trials.jsonl", "status.json", "manifest.json", "scenario.resolved.toml",
              "archive.sha256", "metrics.json"):
        assert (run / f).read_bytes() == (src / RUNS[0] / f).read_bytes(), f
    before = (src / RUNS[0] / "roles.json").read_text().splitlines()
    after = (run / "roles.json").read_text().splitlines()
    assert len(before) == len(after)
    assert [a for a, b in zip(after, before) if a != b] == [
        '  "runner_bind_ip": "<ip>",', '    "host": "<ip>",', '    "<ip>": "runner"']


def test_duplicate_keys_after_redaction_fail(tmp_path):
    # Two IP keys in one object both become "<ip>": publish must not drop one.
    src = _sweep(tmp_path, dup_keys=True)
    with pytest.raises(PublishError, match="duplicate keys"):
        publish([src], tmp_path / "pub")
    assert not (tmp_path / "pub").exists()


def test_index(tmp_path):
    src = _sweep(tmp_path)
    out = publish([src], tmp_path / "pub")
    index = (out / "INDEX.md").read_text()
    assert f"## {SWEEP}" in index
    for p in ("- Scenario: `exec-scaling`", "- hogd_ref: -", "- Storage: `tmpfs`",
              "- exec_allocator: `system`", "- exec_per_l3: `false`", "- n_list: `[1, 2]`",
              "- Runs: 2 ok, 0 failed", "stopped early",
              f"![Scaling chart, batch size 5]({SWEEP}/scaling-b5.png)",
              "| tmpfs | 1 |", "Left out: sar data, daemon logs",
              "Left out of each of the 2 runs: `aws.json`, `daemon-logs/`, "
              "`host-facts/notes.txt`, `metrics/`, `sar/`, `stray.txt`.",
              "Left out of the sweep dir: `prof/`."):
        assert p in index, p
    assert (out / SWEEP / "scaling-b5.png").stat().st_size > 0


def test_index_reads_manifest(tmp_path):
    src = _sweep(tmp_path)
    for r in RUNS:
        (src / r / "manifest.json").write_text(json.dumps(
            {"hogd_ref": "v0.1.31", "hogd_commit": "c" * 40, "whitney_commit": "d" * 40},
            indent=2) + "\n")
    index = (publish([src], tmp_path / "pub") / "INDEX.md").read_text()
    assert f"- hogd_commit: `{'c' * 40}`" in index
    assert f"- whitney_commit: `{'d' * 40}`" in index
    assert f"- Archive SHA-256: `{'a' * 64}`" in index


def test_lone_run_dir(tmp_path):
    src = _sweep(tmp_path) / RUNS[1]
    out = publish([src], tmp_path / "pub")
    assert (out / RUNS[1] / "summary-b5.md").is_file()
    assert (out / RUNS[1] / "scaling-b5.png").is_file()
    assert f"## {RUNS[1]}" in (out / "INDEX.md").read_text()


def test_out_must_not_exist(tmp_path):
    src = _sweep(tmp_path)
    (tmp_path / "pub").mkdir()
    with pytest.raises(PublishError, match="--out exists"):
        publish([src], tmp_path / "pub")


def test_batch_size_required_with_several(tmp_path):
    src = _sweep(tmp_path, batch_sizes=(5, 10))
    with pytest.raises(PublishError, match="--batch-size"):
        publish([src], tmp_path / "pub")
    assert not (tmp_path / "pub").exists()
    out = publish([src], tmp_path / "pub", batch_size=10)
    assert (out / SWEEP / "summary-b10.md").is_file()
    with pytest.raises(PublishError, match="not in the scenario"):
        publish([src], tmp_path / "pub2", batch_size=7)


def test_leftover_scan_fails_when_redaction_misses(tmp_path, monkeypatch):
    src = _sweep(tmp_path)
    monkeypatch.setattr(whitney.publish, "redact", lambda text: text)
    with pytest.raises(PublishError) as e:
        publish([src], tmp_path / "pub")
    msg = str(e.value)
    assert f"{SWEEP}/{RUNS[0]}/roles.json:3: IPv4 address: 10.42.1.85" in msg
    for what in ("EC2 host name", "bucket name", "S3 URL", "home path", "ARN", "SSH key name"):
        assert f": {what}: " in msg, what
    assert not (tmp_path / "pub").exists()  # nothing half-done is left


def test_redact_patterns():
    assert redact("a 10.0.0.1 b 127.0.0.1 c 0.0.0.0 d 999.1.1.1") == \
        "a <ip> b 127.0.0.1 c 0.0.0.0 d 999.1.1.1"
    assert redact("ip-172-31-5-9.ec2.internal.") == "<host>."
    assert redact("https://b.s3.amazonaws.com/k?X-Amz-Signature=s") == "<s3-url>"
    assert redact("arn:aws-us-gov:iam::1:role/x y") == "<arn> y"
    assert redact("/home/ubuntu/x and /Users/bob") == "<home>/x and <home>"


def test_cli_publish(tmp_path, capsys):
    src = _sweep(tmp_path)
    assert main(["publish", str(src), "--out", str(tmp_path / "pub")]) == 0
    assert str(tmp_path / "pub" / "INDEX.md") in capsys.readouterr().out
    assert main(["publish", str(src), "--out", str(tmp_path / "pub")]) == 2
    assert "--out exists" in capsys.readouterr().err
    assert main(["publish", str(tmp_path), "--out", str(tmp_path / "pub3")]) == 2
    assert "not a sweep dir or a run dir" in capsys.readouterr().err
