from pathlib import Path
from typing import Any


class QlibDatasetBuilder:
    """Build Qlib-compatible datasets from normalized files."""

    def is_available(self) -> bool:
        try:
            import qlib  # noqa: F401
        except ImportError:
            return False
        return True

    def build(self, normalized_dir: Path, qlib_dir: Path) -> Path:
        if not self.is_available():
            raise RuntimeError(
                "Qlib is not installed. Install it with `.venv/bin/pip install -r requirements-qlib.txt`."
            )

        try:
            from scripts.dump_bin import DumpDataAll
        except ImportError:
            from qlib.scripts.dump_bin import DumpDataAll  # type: ignore

        qlib_dir.mkdir(parents=True, exist_ok=True)

        builder: Any = DumpDataAll(
            data_path=str(normalized_dir),
            qlib_dir=str(qlib_dir),
            freq="day",
            date_field_name="date",
            file_suffix=".csv",
            symbol_field_name="symbol",
            exclude_fields="symbol,adj_close,dividend,split_ratio",
        )
        builder.dump()
        return qlib_dir
