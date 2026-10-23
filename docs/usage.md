# Using Whitney

This is the reference for running Whitney. For what the numbers mean, read [methodology.md](methodology.md).

All commands run from the repo root through `uv run`.

## Prerequisites

- An AWS account with On-Demand vCPU quota for the R family. The largest published scenario, `exec-scaling-l3`, needs 8 `r8a.16xlarge` and 3 `r8a.24xlarge` at once: 800 vCPUs.
- A region, and an availability zone in it, that offers every instance type of the scenario.
- AWS credentials for the `aws` CLI (see [AWS credentials](#aws-credentials)), an EC2 key pair, and an S3 bucket in the run's region. Whitney uploads the build archive there and gives the hosts a presigned URL.
- Docker on an x86_64 Linux host, for `whitney build` only. To run the CLI from another machine (for example a Mac), build elsewhere and pass `--archive`.
- [OpenTofu](https://opentofu.org) (`tofu`) 1.9 or newer.
- Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/).

Set `TF_PLUGIN_CACHE_DIR` to a local directory, so that OpenTofu does not download the AWS provider again for each cluster.

## AWS credentials

If `aws sts get-caller-identity` works in your shell, Whitney works too. This includes `aws login` and IAM Identity Center (SSO) sessions.

The OpenTofu AWS provider cannot read `aws login` credentials, and exported temporary credentials can expire after 15 minutes. So by default (`--aws-creds process`) Whitney gives `tofu` its own profile:

- Whitney writes a private config file (mode 0600) under `.whitney-cache/aws/` with one profile, `whitney`. Its `credential_process` runs `aws configure export-credentials --format process` with your normal config, credentials file, and profile.
- Each `tofu` call uses that profile with an empty shared-credentials file and without the static `AWS_*` key variables. When the credentials expire, the provider gets new ones.
- Whitney's own `aws` calls (S3 upload and presign) use your normal environment. Whitney does not change your `~/.aws` files.

Before any build, upload, or apply, Whitney checks the credentials once with the `whitney` profile. If the check fails, it stops and spends nothing. It never prints credential values.

Use `--aws-creds env` for static keys in your environment. Then `tofu` gets your environment as is. If `AWS_CREDENTIAL_EXPIRATION` is set and comes sooner than `--max-hours` plus 30 minutes, Whitney stops before any spend.

Each run dir gets `aws.json` with the mode and the caller, without the account ID.

**Your login session must outlive the sweep.** The profile refreshes the short-lived credentials, but not the `aws login` or SSO session behind them. If that session ends, the later applies and destroys fail. See [Cost and cleanup](#cost-and-cleanup).

## Scenarios

The files in `scenarios/` are the published scenarios and two smoke tests. Before you use one, set three values in a copy, or with `--set`:

- `hogd_ref`: the `hogd` tag to build. The files ship with `hogd_ref = "SET-ME"`, and Whitney refuses to run until you change it. The tag must have the `HOGD_EXTRA_BENCH_DIRS` CMake hook in `hogd/hog_exec/bench/CMakeLists.txt`, which builds `whitney-runner` inside the `hogd` tree, and daemons that accept `--metrics-port`. Without the hook, the build stops before any EC2 spend with `build.sh: hogd@<ref> has no HOGD_EXTRA_BENCH_DIRS hook`. With `--archive`, `hogd_ref` must match the ref in the archive's manifest.
- `availability_zone`: a zone that offers every instance type of the scenario. List the zones with:

  ```sh
  aws ec2 describe-instance-type-offerings --location-type availability-zone \
    --filters Name=instance-type,Values=r8a.16xlarge,r8a.24xlarge --region <region>
  ```

- `ami_id`: the Canonical Ubuntu 26.04 AMI ID for the region. The Canonical AMI locator or its SSM parameters give it.

| Scenario | What it is |
|---|---|
| `exec-cores-l3` | One executor host (N = 1), `hog-exec` limited to K = 1, 2, 4, ..., 64 cores, one `hog-exec` per L3 cache domain. 100 accounts, 29,000 txs per core per block. Every host `r8a.16xlarge`. |
| `exec-scaling-l3` | N = 1 to 8 executor hosts, all 64 cores of each (8 `hog-exec`s of 8 cores). 100 accounts, 4,000,000 txs per block. Executor hosts `r8a.16xlarge`; runner, db, and event `r8a.24xlarge`. |
| `exec-cores-l3-wide`, `exec-scaling-l3-wide` | The same with 10,000 accounts. |
| `ec2-smoke-single` | Smoke test: every role on one `m8a.2xlarge`, tiny workload. |
| `ec2-smoke-multi` | Smoke test: every host `r8a.large`, `ebs` storage with a 20 GiB gp3 volume, tiny workload. |

All published scenarios use tmpfs, batch size 5, 1 warm-up and 10 timed blocks, 3 repetitions, and jemalloc in `hog-exec`. Any key can be changed with `--set key=value`, for example `--set 'accounts=1000'`. The resolved scenario is saved in each run dir.

### Scenario keys

| Key | Default | Meaning |
|---|---|---|
| `n_list` | (required) | Executor host counts of a sweep. |
| `accounts` | `100` | Funded sender accounts. |
| `txs_per_block` | `1000000` | Txs per block, the same at every N. |
| `txs_per_block_per_exec` | `0` (off) | Txs per executor host per block (× N), so a block takes about the same time at every N. Set it or `txs_per_block`, not both. |
| `txs_per_block_per_core` | `0` (off) | Txs per core per block (× N × K). It needs `exec_cores`. Not with the two keys above. |
| `exec_cores` | `[]` (no limit) | Core counts per executor host. Each run limits `hog-exec` to K cores, and a sweep runs every (N, K) pair. |
| `exec_per_l3` | `false` | Run one `hog-exec` per L3 cache domain on each executor host. It needs `exec_cores`. See the [methodology](methodology.md#one-executor-per-l3). |
| `exec_allocator` | `"system"` | `"system"`: glibc malloc, as `hogd` ships. `"jemalloc"`: preload jemalloc into `hog-exec` only. See the [methodology](methodology.md#allocator). |
| `batch_sizes` | `[1, 5, 10]` | Txs per propose call. Each batch size is its own run on a fresh genesis. |
| `warmup_blocks`, `timed_blocks` | `0`, `5` | Blocks per batch-size run. |
| `connections_per_exec`, `coros_per_connection` | `10`, `100` | Runner connections per executor and coroutines per connection. |
| `uniquifier_layout` | `"per-sender"` | How the runner gives txs fresh uniquifiers. `"global"` is the original runner's layout; use it only to compare with it. See the [methodology](methodology.md#workload). |
| `indexer` | `false` | Run `hog-indexer` on a fourth support host. |
| `storage` | `"tmpfs"` | Where db, event, and indexer keep their data. See [Storage](#storage). |
| `instance_types` | all `r8a.16xlarge` | Instance type per role: `runner`, `exec`, `db`, `event`, `indexer`, and `single`. |
| `single_host`, `cpu_layout` | `false`, `{}` | Put every role on one host, with a CPU set per role. Not with `exec_cores`, `exec_per_l3`, or `exec_allocator = "jemalloc"`. |
| `repetitions` | `1` | Repetitions of a sweep, each on a fresh cluster. |

The replay cache limits the block count: S × (`warmup_blocks` + `timed_blocks`) must stay under 4,000, where S is the expiry heights per sender per block. Whitney estimates S before the cluster starts, and the runner checks the exact value before the first work block. Long runs also need a larger `--runner-timeout` (default 7,200 s). See the [methodology](methodology.md#run-shape).

### Storage

| Mode | What it is |
|---|---|
| `tmpfs` (default) | RAM-backed tmpfs on every host. fsyncs cost almost nothing. The published results use it: the benchmark targets contract execution, not storage. |
| `ebs` | One EBS data volume (xfs, `noatime`) each for db, event, and indexer, on the same `r8a` hosts. |
| `nvme` | The local NVMe drive of `x8aedz` db and event hosts, for example `--set 'storage="nvme"' --set 'instance_types={db="x8aedz.12xlarge", event="x8aedz.12xlarge"}'`. `x8aedz` capacity is scarce: create an On-Demand Capacity Reservation first. |

`ebs` takes these keys:

| Key | Default | Values |
|---|---|---|
| `ebs_volume_type` | `"gp3"` | `"gp3"` or `"io2"` |
| `ebs_volume_size_gib` | `200` | gp3 1 to 65,536, io2 4 to 65,536 |
| `ebs_iops` | `16000` | gp3 3,000 to 80,000, and at most max(3,000, 500 × size in GiB). io2 100 to 256,000, and at most 1,000 × size in GiB. |
| `ebs_throughput_mibps` | `1000` | gp3 125 to 2,000, and at most `ebs_iops` / 4. io2 ignores it. |

Whitney and OpenTofu check these AWS limits before the cluster starts. The volume is encrypted and is deleted with its instance.

## Commands

### Build

```sh
uv run whitney build --hogd-ref <tag> --out .whitney-cache
```

This builds `hogd` and `whitney-runner` from source at the ref, inside the `hogd` toolchain container, with the release `znver5` flags. `--arch znver4` builds for local tests on a Zen 4 machine. The archive and its `.sha256` go to `--out`. `run` and `sweep` build on their own when no archive exists.

### Run

```sh
uv run whitney run scenarios/exec-scaling-l3.toml --n 4 --cores 64 \
  --bucket <bucket> --ssh-key-name <key pair> \
  --operator-cidr <your ip>/32 --ssh-identity ~/.ssh/<key>.pem
```

One scenario at one executor count, on a fresh cluster. Options:

- `--cores <K>`: limit `hog-exec` to K cores per executor host. Scenarios with `exec_cores` need it, and others refuse it.
- `--archive <path>`: use a prebuilt archive and skip the build.
- `--arch znver5|znver4`: the build arch when no archive is given.
- `--runner-timeout <seconds>`: kill `whitney-runner` after this time. Default 7,200.
- `--max-hours <hours>`: each host powers off and terminates this long after boot. Default 8 (24 for `sweep`).
- `--aws-creds process|env`: see [AWS credentials](#aws-credentials).
- `--instance-create-timeout <duration>`, `--apply-timeout <duration>`: see [EC2 capacity](#ec2-capacity).
- `--metrics-remote-write <url>`: see [Remote write](#remote-write).
- `--keep`: leave the cluster up, until `--max-hours`. You must destroy it.
- `--set key=value`: override a scenario key. You can repeat it.

### Sweep

```sh
uv run whitney sweep scenarios/exec-scaling-l3.toml \
  --bucket <bucket> --ssh-key-name <key pair> \
  --operator-cidr <your ip>/32 --ssh-identity ~/.ssh/<key>.pem
```

A sweep runs each repetition on its own fresh cluster of the largest size. Each repetition runs every N (or every (N, K) pair) in random order; `--seed <int>` repeats an order. It takes the same options as `run`. A failed run is recorded and the sweep goes on. The exit code is 0 only if all runs are ok.

### Summarize and chart

```sh
uv run whitney summarize results/<sweep dir> --batch-size 5
uv run whitney chart results/<sweep dir> --batch-size 5
```

`summarize` writes `summary-b<B>.csv` and `.md` and prints the table. `chart` writes `scaling-b<B>.svg` and `.png`. The x-axis is N, or K when the runs vary in K at one N. A dashed line shows linear scaling from the smallest point.

The summary has one row per N (and K). `median_tps` is the headline: whole blocks, open and close included. It is the median of the per-repetition medians, and `min_tps` and `max_tps` are the band. The other columns break it down:

- `block_txs` and `median_block_ms`: the block size and the median block time.
- `median_exec_tps`: txs over the propose phase only.
- `median_open_ms` and `median_close_ms`: the open and close times.
- `median_runner_cores`: the runner's CPU time over the block time.
- `procs_per_host`: the `hog-exec`s per executor host.

Summaries refuse to pool runs that differ in the build or in any setting that changes the result (for example `accounts`, block size, `exec_allocator`, `instance_types`). The error names the fields. Each `ok` run is also checked against its scenario: block counts, indexes, warm-up flags, txs, executor count, and tps. A run that fails a check is listed as invalid and left out.

### Publish

```sh
uv run whitney publish results/<sweep dir> --out results/published/<name>
git add -f results/published/<name>
```

`results/` is not committed. `publish` makes a clean copy of one or more sweep or run dirs:

- **What it keeps:** the run files that define the result (scenario, manifest, status, trials, runner log, roles, host facts, exec setup), and `sweep.json`.
- **What it leaves out:** `sar`, daemon logs, raw metrics, and `aws.json`.
- **Placeholders:** IP addresses, EC2 host names, bucket names, S3 URLs, home paths, ARNs, and the SSH key name are replaced with placeholders (`<ip>`, `<host>`, `<bucket>`, `<s3-url>`, `<home>`, `<arn>`, `<key-name>`). In JSON files only strings change, and numbers stay byte for byte.
- **Summary and index:** it makes the summary and chart again from the copy, and writes `INDEX.md` with the build, settings, run counts, summary, and chart of each sweep.
- **Final scan:** it scans the copy for private details and writes nothing if it finds one.

Read the copy before you commit it.

## Cost and cleanup

Whitney prints no prices; check the On-Demand prices for your region.

| Run | Instances at once |
|---|---|
| `exec-scaling-l3` sweep | 8 `r8a.16xlarge` and 3 `r8a.24xlarge`, about 20 minutes per repetition. |
| `exec-cores-l3` sweep | 4 `r8a.16xlarge`, about 7 minutes per repetition. |
| `whitney run --n N` | N executor hosts and 3 support hosts (4 with the indexer). |

**Always destroy your clusters.**

- **Automatic destroy:** Whitney destroys each cluster after the run, also after a failure, Ctrl-C, `SIGTERM`, or `SIGHUP`.
- **When you must destroy it yourself:** with `--keep`, or when a destroy fails. Whitney prints the command, with the absolute workdir and the `whitney` profile:

  ```sh
  tofu -chdir=<workdir> destroy
  ```

- **Expired login:** a destroy fails when your login session has ended. Log in again and run the printed command.
- **Dead-man switch:**
  - Each host schedules its own power-off at boot, and EC2 then terminates it. A systemd timer checks the deadline every 2 minutes, so it holds after a reboot. A rebooted host is never used again.
  - The limit is `--max-hours`. Set it above the time the run needs.
  - The switch does not remove the VPC, security group, or placement group. They cost nothing, but run the destroy command to remove them.
- **Orphaned instances:** after each destroy, Whitney looks for instances with its cluster tag that OpenTofu does not know, for example from an apply that was cut off. It terminates them and records them in `status.json` (`cleanup`).

After any run, check the EC2 console for leftover instances.

## EC2 capacity

`InsufficientInstanceCapacity` means EC2 has no free hosts of that type in that zone at the moment. It is not a quota error. Large `r8a` sizes are often short in one zone and free in another.

EC2 returns this error as HTTP 500, so the provider retries while the hosts it already made bill you. Whitney limits this:

- `max_retries` is 5 (the provider default is 25).
- `--apply-timeout` (default `--instance-create-timeout` + 4m, so 10m) stops `tofu apply` with `SIGTERM`, then Whitney destroys.
- `--instance-create-timeout` (default `6m`) limits the wait for an instance to reach `running`.

The run is recorded as failed with `capacity: ...` (the error names the types and zone), `capacity or launch timeout: ...` (the apply ran out of time, most often still in a capacity retry), or `launch timeout: ...` (an instance did not start). In a sweep, the runs of that repetition fail and the sweep goes on.

Try another zone that offers every type, try later, or create an On-Demand Capacity Reservation before a large sweep.

## Metrics

Every run records per-second metrics of each host and each daemon on it:

- **Collector:** Vector 0.43.0 runs on every host, pinned by version and SHA-256 like the build archive.
- **What it reads:** every second, Vector reads the host metrics (CPU per core and mode, memory, network, disk, load) and scrapes each daemon's `/metrics` endpoint. `whitney-runner` starts each daemon with a fixed `--metrics-port`:
  - **Multi-host:** event 9101, db 9102, indexer 9103, executor 9104 on its host.
  - **Single host:** executor K on 9110 + K.
  - **`exec_per_l3`:** `hog-exec` i ≥ 1 of executor host h uses 9200 + 64 × h + i. Vector does not scrape those.
- **Labels:** `job="whitney"`, `whitney_cluster`, `role`, `instance_type`, `whitney_run`, `scenario`, `n`, and `rep`.
- **Where it goes:** after the runner exits, Whitney stops Vector and copies the file to `metrics/<role>.ndjson.gz` in the run dir. One line is one metric, as Vector's `metric_to_log` writes it.

### Remote write

To see metrics live, pass a Prometheus remote-write URL with `--metrics-remote-write`. Give the credentials in your environment:

- **Basic auth:** `WHITNEY_REMOTE_WRITE_USERNAME` and `WHITNEY_REMOTE_WRITE_PASSWORD`.
- **Bearer token:** `WHITNEY_REMOTE_WRITE_BEARER_TOKEN`.

Grafana Cloud is one such endpoint. Its Prometheus page gives the URL, the username (the instance ID), and a token.

How the credentials are handled:

- **On the hosts:** Whitney copies them to each host as `/etc/whitney/vector/remote.env` (mode 0600). They are never on a command line, in OpenTofu state, or in a result file. `metrics.json` records only that remote write was on, and the endpoint host.
- **If the endpoint is slow or down:** Vector drops what it cannot send. The local files are not affected.

## Results format

`run` writes one run dir to `results/`. `sweep` writes a sweep dir with one run dir per (N, repetition) or (N, K, repetition), and a `sweep.json` with the seed, the order, and each run's status. If a sweep stops early, `sweep.json` has `"aborted": true` and `abort_reason`.

A run dir is named `<YYYYMMDD-HHMM>-<scenario>-<hogd_ref>-n<N>-r<rep>`, or `...-n<N>-c<K>-r<rep>` with `exec_cores`. It holds:

| File | Content |
|---|---|
| `status.json` | `ok` or `failed`, the error, start and end times, and `cleanup`. |
| `trials.jsonl` | One line per block: executor count (`nexec`), batch size, block index, warm-up flag, txs, `elapsed_ns`, tps, work-tx hash, the open, propose, and close times (`open_ns`, `exec_ns`, `close_ns`), and the runner's CPU time (`runner_cpu_ns`). |
| `scenario.resolved.toml` | The scenario after `--set`. |
| `manifest.json`, `archive.sha256` | The build: `hogd` ref and commit, flags, toolchain versions and image digest, and the archive hash. |
| `command.txt` | The `whitney` command and the runner command. |
| `roles.json` | The role addresses and ports given to the runner. |
| `exec-setup.json` | Per executor host: the L3 groups used (`l3`), and per control unit the cpuset Whitney set (`want`), the effective cpuset (`cpus`), and the allocator (`allocator`). |
| `runner.log` | Raw runner output. |
| `host-facts/<role>.json` | Instance facts, mounts, the data volume, and the tuning that was applied or failed. |
| `metrics/<role>.ndjson.gz`, `metrics.json` | Per-second metrics, and the Vector version and remote-write state. |
| `sar/<role>.bin` | `sar` data per host. Read it with `sar -f`. |
| `daemon-logs/<role>/` | Daemon output from each host. |
| `aws.json` | The credential mode and the caller, without the account ID. |

Failed runs stay in the results. An interrupted run is recorded as failed (`interrupted: ...`), and Whitney spends up to 60 s collecting what is there before it destroys the cluster.

## Known limits

- Some EC2 tuning may not apply. Check `host-facts/` in each run.
- A support host can limit the curve. The per-host metrics and `sar` data name it.
- The runner checks per-tx results only. There is no balance read-back yet.
- End-to-end numbers (mempool and consensus) will follow.
