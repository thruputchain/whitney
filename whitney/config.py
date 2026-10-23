"""Scenario config: loading, overrides, validation, and TOML round-trip."""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

STORAGE = ("tmpfs", "ebs", "nvme")
# Families whose instances have local NVMe instance storage (storage = "nvme").
# x8aedz is the only AMD Zen 5 family with it.
INSTANCE_STORE_FAMILIES = ("x8aedz",)
# Roles that keep data (hog-db, hog-event, hog-indexer); only these get the
# NVMe mount or the EBS data volume.
DATA_ROLES = ("db", "event", "indexer")
EBS_VOLUME_TYPES = ("gp3", "io2")
# EBS limits per volume type (AWS docs): size GiB, IOPS, throughput MiB/s.
EBS_SIZE_GIB = {"gp3": (1, 65536), "io2": (4, 65536)}
EBS_IOPS = {"gp3": (3000, 80000), "io2": (100, 256000)}
GP3_THROUGHPUT_MIBPS = (125, 2000)
# Ratio limits: AWS rejects a volume over them at RunInstances, after other
# hosts are up. gp3: 500 IOPS per GiB (3000 always allowed), 0.25 MiB/s per
# IOPS. io2: 1000 IOPS per GiB. infra/variables.tf checks the same.
GP3_IOPS_PER_GIB = 500
GP3_BASELINE_IOPS = 3000
GP3_IOPS_PER_MIBPS = 4
IO2_IOPS_PER_GIB = 1000
EXPERIMENTS = ("erc20_payments",)
# hog-exec's malloc: glibc ("system", as hogd ships) or a preloaded jemalloc.
EXEC_ALLOCATORS = ("system", "jemalloc")
# Parsed so the error can say why: its signatures cover the whole tx,
# uniquifier included, and the runner rewrites the uniquifier per block.
UNSUPPORTED_EXPERIMENTS = {
    "erc20_basic_auth_payments": "its signatures cover the uniquifier the runner rewrites",
}
MIN_TXS_PER_BLOCK = 4000  # the global uniquifier layout needs it
MIN_ACCOUNTS = 2  # hogd's loadgen needs at least two addresses
UINT32_MAX = 2**32 - 1  # whitney-runner parses these as uint32
UINT32_FIELDS = ("accounts", "txs_per_block", "warmup_blocks", "timed_blocks",
                 "connections_per_exec", "coros_per_connection")
# Replay cache: the runner gives each block's txs fresh uniquifiers, and
# hog-db caps live expiry heights per sender wallet. The per-sender layout puts
# up to 7000 of one sender's nonces on a height; the global layout (the
# original runner's) puts 4000 nonces of all senders on a height.
UNIQUIFIER_LAYOUTS = ("per-sender", "global")
SENDER_NONCES_PER_HEIGHT = 7000
GLOBAL_NONCES_PER_HEIGHT = 4000
MAX_EXPIRY_HEIGHTS = 4000
# The shipped scenarios carry this until a hogd release has the bench hook.
HOGD_REF_PLACEHOLDER = "SET-ME"
# Names that go into result and work dir paths (scenario name, hogd_ref).
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class ScenarioError(ValueError):
    pass


def _default_instance_types() -> dict[str, str]:
    return {
        "runner": "r8a.16xlarge",
        "exec": "r8a.16xlarge",
        "db": "r8a.16xlarge",
        "event": "r8a.16xlarge",
        "indexer": "r8a.16xlarge",
        "single": "m8a.48xlarge",
    }


@dataclass(frozen=True)
class Scenario:
    name: str
    hogd_ref: str
    region: str
    n_list: list[int]
    availability_zone: str = ""  # must offer every instance type of the scenario
    ami_id: str = ""  # Canonical Ubuntu 26.04 for the region
    storage: Literal["tmpfs", "ebs", "nvme"] = "tmpfs"
    indexer: bool = False
    single_host: bool = False
    instance_types: dict[str, str] = field(default_factory=_default_instance_types)
    experiment: Literal["erc20_payments"] = "erc20_payments"
    accounts: int = 100
    txs_per_block: int = 1_000_000
    # > 0: each block has txs_per_block_per_exec × N txs, so block time stays
    # about the same at every N. txs_per_block is then not used.
    txs_per_block_per_exec: int = 0
    # Core counts for hog-exec on each executor host (an axis like n_list).
    # Empty: no limit.
    exec_cores: list[int] = field(default_factory=list)
    # > 0: each block has txs_per_block_per_core × N × K txs (K = the run's
    # exec cores), so block time stays about the same at every K.
    txs_per_block_per_core: int = 0
    exec_allocator: Literal["system", "jemalloc"] = "system"  # multi-host only
    # One hog-exec per L3 cache domain on each executor host: the K cores
    # split into ceil(K / L) hog-execs of up to L cores (L = the L3 size).
    exec_per_l3: bool = False
    batch_sizes: list[int] = field(default_factory=lambda: [1, 5, 10])
    warmup_blocks: int = 0
    timed_blocks: int = 5
    connections_per_exec: int = 10
    coros_per_connection: int = 100
    uniquifier_layout: Literal["per-sender", "global"] = "per-sender"
    repetitions: int = 1
    cpu_layout: dict[str, str] = field(default_factory=dict)  # single-host only
    # The data volume of db, event, and indexer when storage = "ebs".
    ebs_volume_type: Literal["gp3", "io2"] = "gp3"
    ebs_volume_size_gib: int = 200
    ebs_iops: int = 16000
    ebs_throughput_mibps: int = 1000  # gp3 only; io2 ignores it

    def block_txs(self, n: int, cores: int | None = None) -> int:
        """Txs per block for a run with n executors of cores each (None: no limit)."""
        if self.txs_per_block_per_core > 0:
            if cores is None:
                raise ValueError("txs_per_block_per_core needs the run's exec cores")
            return self.txs_per_block_per_core * n * cores
        if self.txs_per_block_per_exec > 0:
            return self.txs_per_block_per_exec * n
        return self.txs_per_block

    def to_toml(self) -> str:
        scalars, tables = [], []
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, dict):
                lines = [f"[{f.name}]"] + [f"{_key(k)} = {_val(x)}" for k, x in v.items()]
                tables.append("\n".join(lines))
            else:
                scalars.append(f"{f.name} = {_val(v)}")
        return "\n".join(scalars) + "\n" + "".join(f"\n{t}\n" for t in tables)


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else json.dumps(k)


def _val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    return str(v)


_FIELD_TYPES: dict[str, Any] = {
    "name": str, "hogd_ref": str, "region": str, "availability_zone": str,
    "ami_id": str, "storage": str, "experiment": str, "uniquifier_layout": str,
    "exec_allocator": str,
    "indexer": bool, "single_host": bool, "exec_per_l3": bool,
    "accounts": int, "txs_per_block": int, "txs_per_block_per_exec": int,
    "txs_per_block_per_core": int, "warmup_blocks": int,
    "timed_blocks": int, "connections_per_exec": int,
    "coros_per_connection": int, "repetitions": int,
    "n_list": list, "exec_cores": list, "batch_sizes": list,
    "instance_types": dict, "cpu_layout": dict,
    "ebs_volume_type": str, "ebs_volume_size_gib": int, "ebs_iops": int,
    "ebs_throughput_mibps": int,
}
_REQUIRED = ("name", "hogd_ref", "region", "n_list")


def _check_type(key: str, value: Any) -> None:
    want = _FIELD_TYPES[key]
    ok = isinstance(value, want)
    if want is int and isinstance(value, bool):
        ok = False
    if not ok:
        raise ScenarioError(f"{key} must be {want.__name__}")
    if want is list and not all(isinstance(x, int) and not isinstance(x, bool) for x in value):
        raise ScenarioError(f"{key} must be a list of int")
    if want is dict and not all(isinstance(k, str) and isinstance(x, str) for k, x in value.items()):
        raise ScenarioError(f"{key} must map str to str")


def _parse_override(item: str) -> tuple[str, Any]:
    key, sep, raw = item.partition("=")
    key = key.strip()
    if not sep or not key:
        raise ScenarioError(f"override must be key=value: {item}")
    try:
        parsed = tomllib.loads(f"v = {raw}")
    except tomllib.TOMLDecodeError as e:
        raise ScenarioError(f"bad override value for {key}: {e}") from e
    return key, parsed["v"]


def sender_heights_estimate(txs_per_block: int, accounts: int) -> int:
    """Per-sender layout: expiry heights per sender per block, estimated.

    The runner computes it exactly from the work txs: ceil(the busiest
    sender's txs / 7000). Senders are drawn at random, so the busiest sender
    has more than the mean, txs_per_block / accounts. The 1.25 factor covers
    that. The busiest of many senders sits a few standard deviations (about
    sqrt(mean) each) above the mean: about 5% at 7000 txs per sender, less
    above. Below 5600 txs per sender the estimate is 1, and the busiest sender
    would need 25% over the mean to reach 7000.
    """
    # ceil(1.25 × txs / accounts / 7000), in integers.
    return -(-5 * txs_per_block // (4 * accounts * SENDER_NONCES_PER_HEIGHT))


def heights_per_block(s: Scenario, txs: int) -> int:
    """Expiry heights one sender gets per block of txs (an upper bound)."""
    if s.uniquifier_layout == "global":
        return -(-txs // GLOBAL_NONCES_PER_HEIGHT)
    return sender_heights_estimate(txs, s.accounts)


def check_block_size(s: Scenario, n: int, cores: int | None = None) -> None:
    """Block size limits for a run with n executors of cores each."""
    txs = s.block_txs(n, cores)
    if s.txs_per_block_per_core > 0:
        key = f"txs_per_block_per_core × N × K ({txs} at N={n}, K={cores})"
        at_n = f", {txs} txs per block at N={n}, K={cores}"
    elif s.txs_per_block_per_exec > 0:
        key = f"txs_per_block_per_exec × N ({txs} at N={n})"
        at_n = f", {txs} txs per block at N={n}"
    else:
        key, at_n = "txs_per_block", ""
    if txs < MIN_TXS_PER_BLOCK:
        raise ScenarioError(f"{key} must be >= {MIN_TXS_PER_BLOCK}")
    if txs > UINT32_MAX:
        raise ScenarioError(f"{key} must be <= {UINT32_MAX}")
    # The same rule as whitney-runner, which checks it exactly before the
    # first work block. Here the per-sender value is an estimate.
    per_block = heights_per_block(s, txs)
    blocks = s.warmup_blocks + s.timed_blocks
    if per_block * blocks >= MAX_EXPIRY_HEIGHTS:
        est = " (estimated)" if s.uniquifier_layout == "per-sender" else ""
        raise ScenarioError(
            f"replay-cache budget: {per_block} expiry heights per sender per block{est} "
            f"× {blocks} blocks = {per_block * blocks}, must be < {MAX_EXPIRY_HEIGHTS} "
            f"(uniquifier_layout {s.uniquifier_layout}{at_n})")


def has_instance_store(instance_type: str) -> bool:
    return instance_type.split(".")[0] in INSTANCE_STORE_FAMILIES


def data_roles(s: Scenario) -> list[str]:
    """Roles with a data mount: db and event, plus the indexer when it is on."""
    return [r for r in DATA_ROLES if r != "indexer" or s.indexer]


def _check_range(key: str, value: int, kind: str, lo: int, hi: int) -> None:
    if not lo <= value <= hi:
        raise ScenarioError(f"{key} for {kind} must be {lo}-{hi}")


def _validate_storage(s: Scenario) -> None:
    if s.storage not in STORAGE:
        raise ScenarioError(f"storage must be one of {', '.join(STORAGE)}")
    if s.ebs_volume_type not in EBS_VOLUME_TYPES:
        raise ScenarioError(f"ebs_volume_type must be one of {', '.join(EBS_VOLUME_TYPES)}")
    v = s.ebs_volume_type
    _check_range("ebs_volume_size_gib", s.ebs_volume_size_gib, v, *EBS_SIZE_GIB[v])
    _check_range("ebs_iops", s.ebs_iops, v, *EBS_IOPS[v])
    if v == "gp3":
        _check_range("ebs_throughput_mibps", s.ebs_throughput_mibps, v, *GP3_THROUGHPUT_MIBPS)
    got = f"got {s.ebs_iops} IOPS for {s.ebs_volume_size_gib} GiB"
    if v == "gp3" and s.ebs_iops > max(GP3_BASELINE_IOPS, GP3_IOPS_PER_GIB * s.ebs_volume_size_gib):
        raise ScenarioError(f"ebs_iops for gp3 must be <= max({GP3_BASELINE_IOPS}, "
                            f"{GP3_IOPS_PER_GIB} x ebs_volume_size_gib): {got}")
    if v == "gp3" and s.ebs_throughput_mibps * GP3_IOPS_PER_MIBPS > s.ebs_iops:
        raise ScenarioError("ebs_throughput_mibps for gp3 must be <= ebs_iops / 4 "
                            f"(0.25 MiB/s per IOPS): got {s.ebs_throughput_mibps} MiB/s "
                            f"for {s.ebs_iops} IOPS")
    if v == "io2" and s.ebs_iops > IO2_IOPS_PER_GIB * s.ebs_volume_size_gib:
        raise ScenarioError(f"ebs_iops for io2 must be <= {IO2_IOPS_PER_GIB} x "
                            f"ebs_volume_size_gib: {got}")
    if s.storage == "nvme" and not s.single_host:
        roles = data_roles(s)
        bad = [f"{r}={s.instance_types[r]}" for r in roles
               if not has_instance_store(s.instance_types[r])]
        if bad:
            raise ScenarioError(
                f"storage nvme needs instance-store types for {'/'.join(roles)} "
                f"(e.g. x8aedz.12xlarge); got {', '.join(bad)}")


def _validate(s: Scenario) -> None:
    if not SAFE_NAME.fullmatch(s.name):
        raise ScenarioError(
            f"name must match {SAFE_NAME.pattern} (it names result dirs): {s.name!r}")
    if s.hogd_ref == HOGD_REF_PLACEHOLDER:
        raise ScenarioError("hogd_ref must be set to a hogd tag or commit that contains "
                            "the HOGD_EXTRA_BENCH_DIRS hook (for example --set hogd_ref='<ref>')")
    _validate_storage(s)
    if s.experiment in UNSUPPORTED_EXPERIMENTS:
        raise ScenarioError(f"experiment {s.experiment} is not supported: "
                            f"{UNSUPPORTED_EXPERIMENTS[s.experiment]}")
    if s.experiment not in EXPERIMENTS:
        raise ScenarioError(f"experiment must be one of {', '.join(EXPERIMENTS)}")
    for key in UINT32_FIELDS:
        if getattr(s, key) > UINT32_MAX:
            raise ScenarioError(f"{key} must be <= {UINT32_MAX}")
    if any(b > UINT32_MAX for b in s.batch_sizes):
        raise ScenarioError(f"batch_sizes values must be <= {UINT32_MAX}")
    if s.txs_per_block_per_exec < 0:
        raise ScenarioError("txs_per_block_per_exec must be >= 0")
    if s.txs_per_block_per_exec > 0 and s.txs_per_block != Scenario.txs_per_block:
        raise ScenarioError("set txs_per_block or txs_per_block_per_exec, not both")
    if s.txs_per_block_per_core < 0:
        raise ScenarioError("txs_per_block_per_core must be >= 0")
    if s.txs_per_block_per_core > 0:
        if s.txs_per_block != Scenario.txs_per_block:
            raise ScenarioError("set txs_per_block or txs_per_block_per_core, not both")
        if s.txs_per_block_per_exec > 0:
            raise ScenarioError("set txs_per_block_per_exec or txs_per_block_per_core, not both")
        if not s.exec_cores:
            raise ScenarioError("txs_per_block_per_core requires exec_cores")
    if s.warmup_blocks < 0:
        raise ScenarioError("warmup_blocks must be >= 0")
    if s.timed_blocks < 1:
        raise ScenarioError("timed_blocks must be >= 1")
    if s.uniquifier_layout not in UNIQUIFIER_LAYOUTS:
        raise ScenarioError(f"uniquifier_layout must be one of {', '.join(UNIQUIFIER_LAYOUTS)}")
    if s.accounts < MIN_ACCOUNTS:
        raise ScenarioError(f"accounts must be >= {MIN_ACCOUNTS}")
    # Block size grows with N (and K) when txs_per_block_per_exec (or
    # txs_per_block_per_core) is set: check both ends of n_list × exec_cores
    # (both lists are checked below).
    ns = [n for n in s.n_list if n >= 1]
    ks = [k for k in s.exec_cores if k >= 1]
    if ns and (ks or not s.exec_cores):
        check_block_size(s, min(ns), min(ks) if ks else None)
        check_block_size(s, max(ns), max(ks) if ks else None)
    if s.single_host and s.storage != "tmpfs":
        raise ScenarioError("single_host requires storage = tmpfs")
    if s.cpu_layout and not s.single_host:
        raise ScenarioError("cpu_layout requires single_host = true")
    if s.exec_cores and s.single_host:
        raise ScenarioError("exec_cores requires single_host = false (use cpu_layout)")
    if s.exec_per_l3 and s.single_host:
        raise ScenarioError("exec_per_l3 requires single_host = false")
    if s.exec_per_l3 and not s.exec_cores:
        raise ScenarioError("exec_per_l3 requires exec_cores")
    if s.exec_allocator not in EXEC_ALLOCATORS:
        raise ScenarioError(f"exec_allocator must be one of {', '.join(EXEC_ALLOCATORS)}")
    if s.exec_allocator == "jemalloc" and s.single_host:
        raise ScenarioError('exec_allocator = "jemalloc" requires single_host = false')
    # Same count guards as whitney-runner, so a bad --set fails here.
    for key in ("connections_per_exec", "coros_per_connection"):
        if getattr(s, key) <= 0:
            raise ScenarioError(f"{key} must be > 0")
    if s.repetitions < 1:
        raise ScenarioError("repetitions must be >= 1")
    if not s.batch_sizes:
        raise ScenarioError("batch_sizes must not be empty")
    if any(b < 1 for b in s.batch_sizes):
        raise ScenarioError("batch_sizes values must be >= 1")
    repeated = sorted({b for b in s.batch_sizes if s.batch_sizes.count(b) > 1})
    if repeated:  # each batch size is one fresh-genesis run with its own trial rows
        raise ScenarioError(f"batch_sizes must not repeat: {', '.join(map(str, repeated))}")
    if not s.n_list:
        raise ScenarioError("n_list must not be empty")
    if any(n < 1 for n in s.n_list):
        raise ScenarioError("n_list values must be >= 1")
    dups = sorted({n for n in s.n_list if s.n_list.count(n) > 1})
    if dups:  # each (N, rep) is one run dir
        raise ScenarioError(f"n_list must not repeat a value: {', '.join(map(str, dups))}")
    if any(k < 1 for k in s.exec_cores):
        raise ScenarioError("exec_cores values must be >= 1")
    dups = sorted({k for k in s.exec_cores if s.exec_cores.count(k) > 1})
    if dups:  # each (N, K, rep) is one run dir
        raise ScenarioError(f"exec_cores must not repeat a value: {', '.join(map(str, dups))}")


def load_scenario(path: Path, overrides: list[str]) -> Scenario:
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ScenarioError(f"cannot parse {path}: {e}") from e
    for item in overrides:
        key, value = _parse_override(item)
        data[key] = value
    for key in data:
        if key not in _FIELD_TYPES:
            raise ScenarioError(f"unknown scenario key: {key}")
    for key in _REQUIRED:
        if key not in data:
            raise ScenarioError(f"missing required scenario key: {key}")
    for key, value in data.items():
        _check_type(key, value)
    if "instance_types" in data:
        data["instance_types"] = {**_default_instance_types(), **data["instance_types"]}
    s = Scenario(**data)
    _validate(s)
    return s
