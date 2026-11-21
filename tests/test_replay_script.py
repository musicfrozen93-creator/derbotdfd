"""scripts/replay_strategy_model.py on a small synthetic history (the full 7-day replay is run
separately on the stored Deriv ticks; see ANALYZER_STRATEGY_ALIGNMENT/BACKTEST_COMPARISON.md)."""
import gzip
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import replay_strategy_model as replay  # noqa: E402


def write_ticks(d: Path, rng, n=4000):
    t0 = 1_800_000_000
    for sym in replay.SYMBOLS:
        step = 1 if sym.startswith("1HZ") else 2
        dec = 2 if sym in ("R_100", "1HZ10V", "1HZ50V") else 3
        price = 1000.0 + np.cumsum(rng.normal(0, 0.05, n))
        with gzip.open(d / f"{sym}.csv.gz", "wt", encoding="utf-8") as fh:
            fh.write("epoch,quote,last_digit\n")
            for i in range(n):
                q = f"{price[i]:.{dec}f}"
                fh.write(f"{t0 + i * step},{q},{q[-1]}\n")


def test_replay_runs_end_to_end_without_look_ahead(tmp_path, rng):
    write_ticks(tmp_path, rng)
    out = tmp_path / "replay.json"
    assert replay.main(["--ticks", str(tmp_path), "--sample-minutes", "5", "--out", str(out)]) == 0
    r = json.loads(out.read_text(encoding="utf-8"))
    assert {s["scenario"] for s in r["scenarios"]} == {"default_config", "bankroll_configured",
                                                         "sensitivity_without_economics_gate"}
    for s in r["scenarios"]:
        ev = s["evaluator_vs_emulation"]
        assert ev["total"] > 0 and ev["agree"] == ev["total"], ev["disagree"][:3]
    default = r["scenarios"][0]
    assert default["starts_total"] == 0                     # no bankroll configured: risk UNKNOWN
    assert all(v["status"] in ("UNSAFE", "UNKNOWN") for v in default["risk"].values())
    assert default["risk"]["predator"]["status"] == "UNSAFE"
    assert r["data"]["R_10"]["decimals"] == 3 and r["data"]["R_10"]["digit_column_mismatch"] == 0
    assert len(r["outcomes_7d"]) == 18 * 7
