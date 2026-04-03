import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.sample_data import seed_sample_data


def main() -> None:
    for result in seed_sample_data():
        print(f"Seeded {result['ticker']}: {result['rows']} rows")


if __name__ == "__main__":
    main()
