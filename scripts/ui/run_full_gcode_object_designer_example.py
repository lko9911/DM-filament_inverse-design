from __future__ import annotations

from pathlib import Path

try:
    from .full_gcode_object_property_designer import make_live_pyvista_preview, parse_full_gcode_objects
except ImportError:
    from full_gcode_object_property_designer import make_live_pyvista_preview, parse_full_gcode_objects
from qt_full_gcode_object_property_designer import QtFullGcodeObjectDesigner
try:
    from .component_property_designer import DEFAULT_OUTPUT_PATH, launch_ui
except ImportError:
    from component_property_designer import DEFAULT_OUTPUT_PATH, launch_ui


INPUT_GCODE = r"Sample_compenent\origami_gripper - base^origami_gripper-2_0.15mm_ABS_MK3S_55m.gcode"
OUTPUT_JSON = r"input\config\Property_sample.json"
VOXEL_THRESHOLD_E = 2.0
USE_QT_EMBEDDED_3D_UI = True
USE_PYVISTA_PREVIEW = False


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    input_path = Path(INPUT_GCODE)
    output_path = Path(OUTPUT_JSON) if OUTPUT_JSON else DEFAULT_OUTPUT_PATH

    if not input_path.is_absolute():
        input_path = project_root / input_path
    if not output_path.is_absolute():
        output_path = project_root / output_path

    components = parse_full_gcode_objects(input_path)
    if not components:
        raise SystemExit(f"No object components found in: {input_path}")

    for component in components:
        print(
            f"C{component.index}: {component.display_name} | "
            f"E={component.total_e:.6f} | layers={component.layer_count} | segments={len(component.segments)}"
        )

    if USE_QT_EMBEDDED_3D_UI:
        from PyQt5 import QtWidgets
        app = QtWidgets.QApplication([])
        window = QtFullGcodeObjectDesigner(components, output_path, VOXEL_THRESHOLD_E)
        window.show()
        app.exec_()
        return

    preview_controller = make_live_pyvista_preview(components) if USE_PYVISTA_PREVIEW else None
    launch_ui(components, output_path, VOXEL_THRESHOLD_E, preview_controller=preview_controller)


if __name__ == "__main__":
    main()
