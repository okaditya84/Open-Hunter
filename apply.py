#!/usr/bin/env python3
"""Entry point for the auto-apply agent.

    python apply.py output/jobs_XXXX.csv --mode review --match-only --limit 5

Modes:
    review  fill the form, then pause for your one-click submit (default)
    draft   fill + screenshot, never submit
    auto    fill and submit automatically (use with caution)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from open_hunter.apply.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
