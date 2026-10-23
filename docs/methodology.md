# Methodology

## What is measured

Whitney measures ERC20 payments per second inside the node, as cores are added to one executor host and as executor hosts are added. The headline claim is scaling against this harness's own baseline (1 core, or 1 executor host), on the same hardware. It is not a comparison with other chains.

### In the path

- `hog-exec` proposal execution (`executorsched_propose_batch`) of signed ERC20 payment txs, in batches.
- `hog-db` snapshot, close, and commit.
- `hog-event` open, close, and commit of each block, including their fsyncs. In the published setup (tmpfs) these fsyncs cost almost nothing. See [Storage](#storage).
- Ed25519 signature verification. Each tx goes through the `simple_wallet` contract, and the contract checks an Ed25519 signature. The load generator signs every tx before timing starts. The timed window pays for verification, not for signing.

### Out of the path

- Consensus.
- Mempool.
- Validator re-execution.
- The real scheduler and coordinator. The runner (`whitney-runner`, a fork of the benchmark runner in `hogd`, called "the original runner" below) replaces them with mocks in its own process. It opens each block the way the `hogd` scheduler does: the event block and the db snapshot first, then every executor, and it checks each executor's result.
- Client submission over the network.
- Harness bookkeeping. The runner allocates its per-block buffers once, before the first work block, so the timed window does not pay for them.
- Query visibility. The indexer is off by default. When it is on, it is not out of the path: it ingests blocks during the timed window, which adds concurrent load on the event host, and the runner waits for it to drain after the timed blocks of each batch-size run. Compare indexer-on numbers only with other indexer-on numbers.

### Why measure the execution core first

This benchmark measures the execution core: the part of Thruput that runs contracts, and the foundation that the rest of the system is built around. It is the first benchmark, and the most fundamental one. It shows what the execution model can do when it is kept busy.

It is not a forecast of end-to-end throughput. A full node adds consensus, a mempool, and client traffic, and those parts must feed the executors fast enough to keep them busy. So these numbers set the bar for the rest of the system: how much of this capacity can a full node use? End-to-end numbers, with a real mempool and consensus, will follow in a separate publication.

## Workload

- Contracts: `itc_erc20` and `simple_wallet`. Both are RV32 contracts. This is a native RV32 ERC20, not an EVM ERC20.
- Balance updates use native strict-increment integer operations. This sets how much the txs conflict with each other.
- Each tx is a constant-amount transfer between two accounts. The pair is random and uniform, and the two accounts are always different.
- The published scenarios use 100 funded accounts (high contention: many txs touch the same balances) and, in the `-wide` scenarios, 10,000 accounts (low contention). `accounts` is a scenario key.
- The runner makes one block of work txs (the scenario's block size) and replays the same work txs across blocks. Each replay gets fresh uniquifiers, so the chain accepts them as new txs. The runner does not make new signed txs per block.
- A uniquifier is an expiry height and a nonce. `hog-db` keeps a replay cache per sender wallet (the address the tx invokes): one entry per expiry height, with the nonces at that height. It allows at most 4,000 live expiry heights per wallet and 8,000 nonces per height. A height is live until the chain passes it.
- The runner uses the per-sender layout (`uniquifier_layout = "per-sender"`, the default). In each block, a sender's txs share as few expiry heights as fit, 7,000 nonces per height at most. The busiest sender sets S, the heights per sender per block: ceil(its txs / 7,000). Block i uses heights `work_base + i × S` to `work_base + i × S + S − 1`. With 4,000,000 txs per block, S is 6 with 100 accounts and 1 with 10,000 accounts.
- Why: the replay-cache work per block then follows the number of senders, and the layout does not add to it. The original runner (`uniquifier_layout = "global"`) puts 4,000 nonces of all senders on each height: a 1,000,000-tx block uses 250 heights, and each sender's txs spread over them. With 10,000 accounts (about 100 txs per sender) that is about 1,000,000 new (wallet, height) entries per block, and in our test runs the per-block execute and close times grew block by block. With 100 accounts it is about 25,000. So the global layout makes runs with many accounts look slower than they are. Use it only to compare with the original runner.
- Every nonce is unique in a run: block i, tx j gets nonce i × txs_per_block + j, in both layouts.
- The workload (`experiment`) is `erc20_payments`, the only supported one. Whitney rejects `erc20_basic_auth_payments`: its signatures cover the uniquifier, so the per-block rewrite would make them invalid.
- The pair choice has no seed yet. It uses the system random source, so two runs do not make the same txs. Whitney publishes a hash of the generated work txs for each batch-size run instead. The hash is in the `work_tx_hash` field of each trial line.

## Machine count

Every point on a chart or in a table reads "N executor hosts + 3 support hosts". The support hosts are the runner, the event host, and the db host. With the indexer on, it is "N executor hosts + 4 support hosts". N is the x-axis. The support hosts are always shown. The core curve has one executor host, and its x-axis is the `hog-exec` core count (see [Core scaling](#core-scaling)).

## Hardware

| Role | Instance type |
|---|---|
| executors | `r8a.16xlarge` |
| runner, db, event: core curve (`exec-cores-l3`) | `r8a.16xlarge` |
| runner, db, event: host curve (`exec-scaling-l3`) | `r8a.24xlarge` |
| db, event (and indexer), only with `storage = "nvme"` | `x8aedz.12xlarge` (opt-in) |

- `r8a.16xlarge` has 64 vCPUs (AMD Zen 5, one vCPU per physical core) and 512 GiB of memory. It has no local NVMe. AWS names `r7a.16xlarge` in its guide for Solana RPC nodes, and says consensus nodes can use the same size ([AWS guide](https://aws.amazon.com/blogs/database/run-solana-nodes-on-aws)). The `r7a` is the Zen 4 version of this shape. Whitney uses the Zen 5 successor, so it can use the same build flags as the Zen 5 tuned release. Anza lists a smaller validator minimum: 12 cores, 24 threads, 256 GB ([Anza docs](https://docs.anza.xyz/operations/requirements)). These references are context for "commodity, recognizable hardware". They are not a like-for-like comparison.
- All hosts are `r8a`, so every role has the same CPUs, and one instance family makes EC2 capacity easier to get. On the host curve, the runner, db, and event hosts are `r8a.24xlarge` (96 vCPUs, 40 Gbps): at 8 executor hosts the event host receives more than the 30 Gbps NIC of an `r8a.16xlarge` carries.
- An `r8a.16xlarge` has 8 L3 cache domains of 8 cores each. The published runs put one `hog-exec` on each (see [One executor per L3](#one-executor-per-l3)).
- `x8aedz.12xlarge` has 48 vCPUs, 1,536 GiB of memory, and one 3,800 GB local NVMe drive. The `x8aedz` family is the only AMD Zen 5 family with local NVMe, so the `znver5` binaries still run. It is opt-in, for `storage = "nvme"` only. It is scarce: EC2 often has no `x8aedz` capacity in a zone. Before an `nvme` sweep, create an On-Demand Capacity Reservation for the db and event hosts.
- All hosts run the stock Canonical Ubuntu 26.04 AMI, pinned by AMI ID. All hosts of one run sit in one region, one availability zone, and one cluster placement group. The zone must offer every instance type of the scenario (`r8a`, and `x8aedz` for `nvme`).
- Hosts install `libjemalloc2 libatomic1 libsnappy1v5` for the daemons. Host setup also installs `sysstat`, `jq`, `nvme-cli`, `xfsprogs`, and `curl`.

If a support host limits the curve before the largest N, the write-up shows it and names the limiting role from the per-host `sar` data. Bigger support hosts are a later decision.

## Storage

Storage is a scenario knob: `storage = "tmpfs"`, `"ebs"`, or `"nvme"`. It sets where the work directory of `control_server` lives. All `hog-db`, `hog-event`, and `hog-indexer` data lives there. These roles fsync inside the timed window, so storage can change the number.

- `tmpfs` is the published setup and the default. The work directory is on a RAM-backed tmpfs on every host, and fsyncs cost almost nothing. This benchmark targets contract execution inside the executor, not storage, so the published curve leaves storage cost out on purpose. It is not a durable validator setup.
- `ebs` (`storage = "ebs"`) gives durable storage on the same `r8a` hosts. The db and event hosts (and the indexer host when it is on) each get one EBS data volume, formatted xfs and mounted with `noatime`. The default volume is gp3, 200 GiB, 16,000 IOPS, 1,000 MiB/s. Scenario keys change it: `ebs_volume_type` (`"gp3"` or `"io2"`), `ebs_volume_size_gib`, `ebs_iops`, and `ebs_throughput_mibps` (gp3 only). The volume is encrypted and is deleted with its instance. An EBS fsync is a network round trip, so it has a higher latency than a local NVMe drive.
- `nvme` uses the local NVMe drive of `x8aedz` db and event hosts (and the indexer host). Set those instance types too: Whitney refuses `nvme` without instance-store types for these roles. `x8aedz` is scarce. See [Hardware](#hardware).
- In `ebs` and `nvme` modes, the runner and executor hosts have no data mount: their `/mnt/whitney` is a plain directory on the root EBS volume.
- The single host run is `tmpfs` only.

In all modes the work directory is a subdirectory of `/mnt/whitney`, and `TMPDIR` for `control_server` (where daemon logs go) is in the same place. In `ebs` and `nvme` modes, executor daemon logs and `TMPDIR` are on the root EBS volume. Executors should not touch disk much during a run. Check the per-host disk data in `sar` to confirm. Each data host records its data volume (device, size, and volume type) in `host-facts.json`.

What matching curves show: if the `tmpfs` and `ebs` (or `nvme`) curves on the same hosts match, storage is not the limit. What they do not show: that the executor is the limit. The runner, the network, or db CPU can limit both curves. The per-host `sar` data names the limiting role.

## Run shape

- Each batch-size run starts from a fresh genesis.
- It runs 1 warm-up block. Whitney discards it.
- It then runs 10 timed blocks.
- Block size: the shipped scenarios use a fixed 4,000,000 txs per block at every N. hog-event caps a block at about 4.5M ERC20 payment txs today: at commit it writes the block's event logs and tx bodies as one indexer record of at most 2 GiB (about 450 bytes per tx), and a larger block fails to commit ([hogd#4545](https://github.com/thruputchain/hogd/issues/4545)). Once that cap is lifted, the scenarios will size blocks per executor (`txs_per_block_per_exec` × N txs), so a block takes about the same time at every N. The target is 1,750,000 txs per executor: about 2.5 s per block, close to Thruput's expected production block time of 2 to 3 s. Each block has a cost that does not shrink as N grows (db close and commit, executor block finish). With a fixed tx count per block, that cost is a larger part of each block as N grows, and the curve bends for a reason other than executor scaling. The summary states the block size, the median block time, and the open and close times of every point, so that cost is visible.
- The guard is the replay-cache budget: S × blocks (warm-up included) must stay under 4,000 live heights per sender. The runner computes S exactly from the work txs and refuses the run before the first work block. Whitney checks the scenario first with an estimate. Per-sender: S = ceil(1.25 × txs per block / `accounts` / 7,000). The 1.25 factor covers the random sender counts: the busiest sender has more txs than the mean. Global: S = ceil(txs per block / 4,000). With `txs_per_block_per_exec`, Whitney checks the largest N. With `txs_per_block_per_core`, it checks the largest N and K. For example, with 1,000,000 txs per block, 100 accounts allow 1,999 blocks (S = 2), 2 accounts allow 44 (S = 90), and the global layout allows 15 (S = 250). There is no other block cap: the old cap of 15 blocks was this budget for the global layout.
- More blocks cost more. Runner time grows with the block count, and so does the data db and event keep. On tmpfs that data is in RAM. The runner gets `--runner-timeout` seconds (default 7,200) for the whole run, all batch sizes included. For a long run, raise `--runner-timeout` and check the RAM on the db and event hosts.
- Both also need `accounts` >= 2 and at least 4,000 txs per block.
- The runner uses 10 connections per executor and 100 coroutines per connection. The published scenarios use one batch size, 5.

### Core scaling

The core curve (`exec-cores-l3`) shows how one executor host scales with cores. It uses one executor host (N = 1) and limits `hog-exec` to K cores, for K = 1, 2, 4, ..., 64. This section describes the limit for one `hog-exec`. With `exec_per_l3` the K cores are split over one `hog-exec` per L3 group (next section), each limited the same way.

- Whitney limits the CPUs with a cgroup cpuset. Before each run it sets `AllowedCPUs=0-<K-1>` on the control unit of the executor host (`systemctl set-property --runtime whitney-control@<port>.service`). Then it restarts the unit. `control_server` starts `hog-exec` with `execvp`, so `hog-exec` runs in the cgroup of that unit.
- `hog-exec` sizes its thread pool from the CPUs it may use. So the limit sets both the CPUs that `hog-exec` runs on and the number of its worker threads.
- `r8a.16xlarge` has one vCPU per physical core, so K CPUs are K cores. Whitney stops the run if the host has fewer than K CPUs.
- After the restart, Whitney reads the effective cpuset of each control unit and writes it to `exec-setup.json`. If a unit's cpuset is not the one Whitney set, or the units do not have K CPUs in all, the run fails.
- A run without a core count removes the limit (`AllowedCPUs=` with no value).
- The runner, db, and event do not change. Each keeps its own host and all of its cores.
- Block size grows with the cores: `txs_per_block_per_core` × N × K txs. So a block takes about the same time at every K. The scenario uses 29,000 txs per core: about 1.2 to 1.6 s per block in the published runs.

### One executor per L3

A single `hog-exec` loses per-core rate when its threads span more than one L3 cache domain. An `r8a.16xlarge` has 8 L3 groups of 8 cores. With `exec_per_l3 = true`, each executor host runs one `hog-exec` per L3 group instead of one `hog-exec` for the host. In experiments this gave about 39% more txs/s per host.

- Before each run, Whitney reads the L3 groups of each executor host in use: the distinct `cache/index3/shared_cpu_list` values of the online CPUs in `/sys/devices/system/cpu`, sorted by their lowest CPU. The run fails if the groups are not all the same size, if the hosts do not agree on the size (L), or if K is more than the CPUs in the groups.
- K <= L: one `hog-exec` on the first K CPUs of the first L3 group.
- K > L: K must be a multiple of L, or the run fails. The host runs P = K / L `hog-exec`s, and `hog-exec` i gets all CPUs of L3 group i.
- An executor host boots with one control unit (port 11111). For each extra `hog-exec` i, Whitney adds a control unit on port 11111 + i before the restart. Its env file is a copy of the base one, with `ROLE=<role>-<i>` and `WORK_DIR=/mnt/whitney/work/<role>-<i>`. Each unit gets `AllowedCPUs` set to the CPUs of its `hog-exec`. Units from an earlier run that this run does not use are stopped and removed. `exec_allocator` applies to every unit.
- `roles.json` lists P executors per host: `exec<h>` and `exec<h>-<i>`. Each has its own control port, work dir, and metrics port. Vector does not scrape the metrics ports of the extra `hog-exec`s.
- Block size depends on K, not on P. The trials record N × P executors (`nexec`). `exec-setup.json` records the L3 groups, and `summarize` uses them to check `nexec`.
- It needs a `hogd` build where executors may be pinned off CPU 0 ([hogd#4564](https://github.com/thruputchain/hogd/issues/4564)).

### Allocator

The shipped `hogd` build uses glibc malloc. That is the default (`exec_allocator = "system"`). With `exec_allocator = "jemalloc"`, Whitney adds `LD_PRELOAD` of the Ubuntu `libjemalloc2` library to the env file of each control unit on the executor hosts. `control_server` starts `hog-exec` with `execvp`, so `hog-exec` gets jemalloc too. The runner, db, and event do not change. After the restart, Whitney checks which allocator each `control_server` loaded, and records it in `exec-setup.json`. If it is not the one the scenario asks for, the run fails. A published result must state which allocator it used. The summary never pools runs with different allocators.

### Published protocol

- 3 repetitions. Each repetition is a fresh cluster of the largest size, with new instances. It runs every N in random order. The order is recorded in `sweep.json`, and you can set it with `--seed`.
- Each repetition is one independent sample. The 10 timed blocks inside one repetition share the same hosts, so they are not independent.
- The headline value for one N is the median of the per-repetition medians. The band is the min and max of the per-repetition medians. With 1 repetition, the band is a single point.
- Block-level min and max are reported separately in the summary table.
- The headline is whole-block throughput: block size over the wall time from open to commit. That is the work a validator's machines do for each block: the executors receive the block's txs over the network from another host, and the block is opened, closed, and committed in db and event. The propose-phase rate and the open and close times are reported next to it as a breakdown, not as the headline.
- The runner must not be the limit. Each block records the runner process's CPU time (all threads), and the summary reports it as cores in use during the block (`median_runner_cores`). In the published host curve the runner used at most 25 of its 96 cores.
- The primary curve uses one fixed batch size for all N. It is chosen and written down before the run. Other batch sizes go in an appendix. Whitney does not pick the best batch size per point.
- A run that crashes, panics, or fails the reconciliation check is published as failed, with its runner log. It is never dropped.
- A published result is the output of `whitney publish`: the run files without host data such as `sar` and daemon logs, with private details replaced by placeholders, and the summary and chart made again from those files.
- The summary checks each successful run's result file against its scenario: block count and indexes per batch size, warm-up flags, txs, N, accounts, workload, and tps against txs and elapsed time. A run that does not match is shown as invalid, with the reason, and is not used.

## Reconciliation

Version 1 relies on the per-tx check in the runner. The runner aborts on any tx result that is not OK. So every counted tx executed and committed with an OK result. There is no balance read-back yet. A later version will read back per-account balances and transfer-event counts.

## Build

Whitney builds `hogd` from source at a pinned ref.

- The build runs inside a toolchain container. Whitney builds the container from the `.devcontainer/Dockerfile` of `hogd` at that ref.
- Release flags: Release build, gcc, `-DBUILD_ARCHITECTURE=znver5 -DRUST_BUILD_CPU=znver5`, plus `-DBUILD_BENCHMARKS=ON`. `--arch znver4` exists for local tests on Zen 4 machines. Published runs use `znver5`.
- `whitney-runner` and `control_server` come from the same build. `control_server` rejects calls from a build with a different git hash.
- The output is one archive. Its key is `<hogd_ref>-<whitney_src_hash12>-<arch>`. A SHA-256 file sits next to it. Every host gets the same archive and checks the hash.
- The manifest inside the archive records the `hogd` ref and commit, the flags, the toolchain versions (gcc, clang, rustc, cmake), and the toolchain image digest.
- The claim is "built from tag X with the release `znver5` flags". It is not "byte-for-byte the shipped binaries".
- The toolchain image is not fully pinned yet. Two builders can get different toolchain versions from the same ref. The manifest records what was used. Before a public result, the exact image will be published and referenced by digest.

## Tuning and per-host data

Host setup applies the benchmark tuning that works on EC2, such as the CPU governor. Some tuning cannot be applied on some instances. Each host writes what it applied, and what failed, to `host-facts.json`. Check this file in each run dir.

Every host records `sar` data for the whole run: CPU, network, and disk. This data is how the write-up names the limiting role.

## Per-second metrics

Every run records per-second metrics of each host it uses and of each daemon on it. These metrics are part of the raw data, next to `sar` and the trials. They show where a support role (the runner CPU, db ingest, the event NIC) limits the curve.

- Vector 0.43.0, pinned by version and SHA-256, runs on every host. It scrapes the `/metrics` endpoint of each daemon on the host, and reads the host metrics (CPU per core, memory, network, disk, load), every second.
- Each daemon serves its metrics on a fixed port, on the host's private IP. A scrape is a small HTTP request every second per daemon. It is the same work the daemons do under a production metrics scraper.
- Vector starts before the runner and stops after it, once per run. Each run's metrics are in their own file, with the run, scenario, N, and repetition as labels.
- Optionally, Vector also sends the metrics to a Prometheus remote-write endpoint. This does not change what is recorded in the results.

## Known limits

- Some EC2 tuning may not apply. The host facts record it.
- Support hosts may limit the curve. The write-up names the limiting role from `sar` data.
- The numbers cover execution, db, and event only. End-to-end numbers (mempool and consensus) will follow.
- The published curve uses tmpfs, so it leaves out the cost of durable db and event storage. Use `ebs` or `nvme` runs to see that cost.
- No random seed yet. Work txs differ between runs. The work-tx hash identifies the txs of each run.
- Reconciliation checks per-tx results only. There is no balance read-back.
- The toolchain image is recorded by digest, but it is not published yet.
- The numbers are for this hardware, in one availability zone. Other hardware gives other numbers.
- The host curve uses a fixed 4,000,000 txs per block, because `hog-event` cannot commit a larger block yet ([Run shape](#run-shape)). Blocks get shorter as N grows, so the fixed close cost is a larger part of each block at N = 8.
- jemalloc is preloaded into `hog-exec` by Whitney. The `hogd` build itself uses glibc malloc ([Allocator](#allocator)).
