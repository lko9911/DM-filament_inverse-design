from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
VER3_DATASET_DIR = BASE_DIR.parent / "DM_filament_model ver3" / "Dataset_Make"
VER3_PATH = VER3_DATASET_DIR / "Gcode_Property_Program_Designer.py"

if not VER3_PATH.exists():
    raise ModuleNotFoundError(f"Cannot locate ver3 designer module: {VER3_PATH}")

_spec = importlib.util.spec_from_file_location("_dm_filament_model_ver3_gcode_property_program_designer", VER3_PATH)
if _spec is None or _spec.loader is None:
    raise ModuleNotFoundError(f"Failed to load ver3 designer module from: {VER3_PATH}")

_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_previous_path = list(sys.path)
try:
    sys.path.insert(0, str(VER3_DATASET_DIR))
    _spec.loader.exec_module(_module)
finally:
    sys.path[:] = _previous_path

for _name in dir(_module):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_module, _name)


def _normalize_assignment_for_save(assignment: dict) -> dict:
    normalized = dict(assignment)
    property_enabled = bool(normalized.get("property_enabled", True))
    mat_ratio_1 = float(normalized.get("mat_ratio_1", normalized.get("color_ratio_1", 100.0)))
    mat_ratio_2 = float(normalized.get("mat_ratio_2", normalized.get("color_ratio_2", 0.0)))
    gradient_steps = int(float(normalized.get("gradient_steps", 1)))
    gradient_direction = str(normalized.get("gradient_direction", normalized.get("direction", "layer"))).strip().lower()
    if gradient_direction not in {"layer", "printing"}:
        gradient_direction = "layer"

    compat = dict(normalized)
    compat["property_enabled"] = property_enabled
    compat["mat_ratio_1"] = mat_ratio_1
    compat["mat_ratio_2"] = mat_ratio_2
    compat["gradient_steps"] = gradient_steps
    compat["gradient_direction"] = gradient_direction
    compat["color_ratio_1"] = mat_ratio_1
    compat["color_ratio_2"] = mat_ratio_2
    compat["direction"] = "filament_path" if gradient_direction == "layer" else "reverse_filament_path"
    compat["transition"] = f"{gradient_steps}-step" if gradient_steps > 1 else "non-graded"
    if not property_enabled:
        compat["material_count"] = 0
        compat["material_1"] = None
        compat["material_2"] = None
        compat["mat_ratio_1"] = 0.0
        compat["mat_ratio_2"] = 0.0
        compat["color_ratio_1"] = 0.0
        compat["color_ratio_2"] = 0.0
        compat["gradient_steps"] = 0
    return compat


def _sanitize_saved_assignment(assignment: dict) -> dict:
    gradient_direction = str(assignment.get("gradient_direction", assignment.get("direction", "layer"))).strip().lower()
    if gradient_direction not in {"layer", "printing"}:
        gradient_direction = "layer"

    sanitized = {
        "assignment_index": int(assignment.get("assignment_index", 1)),
        "start_voxel": int(assignment.get("start_voxel", 1)),
        "end_voxel": int(assignment.get("end_voxel", assignment.get("start_voxel", 1))),
        "layer_start": int(assignment.get("layer_start", -1)),
        "layer_end": int(assignment.get("layer_end", -1)),
        "layer_count": int(assignment.get("layer_count", 0)),
        "voxel_count": int(assignment.get("voxel_count", 0)),
        "total_filament_e_mm": round(float(assignment.get("total_filament_e_mm", 0.0)), 6),
        "total_filament_length_mm": round(float(assignment.get("total_filament_length_mm", 0.0)), 6),
        "voxel_layer_table": [
            {
                "voxel_id": int(item.get("voxel_id", 0)),
                "layer_num": int(item.get("layer_num", -1)),
            }
            for item in assignment.get("voxel_layer_table", [])
            if isinstance(item, dict)
        ],
        "material_count": int(assignment.get("material_count", 1)),
        "material_1": assignment.get("material_1"),
        "material_2": assignment.get("material_2") if int(assignment.get("material_count", 1)) >= 2 else None,
        "gradient_steps": int(float(assignment.get("gradient_steps", 1))),
        "gradient_direction": gradient_direction,
        "eta": float(assignment.get("eta", 0.5)),
        "property_enabled": bool(assignment.get("property_enabled", True)),
        "mat_ratio_1": float(assignment.get("mat_ratio_1", assignment.get("color_ratio_1", 100.0))),
        "mat_ratio_2": float(assignment.get("mat_ratio_2", assignment.get("color_ratio_2", 0.0))),
        "estimated_material_1_length_mm": round(float(assignment.get("estimated_material_1_length_mm", 0.0)), 6),
        "estimated_material_2_length_mm": round(float(assignment.get("estimated_material_2_length_mm", 0.0)), 6),
    }
    if sanitized["material_count"] < 2:
        sanitized["material_2"] = None
        sanitized["mat_ratio_2"] = 0.0
        sanitized["estimated_material_2_length_mm"] = 0.0
    return sanitized


def save_property_program(output_paths, gcode_path, delta_e, voxels, preprint_e, assignments):
    compat_assignments = [_normalize_assignment_for_save(assignment) for assignment in assignments]
    _module.save_property_program(output_paths, gcode_path, delta_e, voxels, preprint_e, compat_assignments)

    program_json_path = Path(output_paths["program_json"])
    if not program_json_path.exists():
        return

    try:
        payload = json.loads(program_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return

    if isinstance(payload, dict) and isinstance(payload.get("assignments"), list):
        payload["assignment_schema"] = "mat_ratio_gradient"
        payload["assignments"] = [
            _sanitize_saved_assignment(assignment)
            for assignment in payload["assignments"]
            if isinstance(assignment, dict)
        ]
        program_json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


__all__ = [name for name in globals() if not name.startswith("_")]
