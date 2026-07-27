from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build.assignment_length_matrix import build_length_matrix
from scripts.build.build_assignment_candidate_matrix import (
    build_repeated_layer_template_summary,
    get_assignment_step_target_counts,
)
from scripts.build.genetic_algorithm_step_adjacency_from_text import (
    CandidateState,
    select_repeated_layer_best_states,
)
from scripts.property_guided.expand_layer_region_program import (
    expand_layer_region_program,
)
from scripts.property_guided.resolve_property_guided_program import (
    resolve_property_guided_program,
)
from scripts.utils.property_program_utils import resolve_assignment_material_pair
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

    def test_execution_plan_repeats_region_ratio_with_exact_event_lengths(self) -> None:
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
        self.assertEqual(first_ratio, second_ratio)
        self.assertAlmostEqual(first_ratio, 0.5)

    def test_layer_region_expands_property_type_gradient_to_direct_materials(self) -> None:
        program = {
            "assignments": [
                {
                    "assignment_index": 2,
                    "source_component_index": 2,
                    "Property_type": "Property",
                    "material_count": 1,
                    "material_start": "MAGENTA",
                },
                {
                    "assignment_index": 3,
                    "source_component_index": 3,
                    "Property_type": "Gradient",
                    "gradient_steps": 5,
                    "gradient_direction": "printing",
                    "Property_start": 2,
                    "Property_end": 4,
                    "eta": 2.0,
                },
                {
                    "assignment_index": 4,
                    "source_component_index": 4,
                    "Property_type": "Property",
                    "material_count": 1,
                    "material_start": "YELLOW",
                },
            ],
            "layer_region_execution_plan": {
                "events": [
                    {
                        "execution_step_index": index,
                        "sequence_index": index,
                        "source_component_index": source_index,
                        "region_name": f"REGION_{source_index}",
                        "layer_index": 0,
                        "extrusion_e_mm": 1.0,
                    }
                    for index, source_index in enumerate((2, 3, 4), start=1)
                ]
            },
        }

        expanded, _summary = expand_layer_region_program(program)
        gradient = expanded["assignments"][1]

        self.assertEqual(gradient["Property_type"], "Gradient")
        self.assertEqual(gradient["gradient_steps"], 1)
        self.assertEqual(gradient["material_start"], "MAGENTA")
        self.assertEqual(gradient["material_end"], "YELLOW")
        self.assertEqual(
            get_assignment_step_target_counts(expanded, gradient, 0, 1)[:2],
            (24, 24),
        )
        self.assertEqual(
            resolve_assignment_material_pair(expanded, gradient),
            ("MAGENTA", "YELLOW"),
        )

    def test_repeated_layer_template_counts_only_one_layer_patterns(self) -> None:
        assignments = []
        cells = []
        for assignment_index, (layer_index, source_index, candidate_count) in enumerate(
            (
                (0, 2, 1),
                (0, 3, 4),
                (1, 2, 1),
                (1, 3, 4),
            ),
            start=1,
        ):
            assignments.append(
                {
                    "assignment_index": assignment_index,
                    "layer_region_event": {
                        "layer_index": layer_index,
                        "source_component_index": source_index,
                    },
                }
            )
            case_keys = [f"case_{index}" for index in range(candidate_count)]
            cells.append(
                {
                    "assignment_index": assignment_index,
                    "assignment_property_type": "Gradient" if source_index == 3 else "Property",
                    "assignment_material_start": "MAGENTA",
                    "assignment_material_end": "YELLOW" if source_index == 3 else "MAGENTA",
                    "target_material_start_count": 24 if source_index == 3 else 48,
                    "target_material_end_count": 24 if source_index == 3 else 0,
                    "candidate_count": candidate_count,
                    "candidates": [{"case_key": case_key} for case_key in case_keys],
                }
            )

        summary = build_repeated_layer_template_summary(
            {"assignments": assignments},
            cells,
        )

        self.assertIsNotNone(summary)
        self.assertEqual(summary["layer_count"], 2)
        self.assertEqual(summary["steps_per_layer"], 2)
        self.assertEqual(summary["template_pattern_count"], 4)
        self.assertEqual(summary["expanded_step_count"], 4)

    def test_repeated_layer_selection_minimizes_switches_before_score(self) -> None:
        high_score = CandidateState(
            selected_case_keys=["high_score"],
            selected_rows_per_step=[],
            step_scores=[],
            total_score=100,
            eta_sum=5.0,
            material_switch_count=6,
        )
        low_switch = CandidateState(
            selected_case_keys=["low_switch"],
            selected_rows_per_step=[],
            step_scores=[],
            total_score=80,
            eta_sum=5.0,
            material_switch_count=5,
        )

        selected = select_repeated_layer_best_states([high_score, low_switch])

        self.assertEqual(selected, [low_switch])

    def test_z_axis_uses_component_program_without_layer_region_expansion(self) -> None:
        plan = {
            "events": [
                {
                    "execution_step_index": 1,
                    "sequence_index": 10,
                    "source_component_index": 0,
                    "region_name": "A.stl",
                    "layer_index": 0,
                    "extrusion_e_mm": 2.0,
                },
                {
                    "execution_step_index": 2,
                    "sequence_index": 20,
                    "source_component_index": 0,
                    "region_name": "A.stl",
                    "layer_index": 1,
                    "extrusion_e_mm": 3.0,
                },
                {
                    "execution_step_index": 3,
                    "sequence_index": 30,
                    "source_component_index": 0,
                    "region_name": "A.stl",
                    "layer_index": 2,
                    "extrusion_e_mm": 5.0,
                },
            ]
        }
        program = {
            "region_recognition_mode": "z-axis",
            "assignments": [
                {
                    "assignment_index": 1,
                    "source_component_index": 0,
                    "component_name": "A.stl",
                    "Property_type": "Gradient",
                    "type": "Gradient",
                    "start_voxel_index": 1,
                    "end_voxel_index": 3,
                    "start_layer": 0,
                    "end_layer": 2,
                    "gradient_steps": 30,
                    "gradient_direction": "layer",
                    "material_start": "PLA",
                    "material_end": "TPU",
                }
            ],
            "layer_region_execution_plan": plan,
        }

        expanded, summary = expand_layer_region_program(program)
        self.assertIs(expanded, program)
        self.assertEqual(summary["expanded_event_count"], 0)
        self.assertIn("component-level", summary["reason"])
        self.assertEqual(len(expanded["assignments"]), 1)
        self.assertNotIn("layer_region_event", expanded["assignments"][0])
        self.assertEqual(expanded["assignments"][0]["gradient_steps"], 30)

    def test_z_axis_environment_skips_property_layer_expansion(self) -> None:
        program = {
            "assignments": [
                {
                    "assignment_index": 1,
                    "source_component_index": 0,
                    "component_name": "PROPERTY_A",
                    "Property_type": "Property",
                    "type": "Property",
                    "start_voxel_index": 1,
                    "end_voxel_index": 2,
                    "start_layer": 0,
                    "end_layer": 1,
                    "gradient_steps": 1,
                    "gradient_direction": "printing",
                    "material_start": "PLA",
                }
            ],
            "layer_region_execution_plan": {
                "events": [
                    {
                        "execution_step_index": 1,
                        "sequence_index": 10,
                        "source_component_index": 0,
                        "region_name": "PROPERTY_A",
                        "layer_index": 0,
                        "extrusion_e_mm": 4.0,
                    },
                    {
                        "execution_step_index": 2,
                        "sequence_index": 20,
                        "source_component_index": 0,
                        "region_name": "PROPERTY_A",
                        "layer_index": 1,
                        "extrusion_e_mm": 6.0,
                    },
                ]
            },
        }

        with patch.dict(
            "os.environ",
            {"B_FDM_REGION_RECOGNITION_MODE": "z-axis"},
        ):
            expanded, summary = expand_layer_region_program(program)

        self.assertIs(expanded, program)
        self.assertEqual(summary["expanded_event_count"], 0)
        self.assertEqual(len(expanded["assignments"]), 1)
        self.assertEqual(expanded["assignments"][0]["Property_type"], "Property")
        self.assertEqual(expanded["assignments"][0]["gradient_steps"], 1)

    def test_z_axis_preserves_gradient_property_references(self) -> None:
        program = {
            "region_recognition_mode": "z-axis",
            "assignments": [
                {
                    "assignment_index": 2,
                    "source_component_index": 2,
                    "Property_type": "Property",
                    "material_count": 1,
                    "material_start": "MAGENTA",
                },
                {
                    "assignment_index": 3,
                    "source_component_index": 3,
                    "Property_type": "Gradient",
                    "gradient_steps": 30,
                    "gradient_direction": "printing",
                    "Property_start": 2,
                    "Property_end": 4,
                },
                {
                    "assignment_index": 4,
                    "source_component_index": 4,
                    "Property_type": "Property",
                    "material_count": 1,
                    "material_start": "YELLOW",
                },
            ],
            "layer_region_execution_plan": {
                "events": [
                    {
                        "execution_step_index": index,
                        "sequence_index": index,
                        "source_component_index": source_index,
                        "region_name": f"REGION_{source_index}",
                        "layer_index": index,
                        "extrusion_e_mm": 1.0,
                    }
                    for index, source_index in enumerate((2, 3, 4), start=1)
                ]
            },
        }

        expanded, _summary = expand_layer_region_program(program)
        gradient = expanded["assignments"][1]

        self.assertEqual(gradient["Property_start"], 2)
        self.assertEqual(gradient["Property_end"], 4)
        self.assertEqual(
            set(resolve_assignment_material_pair(expanded, gradient)),
            {"MAGENTA", "YELLOW"},
        )


if __name__ == "__main__":
    unittest.main()
