#!/usr/bin/env python3
"""Download historical bars from a data source and cache them as a CSV.

Example:
    python3 ingest.py --source histdata --instrument EURUSD --timeframe H1 \\
        --start 2023-01-01 --end 2023-12-31
"""
import argparse
from datetime import date
from pathlib import Path

from data_sources import SOURCES, FetchRequest


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=sorted(SOURCES), default="histdata")
    parser.add_argument("--instrument", required=True, help="e.g. EURUSD")
    parser.add_argument("--timeframe", default="H1", help="e.g. H1, M1")
    parser.add_argument("--start", type=parse_date, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", type=parse_date, required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--out-dir", default="data/raw", help="Directory to cache the normalized CSV under"
    )
    args = parser.parse_args()

    source = SOURCES[args.source]()
    request = FetchRequest(
        instrument=args.instrument.upper(),
        timeframe=args.timeframe.upper(),
        start=args.start,
        end=args.end,
    )
    df = source.fetch(request)

    out_dir = Path(args.out_dir) / args.source / request.instrument
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{request.timeframe}.csv"
    df.to_csv(out_path)
    print(f"Wrote {len(df)} bars ({df.index.min()} .. {df.index.max()}) to {out_path}")


if __name__ == "__main__":
    main()
