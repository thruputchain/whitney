"""Publish: a clean copy of sweep or run dirs to commit as a reference result.

Copies an allowlist of small files from each run dir, replaces private
details (addresses, host names, bucket names, home paths, ARNs) with
placeholders, writes the summary and chart again from the copied files,
and writes INDEX.md. Fails if any private detail is still in the output.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from whitney.proc import redact as redact_url_queries
from whitney.results import Run, load_runs, summarize, write_summary

# Run dir files that are published. host-facts/*.json is published too.
RUN_FILES = ("scenario.resolved.toml", "manifest.json", "archive.sha256", "status.json",
             "trials.jsonl", "exec-setup.json", "roles.json", "metrics.json", "runner.log",
             "command.txt")
HOST_FACTS = "host-facts"
SWEEP_FILES = ("sweep.json",)
# Written again from the published files, so never copied.
_REGENERATED = re.compile(r"^(summary|scaling)-b\d+\.(csv|md|svg|png)$")
_RUN_NAME = re.compile(r"-n\d+(?:-c\d+)?-r\d+$")

_KEEP_IPS = {"127.0.0.1", "0.0.0.0"}
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")
_EC2_HOST = re.compile(r"(?<![A-Za-z0-9])ip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}"
                       r"(?:\.[A-Za-z0-9-]+)*")
_BUCKET = re.compile(r"whitney-runs-\d{12}-[a-z0-9-]+")
_S3_URL = re.compile(r"s3://[^\s'\"]+"
                     r"|https?://[A-Za-z0-9.<>-]*\bs3[.-][A-Za-z0-9.-]*amazonaws\.com[^\s'\"]*")
_HOME = re.compile(r"/(?:Users|home)/[^/\s'\"<>]+")
_ARN = re.compile(r"arn:aws[a-z-]*:[^\s'\"]+")
_SSH_KEY_NAME = re.compile(r"(--ssh-key-name[ =])(?!<key-name>)('[^']*'|\S+)")


def _ip(m: re.Match) -> str:
    text = m.group(0)
    if text in _KEEP_IPS or any(int(o) > 255 for o in text.split(".")):
        return text
    return "<ip>"


def redact(text: str) -> str:
    """text with private details replaced by placeholders."""
    text = _S3_URL.sub("<s3-url>", text)  # first, so it takes the query string too
    text = redact_url_queries(text)
    text = _ARN.sub("<arn>", text)
    text = _BUCKET.sub("<bucket>", text)
    text = _EC2_HOST.sub("<host>", text)
    text = _IPV4.sub(_ip, text)
    text = _HOME.sub("<home>", text)
    return _SSH_KEY_NAME.sub(r"\1<key-name>", text)


# What the leftover scan looks for, by name.
LEFTOVERS: tuple[tuple[str, re.Pattern], ...] = (
    ("IPv4 address", _IPV4),
    ("EC2 host name", _EC2_HOST),
    ("bucket name", re.compile(r"whitney-runs-\d{12}")),
    ("S3 URL", _S3_URL),
    ("home path", _HOME),
    ("ARN", _ARN),
    ("presigned URL", re.compile(r"X-Amz-(?:Signature|Credential|Security-Token)", re.I)),
    ("SSH key name", re.compile(r"--ssh-key-name[ =](?!<key-name>)")),
)


class PublishError(Exception):
    pass


# A JSON string literal, as it is in the file.
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


def _redact_json_text(text: str) -> str:
    """Redact only the string literals (keys and values) of JSON text.
    Numbers, spacing, and order stay byte for byte."""
    def sub(m: re.Match) -> str:
        s = json.loads(m.group(0))
        r = redact(s)
        return m.group(0) if r == s else json.dumps(r)
    return _JSON_STRING.sub(sub, text)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError(f"duplicate keys after redaction: {sorted(keys)}")
    return dict(pairs)


def _check_json(text: str, lines: bool) -> None:
    docs = [x for x in text.splitlines() if x.strip()] if lines else [text]
    for doc in docs:
        json.loads(doc, object_pairs_hook=_no_duplicate_keys)


def copy_redacted(src: Path, dst: Path) -> None:
    """Copy one text file with redact applied. A .json or .jsonl file must
    parse before and after (only its strings change)."""
    text = src.read_text(errors="surrogateescape")
    kind = src.suffix
    if kind in (".json", ".jsonl"):
        try:
            _check_json(text, kind == ".jsonl")
            out = _redact_json_text(text)
            _check_json(out, kind == ".jsonl")
        except ValueError as e:
            raise PublishError(f"{src}: not valid JSON before or after redaction: {e}") from None
    else:
        out = redact(text)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(out, errors="surrogateescape")


def _rel(p: Path, root: Path) -> str:
    rel = p.relative_to(root).as_posix()
    return rel + "/" if p.is_dir() else rel


def publish_run(src: Path, dst: Path) -> list[str]:
    """Copy the allowlist of run dir src to dst. Returns what was left out."""
    dst.mkdir(parents=True)
    keep: list[Path] = [src / f for f in RUN_FILES if (src / f).is_file()]
    facts = src / HOST_FACTS
    if facts.is_dir():
        keep += sorted(facts.glob("*.json"))
    for f in keep:
        copy_redacted(f, dst / f.relative_to(src))
    kept = set(keep) | ({facts} if facts.is_dir() else set())
    skipped = [p for p in sorted(src.iterdir()) if p not in kept]
    if facts.is_dir():
        skipped += [p for p in sorted(facts.iterdir()) if p not in kept]
    return sorted(_rel(p, src) for p in skipped)


def _is_run_dir(p: Path) -> bool:
    return (p / "status.json").is_file() or bool(_RUN_NAME.search(p.name))


def _publish_dir(src: Path, dst: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Copy a sweep or run dir. Returns (left out per run, left out of the sweep)."""
    if _is_run_dir(src):
        return {src.name: publish_run(src, dst)}, []
    runs = [p for p in sorted(src.iterdir()) if p.is_dir() and _is_run_dir(p)]
    if not runs and not (src / "sweep.json").is_file():
        raise PublishError(f"not a sweep dir or a run dir: {src}")
    dst.mkdir(parents=True)
    for f in SWEEP_FILES:
        if (src / f).is_file():
            copy_redacted(src / f, dst / f)
    skipped = {r.name: publish_run(r, dst / r.name) for r in runs}
    rest = [_rel(p, src) for p in sorted(src.iterdir())
            if p not in runs and p.name not in SWEEP_FILES and not _REGENERATED.match(p.name)]
    return skipped, rest


def _batch_size(runs: list[Run], batch_size: int | None, where: Path) -> int:
    sizes = sorted({b for r in runs for b in r.scenario.batch_sizes})
    if batch_size is None:
        if len(sizes) != 1:
            raise PublishError(f"{where}: the scenario has batch sizes {sizes}: "
                               "give one with --batch-size")
        return sizes[0]
    if batch_size not in sizes:
        raise PublishError(f"{where}: batch size {batch_size} is not in the scenario {sizes}")
    return batch_size


def _summarize(root: Path, batch_size: int | None) -> tuple[list[Run], int, str | None]:
    """Write the summary and chart into root from its run dirs.
    Returns (runs, batch size, why there is no chart or None)."""
    from whitney.chart import scaling_chart
    runs = load_runs(root)
    if not runs:
        raise PublishError(f"no run dirs with status.json under {root}")
    b = _batch_size(runs, batch_size, root)
    try:
        points = summarize(runs, b)
    except ValueError as e:
        raise PublishError(f"{root}: {e}") from None
    write_summary(points, root / f"summary-b{b}")
    by_storage: dict[str, list] = {}
    for p in points:
        by_storage.setdefault(p.storage, []).append(p)
    try:
        scaling_chart(by_storage, root / f"scaling-b{b}")
    except ValueError as e:  # for example single-host runs: summary table only
        return runs, b, str(e)
    return runs, b, None


def scan_leftovers(root: Path) -> list[str]:
    """file:line: what, for each private detail still in a text file under root."""
    hits = []
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        if f.suffix == ".png":
            continue
        text = f.read_text(errors="surrogateescape")
        for i, line in enumerate(text.splitlines(), start=1):
            for what, pat in LEFTOVERS:
                for m in pat.finditer(line):
                    if what == "IPv4 address" and _ip(m) != "<ip>":
                        continue
                    hits.append(f"{f.relative_to(root)}:{i}: {what}: {m.group(0)}")
    return hits


def _manifest(runs: list[Run]) -> dict[str, set[str]]:
    keys = ("hogd_ref", "hogd_commit", "whitney_commit")
    found: dict[str, set[str]] = {k: set() for k in keys}
    for r in runs:
        try:
            doc = json.loads((r.dir / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        for k in keys:
            if isinstance(doc, dict) and doc.get(k) is not None:
                found[k].add(str(doc[k]))
    return found


def _values(runs: list[Run], get: Callable[[Run], object]) -> str:
    seen: list[str] = []
    for r in runs:
        v = get(r)
        text = json.dumps(v) if isinstance(v, (list, bool)) or v is None else str(v)
        if text not in seen:
            seen.append(text)
    return ", ".join(f"`{v}`" for v in seen) or "-"


def _aborted(root: Path) -> list[str]:
    try:
        doc = json.loads((root / "sweep.json").read_text())
    except (OSError, ValueError):
        return []
    if isinstance(doc, dict) and doc.get("aborted"):
        return ["- The sweep stopped early. The reason is `abort_reason` in `sweep.json`."]
    return []


def _instance_types(r: Run) -> str:
    s = r.scenario
    roles = (["single"] if s.single_host else
             ["runner", "exec", "db", "event", *(["indexer"] if s.indexer else [])])
    return ", ".join(f"{role} {s.instance_types[role]}" for role in roles)


def _skipped_lines(skipped: dict[str, list[str]]) -> list[str]:
    groups: dict[tuple[str, ...], list[str]] = {}
    for run, what in skipped.items():
        groups.setdefault(tuple(what), []).append(run)
    if len(groups) == 1:
        (what, names), = groups.items()
        n = len(names)
        return [f"Left out of {'the run' if n == 1 else f'each of the {n} runs'}: "
                f"{', '.join(f'`{w}`' for w in what) or 'nothing'}."]
    lines = ["Left out per run:", ""]
    lines += [f"- {run}: {', '.join(f'`{w}`' for w in what) or 'nothing'}"
              for run, what in skipped.items()]
    return lines


def _section(name: str, root: Path, runs: list[Run], b: int, no_chart: str | None,
             skipped: dict[str, list[str]], rest: list[str]) -> list[str]:
    m = _manifest(runs)
    ok = sum(r.status == "ok" for r in runs)
    failed = sum(r.status == "failed" for r in runs)
    invalid = sum(r.status == "invalid" for r in runs)

    def one(k: str) -> str:
        return ", ".join(f"`{v}`" for v in sorted(m[k])) or "-"
    lines = [f"## {name}", "",
             f"- Scenario: {_values(runs, lambda r: r.scenario.name)}",
             f"- hogd_ref: {one('hogd_ref')}",
             f"- hogd_commit: {one('hogd_commit')}",
             f"- whitney_commit: {one('whitney_commit')}",
             f"- Archive SHA-256: {_values(runs, lambda r: r.archive_sha256 or '-')}",
             f"- Instance types: {_values(runs, _instance_types)}",
             f"- Storage: {_values(runs, lambda r: r.scenario.storage)}",
             f"- exec_allocator: {_values(runs, lambda r: r.scenario.exec_allocator)}",
             f"- exec_per_l3: {_values(runs, lambda r: r.scenario.exec_per_l3)}",
             f"- exec_cores: {_values(runs, lambda r: r.scenario.exec_cores)}",
             f"- n_list: {_values(runs, lambda r: r.scenario.n_list)}",
             f"- repetitions: {_values(runs, lambda r: r.scenario.repetitions)}",
             f"- Runs: {ok} ok, {failed + invalid} failed"
             + (f" ({invalid} invalid)" if invalid else ""),
             *_aborted(root),
             "", f"Summary at batch size {b} (`{name}/summary-b{b}.md`):", "",
             (root / f"summary-b{b}.md").read_text().rstrip(), ""]
    if no_chart:
        lines += [f"No chart: {no_chart}", ""]
    else:
        lines += [f"![Scaling chart, batch size {b}]({name}/scaling-b{b}.png)", ""]
    lines += _skipped_lines(skipped)
    if rest:
        lines += ["", "Left out of the sweep dir: " + ", ".join(f"`{w}`" for w in rest) + "."]
    return lines + [""]


_HEADER = [
    "# Published Whitney results", "",
    "Each section is one sweep or run, copied by `whitney publish`. The summary and "
    "chart are made again from the published files.", "",
    "Left out: sar data, daemon logs, raw metrics (`metrics/`), and `aws.json`. "
    "IP addresses, EC2 host names, bucket names, S3 URLs, home paths, ARNs, and SSH key "
    "names are replaced with placeholders (`<ip>`, `<host>`, `<bucket>`, `<s3-url>`, "
    "`<home>`, `<arn>`, `<key-name>`).", "",
]


def publish(srcs: list[Path], out: Path, batch_size: int | None = None) -> Path:
    """Publish each sweep or run dir in srcs to out/<its name>/ and write
    out/INDEX.md. out must not exist. Work goes to a temp dir next to out,
    which becomes out only when the leftover scan is clean."""
    if out.exists():
        raise PublishError(f"--out exists: {out}")
    names = [s.resolve().name for s in srcs]
    for s in srcs:
        if not s.is_dir():
            raise PublishError(f"not a dir: {s}")
    if len(set(names)) != len(names):
        raise PublishError(f"two sources have the same name: {names}")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / f".{out.name}.publish-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        index = list(_HEADER)
        for src, name in zip(srcs, names):
            skipped, rest = _publish_dir(src, tmp / name)
            runs, b, no_chart = _summarize(tmp / name, batch_size)
            index += _section(name, tmp / name, runs, b, no_chart, skipped, rest)
        (tmp / "INDEX.md").write_text("\n".join(index).rstrip() + "\n")
        hits = scan_leftovers(tmp)
        if hits:
            raise PublishError("private details left after redaction:\n" + "\n".join(hits))
        tmp.rename(out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return out
