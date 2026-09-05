import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Every test that touches the filesystem or the config expects the repo root as its cwd, the
# same assumption serve.sh makes.
os.chdir(ROOT)
