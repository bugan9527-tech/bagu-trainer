"""Compatibility CLI adapter for the internal question-bank build module."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bagu_trainer.bank_build.importer import *

if __name__ == "__main__":
    main()
