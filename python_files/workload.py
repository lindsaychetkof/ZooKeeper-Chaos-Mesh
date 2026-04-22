#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workload.py
-----------
Continuous ZooKeeper read/write workload used alongside chaos experiments.

Logs to BOTH stdout AND logs/workload/workload_<timestamp>.txt so that
disruptions (KazooExceptions) can be correlated with experiment injection
events by matching timestamps.

Correlation guide
-----------------
  Experiment log:  "[2026-04-10 10:05:22.410] FAULT INJECTED ..."
  Workload log:    "[2026-04-10 10:05:22.xxx] ERROR - ..."
  Both use the same wall-clock format: YYYY-MM-DD HH:MM:SS.mmm
  Look for ERROR bursts in this log that align with FAULT INJECTED /
  FAULT REMOVED events in logs/log_v2_all_experiments_master.txt.
"""

import os
import sys
import time
from datetime import datetime
from kazoo.client import KazooClient
from kazoo.exceptions import KazooException

# Force UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ZK_HOST  = "127.0.0.1:2181"
INTERVAL = 0.5   # seconds between operations
LOG_DIR  = os.path.join("logs", "workload")

os.makedirs(LOG_DIR, exist_ok=True)
_start_dt  = datetime.now()
_start_str = _start_dt.strftime("%Y%m%d_%H%M%S")
LOG_FILE   = os.path.join(LOG_DIR, f"workload_{_start_str}.txt")

_log_fh = open(LOG_FILE, "w", buffering=1, encoding="utf-8")


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _write(line: str):
    """Write to both stdout and the log file."""
    print(line, flush=True)
    _log_fh.write(line + "\n")


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
_write("# ================================================================")
_write("# ZooKeeper Workload Log")
_write(f"# Started     : {_start_dt.strftime('%Y-%m-%d %H:%M:%S')}")
_write(f"# ZK host     : {ZK_HOST}")
_write(f"# Log file    : {LOG_FILE}")
_write(f"# Interval    : {INTERVAL}s between operations")
_write("#")
_write("# Correlation : match timestamps below with FAULT INJECTED /")
_write("#   FAULT REMOVED events in logs/log_v2_all_experiments_master.txt")
_write("#   Format: [YYYY-MM-DD HH:MM:SS.mmm] OK/ERROR - details")
_write("# ================================================================")
_write("")

# --------------------------------------------------------------------------
# Connect (with retry so a slow port-forward doesn't crash the script)
# --------------------------------------------------------------------------
_write(f"[{_ts()}] CONNECTING to ZooKeeper at {ZK_HOST} ...")
zk = KazooClient(hosts=ZK_HOST)
while True:
    try:
        zk.start(timeout=15)
        break
    except Exception as e:
        _write(f"[{_ts()}] CONNECT RETRY - {type(e).__name__}: {e}")
        time.sleep(3)
        zk = KazooClient(hosts=ZK_HOST)

zk.ensure_path("/test")
_write(f"[{_ts()}] CONNECTED. Starting read/write loop (Ctrl-C to stop).")
_write("")

# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
counter    = 0
ok_count   = 0
err_count  = 0

try:
    while True:
        now = _ts()
        try:
            zk.set("/test", str(counter).encode())
            data, _ = zk.get("/test")
            _write(f"[{now}] OK    wrote={counter}  read={data.decode()}")
            counter  += 1
            ok_count += 1
        except KazooException as e:
            _write(f"[{now}] ERROR {type(e).__name__}: {e}")
            err_count += 1
        except Exception as e:
            _write(f"[{now}] ERROR {type(e).__name__}: {e}")
            err_count += 1
        time.sleep(INTERVAL)
except KeyboardInterrupt:
    pass
finally:
    end_ts = _ts()
    _write("")
    _write(f"[{end_ts}] STOPPED. total_ok={ok_count}  total_errors={err_count}  ops={ok_count+err_count}")
    _log_fh.close()
