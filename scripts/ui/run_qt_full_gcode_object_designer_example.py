from __future__ import annotations

import sys
from pathlib import Path

from PyQt5 import QtWidgets

try:
    from .full_gcode_object_property_designer import parse_full_gcode_objects
except ImportError:
    from full_gcode_object_property_designer import parse_full_gcode_objects

INPUT_GCODE = r"Sample_compenent\origami_gripper - base^origami_gripper-2_0.15mm_ABS_MK3S_55m.gcode"
OUTPUT_JSON = r"input\config\Property_sample.json"
VOXEL_THRESHOLD_E = 2.0


def resolve_project_path(path_text: str) -> Path:
    project_root = Path(__file__).resolve().parents[2]
    path = Path(path_text)
    return path if path.is_absolute() else project_root / path


def main() -> None:
    input_path = resolve_project_path(INPUT_GCODE)
    output_path = resolve_project_path(OUTPUT_JSON)

    components = parse_full_gcode_objects(input_path)
    if not components:
        raise SystemExit(f"No object components found in: {input_path}")

    for component in components:
        print(
            f"C{component.index}: {component.display_name} | "
            f"E={component.total_e:.6f} | layers={component.layer_count} | segments={len(component.segments)}"
        )

    from qt_full_gcode_object_property_designer import QtFullGcodeObjectDesigner
    app = QtWidgets.QApplication(sys.argv)
    window = QtFullGcodeObjectDesigner(components, output_path, VOXEL_THRESHOLD_E)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
