import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("AIRLOCK_EXECUTION_MODE", "local")
os.environ.setdefault("AIRLOCK_WORK_ROOT", "/tmp/airlock-pytest")
