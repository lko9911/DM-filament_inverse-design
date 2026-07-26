from __future__ import annotations

import unittest

from scripts.ui.dm_spiral_mapping import build_spiral_mapping
from scripts.ui.layer_region_analysis import (
    LayerRegionAnalysis,
    LayerRegionOccurrence,
    LayerRegionSegment,
)


class DmSpiralMappingTests(unittest.TestCase):
    def test_occurrences_map_contiguously_after_start_feed(self) -> None:
        occurrences = []
        for sequence_index, extrusion in enumerate((10.0, 20.0), start=1):
            occurrences.append(
                LayerRegionOccurrence(
                    sequence_index=sequence_index,
                    layer_index=0,
                    layer_label=0,
                    layer_z=0.2,
                    region_name=f"R{sequence_index}",
                    occurrence_index=1,
                    source_line_start=sequence_index,
                    source_line_end=sequence_index,
                    segments=[
                        LayerRegionSegment(
                            0.0,
                            0.0,
                            0.2,
                            1.0,
                            0.0,
                            0.2,
                            extrusion,
                            sequence_index,
                        )
                    ],
                )
            )
        analysis = LayerRegionAnalysis(
            source_gcode="test.gcode",
            occurrences=occurrences,
            total_deposition_e_mm=30.0,
            region_deposition_e_mm=30.0,
            non_region_deposition_e_mm=0.0,
            layer_count=1,
            region_names=["R1", "R2"],
        )

        mapping = build_spiral_mapping(
            analysis,
            feed_start_mm=5.0,
            feed_end_mm=2.0,
            sample_mm=1.0,
        )

        first = mapping.occurrence_segment(1)
        second = mapping.occurrence_segment(2)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertAlmostEqual(second.filament_start_mm, 5.0)
        self.assertAlmostEqual(second.filament_end_mm, 25.0)
        self.assertAlmostEqual(first.filament_start_mm, 25.0)
        self.assertAlmostEqual(first.filament_end_mm, 35.0)
        self.assertAlmostEqual(mapping.mapped_length_mm, 30.0)
        self.assertAlmostEqual(mapping.total_length_with_feed_mm, 37.0)
        self.assertGreater(mapping.outer_radius_mm, mapping.inner_radius_mm)

        filtered = build_spiral_mapping(
            analysis,
            feed_start_mm=5.0,
            feed_end_mm=2.0,
            sample_mm=1.0,
            included_sequence_indices={2},
        )
        self.assertIsNone(filtered.occurrence_segment(1))
        self.assertAlmostEqual(filtered.mapped_length_mm, 20.0)
        self.assertEqual(
            filtered.to_payload()["schema"],
            "b_fdm.object_spiral_mapping.v1",
        )
        self.assertEqual(
            filtered.to_payload()["manufacturing_order"],
            "reverse_execution",
        )


if __name__ == "__main__":
    unittest.main()
