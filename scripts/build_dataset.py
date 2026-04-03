import sys
import argparse
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import get_settings
from app.services.normalizer import MarketDataNormalizer
from app.services.qlib_builder import QlibDatasetBuilder


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize raw CSVs and optionally build a Qlib dataset.")
    parser.add_argument(
        "--normalize-only",
        action="store_true",
        help="Only write normalized CSV files and skip building the Qlib dataset.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    normalizer = MarketDataNormalizer()
    builder = QlibDatasetBuilder()
    raw_files = sorted(settings.raw_data_dir.glob("*.csv"))

    if not raw_files:
        print("No raw CSV files found. Run sync_market_data.py first.")
        return

    for raw_file in raw_files:
        target_path = settings.normalized_data_dir / raw_file.name
        normalizer.normalize_symbol_file(raw_file, target_path)
        print(f"Normalized {raw_file.name} -> {target_path}")

    if args.normalize_only:
        print("Normalization complete. Skipped Qlib dataset build.")
        return

    try:
        builder.build(settings.normalized_data_dir, settings.qlib_data_dir)
        print(f"Built Qlib dataset in {settings.qlib_data_dir}")
    except RuntimeError as exc:
        print(f"Normalization complete, but Qlib dataset build was skipped: {exc}")


if __name__ == "__main__":
    main()
