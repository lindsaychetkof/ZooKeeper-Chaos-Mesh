#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_final_experiments.py

ZooKeeper Chaos Engineering Suite - 15 runs total:
  Experiment A: Kill Leader        (5 runs)
  Experiment B: Network Partition  (5 runs)
  Experiment C: Cascading Failure  (5 runs)

New files created (existing files are NEVER touched):
  log_kill_leader_run{1-5}.txt
  log_network_partition_run{1-5}.txt
  log_cascading_failure_run{1-5}.txt
  log_all_experiments_master.txt
  results_live.csv
  chaos_kill_leader_run{N}_dynamic.yaml
  chaos_partition_run{N}_dynamic.yaml
  chaos_cascade_follower_run{N}_dynamic.yaml
  chaos_cascade_leader_run{N}_dynamic.yaml
"""

import csv
import os
import subprocess
import sys
import time
from datetime import datetime

# Force UTF-8 output on Windows so box-drawing characters work
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# -- Constants -----------------------------------------------------------------
ZK_PODS   = ["zookeeper-0", "zookeeper-1", "zookeeper-2"]
ZK_BIN    = "/apache-zookeeper-3.9.3-bin/bin/zkServer.sh"
NAMESPACE = "default"
MASTER_LOG   = "log_all_experiments_master.txt"
RESULTS_LIVE = "results_live.csv"
RESULTS_LIVE_FIELDS = [
    "experiment", "run", "leader_before", "leader_after",
    "leadership_changed", "injection_time", "recovery_time",
    "recovery_seconds", "quorum_lost", "notes",
]

_summary_rows = []


# -- Timestamp helper ----------------------------------------------------------
def ts(dt=None) -> str:
    if dt is None:
        dt = datetime.now()
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# -- Dual Logger ---------------------------------------------------------------
class Logger:
    """Writes every line to both the run-specific log and the master log."""

    def __init__(self, run_log_path: str):
        self._run_fh = open(run_log_path, "w", buffering=1)
        self._path   = run_log_path

    def _append_master(self, line: str):
        with open(MASTER_LOG, "a", buffering=1) as mf:
            mf.write(line + "\n")

    def log(self, msg: str):
        """Timestamped log line."""
        line = f"[{ts()}] {msg}"
        self._run_fh.write(line + "\n")
        self._append_master(line)
        print(line, flush=True)

    def raw(self, msg: str):
        """Write without adding a timestamp (for header/footer blocks)."""
        self._run_fh.write(msg + "\n")
        self._append_master(msg)
        print(msg, flush=True)

    def close(self):
        self._run_fh.close()


# -- ZK status helpers ---------------------------------------------------------
def get_zk_status(pod: str) -> str:
    try:
        r = subprocess.run(
            ["kubectl", "exec", pod, "--", ZK_BIN, "status"],
            capture_output=True, text=True, timeout=15,
        )
        out = (r.stdout + r.stderr).lower()
        if "leader"      in out: return "leader"
        if "follower"    in out: return "follower"
        if "not running" in out: return "not_running"
        if "error"       in out: return "error"
        return "unknown"
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:
        return f"err:{e}"


def get_all_statuses() -> dict:
    return {pod: get_zk_status(pod) for pod in ZK_PODS}


def resolve_roles(statuses: dict):
    leader    = next((p for p, s in statuses.items() if s == "leader"), None)
    followers = [p for p, s in statuses.items() if s == "follower"]
    return leader, followers


def check_pods_running() -> tuple:
    """Returns (all_3_running: bool, raw_output: str)."""
    try:
        r = subprocess.run(
            ["kubectl", "get", "pods", "-l", "app=zookeeper", "--no-headers"],
            capture_output=True, text=True, timeout=20,
        )
        lines   = [l for l in r.stdout.strip().splitlines() if l.strip()]
        running = [l for l in lines if "Running" in l and "1/1" in l]
        return len(running) == 3, r.stdout.strip()
    except Exception as e:
        return False, str(e)


def poll_statuses(logger: Logger, label: str = "") -> dict:
    """Poll all 3 pods, log the result, return statuses dict."""
    statuses   = get_all_statuses()
    status_str = " | ".join(f"{p}:{s}" for p, s in statuses.items())
    prefix     = f"POLL{' ' + label if label else ''}"
    logger.log(f"{prefix}: {status_str}")
    return statuses


# -- Preflight -----------------------------------------------------------------
def preflight(logger: Logger) -> tuple:
    """
    Checks:
      1. All 3 pods 1/1 Running (kubectl get pods)
      2. Exactly 1 leader, 2 followers (zkServer.sh status)
    Retries up to 6 times with 30 s gaps (3 min max).
    Returns (leader, follower_1, follower_2) or calls sys.exit(1).
    """
    for attempt in range(1, 7):
        now = ts()
        logger.log(f"PREFLIGHT attempt {attempt}/6 at {now}")

        pods_ok, pods_text = check_pods_running()
        if not pods_ok:
            logger.log(f"  Check 1 FAIL: not all 3 pods 1/1 Running")
            logger.log(f"  kubectl output:\n{pods_text}")
        else:
            logger.log("  Check 1 PASS: all 3 pods are 1/1 Running")

            statuses = get_all_statuses()
            for pod, s in statuses.items():
                logger.log(f"    {pod}: {s}")
            leader, followers = resolve_roles(statuses)

            if leader and len(followers) == 2:
                logger.log(f"  Check 2 PASS: leader={leader}, followers={followers}")
                logger.log(f"PREFLIGHT PASSED [{ts()}]")
                return leader, followers[0], followers[1]
            else:
                logger.log(
                    f"  Check 2 FAIL: leader={leader}, followers={followers} "
                    f"(need exactly 1 leader + 2 followers)"
                )

        if attempt < 6:
            logger.log(f"  Waiting 30 s before retry {attempt + 1}/6 ...")
            time.sleep(30)

    logger.log(f"PREFLIGHT FAILED [{ts()}] after 6 attempts (3 min). Stopping.")
    sys.exit(1)


# -- YAML builders -------------------------------------------------------------
def build_pod_kill_yaml(resource_name: str, pod_name: str) -> str:
    return f"""\
apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: {resource_name}
  namespace: {NAMESPACE}
spec:
  action: pod-kill
  mode: one
  gracePeriod: 0
  selector:
    namespaces:
      - {NAMESPACE}
    expressionSelectors:
      - key: statefulset.kubernetes.io/pod-name
        operator: In
        values:
          - {pod_name}
"""


def build_network_partition_yaml(resource_name: str,
                                  leader_pod: str,
                                  follower_pods: list) -> str:
    follower_lines = "\n".join(f"          - {p}" for p in follower_pods)
    return f"""\
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: {resource_name}
  namespace: {NAMESPACE}
spec:
  action: partition
  mode: all
  selector:
    namespaces:
      - {NAMESPACE}
    expressionSelectors:
      - key: statefulset.kubernetes.io/pod-name
        operator: In
        values:
          - {leader_pod}
  direction: both
  target:
    mode: all
    selector:
      namespaces:
        - {NAMESPACE}
      expressionSelectors:
        - key: statefulset.kubernetes.io/pod-name
          operator: In
          values:
{follower_lines}
  duration: "60s"
"""


def write_yaml_file(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)


def apply_chaos(yaml_path: str) -> datetime:
    subprocess.run(["kubectl", "apply", "-f", yaml_path], check=True, timeout=30)
    return datetime.now()


def delete_chaos(yaml_path: str) -> datetime:
    subprocess.run(
        ["kubectl", "delete", "-f", yaml_path, "--ignore-not-found=true"],
        check=True, timeout=30,
    )
    return datetime.now()


def chaos_object_exists(kind: str, name: str) -> bool:
    """Returns True if the Chaos Mesh object is still present."""
    try:
        r = subprocess.run(
            ["kubectl", "get", kind, name, "--ignore-not-found"],
            capture_output=True, text=True, timeout=15,
        )
        return name in r.stdout
    except Exception:
        return False


# -- Recovery polling ----------------------------------------------------------
def wait_full_recovery(logger: Logger,
                        inject_dt: datetime,
                        timeout_s: int = 180) -> tuple:
    """
    Polls every 5 s until all 3 pods are 1/1 Running and exactly 1 is leader.
    Returns (final_leader, recovery_seconds_from_inject_dt, recovery_datetime).
    On timeout returns (None, elapsed_s, None).
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        pods_ok, _ = check_pods_running()
        statuses   = get_all_statuses()
        status_str = " | ".join(f"{p}:{s}" for p, s in statuses.items())
        leader, followers = resolve_roles(statuses)

        logger.log(f"RECOVERY POLL: pods_running={pods_ok} | {status_str}")

        if pods_ok and leader and len(followers) == 2:
            recovery_dt = datetime.now()
            recovery_s  = round((recovery_dt - inject_dt).total_seconds(), 1)
            logger.log(
                f"RECOVERY CONFIRMED at {ts(recovery_dt)}: "
                f"leader={leader}, recovery_seconds={recovery_s}s (from injection)"
            )
            return leader, recovery_s, recovery_dt

        time.sleep(5)

    elapsed = round(time.monotonic() - start, 1)
    logger.log(f"RECOVERY TIMEOUT after {elapsed}s - cluster did not fully recover")
    return None, elapsed, None


# -- CSV helpers ---------------------------------------------------------------
def init_results_csv():
    """Always create fresh at script start."""
    with open(RESULTS_LIVE, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=RESULTS_LIVE_FIELDS).writeheader()


def append_result(row: dict):
    with open(RESULTS_LIVE, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=RESULTS_LIVE_FIELDS).writerow(row)


# -- Experiment A: Kill Leader -------------------------------------------------
def run_kill_leader(run_num: int):
    log_path = f"log_kill_leader_run{run_num}.txt"
    logger   = Logger(log_path)
    start_dt = datetime.now()

    try:
        # -- Header ------------------------------------------------------------
        logger.raw("EXPERIMENT: kill_leader")
        logger.raw(f"RUN: {run_num}")
        logger.raw(f"START TIME: {ts(start_dt)}")

        leader, follower_1, follower_2 = preflight(logger)

        logger.raw(f"LEADER AT START: {leader}")
        logger.raw(f"FOLLOWER 1: {follower_1}")
        logger.raw(f"FOLLOWER 2: {follower_2}")
        logger.raw("")

        # -- Generate YAML -----------------------------------------------------
        res_name  = f"kill-leader-r{run_num}"
        yaml_path = f"chaos_kill_leader_run{run_num}_dynamic.yaml"
        write_yaml_file(yaml_path, build_pod_kill_yaml(res_name, leader))
        logger.log(f"Generated YAML: {yaml_path} (targeting {leader})")

        # -- Inject fault ------------------------------------------------------
        inject_dt = apply_chaos(yaml_path)
        logger.log(f"FAULT INJECTED at {ts(inject_dt)}: kubectl apply -f {yaml_path}")

        # -- Poll every 5 s for 60 s -------------------------------------------
        new_leader_observed = None
        logger.log("--- Polling every 5 s for 60 seconds ---")
        poll_end = time.monotonic() + 60

        while time.monotonic() < poll_end:
            statuses = poll_statuses(logger)
            ldr, _   = resolve_roles(statuses)
            if ldr and ldr != leader and new_leader_observed is None:
                new_leader_observed = ldr
                logger.log(f"NEW LEADER ELECTED: {ldr} at {ts()}")
            time.sleep(5)

        # -- Remove fault ------------------------------------------------------
        removal_dt = delete_chaos(yaml_path)
        logger.log(f"FAULT REMOVED at {ts(removal_dt)}: kubectl delete -f {yaml_path}")

        # -- Poll until full recovery -------------------------------------------
        logger.log("--- Polling for full recovery (all 3 pods Running, exactly 1 leader) ---")
        final_leader, recovery_s, recovery_dt = wait_full_recovery(logger, inject_dt)

        # -- Footer ------------------------------------------------------------
        end_dt = datetime.now()
        leadership_changed = "yes" if (final_leader and final_leader != leader) else "no"
        notes_parts = []
        if new_leader_observed:
            notes_parts.append(f"new leader elected during fault: {new_leader_observed}")
        else:
            notes_parts.append("no leader change observed during 60s fault window")
        if final_leader is None:
            notes_parts.append("RECOVERY TIMEOUT")

        logger.raw("")
        logger.raw(f"EXPERIMENT END TIME: {ts(end_dt)}")
        logger.raw(f"LEADER AT END: {final_leader or 'unknown'}")
        logger.raw(f"LEADERSHIP CHANGED: {leadership_changed}")
        logger.raw(f"TOTAL DOWNTIME SECONDS: {recovery_s}")
        logger.raw(f"RECOVERY SECONDS: {recovery_s}")
        logger.raw(f"NOTES: {'; '.join(notes_parts)}")

        # -- CSV row -----------------------------------------------------------
        append_result({
            "experiment":         "kill_leader",
            "run":                run_num,
            "leader_before":      leader,
            "leader_after":       final_leader or "unknown",
            "leadership_changed": leadership_changed,
            "injection_time":     ts(inject_dt),
            "recovery_time":      ts(recovery_dt) if recovery_dt else "timeout",
            "recovery_seconds":   recovery_s,
            "quorum_lost":        "no",
            "notes":              "; ".join(notes_parts),
        })

        _summary_rows.append({
            "experiment":    "kill_leader",
            "run":           run_num,
            "leader_before": leader,
            "leader_after":  final_leader or "unknown",
            "recovery_s":    recovery_s,
        })
        logger.log(f"Run complete - log saved to {log_path}")

    finally:
        logger.close()


# -- Experiment B: Network Partition ------------------------------------------
def run_network_partition(run_num: int):
    log_path = f"log_network_partition_run{run_num}.txt"
    logger   = Logger(log_path)
    start_dt = datetime.now()

    try:
        # -- Header ------------------------------------------------------------
        logger.raw("EXPERIMENT: network_partition")
        logger.raw(f"RUN: {run_num}")
        logger.raw(f"START TIME: {ts(start_dt)}")

        leader, follower_1, follower_2 = preflight(logger)

        logger.raw(f"LEADER AT START: {leader}")
        logger.raw(f"FOLLOWER 1: {follower_1}")
        logger.raw(f"FOLLOWER 2: {follower_2}")
        logger.raw("")

        # -- Generate YAML -----------------------------------------------------
        res_name      = f"zk-partition-r{run_num}"
        yaml_path     = f"chaos_partition_run{run_num}_dynamic.yaml"
        follower_pods = [follower_1, follower_2]
        write_yaml_file(yaml_path,
                        build_network_partition_yaml(res_name, leader, follower_pods))
        logger.log(f"Generated YAML: {yaml_path}")
        logger.log(
            f"  action=partition, direction=both, source={leader}, "
            f"target={follower_pods}, duration=60s"
        )

        # -- Inject fault ------------------------------------------------------
        inject_dt = apply_chaos(yaml_path)
        logger.log(f"FAULT INJECTED at {ts(inject_dt)}: kubectl apply -f {yaml_path}")

        # -- Poll every 5 s for 75 s -------------------------------------------
        new_leader_among_followers = None
        split_brain_detected       = False
        logger.log("--- Polling every 5 s for 75 seconds (duration=60s + 15s buffer) ---")
        poll_end = time.monotonic() + 75

        while time.monotonic() < poll_end:
            statuses = poll_statuses(logger)

            isolated_status    = statuses.get(leader, "unknown")
            followers_as_leader = [p for p in follower_pods if statuses.get(p) == "leader"]

            if isolated_status == "leader" and followers_as_leader:
                split_brain_detected = True
                logger.log(
                    f"  SPLIT-BRAIN WINDOW OBSERVED: isolated leader {leader} "
                    f"still reports 'leader' while follower side also elected "
                    f"leader: {followers_as_leader}"
                )

            if followers_as_leader and new_leader_among_followers is None:
                new_leader_among_followers = followers_as_leader[0]
                logger.log(
                    f"  NEW LEADER ELECTED AMONG FOLLOWERS: "
                    f"{new_leader_among_followers} at {ts()}"
                )

            time.sleep(5)

        # -- Check if chaos object still exists; delete if needed --------------
        if chaos_object_exists("NetworkChaos", res_name):
            logger.log("Chaos object still present after 75 s - deleting manually ...")
            removal_dt = delete_chaos(yaml_path)
            logger.log(f"FAULT REMOVED (manual) at {ts(removal_dt)}")
        else:
            removal_dt = datetime.now()
            logger.log(
                f"Chaos object auto-removed by Chaos Mesh before 75 s mark. "
                f"FAULT REMOVED at {ts(removal_dt)}"
            )

        # -- Poll until full recovery -------------------------------------------
        logger.log("--- Polling for full recovery (all 3 pods Running, exactly 1 leader) ---")
        final_leader, recovery_s, recovery_dt = wait_full_recovery(logger, inject_dt)

        # -- Footer ------------------------------------------------------------
        end_dt = datetime.now()
        leadership_changed = "yes" if (final_leader and final_leader != leader) else "no"
        notes_parts = []
        if split_brain_detected:
            notes_parts.append(
                "split-brain window observed: isolated leader + followers both reported 'leader'"
            )
        if new_leader_among_followers:
            notes_parts.append(
                f"new leader elected among followers: {new_leader_among_followers}"
            )
        if not new_leader_among_followers:
            notes_parts.append("no new leader elected among followers during partition")
        if final_leader is None:
            notes_parts.append("RECOVERY TIMEOUT")

        logger.raw("")
        logger.raw(f"EXPERIMENT END TIME: {ts(end_dt)}")
        logger.raw(f"LEADER AT END: {final_leader or 'unknown'}")
        logger.raw(f"LEADERSHIP CHANGED: {leadership_changed}")
        logger.raw(f"TOTAL DOWNTIME SECONDS: {recovery_s}")
        logger.raw(f"RECOVERY SECONDS: {recovery_s}")
        logger.raw(f"NOTES: {'; '.join(notes_parts)}")

        # -- CSV row -----------------------------------------------------------
        append_result({
            "experiment":         "network_partition",
            "run":                run_num,
            "leader_before":      leader,
            "leader_after":       final_leader or "unknown",
            "leadership_changed": leadership_changed,
            "injection_time":     ts(inject_dt),
            "recovery_time":      ts(recovery_dt) if recovery_dt else "timeout",
            "recovery_seconds":   recovery_s,
            "quorum_lost":        "partial (isolated leader loses quorum; follower pair forms quorum)",
            "notes":              "; ".join(notes_parts),
        })

        _summary_rows.append({
            "experiment":    "network_partition",
            "run":           run_num,
            "leader_before": leader,
            "leader_after":  final_leader or "unknown",
            "recovery_s":    recovery_s,
        })
        logger.log(f"Run complete - log saved to {log_path}")

    finally:
        logger.close()


# -- Experiment C: Cascading Failure ------------------------------------------
def run_cascading_failure(run_num: int):
    log_path = f"log_cascading_failure_run{run_num}.txt"
    logger   = Logger(log_path)
    start_dt = datetime.now()

    try:
        # -- Header ------------------------------------------------------------
        logger.raw("EXPERIMENT: cascading_failure")
        logger.raw(f"RUN: {run_num}")
        logger.raw(f"START TIME: {ts(start_dt)}")

        leader, follower_1, follower_2 = preflight(logger)

        logger.raw(f"LEADER AT START: {leader}")
        logger.raw(f"FOLLOWER 1: {follower_1}")
        logger.raw(f"FOLLOWER 2: {follower_2}")
        logger.raw("")
        logger.log(
            f"Plan: kill {follower_1} (follower), wait 10 s, "
            f"then kill {leader} (leader). Surviving pod: {follower_2}"
        )

        # -- Generate both YAMLs -----------------------------------------------
        follower_res  = f"cascade-follower-r{run_num}"
        leader_res    = f"cascade-leader-r{run_num}"
        follower_yaml = f"chaos_cascade_follower_run{run_num}_dynamic.yaml"
        leader_yaml   = f"chaos_cascade_leader_run{run_num}_dynamic.yaml"

        write_yaml_file(follower_yaml, build_pod_kill_yaml(follower_res, follower_1))
        write_yaml_file(leader_yaml,   build_pod_kill_yaml(leader_res,   leader))
        logger.log(f"Generated YAML: {follower_yaml} (targeting follower {follower_1})")
        logger.log(f"Generated YAML: {leader_yaml}   (targeting leader {leader})")

        # --------------------------------------------------------------------
        # Phase 1 - Kill follower
        # --------------------------------------------------------------------
        logger.raw("")
        logger.raw("--- PHASE 1: Kill follower ---")
        follower_kill_dt = apply_chaos(follower_yaml)
        logger.log(
            f"FAULT INJECTED (1/2) at {ts(follower_kill_dt)}: "
            f"kubectl apply -f {follower_yaml} - killed {follower_1}"
        )
        logger.log("Waiting exactly 10 seconds before killing leader ...")
        time.sleep(10)

        # Poll once at the 10 s mark
        statuses_at_10s = poll_statuses(logger, label="10s mark after follower kill")
        ldr_at_10s, _   = resolve_roles(statuses_at_10s)
        logger.log(
            f"  Leader at 10 s mark: {ldr_at_10s or 'none'} "
            f"({follower_1} is down, {leader} still up)"
        )

        # --------------------------------------------------------------------
        # Phase 2 - Kill leader (2/3 nodes now down -> no quorum)
        # --------------------------------------------------------------------
        logger.raw("")
        logger.raw("--- PHASE 2: Kill leader (no quorum) ---")
        leader_kill_dt = apply_chaos(leader_yaml)
        logger.log(
            f"FAULT INJECTED (2/2) at {ts(leader_kill_dt)}: "
            f"kubectl apply -f {leader_yaml} - killed {leader}"
        )
        time_between_kills = round((leader_kill_dt - follower_kill_dt).total_seconds(), 1)
        logger.log(f"Time between follower kill and leader kill: {time_between_kills} s")
        logger.log(
            f"2/3 nodes are now down ({follower_1}, {leader}). "
            f"Surviving node: {follower_2}. NO QUORUM."
        )

        surviving_pod = follower_2
        quorum_lost   = False
        logger.log(
            f"--- Polling surviving pod ({surviving_pod}) "
            f"every 5 s for 60 s ---"
        )
        poll_end = time.monotonic() + 60

        while time.monotonic() < poll_end:
            status = get_zk_status(surviving_pod)
            logger.log(f"POLL {ts()}: {surviving_pod}={status}")
            if status in ("leader", "follower"):
                logger.log(
                    f"  WARNING: {surviving_pod} reports '{status}' unexpectedly "
                    f"(should not have quorum with only 1/3 nodes)"
                )
            else:
                quorum_lost = True
                logger.log(
                    f"  QUORUM LOST CONFIRMED: {surviving_pod} cannot report "
                    f"leader/follower (status={status})"
                )
            time.sleep(5)

        # --------------------------------------------------------------------
        # Phase 3 - Remove both faults and recover
        # --------------------------------------------------------------------
        logger.raw("")
        logger.raw("--- PHASE 3: Remove both faults and recover ---")
        removal_dt_follower = delete_chaos(follower_yaml)
        removal_dt_leader   = delete_chaos(leader_yaml)
        logger.log(
            f"FAULT REMOVED at {ts(removal_dt_follower)}: "
            f"kubectl delete -f {follower_yaml}"
        )
        logger.log(
            f"FAULT REMOVED at {ts(removal_dt_leader)}: "
            f"kubectl delete -f {leader_yaml}"
        )

        # Recovery measured from the first kill (follower_kill_dt)
        logger.log("--- Polling for full recovery (all 3 pods Running, exactly 1 leader) ---")
        final_leader, recovery_s, recovery_dt = wait_full_recovery(
            logger, follower_kill_dt, timeout_s=240
        )

        # -- Footer ------------------------------------------------------------
        end_dt = datetime.now()
        leadership_changed = "yes" if (final_leader and final_leader != leader) else "no"
        notes_parts = [
            f"time_between_kills={time_between_kills}s (target=10s)",
            f"quorum_lost={'yes' if quorum_lost else 'no'}",
        ]
        if quorum_lost:
            notes_parts.append(
                "ZooKeeper correctly refused leadership with only 1/3 nodes alive"
            )
        else:
            notes_parts.append(
                "WARNING: surviving pod unexpectedly reported leader/follower mode"
            )
        if final_leader:
            notes_parts.append(f"post-recovery leader: {final_leader}")
        else:
            notes_parts.append("RECOVERY TIMEOUT")

        logger.raw("")
        logger.raw(f"EXPERIMENT END TIME: {ts(end_dt)}")
        logger.raw(f"LEADER AT END: {final_leader or 'unknown'}")
        logger.raw(f"LEADERSHIP CHANGED: {leadership_changed}")
        logger.raw(f"TOTAL DOWNTIME SECONDS: {recovery_s}")
        logger.raw(f"RECOVERY SECONDS: {recovery_s} (from follower kill to confirmed leader)")
        logger.raw(f"NOTES: {'; '.join(notes_parts)}")

        # -- CSV row -----------------------------------------------------------
        append_result({
            "experiment":         "cascading_failure",
            "run":                run_num,
            "leader_before":      leader,
            "leader_after":       final_leader or "unknown",
            "leadership_changed": leadership_changed,
            "injection_time":     ts(follower_kill_dt),
            "recovery_time":      ts(recovery_dt) if recovery_dt else "timeout",
            "recovery_seconds":   recovery_s,
            "quorum_lost":        "yes" if quorum_lost else "no",
            "notes":              "; ".join(notes_parts),
        })

        _summary_rows.append({
            "experiment":    "cascading_failure",
            "run":           run_num,
            "leader_before": leader,
            "leader_after":  final_leader or "unknown",
            "recovery_s":    recovery_s,
        })
        logger.log(f"Run complete - log saved to {log_path}")

    finally:
        logger.close()


# -- Summary printer -----------------------------------------------------------
def print_summary():
    print()
    print("EXPERIMENT SUMMARY")
    print("=" * 75)
    print(
        f"{'Experiment':<20} | {'Run':>3} | "
        f"{'Leader Before':<14} | {'Leader After':<13} | Recovery(s)"
    )
    print(f"{'-'*20}+{'-'*5}+{'-'*16}+{'-'*15}+{'-'*12}")
    for r in _summary_rows:
        lb = r["leader_before"] or "unknown"
        la = r["leader_after"]  or "unknown"
        rs = r["recovery_s"]
        print(
            f"{r['experiment']:<20} | {r['run']:>3} | "
            f"{lb:<14} | {la:<13} | {rs}s"
        )
    print("=" * 75)
    print()
    print("ALL LOGS SAVED:")
    print("  - log_kill_leader_run1.txt through run5.txt")
    print("  - log_network_partition_run1.txt through run5.txt")
    print("  - log_cascading_failure_run1.txt through run5.txt")
    print("  - log_all_experiments_master.txt")
    print("  - results_live.csv")


# -- Emergency cleanup ---------------------------------------------------------
def cleanup_all():
    print(f"[{ts()}] [CLEANUP] Removing any lingering chaos resources ...")
    for n in range(1, 6):
        for kind, name in [
            ("PodChaos",     f"kill-leader-r{n}"),
            ("NetworkChaos", f"zk-partition-r{n}"),
            ("PodChaos",     f"cascade-follower-r{n}"),
            ("PodChaos",     f"cascade-leader-r{n}"),
        ]:
            subprocess.run(
                ["kubectl", "delete", kind, name, "--ignore-not-found=true"],
                capture_output=True, timeout=15,
            )
    print(f"[{ts()}] [CLEANUP] Done.")


# -- Main ----------------------------------------------------------------------
def main():
    # Initialize output files fresh
    with open(MASTER_LOG, "w", buffering=1) as mf:
        mf.write("ZooKeeper Chaos Engineering - Master Log\n")
        mf.write(f"Suite started: {ts()}\n")
        mf.write("=" * 70 + "\n\n")
    init_results_csv()

    print(f"[{ts()}] ====================================================")
    print(f"[{ts()}]  ZooKeeper Chaos Engineering Suite - 15 runs")
    print(f"[{ts()}]  Master log : {MASTER_LOG}")
    print(f"[{ts()}]  Results    : {RESULTS_LIVE}")
    print(f"[{ts()}] ====================================================")
    print()

    try:
        # -- Experiment A: Kill Leader (5 runs) -----------------------------
        print(f"[{ts()}] === EXPERIMENT A: KILL LEADER - 5 runs ===")
        for run in range(1, 6):
            print(f"\n[{ts()}] --- Kill Leader Run {run}/5 ---")
            run_kill_leader(run)
            if run < 5:
                print(f"[{ts()}] Waiting 90 s for cluster stabilisation before next run ...")
                time.sleep(90)

        print(f"\n[{ts()}] === EXPERIMENT A COMPLETE ===")
        print(f"[{ts()}] Waiting 90 s before Experiment B ...")
        time.sleep(90)

        # -- Experiment B: Network Partition (5 runs) -----------------------
        print(f"\n[{ts()}] === EXPERIMENT B: NETWORK PARTITION - 5 runs ===")
        for run in range(1, 6):
            print(f"\n[{ts()}] --- Network Partition Run {run}/5 ---")
            run_network_partition(run)
            if run < 5:
                print(f"[{ts()}] Waiting 90 s for cluster stabilisation before next run ...")
                time.sleep(90)

        print(f"\n[{ts()}] === EXPERIMENT B COMPLETE ===")
        print(f"[{ts()}] Waiting 90 s before Experiment C ...")
        time.sleep(90)

        # -- Experiment C: Cascading Failure (5 runs) -----------------------
        print(f"\n[{ts()}] === EXPERIMENT C: CASCADING FAILURE - 5 runs ===")
        for run in range(1, 6):
            print(f"\n[{ts()}] --- Cascading Failure Run {run}/5 ---")
            run_cascading_failure(run)
            if run < 5:
                print(f"[{ts()}] Waiting 90 s for cluster stabilisation before next run ...")
                time.sleep(90)

        print(f"\n[{ts()}] === EXPERIMENT C COMPLETE ===")

    except KeyboardInterrupt:
        print(f"\n[{ts()}] Interrupted by user. Running cleanup ...")
        cleanup_all()
        sys.exit(0)
    except Exception as exc:
        print(f"\n[{ts()}] UNHANDLED ERROR: {exc}")
        import traceback
        traceback.print_exc()
        cleanup_all()
        raise

    # Close master log
    with open(MASTER_LOG, "a", buffering=1) as mf:
        mf.write(f"\n{'=' * 70}\n")
        mf.write(f"ALL EXPERIMENTS COMPLETE: {ts()}\n")

    print_summary()


if __name__ == "__main__":
    main()
