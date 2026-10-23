"""whitney command line."""
from __future__ import annotations

import argparse
import contextlib
import ipaddress
import math
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path

from whitney.config import ScenarioError, load_scenario
from whitney.infra import INSTANCE_CREATE_TIMEOUT, DestroyError, duration_seconds
from whitney.metrics import PASS_ENV, TOKEN_ENV, USER_ENV, redact_url, remote_write_from_env
from whitney.proc import CommandError, Exec, SubprocessExec, redact
from whitney.run import RUN_MAX_HOURS, RUNNER_TIMEOUT, SWEEP_MAX_HOURS, RunOptions, run_scenario
from whitney.sweep import sweep


class Terminated(KeyboardInterrupt):
    """SIGTERM or SIGHUP, raised like Ctrl-C so the same cleanup (destroy) runs."""


_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _on_signal(signum: int, frame) -> None:
    name = signal.Signals(signum).name
    # One shot: later SIGTERM/SIGHUP must not interrupt the cleanup this one
    # starts. Ctrl-C still interrupts it.
    for s in _SIGNALS:
        signal.signal(s, _ignore_during_cleanup)
    raise Terminated(f"received {name}")


def _ignore_during_cleanup(signum: int, frame) -> None:
    try:
        print(f"whitney: {signal.Signals(signum).name} ignored: cleanup is running",
              file=sys.stderr, flush=True)
    except OSError:
        pass


@contextlib.contextmanager
def _signals_raise() -> Iterator[None]:
    """SIGTERM and SIGHUP raise Terminated, so finally blocks destroy the cluster."""
    old = {s: signal.signal(s, _on_signal) for s in _SIGNALS}
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def _cidr(value: str) -> str:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a CIDR: {value!r}") from None


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {value!r}")
    return n


def _positive_hours(value: str) -> float:
    try:
        h = float(value)
    except ValueError:
        h = 0.0
    if not (math.isfinite(h) and h > 0):
        raise argparse.ArgumentTypeError(f"must be a positive number of hours: {value!r}")
    return h


def _duration(value: str) -> str:
    """A Go duration as tofu timeouts take it (for example 6m, 1h30m), above 0."""
    try:
        ok = duration_seconds(value) > 0
    except ValueError:
        ok = False
    if not ok:
        raise argparse.ArgumentTypeError(f"must be a duration like 6m or 1h30m: {value!r}")
    return value


def _add_cluster_args(p: argparse.ArgumentParser, max_hours: int) -> None:
    p.add_argument("--arch", choices=["znver5", "znver4"], default="znver5",
                   help="build arch when no --archive is given")
    p.add_argument("--archive", type=Path, help="prebuilt archive to use instead of building")
    p.add_argument("--ssh-key-name", required=True, help="EC2 key pair name for the hosts")
    p.add_argument("--operator-cidr", required=True, type=_cidr,
                   help="CIDR allowed to SSH to the hosts, for example 198.51.100.7/32")
    p.add_argument("--ssh-identity", type=Path, help="private key file for ssh and scp")
    p.add_argument("--runner-timeout", type=_positive_int, default=RUNNER_TIMEOUT,
                   metavar="SECONDS", help="kill whitney-runner after this many seconds")
    p.add_argument("--max-hours", type=_positive_hours, default=max_hours, metavar="HOURS",
                   help="each host powers off and terminates this long after boot, even if "
                        f"whitney never destroys it (default {max_hours})")
    p.add_argument("--instance-create-timeout", type=_duration, default=INSTANCE_CREATE_TIMEOUT,
                   metavar="DURATION",
                   help="aws_instance create timeout: how long an instance may take to "
                        f"reach running after launch (default {INSTANCE_CREATE_TIMEOUT})")
    p.add_argument("--apply-timeout", type=_duration, metavar="DURATION",
                   help="stop tofu apply (SIGTERM; it cancels and writes its state) after this "
                        "time, then destroy. Catches EC2 capacity retries "
                        "(default --instance-create-timeout + 4m)")
    p.add_argument("--aws-creds", choices=["process", "env"], default="process",
                   help="process (default): tofu gets credentials from `aws configure "
                        "export-credentials` and refreshes them (works with `aws login` and "
                        "SSO); env: tofu uses the AWS_* variables in your environment as is")
    p.add_argument("--metrics-remote-write", metavar="URL",
                   help="also send each host's per-second metrics to this Prometheus "
                        f"remote-write endpoint (https). Credentials come only from {USER_ENV} "
                        f"and {PASS_ENV}, or {TOKEN_ENV}")


def build_parser() -> argparse.ArgumentParser:
    # No abbreviated flags: command.txt redaction matches flags by full name.
    p = argparse.ArgumentParser(prog="whitney", description=__doc__, allow_abbrev=False)
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build hogd binaries", allow_abbrev=False)
    b.add_argument("--hogd-ref", required=True)
    b.add_argument("--arch", choices=["znver5", "znver4"], default="znver5")
    b.add_argument("--out", default=".whitney-cache")

    r = sub.add_parser("run", help="run one scenario at one executor count", allow_abbrev=False)
    r.add_argument("scenario")
    r.add_argument("--n", type=_positive_int, required=True)
    r.add_argument("--cores", type=_positive_int, metavar="K",
                   help="limit hog-exec on each executor host to K cores (scenarios with "
                        "exec_cores only, and required there)")
    r.add_argument("--bucket", required=True)
    r.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    r.add_argument("--keep", action="store_true", help="leave the cluster up after the run")
    _add_cluster_args(r, RUN_MAX_HOURS)

    s = sub.add_parser("sweep", help="run a full scenario sweep", allow_abbrev=False)
    s.add_argument("scenario")
    s.add_argument("--bucket", required=True)
    s.add_argument("--seed", type=int)
    s.add_argument("--keep", action="store_true", help="leave each cluster up after its repetition")
    s.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    _add_cluster_args(s, SWEEP_MAX_HOURS)

    for name in ("summarize", "chart"):
        c = sub.add_parser(name, help=f"{name} results", allow_abbrev=False)
        c.add_argument("results_root")
        c.add_argument("--batch-size", type=int, required=True)

    pub = sub.add_parser("publish", allow_abbrev=False,
                         help="copy sweep or run dirs without private details, to commit "
                              "as a reference result")
    pub.add_argument("src", nargs="+", help="sweep dir or run dir")
    pub.add_argument("--out", required=True, type=Path, help="new dir to write (must not exist)")
    pub.add_argument("--batch-size", type=_positive_int,
                     help="batch size of the summary and chart (default: the scenario's "
                          "only batch size)")
    return p


_REMOTE_FLAG = "--metrics-remote-write"


def _redact_command(raw: list[str]) -> list[str]:
    """The command line with the remote-write URL cut to its host."""
    out: list[str] = []
    for i, arg in enumerate(raw):
        if i > 0 and raw[i - 1] == _REMOTE_FLAG:
            out.append(redact_url(arg))
        elif arg.startswith(_REMOTE_FLAG + "="):
            out.append(f"{_REMOTE_FLAG}={redact_url(arg.split('=', 1)[1])}")
        else:
            out.append(arg)
    return out


def _run(args: argparse.Namespace, command: list[str], ex: Exec) -> int:
    try:
        s = load_scenario(Path(args.scenario), args.overrides)
    except (ScenarioError, OSError) as e:
        print(f"whitney: {e}", file=sys.stderr)
        return 2
    remote_write = None
    if args.metrics_remote_write is not None:
        try:  # before any build or spend
            remote_write = remote_write_from_env(args.metrics_remote_write, os.environ)
        except ValueError as e:
            print(f"whitney: {e}", file=sys.stderr)
            return 2
    opts = RunOptions(bucket=args.bucket, ssh_key_name=args.ssh_key_name,
                      operator_cidr=args.operator_cidr, ssh_identity=args.ssh_identity,
                      arch=args.arch, archive=args.archive, keep=args.keep,
                      runner_timeout=args.runner_timeout,
                      max_lifetime_minutes=math.ceil(args.max_hours * 60),
                      aws_creds=args.aws_creds,
                      instance_create_timeout=args.instance_create_timeout,
                      apply_timeout=args.apply_timeout, remote_write=remote_write)
    try:
        with _signals_raise():
            if args.command == "sweep":
                results = sweep(s, opts, ex, seed=args.seed, command=command)
            else:
                r = run_scenario(s, args.n, opts, ex, command, cores=args.cores)
    except KeyboardInterrupt as e:  # Ctrl-C, SIGTERM, SIGHUP: cleanup already ran
        try:
            print(f"whitney: interrupted ({e or 'SIGINT'})", file=sys.stderr, flush=True)
        except OSError:
            pass
        return 130
    except DestroyError as e:
        print(f"whitney: {redact(str(e))}", file=sys.stderr)
        return 1
    except ScenarioError as e:
        print(f"whitney: {e}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError) as e:  # build or archive, before any cluster
        print(f"whitney: {redact(str(e))}", file=sys.stderr)
        return 1
    if args.command == "sweep":
        for r in results:
            print(f"{r.status} {r.run_dir}")
        return 0 if all(r.status == "ok" for r in results) else 1
    if r.error:
        print(f"whitney: run failed: {r.error}", file=sys.stderr)
    print(f"{r.status} {r.run_dir}")
    return 0 if r.status == "ok" else 1


def _results(args: argparse.Namespace) -> int:
    from whitney.chart import scaling_chart
    from whitney.results import load_runs, summarize, write_summary
    root = Path(args.results_root)
    try:
        runs = load_runs(root)
        if not runs:
            raise ValueError(f"no run dirs under {root}")
        points = summarize(runs, args.batch_size)
        base = root / f"summary-b{args.batch_size}"
        if args.command == "summarize":
            write_summary(points, base)
            print((base.parent / (base.name + ".md")).read_text(), end="")
            return 0
        by_storage: dict[str, list] = {}
        for p in points:
            by_storage.setdefault(p.storage, []).append(p)
        scaling_chart(by_storage, root / f"scaling-b{args.batch_size}")
    except (ValueError, OSError) as e:
        print(f"whitney: {e}", file=sys.stderr)
        return 2
    return 0


def _publish(args: argparse.Namespace) -> int:
    from whitney.publish import PublishError, publish
    try:
        out = publish([Path(s) for s in args.src], args.out, args.batch_size)
    except (PublishError, ValueError, OSError) as e:
        print(f"whitney: {e}", file=sys.stderr)
        return 2
    for s in args.src:
        print(out / Path(s).resolve().name)
    print(out / "INDEX.md")
    return 0


def _build(args: argparse.Namespace, ex: Exec) -> int:
    from whitney.build import run_build
    try:
        path = run_build(args.hogd_ref, args.arch, Path(args.out), ex)
    except (CommandError, RuntimeError, OSError) as e:
        print(f"whitney: build failed: {redact(str(e))}", file=sys.stderr)
        return 1
    print(path)
    return 0


def main(argv: list[str] | None = None, ex: Exec | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(raw)
    if args.command in ("summarize", "chart"):
        return _results(args)
    if args.command == "publish":
        return _publish(args)
    if args.command in ("run", "sweep"):
        return _run(args, ["whitney", *_redact_command(raw)], ex or SubprocessExec())
    if args.command == "build":
        return _build(args, ex or SubprocessExec())
    raise NotImplementedError(f"whitney {args.command} is not implemented yet")


if __name__ == "__main__":
    sys.exit(main())
