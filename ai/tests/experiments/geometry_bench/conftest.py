from __future__ import annotations

import sys
from pathlib import Path

_AI_ROOT = str(Path(__file__).resolve().parents[3])
if _AI_ROOT not in sys.path:
    sys.path.insert(0, _AI_ROOT)
