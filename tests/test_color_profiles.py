from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.build.build_assignment_candidate_matrix import (
    build_assignment_candidate_matrix,
)
from scripts.utils.property_excel_lookup import (
    COLOR_PROFILE_OPTIONS,
    resolve_color_recipe,
)


class ColorProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.material_dictionary = json.loads(
            Path("input/config/material_dictionary.json").read_text(encoding="utf-8")
        )

    def build_program(self, brighter: bool) -> dict[str, object]:
        assignments = []
        for index, color_key in enumerate(COLOR_PROFILE_OPTIONS, start=1):
            recipe = resolve_color_recipe(color_key)
            material_count = int(recipe["material_count"])
            assignment = {
                "assignment_index": index,
                "assignment_mode": "manual",
                "Property_type": "Property",
                "gradient_steps": 1,
                "gradient_direction": "printing",
                "eta_mode": "auto",
                "eta": 2.0 if material_count >= 2 else 0.0,
                "material_count": material_count,
                "material_start": recipe["material_start"],
                "material_start_ratio": recipe["material_start_ratio"],
                "material_end_ratio": recipe["material_end_ratio"],
                "requested_color": color_key,
                "color_recipe": recipe,
                "brighter_mode": brighter,
            }
            if material_count >= 2:
                assignment["material_end"] = recipe["material_end"]
            assignments.append(assignment)
        return {"assignments": assignments}

    def test_palette_has_eighteen_cmy_profiles_plus_black_and_white(self) -> None:
        self.assertEqual(len(COLOR_PROFILE_OPTIONS), 20)
        self.assertEqual(COLOR_PROFILE_OPTIONS[-2:], ["BLACK", "WHITE"])

        cmy_profiles = COLOR_PROFILE_OPTIONS[:18]
        self.assertEqual(cmy_profiles[0], "M100")
        self.assertEqual(cmy_profiles[6], "Y100")
        self.assertEqual(cmy_profiles[12], "C100")

    def test_all_twenty_normal_profiles_have_candidates(self) -> None:
        payload = build_assignment_candidate_matrix(
            self.build_program(brighter=False),
            self.material_dictionary,
        )

        self.assertEqual(len(payload["candidate_matrix"]), 20)
        self.assertTrue(
            all(cell["candidate_count"] > 0 for cell in payload["candidate_matrix"])
        )

    def test_all_twenty_brighter_profiles_have_candidates(self) -> None:
        payload = build_assignment_candidate_matrix(
            self.build_program(brighter=True),
            self.material_dictionary,
        )

        self.assertEqual(len(payload["candidate_matrix"]), 20)
        self.assertTrue(
            all(cell["candidate_count"] > 0 for cell in payload["candidate_matrix"])
        )
        for color_key, cell in zip(COLOR_PROFILE_OPTIONS, payload["candidate_matrix"]):
            if color_key == "WHITE":
                self.assertEqual(cell["eta_target"], 0.0)
            elif color_key in {"M100", "Y100", "C100", "BLACK"}:
                self.assertEqual(cell["eta_target"], 2.0)
            else:
                self.assertEqual(cell["eta_target"], 4.0)


if __name__ == "__main__":
    unittest.main()
