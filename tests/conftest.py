import sys
from pathlib import Path

# Make src/efuse_tool.py importable in tests
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
