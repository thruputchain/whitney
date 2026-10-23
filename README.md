# Whitney

Whitney is a reproducible benchmark of how the Thruput execution engine (`hogd`) scales. It runs on EC2. A scenario file describes the hosts and the workload. Whitney builds `hogd` from source, provisions the hosts, runs the scenario, collects the raw results, and destroys the cluster. Every table and chart comes from the result files, and the published results are in this repo.

Whitney is named after Mount Whitney, the highest peak in the lower 48.

## Results

ERC20 payments per second, measured inside the node: the executors run signed ERC20 transfers that a runner sends over the network, and each block is opened, closed, and committed in the db and event services. There is no consensus or mempool in the path yet. See [docs/methodology.md](docs/methodology.md) for exactly what is in and out.

This is the first and most fundamental Thruput benchmark: it measures the execution core that the rest of the system is built around. It is not a claim about end-to-end throughput. It shows the potential of the execution model, and it sets the bar for the rest of the node: how busy can consensus, the mempool, and client traffic keep this core?

**Adding cores to one executor host** (`r8a.16xlarge`, 64 cores):

| Cores | 1 | 2 | 4 | 8 | 16 | 32 | 64 |
|---|---|---|---|---|---|---|---|
| 100 accounts | 23k | 46k | 95k | 187k | 370k | 689k | **1.17M** |
| 10,000 accounts | 19k | 41k | 88k | 178k | 352k | 654k | **1.12M** |

![Cores per executor host, 100 and 10,000 accounts](results/published/2026-10-23-executor-scaling-10k-accounts/compare-cores.png)

**Adding executor hosts** (64 cores each, plus 3 support hosts: runner, db, event):

| Executor hosts | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| 100 accounts | 1.18M | 2.32M | 3.47M | 4.44M | 5.35M | 6.20M | 6.84M | **7.02M** |
| 10,000 accounts | 1.16M | 2.33M | 3.38M | 4.29M | 5.12M | 5.71M | 6.20M | **6.33M** |

![Executor hosts, 100 and 10,000 accounts](results/published/2026-10-23-executor-scaling-10k-accounts/compare-hosts.png)

Each value is the median of 3 repetitions, each on a fresh cluster. The setup:

- **Hosts:** `r8a.16xlarge` executor hosts (AMD Zen 5) in one zone of `us-west-2`. On the host curve, the runner, db, and event hosts are `r8a.24xlarge`.
- **Executors:** one `hog-exec` per L3 cache domain (8 per host), with jemalloc preloaded.
- **Blocks:** 4,000,000 txs per block on the host curve.
- **Storage:** tmpfs.
- **Workload:** 100 sender accounts is the high-contention case; 10,000 is the low-contention reference.

The run files, summaries, and the exact build and settings of each point are in [`results/published/`](results/published/):

- [100 accounts](results/published/2026-10-23-executor-scaling/INDEX.md)
- [10,000 accounts](results/published/2026-10-23-executor-scaling-10k-accounts/INDEX.md)

## Reproduce the results

You need an AWS account and some patience. A full host sweep keeps 11 large instances up for about 20 minutes per repetition. [docs/usage.md](docs/usage.md) has the details of every step.

1. **Install the tools:**
   - Python 3.11+ and [uv](https://docs.astral.sh/uv/);
   - [OpenTofu](https://opentofu.org) 1.9+;
   - the `aws` CLI with working credentials;
   - Docker on an x86_64 Linux host, for the build.
2. **Prepare AWS:** an EC2 key pair, and an S3 bucket in the run's region.
3. **Build** `hogd` and the runner from source at a `hogd` tag that has the Whitney bench hook. The archive goes to `.whitney-cache/`:

   ```sh
   uv run whitney build --hogd-ref <tag> --out .whitney-cache
   ```

4. **Check your setup** with the cheap smoke scenario:

   ```sh
   uv run whitney run scenarios/ec2-smoke-single.toml --n 1 --max-hours 1 \
     --set 'hogd_ref="<tag>"' --set 'availability_zone="<zone>"' --set 'ami_id="<ami>"' \
     --bucket <bucket> --ssh-key-name <key pair> --operator-cidr <your ip>/32 \
     --ssh-identity ~/.ssh/<key>.pem
   ```

5. **Run a published scenario** with the same values:

   ```sh
   uv run whitney sweep scenarios/exec-scaling-l3.toml --set 'hogd_ref="<tag>"' ...
   ```

   The four published scenarios:
   - `exec-cores-l3` and `exec-cores-l3-wide`: cores on one host;
   - `exec-scaling-l3` and `exec-scaling-l3-wide`: executor hosts.

   The `-wide` ones use 10,000 accounts.
6. **Make the table and the chart:**

   ```sh
   uv run whitney summarize results/<sweep dir> --batch-size 5
   uv run whitney chart results/<sweep dir> --batch-size 5
   ```

**Always destroy your clusters.** Whitney destroys each cluster after a run, also after a failure or Ctrl-C, and every host powers itself off after `--max-hours` as a backstop. If a destroy fails, Whitney prints the command to run. Your AWS login session must last longer than the sweep. See [docs/usage.md](docs/usage.md#cost-and-cleanup).

Your numbers will not match ours exactly: hosts and zones differ, and the work txs are random. They should be close, and the methodology says how to tell.

## Docs

- [docs/methodology.md](docs/methodology.md): what is measured, the workload, the hardware, the run shape, and the known limits.
- [docs/usage.md](docs/usage.md): commands and options, scenario keys, AWS credentials, EC2 capacity, metrics, the results format, and how to publish a result.

## Licence

Apache-2.0.
