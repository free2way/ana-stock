import argparse
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.model_output_importer import ExternalModelOutputImporter


def main() -> None:
    parser = argparse.ArgumentParser(description="Import external model predictions into predictions and prediction_details.")
    parser.add_argument("--csv", required=True, help="Path to a CSV containing ticker, trade_date, score, and optional richer model fields.")
    parser.add_argument("--run-name", required=True, help="Name for the imported model run.")
    parser.add_argument("--model-type", default="qlib_external", help="Model type label to store with the imported run.")
    parser.add_argument("--market", default=None, help="Optional market label for the imported model run.")
    parser.add_argument("--universe", default=None, help="Optional universe label for the imported model run.")
    parser.add_argument("--artifact-path", default=None, help="Optional artifact path associated with this imported run.")
    args = parser.parse_args()

    importer = ExternalModelOutputImporter()
    try:
        result = importer.import_csv(
            Path(args.csv),
            run_name=args.run_name,
            model_type=args.model_type,
            market=args.market,
            universe=args.universe,
            artifact_path=args.artifact_path,
        )
        print(
            f"Imported {result['predictions_written']} predictions and {result['details_written']} details "
            f"into model run {result['model_run_id']} ({result['run_name']})."
        )
    except Exception as exc:
        print(exc)


if __name__ == "__main__":
    main()
