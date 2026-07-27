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
            recipe = resolve_color_recipe(color_key, brighter_mode=brighter)
            material_count = int(recipe["material_count"])
            fixed_eta = recipe.get("fixed_eta")
            assignment = {
                "assignment_index": index,
                "assignment_mode": "manual",
                "Property_type": "Property",
                "gradient_steps": 1,
                "gradient_direction": "printing",
                "eta_mode": "manual" if fixed_eta is not None else "auto",
                "eta": (
                    float(fixed_eta)
                    if fixed_eta is not None
                    else 2.0 if material_count >= 2 else 0.0
                ),
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

    def test_palette_has_eighteen_cmy_profiles_plus_purple_black_and_white(self) -> None:
        self.assertEqual(len(COLOR_PROFILE_OPTIONS), 21)
        self.assertEqual(COLOR_PROFILE_OPTIONS[-3:], ["PURPLE", "BLACK", "WHITE"])

        cmy_profiles = COLOR_PROFILE_OPTIONS[:18]
        self.assertEqual(cmy_profiles[0], "M100")
        self.assertEqual(cmy_profiles[6], "Y100")
        self.assertEqual(cmy_profiles[12], "C100")

    def test_all_normal_profiles_have_candidates(self) -> None:
        payload = build_assignment_candidate_matrix(
            self.build_program(brighter=False),
            self.material_dictionary,
        )

        self.assertEqual(len(payload["candidate_matrix"]), 21)
        self.assertTrue(
            all(cell["candidate_count"] > 0 for cell in payload["candidate_matrix"])
        )

    def test_all_brighter_profiles_have_candidates(self) -> None:
        payload = build_assignment_candidate_matrix(
            self.build_program(brighter=True),
            self.material_dictionary,
        )

        self.assertEqual(len(payload["candidate_matrix"]), 21)
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

    def test_purple_uses_ten_cyan_and_thirty_eight_magenta_blocks(self) -> None:
        recipe = resolve_color_recipe("PURPLE")

        self.assertAlmostEqual(recipe["material_start_ratio"], 100.0 * 10.0 / 48.0)
        self.assertAlmostEqual(recipe["material_end_ratio"], 100.0 * 38.0 / 48.0)

        purple_index = COLOR_PROFILE_OPTIONS.index("PURPLE")
        payload = build_assignment_candidate_matrix(
            self.build_program(brighter=False),
            self.material_dictionary,
        )
        cell = payload["candidate_matrix"][purple_index]

        self.assertEqual(cell["target_material_start_count"], 10)
        self.assertEqual(cell["target_material_end_count"], 38)
        self.assertTrue(cell["candidates"])
        self.assertEqual(
            {
                int(candidate["material_start_count"])
                for candidate in cell["candidates"]
            },
            {10},
        )
        self.assertEqual(
            {
                candidate["case_key"]
                for candidate in cell["candidates"]
            },
            {"case_04093"},
        )
        self.assertEqual(cell["compact_material_preference"], "Material_end")


if __name__ == "__main__":
    unittest.main()
