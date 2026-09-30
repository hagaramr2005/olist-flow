"""Build the real-data calibration artifact from the Olist CSVs.

    python scripts/build_calibration.py --data-dir /path/to/olist_csvs
    python scripts/build_calibration.py --data-dir data/raw --no-benchmark

Writes app/data/artifacts/{calibration.json, real_orders.json.gz} and docs/DATA_CARD.md. Needs pandas (requirements-data.txt).
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data import calibration as cal            # noqa: E402
from app.data import olist_etl, datacard            # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--out-dir", default=str(cal.ARTIFACT_DIR))
    ap.add_argument("--no-benchmark", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    with cal.use(None):                      # never calibrate against a previous calibration
        art = olist_etl.build_artifact(a.data_dir, a.out_dir, run_benchmark=not a.no_benchmark)
    doc = ROOT / "docs" / "DATA_CARD.md"
    doc.parent.mkdir(exist_ok=True)
    doc.write_text(datacard.render(art), encoding="utf-8")
    h = art["headline"]
    print(f"built in {time.time() - t0:.0f}s: {h['orders_analysed']:,} orders {h['date_from']}..{h['date_to']}, late {h['late_rate']:.1%}, "
          f"cross-state {h['cross_state_share']:.1%}, checksum {art['meta']['checksum'][:12]}")
    print(f"artifact -> {a.out_dir}\ndata card -> {doc}")


if __name__ == "__main__":
    main()
