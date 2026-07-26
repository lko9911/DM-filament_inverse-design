from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build.assignment_length_matrix import build_length_matrix
from scripts.property_guided.expand_layer_region_program import (
    expand_layer_region_program,
)
from scripts.property_guided.resolve_property_guided_program import (
    resolve_property_guided_program,
)
from scripts.ui.layer_region_analysis import (
    analyze_layer_regions,
    build_execution_plan,
)
from scripts.ui.full_gcode_object_property_designer import parse_full_gcode_objects


class LayerRegionResolutionTests(unittest.TestCase):
    def test_manual_program_does_not_require_property_workbook(self) -> None:
        program = {
            "assignments": [
                {
                    "assignment_index": 1,
                    "assignment_mode": "manual",
                    "Property_type": "Property",
                }
            ]
        }
        with patch(
            "scripts.property_guided.resolve_property_guided_program.load_property_library",
            side_effect=AssertionError("manual mode must not load the workbook"),
        ):
            resolved, summary = resolve_property_guided_program(program)

        self.assertEqual(resolved["assignments"][0]["assignment_mode"], "manual")
        self.assertFalse(summary["library_loaded"])
        self.assertEqual(summary["guided_assignment_count"], 0)

    def test_component_list_uses_first_extrusion_order_not_m486_declaration_order(self) -> None:
        gcode = """\
M486 S0
M486 ABodyA
M486 S-1
M486 S1
M486 ABodyB
M486 S-1
G90
M83
;TYPE:Custom
G1 X10 E3
;LAYER_CHANGE
;Z:0.2
M486 S1
G1 X20 E2
M486 S-1
M486 S0
G1 X30 E1
M486 S-1
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "chronological.gcode"
            path.write_text(gcode, encoding="utf-8")
            components = parse_full_gcode_objects(path)

        self.assertEqual(
            [component.display_name for component in components],
            ["Custom", "BodyB", "BodyA"],
        )
        self.assertEqual(
            [component.index for component in components],
            [1, 2, 3],
        )

    def test_parser_preserves_layer_region_occurrence_order_and_exact_e(self) -> None:
        gcode = """\
G90
M83
;LAYER:0
;Z:0.2
;MESH:A.stl
G1 X10 Y0 E1.0
;MESH:B.stl
G1 X20 Y0 E2.0
;MESH:A.stl
G1 X30 Y0 E3.0
;MESH:NONMESH
G1 X40 Y0 E0.5
;LAYER:1
;Z:0.4
;HEIGHT:0.2
;MESH:B.stl
G1 X40 Y10 E4.0
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "regions.gcode"
            path.write_text(gcode, encoding="utf-8")
            analysis = analyze_layer_regions(path)

        self.assertEqual(
            [(item.layer_label, item.region_name, item.occurrence_index) for item in analysis.occurrences],
            [(0, "A.stl", 1), (0, "B.stl", 1), (0, "A.stl", 2), (1, "B.stl", 1)],
        )
        self.assertAlmostEqual(analysis.region_deposition_e_mm, 10.0)
        self.assertAlmostEqual(analysis.non_region_deposition_e_mm, 0.5)
        self.assertAlmostEqual(analysis.total_deposition_e_mm, 10.5)
        self.assertTrue(analysis.warnings)
        self.assertAlmostEqual(analysis.occurrences[-1].layer_z or 0.0, 0.4)

    def test_execution_plan_expands_into_exact_length_steps(self) -> None:
        gcode = """\
G90
M83
;LAYER:0
;MESH:A.stl
G1 X10 E2.5
;LAYER:1
;MESH:A.stl
G1 X20 E7.5
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "regions.gcode"
            path.write_text(gcode, encoding="utf-8")
            analysis = analyze_layer_regions(path)

        plan = build_execution_plan(analysis, {"A.stl": 0}, {0})
        program = {
            "property_type": "Gradient",
            "assignments": [
                {
                    "assignment_index": 0,
                    "source_component_index": 0,
                    "component_name": "A.stl",
                    "type": "Gradient",
                    "start_voxel_index": 1,
                    "end_voxel_index": 2,
                    "start_layer": 0,
                    "end_layer": 1,
                    "gradient_steps": 4,
                    "gradient_direction": "printing",
                    "material_start": "PLA",
                    "material_end": "TPU",
                    "eta": 0.2,
                }
            ],
            "layer_region_execution_plan": plan,
        }

        expanded, summary = expand_layer_region_program(program)
        lengths, assignments = build_length_matrix({"voxels": []}, expanded)

        self.assertEqual(summary["expanded_event_count"], 2)
        self.assertEqual(lengths, [2.5, 7.5])
        self.assertEqual(
            [row["resolution_mode"] for row in assignments],
            ["layer_region_occurrence", "layer_region_occurrence"],
        )
        first_ratio = expanded["assignments"][0]["resolved_step_targets"][0]["ratio_start"]
        second_ratio = expanded["assignments"][1]["resolved_step_targets"][0]["ratio_start"]
        self.assertGreater(first_ratio, second_ratio)


if __name__ == "__main__":
    unittest.main()
