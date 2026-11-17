#!/usr/bin/env python3
"""Container health check (Docker HEALTHCHECK / Portainer health status).

Uses only what the running service already publishes: the heartbeat file it rewrites
every ``heartbeat.interval_s`` (30 s) from its event loop. It never opens the database
and never calls Deriv or Telegram.

Unhealthy when
* there is no heartbeat file yet (Docker ignores this during ``start_period``),
* the heartbeat is older than ``--max-age`` seconds (event loop stalled / process hung),
* the Deriv connection is down and no tick has arrived for ``--max-silence`` seconds,
* no evaluation cycle has been completed AND saved for ``--max-eval-age`` seconds (default 600 =
  10 cycles): evaluations failing, or every database write failing. Measured from the later of
  the last good cycle and the process start, so a fresh start has the full period of grace.
  Evaluation cycles run every 60 s from the end of market discovery, also while the initial tick
  history is still downloading (they report INSUFFICIENT_DATA then), so a normal initial download
  does not trip this check. Older heartbeats without ``last_eval_ok_at`` skip it.

Exit code 0 = healthy, 1 = unhealthy (one line of reason on stdout).
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.monitoring.heartbeat import read_heartbeat  # noqa: E402


def check(hb: dict | None, now: float, max_age: float, max_silence: float,
          max_eval_age: float = 600.0) -> tuple[bool, str]:
    if not hb:
        return False, "no heartbeat file yet"
    age = now - float(hb.get("written_at") or 0)
    if age > max_age:
        return False, f"heartbeat is {int(age)} s old (limit {int(max_age)} s): service loop not running"
    last_tick = float(hb.get("last_tick_wall") or 0)
    silence = now - last_tick if last_tick else None
    if not hb.get("connected") and (silence is None or silence > max_silence):
        return False, ("Deriv WebSocket disconnected and no tick for "
                       f"{'ever' if silence is None else str(int(silence)) + ' s'}")
    eval_txt = ""
    if "last_eval_ok_at" in hb and max_eval_age > 0:
        ref = max(float(hb.get("last_eval_ok_at") or 0), float(hb.get("started_at") or 0))
        if ref:
            stale = now - ref
            if stale > max_eval_age:
                db = hb.get("db") or {}
                err = hb.get("last_eval_error") or db.get("last_error") or "unknown"
                what = "since start" if not hb.get("last_eval_ok_at") else "since the last good cycle"
                return False, (f"no evaluation cycle completed and saved for {int(stale)} s {what} "
                               f"(limit {int(max_eval_age)} s; consecutive failures "
                               f"{hb.get('eval_failures_consecutive', '?')}; last error: {err})")
            eval_txt = (f", last saved evaluation {int(now - float(hb['last_eval_ok_at']))} s ago"
                        if hb.get("last_eval_ok_at") else ", first evaluation pending")
    return True, (f"ok: heartbeat {int(age)} s ago, connected={hb.get('connected')}, "
                  f"{len(hb.get('monitored_symbols') or [])} markets, "
                  f"active signals={len(hb.get('active_signals') or [])}{eval_txt}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age", type=float, default=120.0)
    ap.add_argument("--max-silence", type=float, default=300.0)
    ap.add_argument("--max-eval-age", type=float, default=600.0, help="0 disables the evaluation check")
    args = ap.parse_args(argv)
    s = load_settings()
    ok, why = check(read_heartbeat(s.path(s.heartbeat.file)), time.time(), args.max_age, args.max_silence,
                    args.max_eval_age)
    print(why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
