"""External commands: the Exec protocol, the real implementation, redaction."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from subprocess import CompletedProcess
from collections.abc import Mapping
from typing import IO, Protocol

# Per-call environment changes: a str sets the variable, None removes it.
EnvOverride = Mapping[str, "str | None"]

# A URL query string can hold a signature (S3 presigned URLs). Logs, errors,
# and result files keep the URL up to "?" only.
_URL_QUERY = re.compile(r"(https?://[^\s?'\"]+)\?[^\s'\"]*")


def redact(text: str) -> str:
    return _URL_QUERY.sub(r"\1?<redacted>", text)


def tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.rstrip().splitlines()[-lines:])


def partial_output(exc: BaseException) -> str:
    """What a command printed before an interrupt stopped it ("" if unknown)."""
    out = getattr(exc, "partial_output", "")
    return out if isinstance(out, str) else ""


def _attach_partial_output(exc: BaseException, out: str) -> None:
    try:
        exc.partial_output = out  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


class Exec(Protocol):
    """Runs argv. env changes the child's environment (the caller's env
    otherwise). secret=True means stdout holds secrets: it is not echoed
    and not put in a CommandError or partial output. A child still running
    after timeout seconds is stopped like on an interrupt, and the call
    raises CommandTimeout (also with check=False)."""

    def __call__(self, argv: list[str], *, cwd: Path | None = None, check: bool = True,
                 env: EnvOverride | None = None, secret: bool = False,
                 timeout: float | None = None) -> CompletedProcess[str]: ...


def child_env(env: EnvOverride | None) -> dict[str, str] | None:
    """os.environ with env applied, or None (inherit) when env is empty."""
    if not env:
        return None
    out = dict(os.environ)
    for k, v in env.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


class CommandError(RuntimeError):
    """A command exited non-zero. The message is redacted."""

    def __init__(self, cp: CompletedProcess[str], what: str | None = None):
        self.returncode = cp.returncode
        self.stdout = cp.stdout or ""
        self.stderr = cp.stderr or ""
        argv = cp.args if isinstance(cp.args, list) else [str(cp.args)]
        out = tail(self.stderr) or tail(self.stdout)
        msg = f"{shlex.join(str(a) for a in argv)} {what or f'exited {cp.returncode}'}"
        super().__init__(redact(f"{msg}: {out}" if out else msg))


class CommandTimeout(CommandError):
    """A command ran past its timeout and was stopped (SIGTERM, then the
    grace time, then SIGKILL). stdout and stderr hold what it printed."""

    def __init__(self, cp: CompletedProcess[str], timeout: float):
        self.timeout = timeout
        super().__init__(cp, f"did not finish within {timeout:g} s and was stopped")


# How long a child gets to exit after SIGTERM when whitney is interrupted.
# tofu uses it to finish in-flight API calls and write its state.
STOP_GRACE_SECONDS = 300


class SubprocessExec:
    """Runs commands, captures stdout and stderr, and echoes both (redacted).

    Children run in their own session, so a terminal hangup or Ctrl-C reaches
    only whitney. If whitney is interrupted while a child runs, the child gets
    SIGTERM and up to STOP_GRACE_SECONDS to exit (then SIGKILL), and the
    interrupt goes on up to the cleanup code, with what the child printed
    so far in its partial_output attribute.
    """

    def __init__(self, echo: IO[str] | None = sys.stderr,
                 stop_grace: float = STOP_GRACE_SECONDS):
        self.echo = echo
        self.stop_grace = stop_grace

    def __call__(self, argv: list[str], *, cwd: Path | None = None, check: bool = True,
                 env: EnvOverride | None = None, secret: bool = False,
                 timeout: float | None = None) -> CompletedProcess[str]:
        argv = [str(a) for a in argv]
        self._say("+ " + shlex.join(argv))
        out: list[str] = []
        err: list[str] = []
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True,
                             env=child_env(env))
        readers = [threading.Thread(target=self._pump, args=(p.stdout, out, not secret),
                                    daemon=True),
                   threading.Thread(target=self._pump, args=(p.stderr, err, True), daemon=True)]
        timed_out = False
        term_at: list[float] = []  # when SIGTERM went out; never send a second one
        try:
            for t in readers:
                t.start()
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._say(f"whitney: {argv[0]} ran past {timeout:g} s; stopping it")
                self._stop(p, term_at)  # SIGTERM first: tofu cancels and writes its state
            for t in readers:
                t.join()
            p.wait()
        except BaseException as e:
            # An interrupt in the grace after the deadline SIGTERM waits out
            # that grace: OpenTofu exits at once on a second signal and can
            # lose its state.
            self._stop(p, term_at)
            for t in readers:  # the child is gone, so the pipes reach EOF
                t.join(timeout=5)
            _attach_partial_output(e, "".join(err) if secret else "".join(out) + "".join(err))
            raise
        finally:
            for stream in (p.stdout, p.stderr):
                if stream is not None and not any(t.is_alive() for t in readers):
                    stream.close()
        cp = CompletedProcess(argv, p.returncode, "".join(out), "".join(err))
        shown = CompletedProcess(argv, cp.returncode, "", cp.stderr) if secret else cp
        if timed_out:
            raise CommandTimeout(shown, timeout or 0)
        if check and cp.returncode != 0:
            raise CommandError(shown)
        return cp

    def _pump(self, stream: IO[str], into: list[str], echo: bool) -> None:
        for line in stream:
            into.append(line)
            if echo:
                self._say(line.rstrip("\n"))

    def _stop(self, p: subprocess.Popen, term_at: list[float]) -> None:
        """SIGTERM (once), wait out the grace from that SIGTERM, then SIGKILL."""
        if p.poll() is not None:
            return
        if not term_at:
            p.terminate()
            term_at.append(time.monotonic())
        left = self.stop_grace - (time.monotonic() - term_at[0])
        try:
            p.wait(timeout=max(0.0, left))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

    def _say(self, line: str) -> None:
        if self.echo is None:
            return
        try:
            print(redact(line), file=self.echo, flush=True)
        except OSError:  # the terminal may be gone (SIGHUP); keep running
            self.echo = None
