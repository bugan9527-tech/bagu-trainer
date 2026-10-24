"""Source-mode compatibility entry for model settings."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bagu_trainer.desktop import main

if __name__ == "__main__":
    raise SystemExit(main(["--configure"]))
