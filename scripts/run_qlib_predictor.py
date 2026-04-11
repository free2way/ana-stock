import argparse
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.qlib_predictor import QlibPredictor


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate the Qlib environment and import Qlib prediction rows from CSV/JSON/JSONL files or an artifact directory."
    )
    parser.add_argument("--run-name", required=True, help="Name for the imported or predicted Qlib model run.")
    parser.add_argument(
        "--artifact-path",
        default=None,
        help="Optional path to a trained Qlib artifact. If it is a directory and contains predictions.csv / predictions.json / predictions.jsonl / native_result.json, that payload will be imported.",
    )
    parser.add_argument("--market", default=None, help="Optional market label.")
    parser.add_argument("--universe", default=None, help="Optional universe label.")
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the expected native Qlib prediction schema and environment manifest, then exit.",
    )
    parser.add_argument(
        "--predictions-csv",
        default=None,
        help="Optional CSV/JSON/JSONL file of Qlib-style prediction rows. If provided, rows will be imported into the app.",
    )
    args = parser.parse_args()

    predictor = QlibPredictor()
    try:
        if args.print_schema:
            manifest = predictor.build_native_inference_manifest(
                run_name=args.run_name,
                artifact_path=args.artifact_path,
                market=args.market,
                universe=args.universe,
            )
            print(manifest)
            return
        result = predictor.predict(
            run_name=args.run_name,
            artifact_path=args.artifact_path,
            market=args.market,
            universe=args.universe,
            predictions_csv=Path(args.predictions_csv) if args.predictions_csv else None,
        )
        if result.get("mode") == "import_csv":
            print(
                f"Imported {result['predictions_written']} predictions and {result['details_written']} details "
                f"into model run {result['model_run_id']} ({result['run_name']})."
            )
        else:
            print(
                f"Imported {result['predictions_written']} predictions and {result['details_written']} details "
                f"into model run {result['model_run_id']} ({result['run_name']}) via native artifact path."
            )
    except Exception as exc:
        print(exc)


if __name__ == "__main__":
    main()
