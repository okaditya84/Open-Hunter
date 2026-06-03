#!/usr/bin/env python3
"""Entry point: `python run.py companies.csv -c "your criteria"`.

Adds src/ to the path so the package is importable without installation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from open_hunter.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
