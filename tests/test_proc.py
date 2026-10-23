"""SubprocessExec: output capture, redaction, secrets, timeouts, and signals."""
import io
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from fakes import PRESIGNED, SIGNATURE
from whitney.cli import Terminated, _signals_raise
from whitney.proc import CommandError, CommandTimeout, SubprocessExec


def test_command_error_redacts():
    cp = subprocess.CompletedProcess(["curl", PRESIGNED], 22, "", f"fetch {PRESIGNED} failed\n")
    msg = str(CommandError(cp))
    assert SIGNATURE not in msg and "exited 22" in msg and "failed" in msg


def test_subprocess_exec_captures_and_redacts():
    echo = io.StringIO()
    ex = SubprocessExec(echo=echo)
    cp = ex(["sh", "-c", f"echo out; echo err >&2; echo '{PRESIGNED}'"])
    assert cp.returncode == 0 and cp.stdout == f"out\n{PRESIGNED}\n" and cp.stderr == "err\n"
    assert "out" in echo.getvalue() and "err" in echo.getvalue()
    assert SIGNATURE not in echo.getvalue()
    with pytest.raises(CommandError, match="exited 3: bad"):
        ex(["sh", "-c", "echo bad >&2; exit 3"])
    assert ex(["sh", "-c", "exit 3"], check=False).returncode == 3


def test_subprocess_exec_env_overrides(monkeypatch):
    monkeypatch.setenv("WHITNEY_T_KEEP", "kept")
    monkeypatch.setenv("WHITNEY_T_DROP", "secret")
    ex = SubprocessExec(echo=None)
    script = 'echo "$WHITNEY_T_KEEP|${WHITNEY_T_DROP-unset}|$WHITNEY_T_NEW"'
    cp = ex(["sh", "-c", script], env={"WHITNEY_T_DROP": None, "WHITNEY_T_NEW": "new"})
    assert cp.stdout == "kept|unset|new\n"
    assert ex(["sh", "-c", script]).stdout == "kept|secret|\n"  # no override: caller's env


def test_subprocess_exec_secret_output_not_echoed():
    echo = io.StringIO()
    ex = SubprocessExec(echo=echo)
    # The argv never holds the secret; only the output does.
    cp = ex(["sh", "-c", "k=AKIA; echo ${k}SECRET; echo warn >&2"], secret=True)
    assert cp.stdout == "AKIASECRET\n"
    assert "AKIASECRET" not in echo.getvalue() and "warn" in echo.getvalue()
    with pytest.raises(CommandError) as e:
        ex(["sh", "-c", "k=AKIA; echo ${k}SECRET; exit 2"], secret=True)
    assert "AKIASECRET" not in str(e.value) and "AKIASECRET" not in echo.getvalue()


def test_subprocess_exec_timeout_stops_the_child_gracefully():
    ex = SubprocessExec(echo=None, stop_grace=10)
    start = time.monotonic()
    with pytest.raises(CommandTimeout) as e:
        ex(["sh", "-c", "trap 'echo cancelled; exit 1' TERM; echo started; "
                        "while :; do sleep 0.1; done"], timeout=0.5, check=False)
    assert time.monotonic() - start < 5
    assert e.value.stdout == "started\ncancelled\n"  # SIGTERM, then the child's own exit
    assert e.value.timeout == 0.5 and "did not finish within 0.5 s" in str(e.value)
    assert ex(["sh", "-c", "echo quick"], timeout=5).stdout == "quick\n"


def test_subprocess_exec_keeps_partial_output_on_interrupt():
    import signal

    ex = SubprocessExec(echo=None, stop_grace=5)
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt) as e:
            ex(["sh", "-c", "echo partial; exec sleep 30"])
    finally:
        timer.cancel()
    assert e.value.partial_output == "partial\n"


def test_secret_output_not_in_partial_output():
    import signal

    ex = SubprocessExec(echo=None, stop_grace=5)
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt) as e:
            ex(["sh", "-c", "k=AKIA; echo ${k}SECRET; echo note >&2; exec sleep 30"], secret=True)
    finally:
        timer.cancel()
    assert e.value.partial_output == "note\n"


def test_interrupt_in_the_stop_grace_sends_no_second_sigterm():
    """OpenTofu exits at once on a second signal and can lose its state."""
    import signal

    ex = SubprocessExec(echo=None, stop_grace=10)
    # The child counts SIGTERMs and exits 1 s after the first one.
    script = ("trap 'echo term; t=1' TERM; i=0; while :; do sleep 0.1; "
              "if [ -n \"$t\" ]; then i=$((i+1)); [ $i -ge 10 ] && exit 0; fi; done")
    timer = threading.Timer(0.6, os.kill, (os.getpid(), signal.SIGINT))  # in the grace
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt) as e:
            ex(["sh", "-c", script], timeout=0.3)
    finally:
        timer.cancel()
    assert e.value.partial_output.count("term") == 1


def test_signal_stops_a_real_child(tmp_path):
    pidfile = tmp_path / "pid"
    main_thread = threading.main_thread().ident
    timer = threading.Timer(0.5, signal.pthread_kill, (main_thread, signal.SIGTERM))
    ex = SubprocessExec(echo=None, stop_grace=10)
    start = time.monotonic()
    with _signals_raise(), pytest.raises(Terminated, match="received SIGTERM"):
        timer.start()
        ex(["sh", "-c", f"echo $$ > {pidfile}; exec sleep 30"])
    assert time.monotonic() - start < 10
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):  # stopped and reaped
        os.kill(pid, 0)


def test_child_runs_in_its_own_session():
    cp = SubprocessExec(echo=None)([sys.executable, "-c", "import os; print(os.getsid(0))"])
    assert int(cp.stdout) != os.getsid(0)


def test_dead_terminal_does_not_stop_commands():
    class DeadTTY:
        def write(self, s):
            raise OSError(5, "Input/output error")

        def flush(self):
            raise OSError(5, "Input/output error")
    cp = SubprocessExec(echo=DeadTTY())(["sh", "-c", "echo still-ran"])
    assert cp.stdout == "still-ran\n"
