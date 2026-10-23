import re
from dataclasses import replace
from pathlib import Path

import pytest

from whitney.config import ScenarioError, load_scenario, sender_heights_estimate
from whitney.sweep import validate_cpu_layout

BASE = 'name="x"\nhogd_ref="v0.1.31"\nregion="us-east-1"\nn_list=[1]'
CORES = ["exec_cores=[1, 2, 4]"]


def write(tmp_path, text, name="s.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def base(tmp_path):
    return write(tmp_path, BASE)


def test_defaults_match_todays_job(tmp_path):
    s = load_scenario(base(tmp_path), [])
    assert (s.txs_per_block, s.batch_sizes, s.connections_per_exec,
            s.coros_per_connection, s.accounts, s.warmup_blocks, s.timed_blocks) == \
           (1_000_000, [1, 5, 10], 10, 100, 100, 0, 5)
    assert s.storage == "tmpfs" and s.indexer is False and s.experiment == "erc20_payments"
    assert s.uniquifier_layout == "per-sender"
    assert (s.exec_cores, s.exec_per_l3, s.exec_allocator) == ([], False, "system")
    assert s.instance_types == {"runner": "r8a.16xlarge", "exec": "r8a.16xlarge",
                                "db": "r8a.16xlarge", "event": "r8a.16xlarge",
                                "indexer": "r8a.16xlarge", "single": "m8a.48xlarge"}
    assert (s.ebs_volume_type, s.ebs_volume_size_gib, s.ebs_iops, s.ebs_throughput_mibps) == \
           ("gp3", 200, 16000, 1000)


def test_override_parses_toml_values(tmp_path):
    s = load_scenario(base(tmp_path), ["accounts=10", "batch_sizes=[5]", "storage='ebs'"])
    assert (s.accounts, s.batch_sizes, s.storage) == (10, [5], "ebs")


@pytest.mark.parametrize("ov,msg", [
    (["uniquifier_layout='sender'"], "uniquifier_layout must be one of per-sender, global"),
    # global: ceil(txs_per_block / 4000) heights per block, exact upper bound.
    (["uniquifier_layout='global'", "timed_blocks=15", "warmup_blocks=1"],
     "replay-cache budget: 250 expiry heights per sender per block × 16 blocks = 4000, "
     "must be < 4000 (uniquifier_layout global)"),
    (["uniquifier_layout='global'", "txs_per_block=1064001", "warmup_blocks=0",
      "timed_blocks=15"],
     "replay-cache budget: 267 expiry heights per sender per block × 15 blocks = 4005, "
     "must be < 4000 (uniquifier_layout global)"),
    # per-sender: ceil(1.25 × 1000000 / 2 / 7000) = 90 heights per block, estimated.
    (["accounts=2", "timed_blocks=45"],
     "replay-cache budget: 90 expiry heights per sender per block (estimated) × 45 blocks "
     "= 4050, must be < 4000 (uniquifier_layout per-sender)"),
    (["txs_per_block=1000"], "txs_per_block must be >= 4000"),
    (["storage='disk'"], "storage must be one of tmpfs, ebs, nvme"),
    (["storage='nvme'"], "storage nvme needs instance-store types for db/event "
                         "(e.g. x8aedz.12xlarge); got db=r8a.16xlarge, event=r8a.16xlarge"),
    (["storage='nvme'", "instance_types={db='x8aedz.12xlarge'}"],
     "storage nvme needs instance-store types for db/event (e.g. x8aedz.12xlarge); "
     "got event=r8a.16xlarge"),
    (["storage='nvme'", "indexer=true",
      "instance_types={db='x8aedz.12xlarge', event='x8aedz.12xlarge'}"],
     "storage nvme needs instance-store types for db/event/indexer (e.g. x8aedz.12xlarge); "
     "got indexer=r8a.16xlarge"),
    (["ebs_volume_type='gp2'"], "ebs_volume_type must be one of gp3, io2"),
    (["ebs_volume_size_gib=0"], "ebs_volume_size_gib for gp3 must be 1-65536"),
    (["ebs_volume_size_gib=65537"], "ebs_volume_size_gib for gp3 must be 1-65536"),
    (["ebs_volume_type='io2'", "ebs_volume_size_gib=3"], "ebs_volume_size_gib for io2 must be 4-65536"),
    (["ebs_iops=2999"], "ebs_iops for gp3 must be 3000-80000"),
    (["ebs_iops=80001"], "ebs_iops for gp3 must be 3000-80000"),
    (["ebs_volume_type='io2'", "ebs_iops=99"], "ebs_iops for io2 must be 100-256000"),
    (["ebs_volume_type='io2'", "ebs_iops=256001"], "ebs_iops for io2 must be 100-256000"),
    (["ebs_throughput_mibps=124"], "ebs_throughput_mibps for gp3 must be 125-2000"),
    (["ebs_throughput_mibps=2001"], "ebs_throughput_mibps for gp3 must be 125-2000"),
    (["ebs_iops=true"], "ebs_iops must be int"),
    (["ebs_iops=3000"], "ebs_throughput_mibps for gp3 must be <= ebs_iops / 4 "
                        "(0.25 MiB/s per IOPS): got 1000 MiB/s for 3000 IOPS"),
    (["ebs_volume_size_gib=20"], "ebs_iops for gp3 must be <= max(3000, 500 x "
                                 "ebs_volume_size_gib): got 16000 IOPS for 20 GiB"),
    (["ebs_volume_type='io2'", "ebs_volume_size_gib=10"],
     "ebs_iops for io2 must be <= 1000 x ebs_volume_size_gib: got 16000 IOPS for 10 GiB"),
    (["experiment='erc721'"], "experiment must be one of erc20_payments"),
    (["experiment='erc20_basic_auth_payments'"],
     "experiment erc20_basic_auth_payments is not supported: its signatures cover the "
     "uniquifier the runner rewrites"),
    (["single_host=true", "storage='nvme'"], "single_host requires storage = tmpfs"),
    (["unknown_key=1"], "unknown scenario key: unknown_key"),
    (["accounts='ten'"], "accounts must be int"),
    (["indexer=1"], "indexer must be bool"),
    (["accounts=1"], "accounts must be >= 2"),
    (["accounts=4294967296"], "accounts must be <= 4294967295"),
    (["txs_per_block=4294967296"], "txs_per_block must be <= 4294967295"),
    (["batch_sizes=[1, 4294967296]"], "batch_sizes values must be <= 4294967295"),
    (["warmup_blocks=4294967296"], "warmup_blocks must be <= 4294967295"),
    (["timed_blocks=4294967296"], "timed_blocks must be <= 4294967295"),
    (["connections_per_exec=4294967296"], "connections_per_exec must be <= 4294967295"),
    (["coros_per_connection=4294967296"], "coros_per_connection must be <= 4294967295"),
    (["connections_per_exec=0"], "connections_per_exec must be > 0"),
    (["warmup_blocks=-5", "timed_blocks=20"], "warmup_blocks must be >= 0"),
    (["timed_blocks=0"], "timed_blocks must be >= 1"),
    (["batch_sizes=[]"], "batch_sizes must not be empty"),
    (["batch_sizes=[1, 0]"], "batch_sizes values must be >= 1"),
    (["batch_sizes=[10, 1, 10, 1]"], "batch_sizes must not repeat: 1, 10"),
    (["n_list=[1, 0]"], "n_list values must be >= 1"),
    (["n_list=[1, 2, 1]"], "n_list must not repeat a value: 1"),
    (["name='../x'"], "name must match [A-Za-z0-9][A-Za-z0-9._-]* (it names result dirs): '../x'"),
    (["name=''"], "name must match [A-Za-z0-9][A-Za-z0-9._-]* (it names result dirs): ''"),
    # txs_per_block_per_exec: the block grows with N.
    (["txs_per_block_per_exec=-1"], "txs_per_block_per_exec must be >= 0"),
    (["txs_per_block_per_exec=1000000", "txs_per_block=2000000"],
     "set txs_per_block or txs_per_block_per_exec, not both"),
    (["txs_per_block_per_exec=3999"],
     "txs_per_block_per_exec × N (3999 at N=1) must be >= 4000"),
    (["txs_per_block_per_exec=4294967295", "n_list=[1, 2]", "accounts=4000000000"],
     "txs_per_block_per_exec × N (8589934590 at N=2) must be <= 4294967295"),
    # 2 accounts: S = ceil(1.25 × 2M × 4 / 2 / 7000) = 715 at N=4; × 6 blocks = 4290.
    (["txs_per_block_per_exec=2000000", "accounts=2", "n_list=[1, 4]", "timed_blocks=6"],
     "replay-cache budget: 715 expiry heights per sender per block (estimated) × 6 blocks "
     "= 4290, must be < 4000 (uniquifier_layout per-sender, 8000000 txs per block at N=4)"),
    # exec_cores and txs_per_block_per_core: the block grows with N and K.
    (["exec_cores=[1, 0]"], "exec_cores values must be >= 1"),
    (["exec_cores=[2, 1, 2]"], "exec_cores must not repeat a value: 2"),
    (["exec_cores=['a']"], "exec_cores must be a list of int"),
    (["exec_cores=[4]", "single_host=true"], "exec_cores requires single_host = false"),
    (["txs_per_block_per_core=-1"], "txs_per_block_per_core must be >= 0"),
    (["txs_per_block_per_core=100000"], "txs_per_block_per_core requires exec_cores"),
    ([*CORES, "txs_per_block_per_core=100000", "txs_per_block=2000000"],
     "set txs_per_block or txs_per_block_per_core, not both"),
    ([*CORES, "txs_per_block_per_core=100000", "txs_per_block_per_exec=100000"],
     "set txs_per_block_per_exec or txs_per_block_per_core, not both"),
    # The small end of n_list × exec_cores: N=1, K=1.
    (["n_list=[1, 2]", *CORES, "txs_per_block_per_core=2000"],
     "txs_per_block_per_core × N × K (2000 at N=1, K=1) must be >= 4000"),
    # The large end: N=2, K=4.
    (["n_list=[1, 2]", *CORES, "txs_per_block_per_core=600000000", "accounts=4000000000"],
     "txs_per_block_per_core × N × K (4800000000 at N=2, K=4) must be <= 4294967295"),
    # 2 accounts: S = ceil(1.25 × 1M × 8 / 2 / 7000) = 715 at K=8; × 6 blocks = 4290.
    (["exec_cores=[1, 8]", "txs_per_block_per_core=1000000", "accounts=2", "timed_blocks=6"],
     "replay-cache budget: 715 expiry heights per sender per block (estimated) × 6 blocks "
     "= 4290, must be < 4000 (uniquifier_layout per-sender, 8000000 txs per block at N=1, K=8)"),
    # exec_allocator and exec_per_l3: multi-host only.
    (["exec_allocator='tcmalloc'"], "exec_allocator must be one of system, jemalloc"),
    (["exec_allocator=1"], "exec_allocator must be str"),
    (["exec_allocator='jemalloc'", "single_host=true"],
     'exec_allocator = "jemalloc" requires single_host = false'),
    (["exec_per_l3=true"], "exec_per_l3 requires exec_cores"),
    (["exec_per_l3=true", "single_host=true"], "exec_per_l3 requires single_host = false"),
    (["exec_per_l3=1"], "exec_per_l3 must be bool"),
])
def test_guards(tmp_path, ov, msg):
    with pytest.raises(ScenarioError, match=re.escape(msg)):
        load_scenario(base(tmp_path), ov)


@pytest.mark.parametrize("ov", [
    ["accounts=4294967295"],
    ["uniquifier_layout='global'", "txs_per_block=1000000", "warmup_blocks=1",
     "timed_blocks=14"],  # 250 x 15 = 3750
    ["uniquifier_layout='global'", "txs_per_block=1064000", "warmup_blocks=0",
     "timed_blocks=15"],  # 266 x 15 = 3990
    # per-sender has no 15-block cap: 100 accounts is 2 heights per block.
    ["warmup_blocks=1", "timed_blocks=99"],
    ["accounts=2", "timed_blocks=44"],  # 90 x 44 = 3960
    ["accounts=10000", "timed_blocks=3999"],  # 1 x 3999
    ["storage='nvme'", "instance_types={db='x8aedz.12xlarge', event='x8aedz.large'}"],
    ["storage='nvme'", "indexer=true", "instance_types={db='x8aedz.12xlarge', "
     "event='x8aedz.12xlarge', indexer='x8aedz.12xlarge'}"],
    # gp3 ratio limits at their edges; 3000 IOPS is allowed on any size.
    ["ebs_volume_size_gib=1", "ebs_iops=3000", "ebs_throughput_mibps=750"],
    ["ebs_volume_size_gib=32", "ebs_iops=16000", "ebs_throughput_mibps=1000"],
    ["ebs_volume_type='io2'", "ebs_volume_size_gib=16", "ebs_iops=16000"],
    # io2 has no throughput knob: the gp3 value is ignored, not checked.
    ["storage='ebs'", "ebs_volume_type='io2'", "ebs_iops=64000", "ebs_throughput_mibps=0"],
])
def test_guard_boundaries_pass(tmp_path, ov):
    load_scenario(base(tmp_path), ov)


@pytest.mark.parametrize("txs,accounts,want", [
    (1_000_000, 100, 2),       # 10,000 per sender, x 1.25 = 12,500
    (1_000_000, 10_000, 1),    # 100 per sender
    (1_000_000, 10, 18),       # 100,000 per sender, x 1.25 = 125,000
    (100_000, 10, 2),          # the smoke scenarios
    (5_600, 1, 1),             # 7,000 after x 1.25: still one bucket
    (5_601, 1, 2),
])
def test_sender_heights_estimate(txs, accounts, want):
    assert sender_heights_estimate(txs, accounts) == want


def test_missing_n_list(tmp_path):
    p = write(tmp_path, 'name="x"\nhogd_ref="main"\nregion="us-east-1"')
    with pytest.raises(ScenarioError, match="missing required scenario key: n_list"):
        load_scenario(p, [])


@pytest.mark.parametrize("ov", [
    ["accounts=10"],
    ["single_host=true", "storage='tmpfs'", "cpu_layout={exec='0-3'}"],
    # scenario.resolved.toml carries every field, txs_per_block at its default.
    ["txs_per_block_per_exec=1750000"],
    [*CORES, "txs_per_block_per_core=29000", "exec_per_l3=true", "exec_allocator='jemalloc'"],
], ids=["plain", "cpu_layout", "per_exec", "cores_l3_jemalloc"])
def test_round_trip(tmp_path, ov):
    s = load_scenario(base(tmp_path), ov)
    assert load_scenario(write(tmp_path, s.to_toml(), "rt.toml"), []) == s


def test_block_txs_per_exec_scales_with_n(tmp_path):
    s = load_scenario(base(tmp_path), ["txs_per_block_per_exec=1750000", "n_list=[1, 8, 16]"])
    assert [s.block_txs(n) for n in (1, 8, 16)] == [1_750_000, 14_000_000, 28_000_000]
    assert load_scenario(base(tmp_path), []).block_txs(16) == 1_000_000


def test_block_txs_per_core_scales_with_n_and_cores(tmp_path):
    s = load_scenario(base(tmp_path), ["n_list=[1, 2]", "exec_cores=[1, 8, 64]",
                                       "txs_per_block_per_core=29000"])
    assert [s.block_txs(n, k) for n, k in ((1, 1), (1, 64), (2, 8))] == \
           [29_000, 1_856_000, 464_000]
    with pytest.raises(ValueError, match="txs_per_block_per_core needs the run's exec cores"):
        s.block_txs(1)


def test_exec_cores_keep_other_block_sizes(tmp_path):
    # Without txs_per_block_per_core the core count does not change the block.
    s = load_scenario(base(tmp_path), CORES)
    assert s.block_txs(1, 4) == s.block_txs(1) == 1_000_000
    s = load_scenario(base(tmp_path), [*CORES, "txs_per_block_per_exec=500000"])
    assert s.block_txs(2, 4) == 1_000_000


# ---- shipped scenarios ----------------------------------------------------------------

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
REF = ["hogd_ref='v0.2.0'"]
# Per file: storage, single_host, exec_allocator, exec_per_l3, and the instance
# types of the roles it uses. The smoke tests are small and run once.
SHIPPED = {
    "ec2-smoke-single": ("tmpfs", True, "system", False, {"single": "m8a.2xlarge"}),
    "ec2-smoke-multi": ("ebs", False, "system", False,
                        {r: "r8a.large" for r in ("runner", "exec", "db", "event", "indexer")}),
    "exec-cores-l3": ("tmpfs", False, "jemalloc", True,
                      {r: "r8a.16xlarge" for r in ("runner", "exec", "db", "event")}),
    "exec-cores-l3-wide": ("tmpfs", False, "jemalloc", True,
                           {r: "r8a.16xlarge" for r in ("runner", "exec", "db", "event")}),
    "exec-scaling-l3": ("tmpfs", False, "jemalloc", True,
                        {"runner": "r8a.24xlarge", "exec": "r8a.16xlarge",
                         "db": "r8a.24xlarge", "event": "r8a.24xlarge"}),
    "exec-scaling-l3-wide": ("tmpfs", False, "jemalloc", True,
                             {"runner": "r8a.24xlarge", "exec": "r8a.16xlarge",
                              "db": "r8a.24xlarge", "event": "r8a.24xlarge"}),
}


def test_every_shipped_scenario_is_listed():
    assert {p.stem for p in SCENARIOS.glob("*.toml")} == set(SHIPPED)


@pytest.mark.parametrize("name", sorted(SHIPPED))
def test_shipped_scenario(name):
    path = SCENARIOS / f"{name}.toml"
    with pytest.raises(ScenarioError, match="hogd_ref must be set to a hogd tag or commit "
                                            "that contains the HOGD_EXTRA_BENCH_DIRS hook"):
        load_scenario(path, [])
    s = load_scenario(path, REF)
    storage, single_host, allocator, per_l3, types = SHIPPED[name]
    assert (s.storage, s.single_host, s.exec_allocator, s.exec_per_l3) == \
        (storage, single_host, allocator, per_l3)
    assert {r: s.instance_types[r] for r in types} == types
    assert (s.region, s.availability_zone, s.ami_id) == ("us-west-2", "", "")
    # The largest block stays under hog-event's 4.5M-tx commit cap.
    n, k = max(s.n_list), max(s.exec_cores) if s.exec_cores else None
    assert s.block_txs(n, k) <= 4_500_000
    if name.startswith("ec2-smoke"):
        assert (s.n_list, s.repetitions, s.batch_sizes) == ([1], 1, [5])
    if single_host:  # fits the 8 vCPUs of m8a.2xlarge
        assert set().union(*validate_cpu_layout(s, 1).values()) == set(range(8))
    # x8aedz capacity is scarce: no shipped scenario needs it.
    text = "".join(x for x in path.read_text().splitlines(True) if not x.startswith("#"))
    assert "x8aedz" not in text


@pytest.mark.parametrize("name", ["exec-cores-l3", "exec-scaling-l3"])
def test_wide_variant_differs_only_in_accounts(name):
    s = load_scenario(SCENARIOS / f"{name}.toml", REF)
    wide = load_scenario(SCENARIOS / f"{name}-wide.toml", REF)
    assert (s.accounts, wide.name, wide.accounts) == (100, f"{name}-wide", 10000)
    assert replace(wide, name=s.name, accounts=s.accounts) == s
