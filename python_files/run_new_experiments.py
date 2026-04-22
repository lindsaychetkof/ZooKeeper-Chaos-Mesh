#!/usr/bin/env python3
"""
run_new_experiments.py

Four new chaos engineering experiments on the 3-node ZooKeeper ensemble.
Leader/follower roles are discovered dynamically at every preflight.
Does NOT launch workload.py as a subprocess — uses an in-process
WorkloadLogger that replicates workload.py behaviour for each experiment's
dedicated log file.

PREREQUISITE: kubectl port-forward svc/zookeeper 2181:2181 must be running
in a separate terminal before launching this script.

Outputs
-------
  workload_repeated_leader_kill_run{1,2,3}.log
  workload_cascading_failure.log
  workload_network_delay.log
  workload_rapid_follower_kill_run{1..5}.log
  results_repeated_kills.csv
  results_cascading.csv
  results_network_delay.csv
  results_rapid_follower.csv
"""

import csv
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

try:
    from kazoo.client import KazooClient
    from kazoo.exceptions import KazooException
except ImportError:
    print("ERROR: kazoo not installed.  Run: pip install kazoo")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ZK_PODS       = ["zookeeper-0", "zookeeper-1", "zookeeper-2"]
ZK_BIN        = "/apache-zookeeper-3.9.3-bin/bin/zkServer.sh"
NAMESPACE     = "default"
ZK_HOST       = "127.0.0.1:2181"
POLL_INTERVAL = 3    # seconds between zkServer.sh status polls
DATE_PREFIX   = datetime.now().strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def banner(label: str) -> None:
    log("=" * 62)
    log(label)
    log("=" * 62)

# ---------------------------------------------------------------------------
# ZooKeeper status
# ---------------------------------------------------------------------------
def get_zk_status(pod: str) -> str:
    try:
        r = subprocess.run(
            ["kubectl", "exec", pod, "--", ZK_BIN, "status"],
            capture_output=True, text=True, timeout=10,
        )
        c = (r.stdout + r.stderr).lower()
        if "leader"      in c: return "leader"
        if "follower"    in c: return "follower"
        if "not running" in c: return "not_running"
        return "unknown"
    except Exception:
        return "error"

def get_all_statuses() -> dict:
    return {pod: get_zk_status(pod) for pod in ZK_PODS}

def resolve_roles(statuses: dict) -> tuple:
    leader    = next((p for p, s in statuses.items() if s == "leader"), None)
    followers = [p for p, s in statuses.items() if s == "follower"]
    return leader, followers

def log_statuses(statuses: dict) -> None:
    for pod, status in statuses.items():
        log(f"    {pod}: {status}")

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def preflight() -> tuple:
    """
    Verifies 3 Running pods and exactly 1 leader.
    Returns (leader_pod, [follower_pods]) or calls sys.exit(1).
    """
    log("Running preflight ...")

    # Pod health
    r = subprocess.run(
        ["kubectl", "get", "pods", "-l", "app=zookeeper", "--no-headers"],
        capture_output=True, text=True, timeout=20,
    )
    lines   = [l for l in r.stdout.strip().splitlines() if l]
    running = [l for l in lines if "Running" in l and "1/1" in l]
    if len(running) != 3:
        log(f"PREFLIGHT FAIL: expected 3 Running pods, found {len(running)}:")
        log(r.stdout)
        sys.exit(1)
    log("  Pods: all 3 are 1/1 Running")

    # Port-forward
    try:
        s = socket.create_connection(("127.0.0.1", 2181), timeout=3)
        s.close()
        log("  Port-forward :2181: OK")
    except OSError:
        log("PREFLIGHT FAIL: nothing listening on 127.0.0.1:2181")
        sys.exit(1)

    # Roles
    statuses = get_all_statuses()
    log_statuses(statuses)
    leader, followers = resolve_roles(statuses)

    if leader is None:
        log("PREFLIGHT FAIL: no leader found")
        sys.exit(1)
    if len(followers) != 2:
        log(f"PREFLIGHT FAIL: expected 2 followers, got {followers}")
        sys.exit(1)

    log(f"  Leader: {leader}  |  Followers: {followers}")
    log("Preflight PASSED.")
    return leader, followers

# ---------------------------------------------------------------------------
# YAML builders
# ---------------------------------------------------------------------------
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

def build_network_delay_yaml(resource_name: str, pod_name: str,
                              latency_ms: int = 500,
                              duration: str = "60s") -> str:
    return f"""\
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: {resource_name}
  namespace: {NAMESPACE}
spec:
  action: delay
  mode: all
  selector:
    namespaces:
      - {NAMESPACE}
    expressionSelectors:
      - key: statefulset.kubernetes.io/pod-name
        operator: In
        values:
          - {pod_name}
  delay:
    latency: "{latency_ms}ms"
    correlation: "0"
    jitter: "0ms"
  duration: "{duration}"
"""

def apply_chaos(yaml_content: str, yaml_path: str) -> datetime:
    with open(yaml_path, "w") as f:
        f.write(yaml_content)
    subprocess.run(["kubectl", "apply", "-f", yaml_path], check=True)
    return datetime.now()

def delete_chaos(kind: str, name: str) -> None:
    subprocess.run(
        ["kubectl", "delete", kind, name, "--ignore-not-found=true"],
        check=True,
    )

# ---------------------------------------------------------------------------
# WorkloadLogger  (in-process, does not spawn workload.py)
# ---------------------------------------------------------------------------
class WorkloadLogger:
    """
    Replicates workload.py behaviour as a controlled background thread.
    Writes to ZooKeeper /test every 500 ms; logs timestamped OK/ERROR
    lines to a file (line-buffered) and a queue for real-time monitoring.
    """

    def __init__(self, log_path: str):
        self.log_path    = log_path
        self.line_q: queue.Queue = queue.Queue()
        self._stop       = threading.Event()
        self._thread     = None
        self._zk         = None
        self._fh         = None
        self.ok_count    = 0
        self.error_count = 0

    def start(self) -> None:
        self._fh  = open(self.log_path, "w", buffering=1)
        self._zk  = KazooClient(hosts=ZK_HOST)
        try:
            self._zk.start(timeout=15)
            self._zk.ensure_path("/test")
        except Exception as exc:
            self._fh.close()
            raise RuntimeError(f"WorkloadLogger kazoo start failed: {exc}") from exc
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        counter = 0
        while not self._stop.is_set():
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            try:
                self._zk.set("/test", str(counter).encode())
                data, _ = self._zk.get("/test")
                line = f"[{ts}] OK  - wrote {counter}, read back {data.decode()}"
                self.ok_count += 1
                counter += 1
            except KazooException as exc:
                line = f"[{ts}] ERROR - {exc}"
                self.error_count += 1
            except Exception as exc:
                line = f"[{ts}] ERROR - {exc}"
                self.error_count += 1
            self._fh.write(line + "\n")
            self._fh.flush()
            self.line_q.put(line)
            self._stop.wait(0.5)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._zk:
            try:
                self._zk.stop()
            except Exception:
                pass
        if self._fh:
            self._fh.close()

# ---------------------------------------------------------------------------
# Timing / line parsing helpers
# ---------------------------------------------------------------------------
TS_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2}\.\d{3})\]")

def fmt(dt) -> str:
    return dt.strftime("%H:%M:%S.%f")[:-3] if dt else "N/A"

def delta_s(a, b):
    if a and b:
        return round((b - a).total_seconds(), 3)
    return "N/A"

def parse_ts(line: str) -> datetime | None:
    m = TS_RE.match(line)
    if m:
        return datetime.strptime(f"{DATE_PREFIX} {m.group(1)}",
                                 "%Y-%m-%d %H:%M:%S.%f")
    return None

def is_ok(line: str)    -> bool: return "] OK"    in line
def is_error(line: str) -> bool: return "] ERROR" in line

def drain_queue(q: queue.Queue, timeout: float = 0.4) -> str | None:
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None

# ---------------------------------------------------------------------------
# Shared polling / wait helpers
# ---------------------------------------------------------------------------
def poll_for_leader(old_leader: str | None, timeout_s: float = 120) -> tuple:
    """
    Poll every POLL_INTERVAL s until any pod reports 'leader'.
    Returns (leader_pod, elapsed_s) or (None, elapsed_s) on timeout.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        statuses = get_all_statuses()
        leader, _ = resolve_roles(statuses)
        log(f"  [poll] {' | '.join(f'{p}:{s}' for p,s in statuses.items())}")
        if leader:
            return leader, round(time.monotonic() - start, 2)
        time.sleep(POLL_INTERVAL)
    return None, round(time.monotonic() - start, 2)

def poll_for_pod_role(pod: str, timeout_s: float = 120) -> tuple:
    """
    Poll until `pod` reports follower or leader.
    Returns (role, elapsed_s) or (None, elapsed_s).
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        statuses = get_all_statuses()
        log(f"  [poll] {' | '.join(f'{p}:{s}' for p,s in statuses.items())}")
        role = statuses.get(pod, "unknown")
        if role in ("follower", "leader"):
            return role, round(time.monotonic() - start, 2)
        time.sleep(POLL_INTERVAL)
    return None, round(time.monotonic() - start, 2)

def wait_for_workload_recovery(wl: WorkloadLogger, after_dt: datetime,
                                timeout_s: float = 120) -> datetime | None:
    """
    Reads workload queue until first OK line timestamped after `after_dt`.
    Returns the recovery datetime or None on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = drain_queue(wl.line_q)
        if line is None:
            continue
        log(f"  [workload] {line}")
        if is_ok(line):
            ts = parse_ts(line)
            if ts and ts > after_dt:
                return ts
    return None

def wait_stabilize(seconds: int, reason: str = "stabilisation") -> None:
    log(f"Waiting {seconds}s ({reason}) ...")
    remaining = seconds
    while remaining > 0:
        chunk = min(10, remaining)
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            log(f"  ... {remaining}s remaining")

# ---------------------------------------------------------------------------
# Experiment 1 — Repeated Leader Kills (3 runs)
# ---------------------------------------------------------------------------
def run_repeated_leader_kills() -> None:
    banner("EXPERIMENT 1  Repeated Leader Kills  (3 runs)")

    CSV  = "results_repeated_kills.csv"
    COLS = ["run", "leader_before", "leader_after",
            "injection_time", "recovery_time",
            "downtime_seconds", "notes"]
    with open(CSV, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=COLS).writeheader()

    for run in range(1, 4):
        log(f"")
        log(f"--- Run {run}/3 ---")

        leader, _ = preflight()
        res_name  = f"kill-leader-run{run}"
        log_path  = f"workload_repeated_leader_kill_run{run}.log"

        wl = WorkloadLogger(log_path)
        wl.start()
        log(f"EXPERIMENT STARTED  run={run}  target={leader}")

        # Baseline: wait for 5 OK writes
        ok_seen, deadline = 0, time.monotonic() + 12
        while time.monotonic() < deadline and ok_seen < 5:
            line = drain_queue(wl.line_q)
            if line:
                log(f"  [workload] {line}")
                if is_ok(line): ok_seen += 1
        if ok_seen == 0:
            log("  WARNING: no OK writes during baseline window")

        # Inject
        inject_dt = apply_chaos(
            build_pod_kill_yaml(res_name, leader),
            f"chaos_repeated_kill_run{run}.yaml",
        )
        log(f"FAULT INJECTED  {fmt(inject_dt)}  (killed {leader})")

        # Detect first client error
        first_err_dt, notes = None, ""
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            line = drain_queue(wl.line_q)
            if not line:
                continue
            log(f"  [workload] {line}")
            if is_error(line) and first_err_dt is None:
                ts = parse_ts(line)
                if ts and ts < inject_dt:
                    continue   # pre-injection stale line
                first_err_dt = ts or datetime.now()
                log(f"  First disruption at {fmt(first_err_dt)}")
                break
        if first_err_dt is None:
            notes = "no client errors detected"
            log("  NOTE: workload showed no errors (quorum may have held)")

        # Poll for new leader
        log("Polling for new leader ...")
        new_leader, poll_s = poll_for_leader(leader)
        if new_leader:
            log(f"  New leader: {new_leader}  (+{poll_s}s from fault)")
        else:
            log("  WARNING: leader election timed out")
            new_leader = "unknown"
            notes = "leader election timeout"

        # Wait for workload recovery
        log("Waiting for workload recovery ...")
        recovery_dt = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            line = drain_queue(wl.line_q)
            if not line:
                continue
            log(f"  [workload] {line}")
            if is_ok(line) and first_err_dt:
                ts = parse_ts(line)
                if ts and ts > first_err_dt:
                    recovery_dt = ts
                    log(f"RECOVERY CONFIRMED  {fmt(recovery_dt)}")
                    break
        if recovery_dt is None and not notes:
            notes = "workload did not recover within timeout"

        wl.stop()
        delete_chaos("PodChaos", res_name)
        log(f"FAULT REMOVED")

        downtime = delta_s(first_err_dt, recovery_dt)
        row = dict(run=run, leader_before=leader, leader_after=new_leader,
                   injection_time=fmt(inject_dt), recovery_time=fmt(recovery_dt),
                   downtime_seconds=downtime, notes=notes)
        with open(CSV, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=COLS).writerow(row)
        log(f"Run {run} complete  downtime={downtime}s  {leader} -> {new_leader}")

        if run < 3:
            wait_stabilize(90, "full ensemble stabilisation before next run")

    log(f"Experiment 1 complete.  Results -> {CSV}")


# ---------------------------------------------------------------------------
# Experiment 2 — Cascading Failure
# ---------------------------------------------------------------------------
def run_cascading_failure() -> None:
    banner("EXPERIMENT 2  Cascading Failure")

    CSV  = "results_cascading.csv"
    COLS = ["follower_kill_time", "leader_kill_time", "min_pods_running",
            "quorum_lost", "recovery_time", "total_downtime_seconds", "notes"]

    leader, followers = preflight()
    follower_target   = followers[0]
    log(f"Plan: kill follower {follower_target}, wait 10s, kill leader {leader}")

    wl = WorkloadLogger("workload_cascading_failure.log")
    wl.start()
    log("EXPERIMENT STARTED")

    # Baseline
    ok_seen, deadline = 0, time.monotonic() + 10
    while time.monotonic() < deadline and ok_seen < 5:
        line = drain_queue(wl.line_q)
        if line:
            log(f"  [workload] {line}")
            if is_ok(line): ok_seen += 1

    # Kill 1: follower
    follower_kill_dt = apply_chaos(
        build_pod_kill_yaml("kill-cascade-follower", follower_target),
        "chaos_cascading_follower.yaml",
    )
    log(f"FAULT INJECTED (1/2)  {fmt(follower_kill_dt)}  killed {follower_target}")
    log("Waiting 10s before killing leader ...")
    time.sleep(10)

    # Kill 2: leader
    leader_kill_dt = apply_chaos(
        build_pod_kill_yaml("kill-cascade-leader", leader),
        "chaos_cascading_leader.yaml",
    )
    log(f"FAULT INJECTED (2/2)  {fmt(leader_kill_dt)}  killed {leader}")

    # Monitor: workload + status polls for up to 120s after last kill
    first_err_dt = None
    recovery_dt  = None
    min_pods     = 3
    quorum_lost  = False
    notes_parts  = []
    last_poll    = 0.0
    mon_end      = time.monotonic() + 120

    log("Monitoring pods and workload (up to 120s) ...")
    while time.monotonic() < mon_end:
        # ZK status poll
        if time.monotonic() - last_poll >= POLL_INTERVAL:
            statuses    = get_all_statuses()
            running_cnt = sum(1 for s in statuses.values()
                              if s in ("leader", "follower"))
            min_pods    = min(min_pods, running_cnt)
            if running_cnt < 2:
                quorum_lost = True
            log_statuses(statuses)
            last_poll = time.monotonic()

        # Workload queue
        line = drain_queue(wl.line_q, timeout=0.3)
        if not line:
            continue
        log(f"  [workload] {line}")

        if is_error(line) and first_err_dt is None:
            ts = parse_ts(line)
            # Count errors only after the leader kill (quorum loss)
            if ts and ts >= leader_kill_dt:
                first_err_dt = ts
                log(f"  First quorum-loss error at {fmt(first_err_dt)}")

        if is_ok(line) and first_err_dt and recovery_dt is None:
            ts = parse_ts(line)
            if ts and ts > first_err_dt:
                recovery_dt = ts
                log(f"RECOVERY CONFIRMED  {fmt(recovery_dt)}")
                mon_end = min(mon_end, time.monotonic() + 5)

    delete_chaos("PodChaos", "kill-cascade-follower")
    delete_chaos("PodChaos", "kill-cascade-leader")
    log("FAULT REMOVED  (both chaos resources deleted)")
    wl.stop()

    if quorum_lost:
        notes_parts.append(
            f"quorum lost (min {min_pods}/3 pods active) -- "
            "writes correctly refused by ZooKeeper until quorum restored"
        )
    if first_err_dt is None:
        notes_parts.append("no client errors detected after leader kill")
    if recovery_dt is None:
        notes_parts.append("workload did not recover within 120s monitoring window")

    total_downtime = delta_s(first_err_dt, recovery_dt)
    row = dict(follower_kill_time=fmt(follower_kill_dt),
               leader_kill_time=fmt(leader_kill_dt),
               min_pods_running=min_pods, quorum_lost=quorum_lost,
               recovery_time=fmt(recovery_dt),
               total_downtime_seconds=total_downtime,
               notes="; ".join(notes_parts) or "nominal")
    with open(CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerow(row)
    log(f"Experiment 2 complete.  Results -> {CSV}")
    log(f"  min_pods={min_pods}  quorum_lost={quorum_lost}  downtime={total_downtime}s")


# ---------------------------------------------------------------------------
# Experiment 3 — Network Delay on Leader
# ---------------------------------------------------------------------------
def run_network_delay() -> None:
    banner("EXPERIMENT 3  Network Delay on Leader (500ms / 60s)")

    CSV  = "results_network_delay.csv"
    COLS = ["injection_time", "removal_time", "writes_during_fault",
            "errors_during_fault", "avg_latency_ms",
            "normal_latency_ms", "notes"]

    leader, _ = preflight()
    log(f"Target: {leader}  |  Injecting 500ms latency for 60s")

    wl = WorkloadLogger("workload_network_delay.log")
    wl.start()
    log("EXPERIMENT STARTED")

    # Pre-chaos baseline (10s)  — collect OK timestamps for latency calc
    baseline_ts = []
    deadline = time.monotonic() + 10
    log("Collecting pre-fault latency baseline (10s) ...")
    while time.monotonic() < deadline:
        line = drain_queue(wl.line_q)
        if line:
            log(f"  [workload] {line}")
            if is_ok(line):
                ts = parse_ts(line)
                if ts:
                    baseline_ts.append(ts)

    normal_latency_ms = "N/A"
    if len(baseline_ts) >= 2:
        intervals = [
            (baseline_ts[i+1] - baseline_ts[i]).total_seconds() * 1000
            for i in range(len(baseline_ts) - 1)
        ]
        normal_latency_ms = round(sum(intervals) / len(intervals), 1)
    log(f"  Baseline interval: {normal_latency_ms} ms")

    # Inject delay
    inject_dt = apply_chaos(
        build_network_delay_yaml("zk-leader-delay", leader, 500, "60s"),
        "chaos_network_delay.yaml",
    )
    log(f"FAULT INJECTED  {fmt(inject_dt)}  (500ms delay on {leader})")

    # Monitor 60s window
    chaos_ok_ts      = []
    chaos_err_count  = 0
    chaos_ok_count   = 0
    chaos_end        = time.monotonic() + 60

    log("Monitoring writes for 60s ...")
    while time.monotonic() < chaos_end:
        line = drain_queue(wl.line_q)
        if not line:
            continue
        log(f"  [workload] {line}")
        if is_ok(line):
            ts = parse_ts(line)
            if ts:
                chaos_ok_ts.append(ts)
            chaos_ok_count += 1
        elif is_error(line):
            chaos_err_count += 1

    removal_dt = datetime.now()
    delete_chaos("NetworkChaos", "zk-leader-delay")
    log(f"FAULT REMOVED  {fmt(removal_dt)}")

    avg_latency_ms = "N/A"
    if len(chaos_ok_ts) >= 2:
        intervals = [
            (chaos_ok_ts[i+1] - chaos_ok_ts[i]).total_seconds() * 1000
            for i in range(len(chaos_ok_ts) - 1)
        ]
        avg_latency_ms = round(sum(intervals) / len(intervals), 1)

    notes_parts = []
    if chaos_err_count == 0:
        notes_parts.append("zero errors -- writes degraded but succeeded throughout")
    else:
        notes_parts.append(f"{chaos_err_count} errors during delay window")
    if isinstance(avg_latency_ms, float) and isinstance(normal_latency_ms, float):
        delta = round(avg_latency_ms - normal_latency_ms, 1)
        notes_parts.append(f"added latency ~{delta}ms per write cycle")

    # Post-removal recovery confirmation
    log("Confirming post-removal recovery ...")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = drain_queue(wl.line_q)
        if line:
            log(f"  [workload] {line}")
            if is_ok(line):
                log("RECOVERY CONFIRMED")
                break

    wl.stop()

    row = dict(injection_time=fmt(inject_dt), removal_time=fmt(removal_dt),
               writes_during_fault=chaos_ok_count, errors_during_fault=chaos_err_count,
               avg_latency_ms=avg_latency_ms, normal_latency_ms=normal_latency_ms,
               notes="; ".join(notes_parts) or "nominal")
    with open(CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerow(row)
    log(f"Experiment 3 complete.  Results -> {CSV}")
    log(f"  OK={chaos_ok_count}  ERR={chaos_err_count}  "
        f"latency: {normal_latency_ms}ms -> {avg_latency_ms}ms")


# ---------------------------------------------------------------------------
# Experiment 4 — Rapid Follower Kills (5 runs, 30s gap)
# ---------------------------------------------------------------------------
def run_rapid_follower_kills() -> None:
    banner("EXPERIMENT 4  Rapid Follower Kills  (5 runs, 30s gap)")

    CSV  = "results_rapid_follower.csv"
    COLS = ["run", "follower_killed", "injection_time",
            "pod_rejoined_time", "rejoin_seconds",
            "any_client_errors", "notes"]
    with open(CSV, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=COLS).writeheader()

    for run in range(1, 6):
        log(f"")
        log(f"--- Run {run}/5 ---")

        leader, followers = preflight()
        if not followers:
            log("ERROR: no followers available — skipping run")
            continue
        target    = followers[0]
        res_name  = f"kill-follower-rapid-run{run}"
        log_path  = f"workload_rapid_follower_kill_run{run}.log"

        wl = WorkloadLogger(log_path)
        wl.start()
        log(f"EXPERIMENT STARTED  run={run}  target={target}")

        # Brief baseline
        ok_seen, deadline = 0, time.monotonic() + 6
        while time.monotonic() < deadline and ok_seen < 3:
            line = drain_queue(wl.line_q)
            if line:
                log(f"  [workload] {line}")
                if is_ok(line): ok_seen += 1

        # Inject
        inject_dt = apply_chaos(
            build_pod_kill_yaml(res_name, target),
            f"chaos_rapid_follower_run{run}.yaml",
        )
        log(f"FAULT INJECTED  {fmt(inject_dt)}  (killed {target})")

        # Poll for target pod to rejoin; simultaneously drain workload queue
        rejoin_start    = time.monotonic()
        rejoined_dt     = None
        rejoin_secs     = None
        client_errors   = 0
        last_poll       = 0.0
        poll_deadline   = time.monotonic() + 120

        log(f"Polling until {target} rejoins ...")
        while time.monotonic() < poll_deadline and rejoined_dt is None:
            if time.monotonic() - last_poll >= POLL_INTERVAL:
                statuses   = get_all_statuses()
                pod_status = statuses.get(target, "unknown")
                log(f"  [poll] {' | '.join(f'{p}:{s}' for p,s in statuses.items())}")
                if pod_status in ("follower", "leader"):
                    rejoin_secs = round(time.monotonic() - rejoin_start, 2)
                    rejoined_dt = datetime.now()
                    log(f"  {target} rejoined as {pod_status} (+{rejoin_secs}s)")
                last_poll = time.monotonic()

            line = drain_queue(wl.line_q, timeout=0.3)
            if line:
                log(f"  [workload] {line}")
                if is_error(line): client_errors += 1

        if rejoined_dt:
            log(f"RECOVERY CONFIRMED  {fmt(rejoined_dt)}")
        else:
            log(f"  WARNING: {target} did not rejoin within timeout")

        wl.stop()
        delete_chaos("PodChaos", res_name)
        log("FAULT REMOVED")

        if rejoined_dt:
            notes = ("no client errors -- quorum maintained throughout"
                     if client_errors == 0
                     else f"{client_errors} client error(s) observed")
        else:
            notes = "pod did not rejoin within 120s"

        row = dict(run=run, follower_killed=target,
                   injection_time=fmt(inject_dt),
                   pod_rejoined_time=fmt(rejoined_dt),
                   rejoin_seconds=rejoin_secs if rejoin_secs is not None else "N/A",
                   any_client_errors=client_errors > 0,
                   notes=notes)
        with open(CSV, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=COLS).writerow(row)
        log(f"Run {run} complete  rejoin={rejoin_secs}s  errors={client_errors}")

        if run < 5:
            wait_stabilize(30, "pod stabilisation before next run")

    log(f"Experiment 4 complete.  Results -> {CSV}")


# ---------------------------------------------------------------------------
# Emergency cleanup
# ---------------------------------------------------------------------------
ALL_CHAOS = [
    ("PodChaos",     "kill-leader-run1"),
    ("PodChaos",     "kill-leader-run2"),
    ("PodChaos",     "kill-leader-run3"),
    ("PodChaos",     "kill-cascade-follower"),
    ("PodChaos",     "kill-cascade-leader"),
    ("NetworkChaos", "zk-leader-delay"),
    ("PodChaos",     "kill-follower-rapid-run1"),
    ("PodChaos",     "kill-follower-rapid-run2"),
    ("PodChaos",     "kill-follower-rapid-run3"),
    ("PodChaos",     "kill-follower-rapid-run4"),
    ("PodChaos",     "kill-follower-rapid-run5"),
]

def cleanup_all() -> None:
    log("Cleaning up all chaos resources ...")
    for kind, name in ALL_CHAOS:
        delete_chaos(kind, name)
    log("Cleanup complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    banner("NEW EXPERIMENTS -- ZooKeeper Chaos Engineering")
    log("Global preflight ...")
    preflight()
    log("Global preflight passed.  Starting all experiments.\n")

    try:
        run_repeated_leader_kills()
        wait_stabilize(30, "inter-experiment gap")

        run_cascading_failure()
        wait_stabilize(30, "inter-experiment gap")

        run_network_delay()
        wait_stabilize(30, "inter-experiment gap")

        run_rapid_follower_kills()

    except KeyboardInterrupt:
        log("Interrupted -- cleaning up chaos resources ...")
        cleanup_all()
    except Exception as exc:
        log(f"Unhandled error: {exc}")
        cleanup_all()
        raise

    banner("ALL EXPERIMENTS COMPLETE")
    outputs = [
        "results_repeated_kills.csv",
        "results_cascading.csv",
        "results_network_delay.csv",
        "results_rapid_follower.csv",
    ]
    for f in outputs:
        size = os.path.getsize(f) if os.path.exists(f) else 0
        log(f"  {f}  ({size} bytes)")
    logs = [f for f in os.listdir(".") if f.startswith("workload_") and
            f.endswith(".log") and "run" in f]
    log(f"  {len(logs)} workload log files written")


if __name__ == "__main__":
    main()
