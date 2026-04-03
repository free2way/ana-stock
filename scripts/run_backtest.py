import argparse
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.backtester import BacktestRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a lightweight backtest from stored predictions.")
    parser.add_argument("--top-n", type=int, default=1, help="Number of top-ranked symbols to hold per day.")
    return parser.parse_args()


def main() -> None:
    runner = BacktestRunner()
    args = parse_args()
    try:
        count = runner.run(top_n=args.top_n)
        print(f"Stored {count} daily backtest rows.")
    except Exception as exc:
        print(exc)


if __name__ == "__main__":
    main()
