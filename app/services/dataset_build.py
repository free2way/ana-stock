from app.core.config import get_settings
from app.services.normalizer import MarketDataNormalizer
from app.services.qlib_builder import QlibDatasetBuilder


def build_dataset(normalize_only: bool = False) -> dict:
    settings = get_settings()
    normalizer = MarketDataNormalizer()
    builder = QlibDatasetBuilder()
    raw_files = sorted(settings.raw_data_dir.glob("*.csv"))

    if not raw_files:
        raise RuntimeError("No raw CSV files found. Sync or seed data first.")

    normalized = []
    for raw_file in raw_files:
        target_path = settings.normalized_data_dir / raw_file.name
        normalizer.normalize_symbol_file(raw_file, target_path)
        normalized.append(str(target_path))

    if normalize_only:
        return {"normalized_files": normalized, "qlib_built": False}

    try:
        qlib_path = builder.build(settings.normalized_data_dir, settings.qlib_data_dir)
        return {
            "normalized_files": normalized,
            "qlib_built": True,
            "qlib_dir": str(qlib_path),
        }
    except RuntimeError as exc:
        return {
            "normalized_files": normalized,
            "qlib_built": False,
            "message": str(exc),
        }
