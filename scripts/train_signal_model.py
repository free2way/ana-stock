from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.trainer import SignalTrainer


def main() -> None:
    trainer = SignalTrainer()
    try:
        count = trainer.train()
        print(f"Stored {count} predictions from the baseline trainer.")
    except Exception as exc:
        print(exc)


if __name__ == "__main__":
    main()
