"""Makes the shared serverctl package importable from the repository, without installing it."""

import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))
