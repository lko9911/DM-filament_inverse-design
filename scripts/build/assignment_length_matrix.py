from __future__ import annotations

import os
from pathlib import Path
import json

import numpy as np

from scripts.utils.property_program_utils import (
    get_effective_gradient_steps,
    get_assignments_in_spatial_order,
    get_property_type,
    resolve_property_program_path,
)


SAMPLE_INFO_PATH_ENV_KEY = "B_FDM_SAMPLE_INFO_PATH"
SAMPLE_INFO_PATH = Path("input/config/sample_info.json")
SAMPLE_INFO_PATH = Path(os.environ.get(SAMPLE_INFO_PATH_ENV_KEY, SAMPLE_INFO_PATH))
PROPERTY_PROGRAM_PATH = resolve_property_program_path()
OUTPUT_PATH = Path("test_sample/derived/matrices/length_matrix.json")
OUTPUT_NPY_PATH = Path("test_sample/derived/matrices/length_matrix.npy")


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_voxel_filament_sum(sample_info: dict, start_voxel_index: int, end_voxel_index: int) -> float:
    voxels = sample_info.get("voxels", [])
    if start_voxel_index < 1 or end_voxel_index < start_voxel_index:
        raise ValueError("Invalid voxel index range")
    if end_voxel_index > len(voxels):
        raise IndexError("Voxel index range exceeds sample_info voxel count")

    total = 0.0
    for voxel in voxels[start_voxel_index - 1 : end_voxel_index]:
        total += float(voxel["voxel_filament_e_mm"])
    return total


def get_voxel_filament_sum_by_layer(sample_info: dict, start_layer: int, end_layer: int) -> float:
    voxels = sample_info.get("voxels", [])
    if start_layer < 0 or end_layer < start_layer:
        raise ValueError("Invalid layer range")

    total = 0.0
    for voxel in voxels:
        layer_num = int(voxel.get("layer_num", -1))
        if start_layer <= layer_num <= end_layer:
            total += float(voxel["voxel_filament_e_mm"])
    return total


def build_index_segments(start_index: int, end_index: int, segment_count: int) -> list[tuple[int, int]]:
    if segment_count <= 0:
        return []
    if end_index < start_index:
        raise ValueError("Invalid index range")

    index_count = end_index - start_index + 1
    segments: list[tuple[int, int]] = []
    for segment_index in range(segment_count):
        seg_start_offset = (segment_index * index_count) // segment_count
        seg_end_offset = (((segment_index + 1) * index_count) // segment_count) - 1
        seg_start = start_index + seg_start_offset
        seg_end = start_index + seg_end_offset
        segments.append((seg_start, seg_end))
    return segments


def get_layer_range_for_voxel_segment(
    sample_info: dict,
    start_voxel_index: int,
    end_voxel_index: int,
) -> tuple[int | None, int | None]:
    voxels = sample_info.get("voxels", [])
    if start_voxel_index < 1 or end_voxel_index < start_voxel_index or end_voxel_index > len(voxels):
        return None, None

    layer_values = [
        int(voxel.get("layer_num", -1))
        for voxel in voxels[start_voxel_index - 1 : end_voxel_index]
        if int(voxel.get("layer_num", -1)) >= 0
    ]
    if not layer_values:
        return None, None
    return min(layer_values), max(layer_values)


def get_voxel_range_for_layer_segment(
    sample_info: dict,
    assignment_start_voxel_index: int,
    assignment_end_voxel_index: int,
    start_layer: int,
    end_layer: int,
) -> tuple[int | None, int | None]:
    voxels = sample_info.get("voxels", [])
    if assignment_start_voxel_index < 1 or assignment_end_voxel_index < assignment_start_voxel_index:
        return None, None
    if assignment_end_voxel_index > len(voxels):
        return None, None

    matched_voxel_ids = [
        int(voxel["voxel_id"])
        for voxel in voxels[assignment_start_voxel_index - 1 : assignment_end_voxel_index]
        if start_layer <= int(voxel.get("layer_num", -1)) <= end_layer
    ]
    if not matched_voxel_ids:
        return None, None
    return min(matched_voxel_ids), max(matched_voxel_ids)


def build_layer_segments(start_layer: int, end_layer: int, gradient_steps: int) -> list[tuple[int, int]]:
    if gradient_steps <= 0:
        return []
    if end_layer < start_layer:
        raise ValueError("Invalid layer range")

    layer_count = end_layer - start_layer + 1
    segments: list[tuple[int, int]] = []

    for step_index in range(gradient_steps):
        seg_start_offset = (step_index * layer_count) // gradient_steps
        seg_end_offset = (((step_index + 1) * layer_count) // gradient_steps) - 1
        seg_start = start_layer + seg_start_offset
        seg_end = start_layer + seg_end_offset
        segments.append((seg_start, seg_end))

    return segments


def get_total_step_count(property_program: dict) -> int:
    assignments = get_assignments_in_spatial_order(property_program)
    return sum(get_effective_gradient_steps(property_program, assignment) for assignment in assignments)


def _coerce_mapped_step_lengths(property_program: dict) -> list[float] | None:
    raw_values = property_program.get("mapped_after_step_lengths_e_mm")
    if not isinstance(raw_values, list):
        return None
    try:
        mapped_values = [float(value) for value in raw_values]
    except (TypeError, ValueError):
        return None
    if len(mapped_values) != get_total_step_count(property_program):
        return None
    return mapped_values


def build_length_matrix(sample_info: dict, property_program: dict) -> tuple[list[float], list[dict]]:
    assignments = get_assignments_in_spatial_order(property_program)
    mapped_step_lengths = _coerce_mapped_step_lengths(property_program)
    length_matrix: list[float] = []
    assignment_summaries: list[dict] = []
    mapped_offset = 0

    for assignment in assignments:
        assignment_index = int(assignment["assignment_index"])
        start_voxel_index = int(assignment["start_voxel_index"])
        end_voxel_index = int(assignment["end_voxel_index"])
        raw_gradient_steps = int(assignment.get("gradient_steps", 0))
        gradient_steps = get_effective_gradient_steps(property_program, assignment)
        gradient_direction = str(assignment.get("gradient_direction", "printing"))

        assignment_total_e = (
            sum(mapped_step_lengths[mapped_offset : mapped_offset + gradient_steps])
            if mapped_step_lengths is not None
            else get_voxel_filament_sum(
                sample_info,
                start_voxel_index,
                end_voxel_index,
            )
        )

        step_table: list[dict] = []
        layer_region_event = assignment.get("layer_region_event")
        if isinstance(layer_region_event, dict):
            event_length = float(
                assignment.get(
                    "execution_filament_e_mm",
                    layer_region_event.get(
                        "extrusion_e_mm",
                        layer_region_event.get("deposition_e_mm", assignment_total_e),
                    ),
                )
            )
            assignment_steps = [event_length]
            mapped_offset += gradient_steps
            step_table.append(
                {
                    "step_index": 1,
                    "resolution_mode": "layer_region_occurrence",
                    "event_sequence": layer_region_event.get(
                        "execution_step_index",
                        layer_region_event.get("sequence_index", layer_region_event.get("sequence")),
                    ),
                    "region_name": layer_region_event.get("region_name"),
                    "region_occurrence_index": layer_region_event.get(
                        "occurrence_index",
                        layer_region_event.get("region_occurrence_index"),
                    ),
                    "start_voxel_index": None,
                    "end_voxel_index": None,
                    "layer_start": layer_region_event.get("layer_index"),
                    "layer_end": layer_region_event.get("layer_index"),
                    "z_mm": layer_region_event.get("layer_z", layer_region_event.get("z_mm")),
                    "source_line_start": layer_region_event.get("source_line_start"),
                    "source_line_end": layer_region_event.get("source_line_end"),
                    "xy_bounds": layer_region_event.get("bounds_xy", layer_region_event.get("xy_bounds")),
                    "segment_count": layer_region_event.get("segment_count"),
                    "feature_types": layer_region_event.get("feature_types", []),
                    "step_filament_e_mm": event_length,
                }
            )
        elif mapped_step_lengths is not None:
            assignment_steps = mapped_step_lengths[mapped_offset : mapped_offset + gradient_steps]
            mapped_offset += gradient_steps
            if gradient_direction == "layer":
                start_layer = int(assignment["start_layer"])
                end_layer = int(assignment["end_layer"])
                layer_segments = build_layer_segments(start_layer, end_layer, gradient_steps)
                for step_index, (step_value, (seg_start, seg_end)) in enumerate(
                    zip(assignment_steps, layer_segments),
                    start=1,
                ):
                    voxel_start, voxel_end = get_voxel_range_for_layer_segment(
                        sample_info,
                        start_voxel_index,
                        end_voxel_index,
                        seg_start,
                        seg_end,
                    )
                    step_table.append(
                        {
                            "step_index": step_index,
                            "start_voxel_index": voxel_start,
                            "end_voxel_index": voxel_end,
                            "layer_start": seg_start,
                            "layer_end": seg_end,
                            "step_filament_e_mm": step_value,
                        }
                    )
            else:
                voxel_segments = build_index_segments(start_voxel_index, end_voxel_index, gradient_steps)
                for step_index, (step_filament, (seg_start_voxel, seg_end_voxel)) in enumerate(
                    zip(assignment_steps, voxel_segments),
                    start=1,
                ):
                    layer_start, layer_end = get_layer_range_for_voxel_segment(
                        sample_info,
                        seg_start_voxel,
                        seg_end_voxel,
                    )
                    step_table.append(
                        {
                            "step_index": step_index,
                            "start_voxel_index": seg_start_voxel,
                            "end_voxel_index": seg_end_voxel,
                            "layer_start": layer_start,
                            "layer_end": layer_end,
                            "step_filament_e_mm": step_filament,
                        }
                    )
        elif gradient_direction == "layer":
            start_layer = int(assignment["start_layer"])
            end_layer = int(assignment["end_layer"])
            layer_segments = build_layer_segments(start_layer, end_layer, gradient_steps)
            assignment_steps = []
            for step_index, (seg_start, seg_end) in enumerate(layer_segments, start=1):
                step_value = get_voxel_filament_sum_by_layer(sample_info, seg_start, seg_end)
                voxel_start, voxel_end = get_voxel_range_for_layer_segment(
                    sample_info,
                    start_voxel_index,
                    end_voxel_index,
                    seg_start,
                    seg_end,
                )
                assignment_steps.append(step_value)
                step_table.append(
                    {
                        "step_index": step_index,
                        "start_voxel_index": voxel_start,
                        "end_voxel_index": voxel_end,
                        "layer_start": seg_start,
                        "layer_end": seg_end,
                        "step_filament_e_mm": step_value,
                    }
                )
        else:
            step_value = assignment_total_e / gradient_steps if gradient_steps > 0 else 0.0
            assignment_steps = [step_value] * gradient_steps
            voxel_segments = build_index_segments(start_voxel_index, end_voxel_index, gradient_steps)
            for step_index, (step_filament, (seg_start_voxel, seg_end_voxel)) in enumerate(zip(assignment_steps, voxel_segments), start=1):
                layer_start, layer_end = get_layer_range_for_voxel_segment(
                    sample_info,
                    seg_start_voxel,
                    seg_end_voxel,
                )
                step_table.append(
                    {
                        "step_index": step_index,
                        "start_voxel_index": seg_start_voxel,
                        "end_voxel_index": seg_end_voxel,
                        "layer_start": layer_start,
                        "layer_end": layer_end,
                        "step_filament_e_mm": step_filament,
                    }
                )

        length_matrix.extend(assignment_steps)

        assignment_summaries.append(
            {
                "assignment_index": assignment_index,
                "start_voxel_index": start_voxel_index,
                "end_voxel_index": end_voxel_index,
                "raw_gradient_steps": raw_gradient_steps,
                "gradient_steps": gradient_steps,
                "gradient_direction": gradient_direction,
                "resolution_mode": (
                    "layer_region_occurrence"
                    if isinstance(layer_region_event, dict)
                    else "component"
                ),
                "assignment_total_filament_e_mm": assignment_total_e,
                "step_values": assignment_steps,
                "step_table": step_table,
                "step_sum_filament_e_mm": sum(assignment_steps),
            }
        )

    return length_matrix, assignment_summaries


def build_output_payload(sample_info: dict, property_program: dict) -> dict:
    length_matrix, assignment_summaries = build_length_matrix(sample_info, property_program)
    base_step_count = get_total_step_count(property_program)
    total_filament_e_mm = float(sample_info.get("total_filament_e_mm", 0.0))
    length_matrix_sum = sum(length_matrix)
    remainder = total_filament_e_mm - length_matrix_sum

    return {
        "sample_info_path": str(SAMPLE_INFO_PATH),
        "property_program_path": str(PROPERTY_PROGRAM_PATH),
        "property_type": get_property_type(property_program),
        "assignment_count": len(get_assignments_in_spatial_order(property_program)),
        "base_step_count": base_step_count,
        "total_step_count": len(length_matrix),
        "total_filament_e_mm": total_filament_e_mm,
        "length_matrix_sum_e_mm": length_matrix_sum,
        "length_matrix_remainder_e_mm": remainder,
        "length_matrix": length_matrix,
        "length_matrix_summary": {
            "format": "1 x total_step_count",
            "base_step_count": base_step_count,
            "total_step_count": len(length_matrix),
            "total_filament_e_mm": total_filament_e_mm,
            "length_matrix_sum_e_mm": length_matrix_sum,
            "length_matrix_remainder_e_mm": remainder,
            "remainder_note": (
                "Remainder is reported only. It is not appended as an extra step because "
                "the result matrix must stay aligned with Property_sample gradient steps."
            ),
        },
        "assignments": assignment_summaries,
    }


def save_output_json(output_path: Path, payload: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_length_matrix_npy(output_path: Path, length_matrix: list[float]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(output_path, np.asarray(length_matrix, dtype=np.float64))


def load_length_matrix_npy(input_path: Path) -> np.ndarray:
    if not input_path.exists():
        raise FileNotFoundError(f"NPY file not found: {input_path}")
    return np.load(input_path, allow_pickle=False)


def print_length_matrix(payload: dict) -> None:
    length_matrix = payload.get("length_matrix", [])
    assignments = payload.get("assignments", [])
    print(f"length_matrix count: {len(length_matrix)}")
    if len(length_matrix) <= 30:
        print(f"length_matrix: {length_matrix}")
    else:
        print(f"length_matrix first 6: {length_matrix[:6]}")
        print(f"length_matrix last 6: {length_matrix[-6:]}")
        print(f"length_matrix sum: {sum(float(value) for value in length_matrix):.6f}")

    if assignments:
        print("assignment step table samples:")
        sample_assignments = assignments if len(assignments) <= 6 else assignments[:3] + assignments[-3:]
        for assignment in sample_assignments:
            print(f"assignment {assignment['assignment_index']}:")
            for step in assignment.get("step_table", []):
                print(step)


def main() -> None:
    sample_info = load_json(SAMPLE_INFO_PATH)
    property_program = load_json(PROPERTY_PROGRAM_PATH)
    payload = build_output_payload(sample_info, property_program)
    save_output_json(OUTPUT_PATH, payload)
    save_length_matrix_npy(OUTPUT_NPY_PATH, payload["length_matrix"])
    print_length_matrix(payload)

    print(f"Assignment count: {payload['assignment_count']}")
    print(f"Total step count: {payload['total_step_count']}")
    print(f"Length matrix saved to: {OUTPUT_PATH}")
    print(f"Length matrix saved to: {OUTPUT_NPY_PATH}")


if __name__ == "__main__":
    main()
