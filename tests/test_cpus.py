import re

import pytest

from whitney.cpus import (cpuset_size, exec_layout, format_cpuset, l3_groups, parse_cpuset,
                          procs_per_host)

# r8a.16xlarge: 64 CPUs, 8 L3 groups of 8.
R8A = [list(range(g * 8, g * 8 + 8)) for g in range(8)]


@pytest.mark.parametrize("text,cpus", [
    ("0", [0]), ("0-7", list(range(8))), ("0-3,8-11", [0, 1, 2, 3, 8, 9, 10, 11]),
    ("0,2,4", [0, 2, 4]), (" 4-5,1\n", [1, 4, 5]), ("3,3", [3]),
])
def test_parse_cpuset(text, cpus):
    assert parse_cpuset(text) == cpus
    assert cpuset_size(text) == len(cpus)


@pytest.mark.parametrize("text", ["", "a", "3-1", "-1", "0-", "0,,1"])
def test_parse_cpuset_rejects(text):
    with pytest.raises(ValueError, match="bad cpuset list"):
        parse_cpuset(text)


@pytest.mark.parametrize("cpus,text", [
    ([0], "0"), (list(range(8)), "0-7"), ([0, 1, 2], "0-2"), ([0, 2, 4], "0,2,4"),
    ([5, 0, 1, 2, 7, 8], "0-2,5,7-8"),
])
def test_format_cpuset(cpus, text):
    assert format_cpuset(cpus) == text
    assert parse_cpuset(text) == sorted(cpus)


def test_l3_groups_distinct_and_sorted_by_lowest_cpu():
    # One line per online CPU in sysfs order (cpu0, cpu1, cpu10, ...), repeats included.
    lines = ["0-7", "0-7", "16-23", "8-15", "16-23", "", "8-15"]
    assert l3_groups(lines) == [R8A[0], R8A[1], R8A[2]]
    # Groups need not be contiguous (SMT siblings, for example).
    assert l3_groups(["32-35,0-3", "4-7,36-39"]) == [[0, 1, 2, 3, 32, 33, 34, 35],
                                                     [4, 5, 6, 7, 36, 37, 38, 39]]


@pytest.mark.parametrize("lines,msg", [
    ([], "no L3 cache groups"),
    (["", "\n"], "no L3 cache groups"),
    (["0-7", "8-11"], "L3 groups are not all the same size: 0-7, 8-11"),
    (["0-7", "4-11"], "L3 groups overlap"),
    (["0-x"], "bad cpuset list"),
])
def test_l3_groups_rejects(lines, msg):
    with pytest.raises(ValueError, match=re.escape(msg)):
        l3_groups(lines)


@pytest.mark.parametrize("cores,want", [
    (1, ["0"]), (8, ["0-7"]),
    (16, ["0-7", "8-15"]),
    (64, ["0-7", "8-15", "16-23", "24-31", "32-39", "40-47", "48-55", "56-63"]),
])
def test_exec_layout(cores, want):
    layout = exec_layout(R8A, cores)
    assert [format_cpuset(c) for c in layout] == want
    assert sum(len(c) for c in layout) == cores
    assert len(layout) == procs_per_host(8, cores)


def test_exec_layout_uses_the_groups_in_order():
    groups = [[0, 1, 2, 3, 32, 33, 34, 35], [4, 5, 6, 7, 36, 37, 38, 39]]
    assert exec_layout(groups, 3) == [[0, 1, 2]]
    assert exec_layout(groups, 16) == groups


@pytest.mark.parametrize("cores,msg", [
    (12, "exec_per_l3: K=12 is not a multiple of the L3 size 8 (K must be <= 8 or a multiple "
         "of it)"),
    (65, "exec_per_l3: K=65 cores but the host has 64 CPUs in L3 groups"),
])
def test_exec_layout_rejects(cores, msg):
    with pytest.raises(ValueError, match=re.escape(msg)):
        exec_layout(R8A, cores)


@pytest.mark.parametrize("size,cores,procs", [(8, 8, 1), (16, 64, 4)])
def test_procs_per_host(size, cores, procs):
    assert procs_per_host(size, cores) == procs
