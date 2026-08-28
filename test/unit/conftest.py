import sys
from pathlib import Path

# Allow running the suite from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
