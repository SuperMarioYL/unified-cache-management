from __future__ import annotations

import sys
from pathlib import Path

PRODUCTION_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PRODUCTION_ROOT.parents[2]

if str(PRODUCTION_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTION_ROOT))
