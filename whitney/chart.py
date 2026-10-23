"""Scaling chart from summary points."""
from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from whitney.results import Point  # noqa: E402

# Reference categorical slots 1 to 3 (blue, orange, green), fixed per storage mode.
COLORS = {"nvme": "#2a78d6", "tmpfs": "#eb6834", "ebs": "#1baf7a"}
_FALLBACK = ["#eda100"]
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e3e2de"


def x_label(indexer: bool) -> str:
    return f"Executor hosts (N) + {4 if indexer else 3} support hosts"


def _n(p: Point) -> int:
    return p.n


def _cores(p: Point) -> int:
    return p.cores or 0


def x_axis(points: list[Point]) -> tuple[Callable[[Point], int], str]:
    """(x of a point, axis label): hog-exec cores when the points vary in
    cores at one N, else N. With exec_per_l3 the label says so."""
    ns, cores = {p.n for p in points}, {p.cores for p in points}
    per_l3 = points[0].per_l3  # a group field: the same for every point
    if len(cores) <= 1:
        return _n, x_label(points[0].indexer) + (" (one hog-exec per L3)" if per_l3 else "")
    if len(ns) > 1:
        raise ValueError("points vary in both N and exec cores: chart one axis at a time "
                         "(results with one N, or with one core count)")
    if None in cores:
        raise ValueError("mixed runs: some points have an exec core limit and some do not")
    (n,) = ns
    if per_l3:
        return _cores, ("cores per executor host (one hog-exec per L3)" if n == 1 else
                        f"cores per executor host ({n} executor hosts, one hog-exec per L3)")
    return _cores, ("hog-exec cores (one executor host)" if n == 1
                    else f"hog-exec cores per executor host ({n} executor hosts)")


def scaling_chart(points_by_storage: dict[str, list[Point]], out: Path) -> None:
    """Write <out>.svg and <out>.png. Median line, min/max band, and a
    linear reference line, one series per storage. The x-axis is N, or the
    exec core count (see x_axis)."""
    allp = [p for ps in points_by_storage.values() for p in ps]
    if not allp:
        raise ValueError("no points to chart")
    if any(p.single_host for p in allp):
        raise ValueError("single-host runs are not charted: N is not a host count there; "
                         "use the summary table")
    if len({p.indexer for p in allp}) > 1:
        raise ValueError("mixed runs: differing fields: indexer")
    x, xlabel = x_axis(allp)
    plt.rcParams["svg.fonttype"] = "none"  # keep text as text
    plt.rcParams["font.family"] = "sans-serif"
    fig, ax = plt.subplots(figsize=(8, 4.8), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    spare = iter(_FALLBACK)
    for i, (storage, pts) in enumerate(sorted(points_by_storage.items())):
        color = COLORS.get(storage) or next(spare)
        pts = sorted(pts, key=x)
        ok = [p for p in pts if p.samples > 0 and not math.isnan(p.median_tps)]
        if ok:
            _linear(ax, ok[0], [x(p) for p in pts], x, color, label=i == 0)
            # Line segments only join adjacent measured Ns; a failed N breaks the line.
            seg: list[Point] = []
            for p in pts:
                if p in ok:
                    seg.append(p)
                    continue
                _line(ax, seg, color, x=x)
                seg = []
            _line(ax, seg, color, label=storage, x=x)
            if len(points_by_storage) >= 2:  # one series needs no name
                last = ok[-1]
                ax.annotate(storage, (x(last), last.median_tps), xytext=(8, 0),
                            textcoords="offset points", va="center", color=INK2,
                            fontsize=10)
        for p in pts:
            if p.failed_runs:  # mark at the baseline; never interpolate
                ax.plot([x(p)], [0], marker="x", color=INK2, markersize=9,
                        markeredgewidth=2, linestyle="none", clip_on=False, zorder=5)
                what = f"{storage}: " if len(points_by_storage) >= 2 else ""
                ax.annotate(f"{what}{p.failed_runs} failed", (x(p), 0),
                            xytext=(0, 12), textcoords="offset points", ha="center",
                            color=INK2, fontsize=9)
    ax.set_xticks(sorted({x(p) for p in allp}))
    ax.set_xlabel(xlabel, color=INK2)
    ax.set_ylabel("ERC20 payments/sec", color=INK2)
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2)
    fig.tight_layout()  # series are named at their line ends; no legend
    for ext in ("svg", "png"):
        fig.savefig(out.parent / f"{out.name}.{ext}", facecolor=SURFACE, dpi=150)
    plt.close(fig)


def _linear(ax, first: Point, xs: list[int], x: Callable[[Point], int], color: str,
            label: bool = True) -> None:
    """Dashed linear scaling from the smallest-x measured point: y0 × x / x0.
    label=False draws the line unnamed (the first series names it)."""
    x0, y0 = x(first), first.median_tps
    xs = [v for v in xs if v >= x0]
    ax.plot(xs, [y0 * v / x0 for v in xs], color=color, linewidth=1, linestyle="--",
            alpha=0.6, zorder=1)
    if label:
        ax.annotate("linear", (xs[-1], y0 * xs[-1] / x0), xytext=(8, 0),
                    textcoords="offset points", va="center", color=INK2, fontsize=9)


def _line(ax, seg: list[Point], color: str, label: str | None = None,
          x: Callable[[Point], int] = _n) -> None:
    if not seg:
        return
    xs = [x(p) for p in seg]
    ax.fill_between(xs, [p.min_tps for p in seg], [p.max_tps for p in seg],
                    color=color, alpha=0.18, linewidth=0)
    if len(seg) == 1:  # a lone point has no band area; show its range as a bar
        ax.vlines(xs[0], seg[0].min_tps, seg[0].max_tps, color=color, alpha=0.4, linewidth=4)
    ax.plot(xs, [p.median_tps for p in seg], color=color, linewidth=2,
            marker="o", markersize=8, markeredgecolor=SURFACE, markeredgewidth=2,
            label=label)
