from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build.build_assignment_candidate_matrix import get_assignment_eta_bounds
from scripts.ui.component_property_designer import (
    ComponentModel,
    build_property_payload,
    default_state,
)


class EtaModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.component = ComponentModel(
            index=1,
            path=Path("green.gcode"),
            segments=[],
            total_e=1.0,
            layer_count=1,
            min_z=0.2,
            max_z=0.2,
            display_name="GREEN",
        )
        self.recipe = {
            "requested_color": "Y50_C50",
            "target_mpa": None,
            "target_gf": None,
            "material_count": 2,
            "material_start": "CYAN",
            "material_end": "YELLOW",
            "material_start_ratio": 50.0,
            "material_end_ratio": 50.0,
            "eta": 1.0,
            "fixed_case_rows": None,
        }

    def build_assignment(self, eta_mode: str, eta: float) -> dict[str, object]:
        state = default_state(self.component)
        state.update({"material_start": "Y50_C50", "eta_mode": eta_mode, "eta": eta})
        with patch(
            "scripts.ui.component_property_designer.resolve_color_recipe",
            return_value=self.recipe,
        ):
            payload = build_property_payload([self.component], {1: state}, 1.0)
        return payload["assignments"][0]

    def test_auto_mixed_eta_is_two_and_not_recipe_eta(self) -> None:
        assignment = self.build_assignment("auto", 0.0)

        self.assertEqual(assignment["material_start"], "CYAN")
        self.assertEqual(assignment["material_end"], "YELLOW")
        self.assertEqual(assignment["eta_mode"], "auto")
        self.assertEqual(assignment["eta"], 2.0)
        self.assertEqual(assignment["color_recipe"]["eta"], 1.0)

    def test_manual_eta_is_preserved_independently_of_color_recipe(self) -> None:
        assignment = self.build_assignment("manual", 3.0)

        self.assertEqual(assignment["eta_mode"], "manual")
        self.assertEqual(assignment["eta"], 3.0)

    def test_legacy_requested_color_uses_auto_eta_two(self) -> None:
        assignment = {
            "Property_type": "Property",
            "material_start": "CYAN",
            "material_end": "YELLOW",
            "requested_color": "GREEN",
            "eta": 0.0,
        }

        _eta_min, eta_limit, assignment_eta, is_single = get_assignment_eta_bounds(
            {},
            assignment,
        )

        self.assertFalse(is_single)
        self.assertEqual(eta_limit, 2.0)
        self.assertEqual(assignment_eta, 2.0)


if __name__ == "__main__":
    unittest.main()
