# Ensure 'src' is importable when running `pytest` from repo root.
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
SRC = _HERE.parents[1]  # .../src
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
