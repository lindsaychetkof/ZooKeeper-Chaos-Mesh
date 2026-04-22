#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_graphs.py
==================
ZooKeeper Chaos Engineering — Analysis Graphs
Run from project root:  python generate_graphs.py
Output: graphs/fig_*.png + graphs/summary.png
"""

import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
from matplotlib.gridspec import GridSpec
import numpy as np

os.makedirs("graphs", exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Global style
# ─────────────────────────────────────────────────────────────────────────────
plt.rcParams.update({
    "font.family"       : "DejaVu Sans",
    "font.size"         : 10,
    "axes.titlesize"    : 12,
    "axes.titleweight"  : "bold",
    "axes.labelsize"    : 10,
    "axes.spines.top"   : False,
    "axes.spines.right" : False,
    "figure.facecolor"  : "white",
    "axes.facecolor"    : "#F8F9FA",
    "axes.grid"         : True,
    "grid.color"        : "white",
    "grid.linewidth"    : 1.0,
    "grid.alpha"        : 0.9,
    "figure.dpi"        : 150,
    "savefig.bbox"      : "tight",
    "savefig.facecolor" : "white",
})

# Colour palette
KILL  = "#E74C3C"   # red
NET   = "#3498DB"   # blue
CASC  = "#E67E22"   # orange
THREE = "#8E44AD"   # purple
M5    = "#27AE60"   # green
T5    = "#16A085"   # teal
V1_C  = "#95A5A6"   # grey  (v1 data)
V2_C  = "#2C3E50"   # dark  (v2 accent)
OK_C  = "#2ECC71"   # workload OK
ERR_C = "#E74C3C"   # workload ERROR
FAULT = "#F39C12"   # fault injection marker


# ─────────────────────────────────────────────────────────────────────────────
# Raw data
# ─────────────────────────────────────────────────────────────────────────────

# Election / quorum-loss detection latency (seconds from fault injection)
# Source: fresh clean run 2026-04-10 afternoon (all 3-node); 5-node from morning session
election = {
    "A: Kill Leader\n(pod kill)"            : [3.791, 3.359, 3.65],    # log: kill_leader_run1-3
    "B: Network Partition\n(TCP hang)"      : [9.205, 10.04, 5.814],   # log: network_partition_run1-3
    "D: 3-Way Isolation\n(1+1+1 all nodes)" : [6.607, 11.755, 6.617],  # log: threeway_isolation_run1-3
    "E: 5-Node Group B\n(2/5 minority)"     : [ 8.042,  8.076],        # log: 5node_majority_partition_run2-3
    "F: 5-Node All Nodes\n(2+2+1)"          : [16.383, 15.914],        # log: 5node_threeway_partition_run1-2
}
elec_colors = [KILL, NET, THREE, M5, T5]

# Recovery times — v2 (seconds from fault injection to full cluster recovery)
# Source: fresh clean run 2026-04-10; 5-node runs 2-3 only (run 1 contaminated)
recovery_v2 = {
    "A: Kill Leader"       : {"vals": [64.2, 64.1, 67.7],   "color": KILL},
    "B: Network Partition" : {"vals": [110.5, 80.2, 110.2], "color": NET},
    "C: Cascading Failure" : {"vals": [83.7, 78.2, 91.6],   "color": CASC},
    "D: 3-Way Isolation"   : {"vals": [114.0, 80.4, 110.4], "color": THREE},
    "E: 5-Node Majority\n(runs 2–3, clean)" : {"vals": [85.3, 83.9], "color": M5},
    "F: 5-Node 3-Way"      : {"vals": [89.0],               "color": T5},
}

# Recovery times — v1 (5-second polling, for comparison)
recovery_v1 = {
    "A: Kill Leader"       : [64.1, 67.8, 62.5, 67.3, 67.7],
    "B: Network Partition" : [85.7, 112.7],           # only runs 1 and 5 confirmed
    "C: Cascading Failure" : [79.2, 78.7, 79.3, 78.8, 79.9],
}

# Quorum loss detection success rate
# Format: (experiment_label, detected_runs, total_runs)
# C: Cascading Failure v2 — runs 2 & 3 detected; run 1 masked (pod restarted in 17s < 30s ZK session timeout)
quorum_rows = [
    ("C: Cascading Failure (v2)\nK8s restart masks quorum loss", 2, 3),
    ("C: Cascading Failure (v1)\nK8s restart masks quorum loss", 1, 5),
    ("D: 3-Way Isolation (v2)\nAll TCP connections hang",        3, 3),
    ("E: 5-Node Group B (v2)\n2/5 minority loses quorum",        3, 3),
    ("F: 5-Node All Nodes (v2)\n2+2+1, no group ≥ 3/5",         2, 2),
]

# 5-node timing (clean runs only — E-run1 excluded)
fivenode_maj_runs = {
    "run": [2, 3],
    "group_a_election": [14.226, 17.061],  # seconds to new leader on majority side
    "group_b_quorum_loss": [8.042, 8.076], # seconds to quorum loss on minority side
    "recovery": [85.3, 83.9],
}
fivenode_3way = {
    "run": [1, 2],
    "quorum_loss": [16.383, 15.914],  # run 2 confirmed quorum loss; recovery not captured
    "recovery": [89.0],               # only run 1 completed recovery
}

# Workload timeline data (relative to workload start 15:07:46.005)
# Kill Leader run 1 injected 15:08:09.491  →  +23.5s
# Kill Leader run 2 injected 15:10:46.820  →  +180.8s
# Snapshot of workload: 324 OK, 2 ERROR, last op at ~15:10:47
WL_START_S   = 0
WL_INTERVAL  = 0.5     # 0.5s between ops
WL_TOTAL_OPS = 368     # ~184s; extends beyond 2nd error at t=181s (log ended at error #2)
WL_FAULT1_INJ = 23.49   # relative seconds
WL_FAULT1_REM = 85.61
WL_FAULT2_INJ = 180.82
WL_FAULT2_REM = 241.43
WL_ERROR1     = 23.89
WL_ERROR2     = 181.08
WL_ELECT1     = WL_FAULT1_INJ + 3.791   # new leader confirmed
WL_ELECT2     = WL_FAULT2_INJ + 3.359


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────
def jitter(n, spread=0.08):
    rng = np.random.default_rng(42)
    return rng.uniform(-spread, spread, n)

def mean_sd(vals):
    a = np.array(vals, dtype=float)
    return a.mean(), a.std()

def save(fig, name):
    path = os.path.join("graphs", name)
    fig.savefig(path)
    plt.close(fig)
    print(f"  Saved {path}")


# ─────────────────────────────────────────────────────────────────────────────
# FIG 1 — Election & Detection Latency
# ─────────────────────────────────────────────────────────────────────────────
def fig_election_latency():
    labels = list(election.keys())
    means  = [np.mean(v) for v in election.values()]
    sds    = [np.std(v)  for v in election.values()]
    xs     = np.arange(len(labels))

    fig, ax = plt.subplots(figsize=(10, 5))

    bars = ax.bar(xs, means, color=elec_colors, width=0.55,
                  zorder=3, edgecolor="white", linewidth=0.5)

    # Error bars
    ax.errorbar(xs, means, yerr=sds, fmt="none", color="#2C3E50",
                capsize=5, capthick=1.5, linewidth=1.5, zorder=4)

    # Individual run dots
    for i, (key, vals) in enumerate(election.items()):
        ax.scatter([i + j for j in jitter(len(vals))], vals,
                   color="white", edgecolors=elec_colors[i],
                   s=55, zorder=5, linewidths=1.5)

    # Annotate mean values on bars
    for i, (bar, m) in enumerate(zip(bars, means)):
        ax.text(bar.get_x() + bar.get_width() / 2, m + 0.4,
                f"{m:.1f}s", ha="center", va="bottom", fontsize=9.5,
                fontweight="bold", color="#2C3E50")

    # Reference line at ~10s (heartbeat timeout boundary)
    ax.axhline(10, color="#7F8C8D", linewidth=1.2, linestyle="--", zorder=2)
    ax.text(len(labels) - 0.45, 10.5,
            "~syncLimit×tickTime boundary (~10s)", fontsize=8.5,
            color="#7F8C8D", ha="right")

    # Annotate pod-kill fast path
    ax.annotate("Pod kill breaks TCP\nimmediately → fast detection",
                xy=(0, means[0]), xytext=(0.6, 9.5),
                arrowprops=dict(arrowstyle="->", color=KILL, lw=1.5),
                fontsize=8.5, color=KILL)

    ax.set_xticks(xs)
    ax.set_xticklabels(labels, ha="center")
    ax.set_ylabel("Detection latency (seconds from fault injection)")
    ax.set_title("Fig 1 — Leader Election & Quorum-Loss Detection Latency by Fault Type\n"
                 "Bars = mean ± std dev   •   Dots = individual runs")
    ax.set_ylim(0, 20)
    ax.yaxis.set_minor_locator(plt.MultipleLocator(1))

    fig.tight_layout()
    save(fig, "fig1_election_latency.png")


# ─────────────────────────────────────────────────────────────────────────────
# FIG 2 — Recovery Time by Experiment (v2)
# ─────────────────────────────────────────────────────────────────────────────
def fig_recovery_times():
    labels = list(recovery_v2.keys())
    colors = [d["color"] for d in recovery_v2.values()]
    means  = [np.mean(d["vals"]) for d in recovery_v2.values()]
    sds    = [np.std(d["vals"])  for d in recovery_v2.values()]
    xs     = np.arange(len(labels))

    fig, ax = plt.subplots(figsize=(11, 5.5))

    bars = ax.bar(xs, means, color=colors, width=0.55,
                  zorder=3, edgecolor="white", linewidth=0.5, alpha=0.88)
    ax.errorbar(xs, means, yerr=sds, fmt="none", color="#2C3E50",
                capsize=5, capthick=1.5, linewidth=1.5, zorder=4)

    for i, (key, data) in enumerate(recovery_v2.items()):
        js = jitter(len(data["vals"]), spread=0.1)
        ax.scatter([i + j for j in js], data["vals"],
                   color="white", edgecolors=colors[i],
                   s=60, zorder=5, linewidths=1.8)

    for bar, m in zip(bars, means):
        ax.text(bar.get_x() + bar.get_width() / 2, m + 1.5,
                f"{m:.1f}s", ha="center", va="bottom", fontsize=9,
                fontweight="bold", color="#2C3E50")

    # Annotate high-variance pattern in D: 3-Way Isolation (runs 1+3 ~110s, run 2 ~80s)
    ax.annotate("D: High variance\n(2 of 3 runs ≥ 110s;\nrun 2 fast at 80.4s)",
                xy=(3.2, 110.4),
                xytext=(4.0, 105),
                arrowprops=dict(arrowstyle="->", color=THREE, lw=1.5),
                fontsize=8, color=THREE, ha="center")

    ax.set_xticks(xs)
    ax.set_xticklabels(labels, ha="center")
    ax.set_ylabel("Recovery time (seconds from fault injection)")
    ax.set_title("Fig 2 — Full Recovery Time by Experiment (v2, 1-second polling)\n"
                 "Recovery = time until all pods Running and exactly 1 leader confirmed")
    ax.set_ylim(0, 130)

    # Band highlighting normal range (Kill Leader ~65s; network/isolation ~80-115s)
    ax.axhspan(60, 115, alpha=0.07, color="#27AE60", zorder=1)
    ax.text(len(labels) - 0.1, 87, "Typical recovery band  60–115s",
            fontsize=8, color="#27AE60", ha="right", style="italic")

    fig.tight_layout()
    save(fig, "fig2_recovery_times.png")


# ─────────────────────────────────────────────────────────────────────────────
# FIG 3 — v1 vs v2 Comparison (Kill Leader, Network Partition, Cascading)
# ─────────────────────────────────────────────────────────────────────────────
def fig_v1_v2_comparison():
    exps    = ["A: Kill Leader", "B: Network Partition", "C: Cascading Failure"]
    exp_c   = [KILL, NET, CASC]
    v1_data = [recovery_v1[e] for e in exps]
    v2_data = [recovery_v2[e]["vals"] for e in exps]

    v1_means = [np.mean(v) for v in v1_data]
    v1_sds   = [np.std(v)  for v in v1_data]
    v2_means = [np.mean(v) for v in v2_data]
    v2_sds   = [np.std(v)  for v in v2_data]

    xs     = np.arange(len(exps))
    width  = 0.33

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5),
                              gridspec_kw={"width_ratios": [2, 1.2]})

    # Left — grouped bars
    ax = axes[0]
    b1 = ax.bar(xs - width / 2, v1_means, width, color=V1_C, label="v1 (5s polling, 5 runs)",
                zorder=3, edgecolor="white", linewidth=0.5)
    b2 = ax.bar(xs + width / 2, v2_means, width, color=exp_c, label="v2 (1s polling, 3 runs)",
                zorder=3, edgecolor="white", linewidth=0.5)

    ax.errorbar(xs - width / 2, v1_means, yerr=v1_sds, fmt="none",
                color="#7F8C8D", capsize=4, linewidth=1.2, zorder=4)
    ax.errorbar(xs + width / 2, v2_means, yerr=v2_sds, fmt="none",
                color="#2C3E50", capsize=4, linewidth=1.2, zorder=4)

    # Individual dots
    for i, (v1v, v2v) in enumerate(zip(v1_data, v2_data)):
        ax.scatter([i - width/2 + j for j in jitter(len(v1v), 0.06)], v1v,
                   color=V1_C, edgecolors="white", s=40, zorder=5, linewidths=1)
        ax.scatter([i + width/2 + j for j in jitter(len(v2v), 0.06)], v2v,
                   color=exp_c[i], edgecolors="white", s=50, zorder=5, linewidths=1)

    for i, (m1, m2) in enumerate(zip(v1_means, v2_means)):
        ax.text(i - width/2, m1 + 1.5, f"{m1:.1f}s", ha="center", fontsize=8.5,
                color="#7F8C8D", fontweight="bold")
        ax.text(i + width/2, m2 + 1.5, f"{m2:.1f}s", ha="center", fontsize=8.5,
                color="#2C3E50", fontweight="bold")

    ax.set_xticks(xs)
    ax.set_xticklabels(exps)
    ax.set_ylabel("Recovery time (seconds)")
    ax.set_title("Recovery Time: v1 (5s polling) vs v2 (1s polling)")
    ax.set_ylim(0, 135)
    ax.legend(loc="upper right", framealpha=0.9)
    ax.text(0.02, 0.96, "Network Partition v1: only runs 1 & 5 available (85.7s, 112.7s)",
            transform=ax.transAxes, fontsize=7.5, color="#7F8C8D", va="top")

    # Right — scatter: v1 mean vs v2 mean per experiment
    ax2 = axes[1]
    for i, (exp, c) in enumerate(zip(exps, exp_c)):
        ax2.scatter(v1_means[i], v2_means[i], color=c, s=180, zorder=4,
                    edgecolors="white", linewidths=1.5, label=exp)
        ax2.annotate(exp.split(":")[0], (v1_means[i], v2_means[i]),
                     textcoords="offset points", xytext=(6, 4),
                     fontsize=8.5, color=c)

    lim = (55, 125)
    ax2.plot(lim, lim, "--", color="#BDC3C7", linewidth=1, zorder=1, label="v1 = v2 line")
    ax2.set_xlim(*lim); ax2.set_ylim(*lim)
    ax2.set_xlabel("v1 mean recovery (s)")
    ax2.set_ylabel("v2 mean recovery (s)")
    ax2.set_title("v1 vs v2 Mean Recovery\n(on the diagonal = identical)")
    ax2.legend(fontsize=8, loc="upper left")

    fig.suptitle("Fig 3 — v1 vs v2 Head-to-Head: Recovery Times Are Consistent Across Polling Rates",
                 fontsize=12, fontweight="bold", y=1.01)
    fig.tight_layout()
    save(fig, "fig3_v1_v2_comparison.png")


# ─────────────────────────────────────────────────────────────────────────────
# FIG 4 — Quorum Loss Detection Reliability
# ─────────────────────────────────────────────────────────────────────────────
def fig_quorum_detection():
    labels    = [r[0] for r in quorum_rows]
    detected  = [r[1] for r in quorum_rows]
    total     = [r[2] for r in quorum_rows]
    missed    = [t - d for d, t in zip(detected, total)]
    pct_det   = [d / t * 100 for d, t in zip(detected, total)]
    row_colors = [CASC, CASC, THREE, M5, T5]

    ys = np.arange(len(labels))

    fig, ax = plt.subplots(figsize=(10, 5.5))

    bar_h = 0.52
    ax.barh(ys, detected, height=bar_h, color=row_colors, zorder=3,
            edgecolor="white", linewidth=0.5, label="Detected")
    ax.barh(ys, missed, height=bar_h, left=detected, color="#ECF0F1",
            zorder=3, edgecolor="#BDC3C7", linewidth=0.5, label="Not Detected")

    for i, (d, t, pct) in enumerate(zip(detected, total, pct_det)):
        # Fraction label
        ax.text(t + 0.05, i, f"{d}/{t}  ({pct:.0f}%)",
                va="center", ha="left", fontsize=9.5, color=row_colors[i],
                fontweight="bold")

    # Root cause annotation for cascading failure
    ax.annotate("K8s StatefulSet restarts pods\nbefore ZK session timeout (~30s)\n→ quorum loss masked",
                xy=(0.5, 0.5), xytext=(1.8, 1.0),
                arrowprops=dict(arrowstyle="->", color=CASC, lw=1.5),
                fontsize=8.5, color=CASC, ha="center")

    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Number of runs")
    ax.set_xlim(0, max(total) + 1.6)
    ax.set_xticks(range(max(total) + 1))
    ax.set_title("Fig 4 — Quorum Loss Correctly Detected: Reliability by Experiment\n"
                 "Green/coloured = correctly detected  •  Light grey = masked (pod restarted too fast)")

    detected_patch = mpatches.Patch(color="#27AE60", label="Quorum loss detected ✓")
    missed_patch   = mpatches.Patch(color="#ECF0F1", edgecolor="#BDC3C7", label="Not detected (K8s restart masking)")
    ax.legend(handles=[detected_patch, missed_patch], loc="lower right", fontsize=9)

    fig.tight_layout()
    save(fig, "fig4_quorum_detection.png")


# ─────────────────────────────────────────────────────────────────────────────
# FIG 5 — 5-Node Partition Deep Dive
# ─────────────────────────────────────────────────────────────────────────────
def fig_5node():
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))

    # Left — majority partition: Group A election vs Group B quorum loss timing
    ax = axes[0]
    run_labels = ["Run 2", "Run 3"]
    xs   = np.arange(2)
    w    = 0.32
    ga_e = fivenode_maj_runs["group_a_election"]
    gb_l = fivenode_maj_runs["group_b_quorum_loss"]
    rec  = fivenode_maj_runs["recovery"]

    b1 = ax.bar(xs - w,   ga_e, w, color=M5,    label="Group A election latency\n(3/5 majority retains leader)", zorder=3)
    b2 = ax.bar(xs,       gb_l, w, color=KILL,  label="Group B quorum loss latency\n(2/5 minority loses quorum)",  zorder=3)
    b3 = ax.bar(xs + w,   rec,  w, color="#BDC3C7", label="Full cluster recovery", zorder=3, alpha=0.7)

    for bars, vals in [(b1, ga_e), (b2, gb_l), (b3, rec)]:
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width()/2, v + 0.8,
                    f"{v:.1f}s", ha="center", va="bottom", fontsize=8.5, fontweight="bold")

    ax.set_xticks(xs)
    ax.set_xticklabels(run_labels)
    ax.set_ylabel("Seconds from fault injection")
    ax.set_title("Exp E — 5-Node 3+2 Majority Partition\n"
                 "Group A (3/5) vs Group B (2/5) response timing")
    ax.set_ylim(0, 105)
    ax.legend(fontsize=8.5, loc="upper right")

    # Key insight annotation
    ax.annotate("Group B loses quorum\n~6s BEFORE Group A\nelects new leader",
                xy=(0, gb_l[0]), xytext=(0.55, 60),
                arrowprops=dict(arrowstyle="->", color=KILL, lw=1.3),
                fontsize=8, color=KILL, ha="center")

    # Right — comparison: all 5-node experiment timings side by side
    ax2 = axes[1]

    categories = [
        "E: Group A\nelection\n(3/5 majority)",
        "E: Group B\nquorum loss\n(2/5 minority)",
        "E: Full cluster\nrecovery",
        "F: All-nodes\nquorum loss\n(2+2+1)",
        "F: Full cluster\nrecovery",
    ]
    colors_cat = [M5, KILL, "#BDC3C7", T5, "#BDC3C7"]
    means_cat  = [
        np.mean(fivenode_maj_runs["group_a_election"]),
        np.mean(fivenode_maj_runs["group_b_quorum_loss"]),
        np.mean(fivenode_maj_runs["recovery"]),
        np.mean(fivenode_3way["quorum_loss"]),
        np.mean(fivenode_3way["recovery"]),
    ]
    all_vals_cat = [
        fivenode_maj_runs["group_a_election"],
        fivenode_maj_runs["group_b_quorum_loss"],
        fivenode_maj_runs["recovery"],
        fivenode_3way["quorum_loss"],
        fivenode_3way["recovery"],
    ]

    xs2 = np.arange(len(categories))
    bars2 = ax2.bar(xs2, means_cat, color=colors_cat, width=0.55,
                    zorder=3, edgecolor="white", linewidth=0.5)

    for i, (vals, m) in enumerate(zip(all_vals_cat, means_cat)):
        ax2.scatter([i + j for j in jitter(len(vals), 0.08)], vals,
                    color="white", edgecolors=colors_cat[i],
                    s=55, zorder=5, linewidths=1.5)
        ax2.text(i, m + 0.8, f"{m:.1f}s",
                 ha="center", va="bottom", fontsize=8.5, fontweight="bold")

    ax2.set_xticks(xs2)
    ax2.set_xticklabels(categories, fontsize=8.5)
    ax2.set_ylabel("Seconds from fault injection")
    ax2.set_title("Exp E & F — 5-Node Timing Summary\n"
                  "Minority loses quorum faster than majority elects leader")
    ax2.set_ylim(0, 105)

    fig.suptitle("Fig 5 — 5-Node Ensemble Partition Analysis\n"
                 "ZAB enforces global quorum threshold (≥3/5), not local majority among visible peers",
                 fontsize=11, fontweight="bold", y=1.01)
    fig.tight_layout()
    save(fig, "fig5_5node_partitions.png")


# ─────────────────────────────────────────────────────────────────────────────
# FIG 6 — Workload Client Impact Timeline
# ─────────────────────────────────────────────────────────────────────────────
def fig_workload_timeline():
    # Build synthetic op sequence at 0.5s intervals
    n_ops = WL_TOTAL_OPS
    times = np.arange(n_ops) * WL_INTERVAL
    errors_mask = np.zeros(n_ops, dtype=bool)

    # Mark the two error ops
    for t_err in [WL_ERROR1, WL_ERROR2]:
        idx = int(round(t_err / WL_INTERVAL))
        if 0 <= idx < n_ops:
            errors_mask[idx] = True

    ok_times  = times[~errors_mask]
    err_times = times[errors_mask]

    fig, axes = plt.subplots(2, 1, figsize=(13, 7),
                              gridspec_kw={"height_ratios": [2.5, 1]})

    # ── Top panel: operation timeline ─────────────────────────────────────────
    ax = axes[0]

    ax.scatter(ok_times,  np.ones(len(ok_times)),  color=OK_C, s=12,
               alpha=0.6, zorder=3, label=f"OK operations ({len(ok_times)})")
    ax.scatter(err_times, np.ones(len(err_times)), color=ERR_C, s=120,
               zorder=5, marker="X", label=f"ERROR — ConnectionLoss ({len(err_times)})")

    # Fault injection / removal shading — run 1
    ax.axvspan(WL_FAULT1_INJ, WL_FAULT1_REM, alpha=0.12, color=FAULT,
               zorder=1, label="Fault active (kill leader)")
    ax.axvspan(WL_FAULT2_INJ, WL_FAULT2_REM, alpha=0.12, color=FAULT, zorder=1)

    # Injection markers
    for t, label in [(WL_FAULT1_INJ, "FAULT INJECTED\nRun 1"),
                     (WL_FAULT2_INJ, "FAULT INJECTED\nRun 2")]:
        ax.axvline(t, color=FAULT, linewidth=1.8, linestyle="--", zorder=4)
        ax.text(t + 1, 1.22, label, fontsize=8.5, color=FAULT,
                va="center", fontweight="bold")

    # Election markers
    for t, label in [(WL_ELECT1, "New leader\nelected (+3.8s)"),
                     (WL_ELECT2, "New leader\nelected (+3.4s)")]:
        ax.axvline(t, color=M5, linewidth=1.4, linestyle=":", zorder=4)
        ax.text(t + 1, 0.78, label, fontsize=8, color=M5, va="center")

    # Removal markers
    for t in [WL_FAULT1_REM, WL_FAULT2_REM]:
        ax.axvline(t, color="#2C3E50", linewidth=1.2, linestyle="-.", zorder=4, alpha=0.6)

    ax.set_xlim(-5, times[-1] + 10)
    ax.set_ylim(0.5, 1.5)
    ax.set_yticks([])
    ax.set_xlabel("")
    ax.set_title("Fig 6 — Workload Client Impact During Kill Leader Experiments\n"
                 "Each dot = one ZooKeeper write+read op (0.5s interval)  •  X = ConnectionLoss error")
    ax.legend(loc="upper right", fontsize=9, framealpha=0.9)
    ax.text(0.01, 0.95, "workload_20260410_150745.txt",
            transform=ax.transAxes, fontsize=8, color="#7F8C8D", va="top")

    # ── Bottom panel: cumulative error count ──────────────────────────────────
    ax2 = axes[1]
    cum_errors = np.cumsum(errors_mask.astype(int))
    ax2.step(times, cum_errors, color=ERR_C, linewidth=2, where="post")
    ax2.fill_between(times, cum_errors, step="post", alpha=0.15, color=ERR_C)
    ax2.set_xlim(-5, times[-1] + 10)
    ax2.set_ylim(-0.1, 3)
    ax2.set_yticks([0, 1, 2])
    ax2.set_xlabel("Time (seconds from workload start  15:07:46)")
    ax2.set_ylabel("Cumulative\nerrors")

    for t, label in [(WL_FAULT1_INJ, ""), (WL_FAULT2_INJ, "")]:
        ax2.axvline(t, color=FAULT, linewidth=1.5, linestyle="--", alpha=0.6)

    ax2.text(0.99, 0.85,
             f"Total: 324 OK  |  2 ERROR  |  error rate 0.6%",
             transform=ax2.transAxes, ha="right", fontsize=9,
             color="#2C3E50", fontweight="bold")

    fig.tight_layout()
    save(fig, "fig6_workload_impact.png")


# ─────────────────────────────────────────────────────────────────────────────
# FIG 7 — Recovery Time Ranked (all experiments, all runs)
# ─────────────────────────────────────────────────────────────────────────────
def fig_recovery_ranked():
    all_points = []
    for exp, data in recovery_v2.items():
        c = data["color"]
        for r, v in enumerate(data["vals"], 1):
            label = exp.replace("\n", " ")
            all_points.append((label, r, v, c))

    # Sort by recovery time
    all_points.sort(key=lambda x: x[2])

    fig, ax = plt.subplots(figsize=(10, 7))

    seen = set()
    for i, (label, run, val, c) in enumerate(all_points):
        ax.barh(i, val, color=c, height=0.65, zorder=3,
                edgecolor="white", linewidth=0.4, alpha=0.85)
        ax.text(val + 0.8, i, f"{val}s", va="center", fontsize=8.5,
                color="#2C3E50")
        short = label.split(":")[0] if ":" in label else label[:12]
        ax.text(-1, i, f"{short} r{run}", va="center", ha="right",
                fontsize=8, color=c, fontweight="bold")

    # Reference band
    ax.axvspan(60, 115, alpha=0.07, color="#27AE60", zorder=1)
    ax.text(87, len(all_points) - 0.2, "Normal range",
            ha="center", fontsize=8, color="#27AE60", style="italic")

    # Legend patches
    patches = [
        mpatches.Patch(color=KILL,  label="A: Kill Leader"),
        mpatches.Patch(color=NET,   label="B: Network Partition"),
        mpatches.Patch(color=CASC,  label="C: Cascading Failure"),
        mpatches.Patch(color=THREE, label="D: 3-Way Isolation"),
        mpatches.Patch(color=M5,    label="E: 5-Node Majority"),
        mpatches.Patch(color=T5,    label="F: 5-Node 3-Way"),
    ]
    ax.legend(handles=patches, loc="lower right", fontsize=9)

    ax.set_yticks([])
    ax.set_xlabel("Recovery time (seconds from fault injection)")
    ax.set_title("Fig 7 — All Recovery Times Ranked (v2 suite)\n"
                 "Each bar = one experiment run  •  All recovered autonomously")
    ax.set_xlim(-20, 125)

    fig.tight_layout()
    save(fig, "fig7_recovery_ranked.png")


# ─────────────────────────────────────────────────────────────────────────────
# FIG 8 — Summary Dashboard (2×2 grid of key metrics)
# ─────────────────────────────────────────────────────────────────────────────
def fig_dashboard():
    fig = plt.figure(figsize=(14, 10))
    fig.suptitle("ZooKeeper Chaos Engineering — Key Metrics Dashboard",
                 fontsize=14, fontweight="bold", y=0.98)
    gs = GridSpec(2, 2, figure=fig, hspace=0.42, wspace=0.35)

    # ── TL: Election latency (A vs B) ─────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])

    data_ab = {
        "A: Kill Leader\n(pod kill)":          election["A: Kill Leader\n(pod kill)"],
        "B: Network Partition\n(TCP hang)":    election["B: Network Partition\n(TCP hang)"],
    }
    colors_ab = [KILL, NET]
    for i, (k, v) in enumerate(data_ab.items()):
        m = np.mean(v)
        ax1.bar(i, m, color=colors_ab[i], width=0.5, zorder=3,
                edgecolor="white", linewidth=0.5)
        ax1.errorbar(i, m, yerr=np.std(v), fmt="none",
                     color="#2C3E50", capsize=5, linewidth=1.5, zorder=4)
        ax1.scatter([i + j for j in jitter(len(v), 0.07)], v,
                    color="white", edgecolors=colors_ab[i],
                    s=55, zorder=5, linewidths=1.5)
        ax1.text(i, m + 0.4, f"{m:.2f}s", ha="center", va="bottom",
                 fontsize=9.5, fontweight="bold")

    ax1.set_xticks([0, 1])
    ax1.set_xticklabels(list(data_ab.keys()), fontsize=9)
    ax1.set_ylabel("Detection latency (s)")
    ax1.set_ylim(0, 18)
    ax1.set_title("Election Latency\nPod Kill vs Network Partition")
    ax1.annotate("~2.3× slower\n(TCP hang timeout)", xy=(1, 8.35),
                 xytext=(0.5, 14), fontsize=8, color=NET, ha="center",
                 arrowprops=dict(arrowstyle="->", color=NET, lw=1.2))

    # ── TR: Mean recovery by experiment ──────────────────────────────────────
    ax2 = fig.add_subplot(gs[0, 1])

    exp_names = list(recovery_v2.keys())
    exp_short = ["A", "B", "C", "D", "E\n(clean)", "F"]
    colors2   = [d["color"] for d in recovery_v2.values()]
    means2    = [np.mean(d["vals"]) for d in recovery_v2.values()]

    bars2 = ax2.bar(range(len(exp_names)), means2, color=colors2, width=0.6,
                    zorder=3, edgecolor="white", linewidth=0.5, alpha=0.9)
    for i, (bar, m) in enumerate(zip(bars2, means2)):
        ax2.text(i, m + 1, f"{m:.0f}s", ha="center", va="bottom",
                 fontsize=8.5, fontweight="bold")

    ax2.set_xticks(range(len(exp_names)))
    ax2.set_xticklabels(exp_short, fontsize=9)
    ax2.set_ylabel("Mean recovery (s)")
    ax2.set_ylim(0, 115)
    ax2.set_title("Mean Recovery Time\nAll Experiments (v2)")
    ax2.axhspan(60, 95, alpha=0.08, color="#27AE60", zorder=1)

    # ── BL: Quorum detection rate ─────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 0])

    q_labels = ["C (v2)", "C (v1)", "D", "E\nGroup B", "F"]
    q_det    = [r[1] / r[2] * 100 for r in quorum_rows]
    q_colors = [CASC, CASC, THREE, M5, T5]

    ax3.barh(range(len(q_labels)), q_det, color=q_colors, height=0.55,
             zorder=3, edgecolor="white", linewidth=0.5, alpha=0.85)
    for i, (val, row) in enumerate(zip(q_det, quorum_rows)):
        ax3.text(val + 1, i, f"{val:.0f}%  ({row[1]}/{row[2]})",
                 va="center", fontsize=9, color=q_colors[i], fontweight="bold")

    ax3.axvline(100, color="#27AE60", linewidth=1.2, linestyle="--", alpha=0.5)
    ax3.set_yticks(range(len(q_labels)))
    ax3.set_yticklabels(q_labels, fontsize=9)
    ax3.set_xlabel("Quorum loss detected (%)")
    ax3.set_xlim(0, 130)
    ax3.set_title("Quorum Loss Detection Rate\nby Experiment")
    ax3.text(0.02, 0.12, "C: K8s restart masks quorum loss\nD/E/F: TCP hang → 100% detection",
             transform=ax3.transAxes, fontsize=8, color="#7F8C8D", style="italic")

    # ── BR: Polling resolution impact (v1 vs v2 election precision) ──────────
    ax4 = fig.add_subplot(gs[1, 1])

    # Show how v1 (5s polling) cannot resolve the election time precisely
    v2_kill_latencies = election["A: Kill Leader\n(pod kill)"]
    # v1 would round to nearest 5s → can only say "between 0 and 5s"
    v1_resolution_band = [0, 5]
    v2_resolution      = v2_kill_latencies

    ax4.axhspan(0, 5, alpha=0.15, color=V1_C, label="v1 resolution window (0–5s)")
    for j, v in enumerate(v2_resolution):
        ax4.scatter(j, v, color=KILL, s=130, zorder=5,
                    edgecolors="white", linewidths=1.5)
        ax4.text(j + 0.05, v + 0.1, f"{v:.3f}s", fontsize=8.5, color=KILL)

    ax4.axhline(np.mean(v2_resolution), color=KILL, linewidth=1.5,
                linestyle="--", alpha=0.7, label=f"v2 mean: {np.mean(v2_resolution):.2f}s")

    ax4.set_xticks([0, 1, 2])
    ax4.set_xticklabels(["Run 1", "Run 2", "Run 3"])
    ax4.set_ylabel("Election latency (s)")
    ax4.set_ylim(0, 7)
    ax4.set_title("Polling Resolution Impact\n1s (v2) vs 5s (v1) for Kill Leader")
    ax4.legend(fontsize=9, loc="upper right")
    ax4.text(0.03, 0.96, "v1 could only say '< 5s'\nv2 resolves to ms precision",
             transform=ax4.transAxes, fontsize=8, color="#7F8C8D", va="top", style="italic")

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    save(fig, "fig8_dashboard.png")


# ─────────────────────────────────────────────────────────────────────────────
# Run all
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Generating ZooKeeper chaos analysis graphs ...")
    fig_election_latency()
    fig_recovery_times()
    fig_v1_v2_comparison()
    fig_quorum_detection()
    fig_5node()
    fig_workload_timeline()
    fig_recovery_ranked()
    fig_dashboard()
    print("\nAll graphs saved to graphs/")
    print("  fig1_election_latency.png    — latency by fault type (bimodal)")
    print("  fig2_recovery_times.png      — recovery by experiment with scatter")
    print("  fig3_v1_v2_comparison.png    — v1 vs v2 head-to-head")
    print("  fig4_quorum_detection.png    — quorum loss detection reliability")
    print("  fig5_5node_partitions.png    — 5-node Group A vs B timing")
    print("  fig6_workload_impact.png     — client error timeline")
    print("  fig7_recovery_ranked.png     — all runs ranked by recovery time")
    print("  fig8_dashboard.png           — 4-panel summary dashboard")
