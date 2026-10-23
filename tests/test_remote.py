"""The host scripts (exec setup, restart, L3 read) run for real in bash, in a
sandbox: the host paths point into tmp_path, and sudo, systemctl, install, and
ss are stubs that log their arguments. A stub sed gives BSD sed GNU's -i."""
import subprocess

import pytest

from whitney import remote
from whitney.cpus import l3_groups

_STUBS = {
    "sudo": 'exec "$@"\n',
    "systemctl": 'echo "systemctl $*" >>"$LOG"\n',
    "install": 'echo "install $*" >>"$LOG"\nfor a; do d=$a; done\nmkdir -p "$d"\n',
    "ss": 'echo "ss $*" >>"$LOG"\necho "LISTEN 0 128 *:x *:*"\n',
    "sed": ('if [ "$1" = -i ] && ! /usr/bin/sed --version >/dev/null 2>&1; then\n'
            "  shift; exec /usr/bin/sed -i '' \"$@\"\nfi\nexec /usr/bin/sed \"$@\"\n"),
}


def _sandbox(tmp_path):
    root = tmp_path / "host"
    stubs = root / "stubs"
    stubs.mkdir(parents=True)
    for name, body in _STUBS.items():
        (stubs / name).write_text("#!/bin/bash\n" + body)
        (stubs / name).chmod(0o755)
    for d in ("etc", "mnt/work/exec2", "mnt/tmp", "run", "var"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "run" / "ready").write_text("")
    (root / "libjemalloc.so.2").write_text("")
    (root / "etc" / "control-11111.env").write_text(
        f"ROLE=exec2\nWORK_DIR={root}/mnt/work/exec2\n")
    return root


def _run_on(root, script: str) -> list[str]:
    """Run script in the sandbox; return the stub log lines it added."""
    for host_path, local in (("/etc/whitney", "etc"), ("/mnt/whitney", "mnt"),
                             ("/run/whitney", "run"), ("/var/lib/whitney", "var"),
                             (remote.JEMALLOC, "libjemalloc.so.2")):
        script = script.replace(host_path, f"{root}/{local}")
    log = root / "log"
    before = log.read_text().splitlines() if log.exists() else []
    env = {"PATH": f"{root / 'stubs'}:/usr/bin:/bin", "LOG": str(log)}
    cp = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return log.read_text().splitlines()[len(before):]


def _env_files(root) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted((root / "etc").glob("control-*.env"))}


def test_exec_setup_runs_adds_and_removes_units(tmp_path):
    root = _sandbox(tmp_path)
    work, lib = root / "mnt" / "work", root / "libjemalloc.so.2"
    # Left from an earlier run with 8 hog-execs: units 11112 and 11118.
    (root / "etc" / "control-11112.env").write_text("ROLE=exec2-1\nWORK_DIR=x\n")
    (root / "etc" / "control-11118.env").write_text("ROLE=exec2-7\nWORK_DIR=x\n")
    (work / "exec2-7").mkdir()
    log = _run_on(root, remote._exec_setup(["0-7", "8-15", "16-23", "24-31"], "jemalloc"))
    assert _env_files(root) == {
        "control-11111.env": f"ROLE=exec2\nWORK_DIR={work}/exec2\nLD_PRELOAD={lib}\n",
        **{f"control-{11111 + i}.env":
           f"ROLE=exec2-{i}\nWORK_DIR={work}/exec2-{i}\nLD_PRELOAD={lib}\n" for i in (1, 2, 3)}}
    assert sorted(p.name for p in work.iterdir()) == ["exec2", "exec2-1", "exec2-2", "exec2-3"]
    assert log == [
        "systemctl stop whitney-control@11118.service",
        *(f"install -d -o ubuntu -g ubuntu {work}/exec2-{i}" for i in (1, 2, 3)),
        *(f"systemctl set-property --runtime whitney-control@{11111 + i}.service "
          f"AllowedCPUs={c}" for i, c in enumerate(("0-7", "8-15", "16-23", "24-31")))]
    # The restart restarts, and waits for, every unit of this run.
    log = _run_on(root, remote._RESTART)
    ports = [str(11111 + i) for i in range(4)]
    assert [x for x in log if "restart" in x] == \
        [f"systemctl restart whitney-control@{p}.service" for p in ports]
    assert [x for x in log if x.startswith("ss ")] == [f"ss -Hltn sport = :{p}" for p in ports]
    # A rerun with one hog-exec and glibc removes the extra units and every preload.
    log = _run_on(root, remote._exec_setup(["0-3"], "system"))
    assert _env_files(root) == {"control-11111.env": f"ROLE=exec2\nWORK_DIR={work}/exec2\n"}
    assert sorted(p.name for p in work.iterdir()) == ["exec2"]
    assert log == [*(f"systemctl stop whitney-control@{p}.service" for p in ports[1:]),
                   "systemctl set-property --runtime whitney-control@11111.service "
                   "AllowedCPUs=0-3"]
    log = _run_on(root, remote._RESTART)
    assert [x for x in log if "restart" in x] == ["systemctl restart whitney-control@11111.service"]


def test_exec_setup_needs_the_base_unit(tmp_path):
    root = _sandbox(tmp_path)
    (root / "etc" / "control-11111.env").write_text("WORK_DIR=x\n")
    with pytest.raises(AssertionError, match="no ROLE in"):
        _run_on(root, remote._exec_setup(["0-7", "8-15"], "system"))
    (root / "etc" / "control-11111.env").unlink()
    with pytest.raises(AssertionError, match="no .*/etc/control-11111.env"):
        _run_on(root, remote._exec_setup(["0-7", "8-15"], "system"))


def test_restart_refuses_a_host_without_the_ready_marker(tmp_path):
    """The ready marker is on /run (tmpfs): a rebooted host has lost it, with
    its mount and control units, and must not run again."""
    assert remote.READY_DIR == "/run/whitney"
    root = _sandbox(tmp_path)
    assert any("restart" in x for x in _run_on(root, remote._RESTART))
    (root / "run" / "ready").unlink()
    with pytest.raises(AssertionError, match="host is not ready"):
        _run_on(root, remote._RESTART)


def test_l3_topology_runs(tmp_path):
    """The L3 read on a fake sysfs: offline CPUs and caches of another level
    are skipped."""
    sysfs = tmp_path / "cpu"
    for c in range(8):
        d = sysfs / f"cpu{c}"
        (d / "cache" / "index3").mkdir(parents=True)
        (d / "cache" / "index3" / "shared_cpu_list").write_text("0-3\n" if c < 4 else "4-7\n")
        (d / "cache" / "index3" / "level").write_text("3\n")
        if c:
            (d / "online").write_text("1\n")
    (sysfs / "cpu7" / "online").write_text("0\n")
    (sysfs / "cpu8" / "cache" / "index3").mkdir(parents=True)  # level 2 only: skipped
    (sysfs / "cpu8" / "cache" / "index3" / "shared_cpu_list").write_text("8-9\n")
    (sysfs / "cpu8" / "cache" / "index3" / "level").write_text("2\n")
    (sysfs / "cpufreq").mkdir()
    script = remote._L3_TOPOLOGY.replace("/sys/devices/system/cpu", str(sysfs))
    cp = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert cp.stdout == "0-3\n4-7\n"
    assert l3_groups(cp.stdout.splitlines()) == [[0, 1, 2, 3], [4, 5, 6, 7]]
