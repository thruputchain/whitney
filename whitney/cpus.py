"""CPU lists (the kernel's cpuset list syntax, such as 0-7,16) and the
per-L3 executor layout: one hog-exec per L3 cache domain."""
from __future__ import annotations


def parse_cpuset(cpus: str) -> list[int]:
    """The CPUs of a cpuset list such as 0-7,16, sorted."""
    out: set[int] = set()
    for part in cpus.strip().split(","):
        lo, sep, hi = part.strip().partition("-")
        try:
            first, last = int(lo), int(hi if sep else lo)
        except ValueError:
            raise ValueError(f"bad cpuset list: {cpus!r}") from None
        if first < 0 or last < first:
            raise ValueError(f"bad cpuset list: {cpus!r}")
        out.update(range(first, last + 1))
    return sorted(out)


def format_cpuset(cpus: list[int]) -> str:
    """A cpuset list with runs of CPUs as ranges: [0, 1, 2, 5] -> 0-2,5."""
    runs: list[list[int]] = []
    for c in sorted(set(cpus)):
        if runs and c == runs[-1][1] + 1:
            runs[-1][1] = c
        else:
            runs.append([c, c])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def cpuset_size(cpus: str) -> int:
    """The CPU count of a cpuset list such as 0-7,16."""
    return len(parse_cpuset(cpus))


def l3_groups(lines: list[str]) -> list[list[int]]:
    """The L3 groups of a host from its shared_cpu_list values (one per
    line, repeats allowed), sorted by their lowest CPU. Fails unless the
    groups are disjoint and all the same size."""
    groups = sorted({tuple(parse_cpuset(x)) for x in lines if x.strip()})
    if not groups:
        raise ValueError("no L3 cache groups (no cache/index3/shared_cpu_list)")
    names = ", ".join(format_cpuset(list(g)) for g in groups)
    seen: set[int] = set()
    for g in groups:
        if seen & set(g):
            raise ValueError(f"L3 groups overlap: {names}")
        seen.update(g)
    if len({len(g) for g in groups}) > 1:
        raise ValueError(f"L3 groups are not all the same size: {names}")
    return [list(g) for g in groups]


def exec_layout(groups: list[list[int]], cores: int) -> list[list[int]]:
    """The CPUs of each hog-exec on a host with these L3 groups (l3_groups),
    for K = cores. K <= L (the L3 size): one hog-exec on the first K CPUs of
    the first group. Else K must be a multiple of L: K / L hog-execs, the
    i-th on all of group i."""
    size, total = len(groups[0]), sum(len(g) for g in groups)
    if cores > total:
        raise ValueError(f"exec_per_l3: K={cores} cores but the host has {total} CPUs "
                         f"in L3 groups")
    if cores <= size:
        return [groups[0][:cores]]
    if cores % size:
        raise ValueError(f"exec_per_l3: K={cores} is not a multiple of the L3 size {size} "
                         f"(K must be <= {size} or a multiple of it)")
    return groups[:cores // size]


def procs_per_host(l3_size: int, cores: int) -> int:
    """The hog-exec count per host of the per-L3 layout (exec_layout)."""
    return 1 if cores <= l3_size else cores // l3_size
