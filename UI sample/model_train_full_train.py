from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
VER3_PATH = BASE_DIR.parent / "DM_filament_model ver3" / "model_train_full_train.py"

if not VER3_PATH.exists():
    raise ModuleNotFoundError(f"Cannot locate ver3 training module: {VER3_PATH}")

_spec = importlib.util.spec_from_file_location("_dm_filament_model_ver3_model_train_full_train", VER3_PATH)
if _spec is None or _spec.loader is None:
    raise ModuleNotFoundError(f"Failed to load ver3 training module from: {VER3_PATH}")

_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

for _name in dir(_module):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_module, _name)

__all__ = [name for name in globals() if not name.startswith("_")]
