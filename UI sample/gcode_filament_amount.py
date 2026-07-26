from __future__ import annotations

import argparse
import json
import math
import re
from itertools import islice, product
from dataclasses import dataclass, asdict
from pathlib import Path

try:
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Circle
    from matplotlib.patches import Rectangle
except ModuleNotFoundError:
    plt = None
    BoundaryNorm = None
    ListedColormap = None
    Circle = None
    Rectangle = None


FILAMENT_USED_MM_RE = re.compile(r"filament used\s*\[mm\]\s*=\s*([-+0-9.eE]+)", re.IGNORECASE)
FILAMENT_USED_M_RE = re.compile(r"filament used:\s*([-+0-9.eE]+)\s*m", re.IGNORECASE)
TOTAL_FILAMENT_USED_G_RE = re.compile(r"total filament used \[g\]\s*=\s*([-+0-9.eE]+)", re.IGNORECASE)
E_TOKEN_RE = re.compile(r"(?:^|\s)E([-+0-9.eE]+)")
MOVE_PATTERN = re.compile(r"^G(?:0|1)\b")
COORD_PATTERN = re.compile(r"([XYZE])([-+]?\d*\.?\d+)")
LAYER_INDEX_PATTERN = re.compile(r"^;LAYER:(-?\d+)\s*$")
LAYER_CHANGE_PATTERN = re.compile(r"^;LAYER_CHANGE\b")
Z_LAYER_PATTERN = re.compile(r"^;Z:([-+]?\d*\.?\d+)\s*$")


@dataclass
class FilamentStats:
    gcode_path: str
    extrusion_mode: str
    total_extrusion_mm: float
    source: str
    filament_diameter_mm: float
    cross_section_area_mm2: float
    filament_volume_mm3: float
    filament_length_m: float
    filament_mass_g: float | None
    gcode_reported_filament_used_g: float | None = None
    voxel_threshold_e: float | None = None
    voxel_count: int | None = None
    voxel_total_e_mm: float | None = None
    voxel_summary: list[dict] | None = None
    assignment_summary: list[dict] | None = None
    test_assignment_summary: list[dict] | None = None
    split_assignment_summary: list[dict] | None = None
    step_material_summary: list[dict] | None = None
    step_material_analysis: list[dict] | None = None
    step_material_candidates: list[dict] | None = None
    step_material_candidate_total_count: int | None = None
    step_material_candidate_total_summary: list[dict] | None = None
    stepwise_exhaustive_candidate_combination_count: int | None = None
    step_material_candidate_analysis: list[dict] | None = None
    step_material_candidate_matrices: list[list[list[list[str]]]] | None = None
    step_material_candidate_eta_summary: list[dict] | None = None


def parse_reported_filament_used_mm(text: str) -> float | None:
    match = FILAMENT_USED_MM_RE.search(text)
    if match:
        return float(match.group(1))

    match = FILAMENT_USED_M_RE.search(text)
    if match:
        return float(match.group(1)) * 1000.0

    return None


def parse_reported_filament_used_g(text: str) -> float | None:
    match = TOTAL_FILAMENT_USED_G_RE.search(text)
    if match:
        return float(match.group(1))
    return None


REPR_ROW_WEIGHTS = [2, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 3, 3, 2]
ETA_DIAMETER_BLOCKS = 4.0


def get_representative_row_weights(row_count: int = 14) -> list[int]:
    row_count = max(1, int(row_count))
    if row_count == len(REPR_ROW_WEIGHTS):
        return list(REPR_ROW_WEIGHTS)
    if row_count == 1:
        return [sum(REPR_ROW_WEIGHTS)]

    base = REPR_ROW_WEIGHTS
    scaled_weights: list[int] = []
    for row_index in range(row_count):
        position = row_index * (len(base) - 1) / (row_count - 1)
        lower_index = int(math.floor(position))
        upper_index = min(lower_index + 1, len(base) - 1)
        blend = position - lower_index
        value = base[lower_index] * (1.0 - blend) + base[upper_index] * blend
        scaled_weights.append(max(1, int(round(value))))
    return scaled_weights


def compute_candidate_eta_proxy(
    row_weights: list[int],
    selected_rows: list[int],
) -> dict:
    selected_set = {int(row) for row in selected_rows}
    interface_count = 0.0
    interface_width_units = 0.0
    interface_block_units = 0.0
    for row_index in range(1, len(row_weights)):
        left_selected = row_index in selected_set
        right_selected = (row_index + 1) in selected_set
        if left_selected != right_selected:
            boundary_width = min(float(row_weights[row_index - 1]), float(row_weights[row_index]))
            interface_count += 1.0
            interface_width_units += boundary_width
            interface_block_units += math.ceil(boundary_width / ETA_DIAMETER_BLOCKS)
    eta_value = interface_block_units
    return {
        "interface_count": round(interface_count, 6),
        "interface_width_units": round(interface_width_units, 6),
        "interface_block_units": round(interface_block_units, 6),
        "eta": round(eta_value, 6),
        "eta_proxy": round(eta_value, 6),
    }


def _extract_e_value(line: str) -> float | None:
    match = E_TOKEN_RE.search(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_gcode_extrusion_segments(gcode_path: Path, include_preprint: bool = False) -> tuple[list[dict], float]:
    segments: list[dict] = []
    preprint_e = 0.0
    fallback_segments: list[dict] = []

    last_x = 0.0
    last_y = 0.0
    last_z = 0.0
    last_e = 0.0
    current_layer = -1
    extrusion_absolute = True
    in_print_block = include_preprint
    saw_layer_marker = include_preprint
    coord_findall = COORD_PATTERN.findall
    move_match = MOVE_PATTERN.match
    layer_index_search = LAYER_INDEX_PATTERN.search

    with gcode_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue

            layer_index_match = layer_index_search(line)
            if layer_index_match:
                saw_layer_marker = True
                current_layer = int(layer_index_match.group(1))
                if include_preprint or int(layer_index_match.group(1)) >= 0:
                    in_print_block = True
                continue
            if LAYER_CHANGE_PATTERN.match(line):
                saw_layer_marker = True
                current_layer = current_layer + 1 if current_layer >= 0 else 0
                in_print_block = True
                continue
            if Z_LAYER_PATTERN.match(line):
                saw_layer_marker = True
                current_layer = current_layer + 1 if current_layer >= 0 else 0
                in_print_block = True
                continue

            if line.startswith("M82"):
                extrusion_absolute = True
                continue
            if line.startswith("M83"):
                extrusion_absolute = False
                continue
            if line.startswith("G92"):
                coords = {axis: float(val) for axis, val in coord_findall(line)}
                if "E" in coords:
                    last_e = coords["E"]
                if "X" in coords:
                    last_x = coords["X"]
                if "Y" in coords:
                    last_y = coords["Y"]
                if "Z" in coords:
                    last_z = coords["Z"]
                continue

            code = line.partition(";")[0].strip()
            if not code or not move_match(code):
                continue

            command = code.partition(" ")[0]
            coords = {axis: float(val) for axis, val in coord_findall(code)}
            new_x = coords.get("X", last_x)
            new_y = coords.get("Y", last_y)
            new_z = coords.get("Z", last_z)

            delta_e = 0.0
            if "E" in coords:
                e_value = coords["E"]
                delta_e = e_value - last_e if extrusion_absolute else e_value
                if extrusion_absolute:
                    last_e = e_value
                else:
                    last_e += e_value

            moved = abs(new_x - last_x) > 1e-9 or abs(new_y - last_y) > 1e-9 or abs(new_z - last_z) > 1e-9
            xy_moved = abs(new_x - last_x) > 1e-9 or abs(new_y - last_y) > 1e-9

            if delta_e > 0 and moved and xy_moved:
                base_segment = {
                    "line_no": line_no,
                    "command": command,
                    "start": [float(last_x), float(last_y), float(last_z)],
                    "end": [float(new_x), float(new_y), float(new_z)],
                    "delta_e": float(delta_e),
                    "layer_num": int(current_layer),
                }
                fallback_segment = dict(base_segment)
                fallback_segment["segment_index"] = len(fallback_segments)
                fallback_segments.append(fallback_segment)
                if in_print_block:
                    segment = dict(base_segment)
                    segment["segment_index"] = len(segments)
                    segments.append(segment)
                else:
                    preprint_e += float(delta_e)

            last_x = new_x
            last_y = new_y
            last_z = new_z

    if not saw_layer_marker and not include_preprint:
        return fallback_segments, 0.0
    return segments, preprint_e


def summarize_voxel_bundles(segments: list[dict], threshold_e: float) -> list[dict]:
    voxels: list[dict] = []
    pending: list[dict] = []
    pending_layers: list[int] = []
    pending_sum = 0.0
    cumulative_before = 0.0

    for segment in segments:
        pending.append(segment)
        pending_sum += float(segment["delta_e"])
        layer_num = int(segment.get("layer_num", -1))
        pending_layers.append(layer_num)

        if pending_sum + 1e-12 >= threshold_e:
            voxel_id = len(voxels) + 1
            voxel_e = float(sum(seg["delta_e"] for seg in pending))
            cumulative_after = cumulative_before + voxel_e
            layer_ids = [layer for layer in pending_layers if layer >= 0]
            layer_start = min(layer_ids) if layer_ids else -1
            layer_end = max(layer_ids) if layer_ids else -1
            voxels.append(
                {
                    "voxel_id": voxel_id,
                    "threshold_e": float(threshold_e),
                    "voxel_e": voxel_e,
                    "cumulative_e_before": float(cumulative_before),
                    "cumulative_e_after": float(cumulative_after),
                    "segment_count": len(pending),
                    "layer_num": int(pending[0].get("layer_num", -1)),
                    "layer_start": int(layer_start),
                    "layer_end": int(layer_end),
                    "layer_count": int(len(set(layer_ids))) if layer_ids else 0,
                    "line_start": int(pending[0]["line_no"]),
                    "line_end": int(pending[-1]["line_no"]),
                    "x_start": float(pending[0]["start"][0]),
                    "y_start": float(pending[0]["start"][1]),
                    "z_start": float(pending[0]["start"][2]),
                    "x_end": float(pending[-1]["end"][0]),
                    "y_end": float(pending[-1]["end"][1]),
                    "z_end": float(pending[-1]["end"][2]),
                }
            )
            cumulative_before = cumulative_after
            pending = []
            pending_layers = []
            pending_sum = 0.0

    if pending:
        voxel_id = len(voxels) + 1
        voxel_e = float(sum(seg["delta_e"] for seg in pending))
        cumulative_after = cumulative_before + voxel_e
        layer_ids = [layer for layer in pending_layers if layer >= 0]
        layer_start = min(layer_ids) if layer_ids else -1
        layer_end = max(layer_ids) if layer_ids else -1
        voxels.append(
            {
                "voxel_id": voxel_id,
                "threshold_e": float(threshold_e),
                "voxel_e": voxel_e,
                "cumulative_e_before": float(cumulative_before),
                "cumulative_e_after": float(cumulative_after),
                "segment_count": len(pending),
                "layer_num": int(pending[0].get("layer_num", -1)),
                "layer_start": int(layer_start),
                "layer_end": int(layer_end),
                "layer_count": int(len(set(layer_ids))) if layer_ids else 0,
                "line_start": int(pending[0]["line_no"]),
                "line_end": int(pending[-1]["line_no"]),
                "x_start": float(pending[0]["start"][0]),
                "y_start": float(pending[0]["start"][1]),
                "z_start": float(pending[0]["start"][2]),
                "x_end": float(pending[-1]["end"][0]),
                "y_end": float(pending[-1]["end"][1]),
                "z_end": float(pending[-1]["end"][2]),
            }
        )

    return voxels


def load_assignment_records(property_json_path: Path) -> list[dict]:
    payload = json.loads(property_json_path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        assignments = payload.get("assignments", [])
        if isinstance(assignments, list):
            return [dict(item) for item in assignments if isinstance(item, dict)]
    raise ValueError(
        f"Unsupported assignment JSON structure in {property_json_path}. "
        "Expected either a list of assignment objects or a dict with an 'assignments' list."
    )


def resolve_property_json_path(gcode_path: Path, property_json_arg: Path | None) -> Path | None:
    if property_json_arg is not None:
        return property_json_arg.resolve()

    stem_candidate = gcode_path.with_name(f"{gcode_path.stem}_property_program.json")
    if stem_candidate.exists():
        return stem_candidate.resolve()

    default_candidate = gcode_path.with_name("vase_property_program.json")
    if default_candidate.exists():
        return default_candidate.resolve()

    return None


def build_assignment_step_segments(
    assignment: dict,
    voxel_summary: list[dict],
) -> list[dict]:
    step_count = max(1, int(assignment.get("gradient_steps", 1)))
    direction = str(assignment.get("gradient_direction", "layer")).strip().lower()
    start_voxel = int(assignment.get("start_voxel", 1))
    end_voxel = int(assignment.get("end_voxel", start_voxel))
    if end_voxel < start_voxel:
        start_voxel, end_voxel = end_voxel, start_voxel

    voxel_map = {int(voxel["voxel_id"]): voxel for voxel in voxel_summary}
    selected_voxels = [
        voxel_map[voxel_id]
        for voxel_id in range(start_voxel, end_voxel + 1)
        if voxel_id in voxel_map
    ]
    if not selected_voxels:
        return []

    total_e = float(sum(float(voxel["voxel_e"]) for voxel in selected_voxels)) or 1.0

    if step_count <= 1:
        return [
            {
                "step_index": 1,
                "start_voxel": start_voxel,
                "end_voxel": end_voxel,
                "voxel_count": len(selected_voxels),
                "total_filament_length_mm": round(total_e, 6),
                "start_fraction": 0.0,
                "end_fraction": 1.0,
            }
        ]

    segments: list[dict] = []

    if direction == "layer":
        voxel_layer_table = assignment.get("voxel_layer_table")
        if not isinstance(voxel_layer_table, list) or not voxel_layer_table:
            return []

        layer_to_voxels: dict[int, list[dict]] = {}
        layer_lookup = {
            int(item.get("voxel_id", -1)): int(item.get("layer_num", -1))
            for item in voxel_layer_table
            if isinstance(item, dict)
        }
        for voxel in selected_voxels:
            layer_num = int(layer_lookup.get(int(voxel["voxel_id"]), -1))
            if layer_num < 0:
                continue
            layer_to_voxels.setdefault(layer_num, []).append(voxel)

        layer_ids = sorted(layer_to_voxels.keys())
        if not layer_ids:
            return []

        min_layer = layer_ids[0]
        max_layer = layer_ids[-1]
        layer_span = max(1, max_layer - min_layer + 1)
        voxel_prefix_by_id: dict[int, float] = {}
        running_selected_e = 0.0
        for voxel in selected_voxels:
            voxel_id = int(voxel["voxel_id"])
            voxel_prefix_by_id[voxel_id] = running_selected_e
            running_selected_e += float(voxel["voxel_e"])

        for step_index in range(step_count):
            step_start = min_layer + int((layer_span * step_index) / step_count)
            step_end_exclusive = min_layer + int((layer_span * (step_index + 1)) / step_count)
            if step_index == step_count - 1:
                step_end_exclusive = max_layer + 1
            step_layers = [layer for layer in layer_ids if step_start <= layer < step_end_exclusive]
            step_voxels = [voxel for layer in step_layers for voxel in layer_to_voxels[layer]]
            step_e = float(sum(float(voxel["voxel_e"]) for voxel in step_voxels))
            step_start_fraction = 0.0
            step_end_fraction = 0.0
            if step_voxels:
                step_start_voxel_id = int(step_voxels[0]["voxel_id"])
                step_end_voxel_id = int(step_voxels[-1]["voxel_id"])
                step_start_fraction = float(voxel_prefix_by_id.get(step_start_voxel_id, 0.0)) / total_e
                step_end_fraction = (
                    float(voxel_prefix_by_id.get(step_end_voxel_id, 0.0) + float(step_voxels[-1]["voxel_e"]))
                    / total_e
                )
            segments.append(
                {
                    "step_index": step_index + 1,
                    "layer_start": step_start,
                    "layer_end": max(step_start, step_end_exclusive - 1),
                    "voxel_count": len(step_voxels),
                    "total_filament_length_mm": round(step_e, 6),
                    "start_fraction": round(max(0.0, min(1.0, step_start_fraction)), 6),
                    "end_fraction": round(max(0.0, min(1.0, step_end_fraction)), 6),
                }
            )
        return segments

    voxel_running_e = 0.0
    target_step_e = total_e / step_count
    current_step = 1
    current_voxels: list[dict] = []
    current_start_e = 0.0

    for idx, voxel in enumerate(selected_voxels):
        voxel_e = float(voxel["voxel_e"])
        current_voxels.append(voxel)
        voxel_running_e += voxel_e
        is_last_voxel = idx == len(selected_voxels) - 1
        should_close = (
            current_step < step_count
            and voxel_running_e >= current_step * target_step_e
            and not is_last_voxel
        )
        if should_close:
            step_e = float(sum(float(v["voxel_e"]) for v in current_voxels))
            segments.append(
                {
                    "step_index": current_step,
                    "start_voxel": int(current_voxels[0]["voxel_id"]),
                    "end_voxel": int(current_voxels[-1]["voxel_id"]),
                    "voxel_count": len(current_voxels),
                    "total_filament_length_mm": round(step_e, 6),
                    "start_fraction": round(current_start_e / total_e, 6),
                    "end_fraction": round(voxel_running_e / total_e, 6),
                }
            )
            current_step += 1
            current_voxels = []
            current_start_e = voxel_running_e

    if current_voxels or not segments:
        step_e = float(sum(float(v["voxel_e"]) for v in current_voxels))
        segments.append(
            {
                "step_index": current_step,
                "start_voxel": int(current_voxels[0]["voxel_id"]) if current_voxels else start_voxel,
                "end_voxel": int(current_voxels[-1]["voxel_id"]) if current_voxels else end_voxel,
                "voxel_count": len(current_voxels),
                "total_filament_length_mm": round(step_e, 6),
                "start_fraction": round(current_start_e / total_e, 6),
                "end_fraction": 1.0,
            }
        )

    while len(segments) < step_count:
        segments.append(
            {
                "step_index": len(segments) + 1,
                "start_voxel": end_voxel,
                "end_voxel": end_voxel,
                "voxel_count": 0,
                "total_filament_length_mm": 0.0,
                "start_fraction": 1.0,
                "end_fraction": 1.0,
            }
        )

    return segments[:step_count]


def _get_assignment_ratio_pair(assignment: dict) -> tuple[float, float]:
    ratio_1 = float(assignment.get("mat_ratio_1", assignment.get("color_ratio_1", 100.0)))
    ratio_2 = float(assignment.get("mat_ratio_2", assignment.get("color_ratio_2", 0.0)))
    ratio_sum = ratio_1 + ratio_2
    if ratio_sum <= 1e-12:
        return 0.5, 0.5
    return ratio_1 / ratio_sum, ratio_2 / ratio_sum


def _build_threshold_row_pattern(
    row_weights: list[int],
    target_secondary_ratio: float,
    material_1_name: str,
    material_2_name: str,
) -> list[str]:
    total_weight = float(sum(row_weights)) or float(len(row_weights)) or 1.0
    secondary_threshold = max(0.0, min(total_weight, (1.0 - target_secondary_ratio) * total_weight))
    cumulative_weight = 0.0
    row_pattern: list[str] = []
    for row_weight in row_weights:
        row_start = cumulative_weight
        row_end = cumulative_weight + float(row_weight)
        row_midpoint = (row_start + row_end) * 0.5
        row_pattern.append(material_2_name if row_midpoint >= secondary_threshold else material_1_name)
        cumulative_weight = row_end
    return row_pattern


def _solve_stepwise_gamma(step_weights: list[float], target_secondary_ratio: float) -> float:
    if not step_weights:
        return 1.0

    target_secondary_ratio = max(0.0, min(1.0, float(target_secondary_ratio)))
    if len(step_weights) <= 1:
        return 1.0
    if target_secondary_ratio <= 1e-12:
        return 64.0
    if target_secondary_ratio >= 1.0 - 1e-12:
        return 1e-6

    positions = [index / float(len(step_weights) - 1) for index in range(len(step_weights))]
    total_weight = float(sum(step_weights)) or 1.0

    def weighted_average(gamma: float) -> float:
        return sum(weight * (position ** gamma) for weight, position in zip(step_weights, positions, strict=False)) / total_weight

    low = 1e-6
    high = 64.0
    for _ in range(80):
        mid = (low + high) * 0.5
        average = weighted_average(mid)
        if average > target_secondary_ratio:
            low = mid
        else:
            high = mid
    return (low + high) * 0.5


def build_stepwise_transition_profile(
    assignment: dict,
    step_count: int,
    step_weights: list[float] | None = None,
) -> list[dict]:
    step_count = max(1, int(step_count))
    target_ratio_1, target_ratio_2 = _get_assignment_ratio_pair(assignment)

    if step_weights is None or len(step_weights) != step_count:
        step_weights = [1.0] * step_count
    else:
        step_weights = [max(0.0, float(value)) for value in step_weights]

    gamma = _solve_stepwise_gamma(step_weights, target_ratio_2)
    if step_count <= 1:
        return [
            {
                "step_index": 1,
                "target_material_1_ratio": round(target_ratio_1 * 100.0, 6),
                "target_material_2_ratio": round(target_ratio_2 * 100.0, 6),
                "step_weight": round(float(step_weights[0]), 6),
                "step_progress": 0.0,
                "gamma": round(gamma, 6),
            }
        ]

    profile: list[dict] = []
    for step_index in range(step_count):
        progress = step_index / float(step_count - 1)
        secondary_ratio = progress ** gamma
        profile.append(
            {
                "step_index": step_index + 1,
                "target_material_1_ratio": round((1.0 - secondary_ratio) * 100.0, 6),
                "target_material_2_ratio": round(secondary_ratio * 100.0, 6),
                "step_weight": round(float(step_weights[step_index]), 6),
                "step_progress": round(progress, 6),
                "gamma": round(gamma, 6),
            }
        )
    return profile


def build_assignment_step_material_matrix(
    assignment: dict,
    row_count: int = 14,
    candidate_rank: int = 1,
) -> list[list[str]]:
    row_count = max(1, int(row_count))
    material_1_name = str(assignment.get("material_1") or "").strip()
    material_2_name = str(assignment.get("material_2") or "").strip()
    if not material_1_name and material_2_name:
        material_1_name = material_2_name
    if int(assignment.get("material_count", 1)) < 2 or not material_2_name:
        material_2_name = material_1_name

    step_count = max(1, int(assignment.get("gradient_steps", 1)))
    matrix = [[material_1_name for _ in range(step_count)] for _ in range(row_count)]
    if step_count <= 0:
        return matrix

    stepwise = build_assignment_stepwise_material_selection(
        assignment,
        row_count=row_count,
        candidate_rank=candidate_rank,
    )
    for step_index, step_choice in enumerate(stepwise["step_choices"]):
        selected_rows = {int(row) for row in step_choice.get("selected_rows", [])}
        for row_index in range(1, row_count + 1):
            matrix[row_index - 1][step_index] = material_2_name if row_index in selected_rows else material_1_name

    return matrix


def build_assignment_step_material_analysis(
    assignment: dict,
    row_count: int = 14,
) -> dict:
    row_count = max(1, int(row_count))
    material_1_name = str(assignment.get("material_1") or "").strip()
    material_2_name = str(assignment.get("material_2") or "").strip()
    if not material_1_name and material_2_name:
        material_1_name = material_2_name
    if int(assignment.get("material_count", 1)) < 2 or not material_2_name:
        material_2_name = material_1_name

    step_count = max(1, int(assignment.get("gradient_steps", 1)))
    target_ratio_1, target_ratio_2 = _get_assignment_ratio_pair(assignment)

    row_weights = get_representative_row_weights(row_count)
    total_weight = float(sum(row_weights)) or float(row_count)
    secondary_threshold = target_ratio_1 * total_weight

    cumulative_weight = 0.0
    actual_1_units = 0.0
    actual_2_units = 0.0
    row_pattern: list[str] = []
    for row_weight in row_weights:
        row_start = cumulative_weight
        row_end = cumulative_weight + float(row_weight)
        row_midpoint = (row_start + row_end) * 0.5
        row_material = material_2_name if row_midpoint >= secondary_threshold else material_1_name
        row_pattern.append(row_material)
        if row_material == material_2_name:
            actual_2_units += float(row_weight)
        else:
            actual_1_units += float(row_weight)
        cumulative_weight = row_end

    actual_ratio_1 = actual_1_units / total_weight if total_weight > 1e-12 else 0.0
    actual_ratio_2 = actual_2_units / total_weight if total_weight > 1e-12 else 0.0

    return {
        "assignment_index": int(assignment.get("assignment_index", 0)),
        "gradient_steps": step_count,
        "row_count": row_count,
        "row_weights": row_weights,
        "row_weight_total": int(total_weight),
        "material_1": material_1_name,
        "material_2": material_2_name,
        "target_material_1_ratio": round(target_ratio_1 * 100.0, 6),
        "target_material_2_ratio": round(target_ratio_2 * 100.0, 6),
        "actual_material_1_units": int(round(actual_1_units)),
        "actual_material_2_units": int(round(actual_2_units)),
        "actual_material_1_ratio": round(actual_ratio_1 * 100.0, 6),
        "actual_material_2_ratio": round(actual_ratio_2 * 100.0, 6),
        "ratio_error_material_1": round((actual_ratio_1 - target_ratio_1) * 100.0, 6),
        "ratio_error_material_2": round((actual_ratio_2 - target_ratio_2) * 100.0, 6),
        "row_pattern": row_pattern,
        "stepwise_transition_profile": build_stepwise_transition_profile(assignment, step_count),
        "stepwise_selection": build_assignment_stepwise_material_selection(assignment, row_count=row_count)["step_choices"],
    }


def build_assignment_step_material_candidates(
    assignment: dict,
    row_count: int = 14,
    target_ratio_2: float | None = None,
) -> dict:
    row_count = max(1, int(row_count))
    material_1_name = str(assignment.get("material_1") or "").strip()
    material_2_name = str(assignment.get("material_2") or "").strip()
    if not material_1_name and material_2_name:
        material_1_name = material_2_name
    if int(assignment.get("material_count", 1)) < 2 or not material_2_name:
        material_2_name = material_1_name

    ratio_1, ratio_2 = _get_assignment_ratio_pair(assignment)
    if target_ratio_2 is None:
        target_ratio_2 = ratio_2
    target_ratio_2 = max(0.0, min(1.0, float(target_ratio_2)))
    target_ratio_1 = 1.0 - target_ratio_2

    row_weights = get_representative_row_weights(row_count)
    total_weight = float(sum(row_weights)) or float(row_count)
    exact_secondary_units = target_ratio_2 * total_weight
    target_unit_options = sorted(
        {
            max(0, min(int(total_weight), int(math.floor(exact_secondary_units)))),
            max(0, min(int(total_weight), int(math.ceil(exact_secondary_units)))),
        }
    )

    suffix_weight = [0] * (row_count + 1)
    for idx in range(row_count - 1, -1, -1):
        suffix_weight[idx] = suffix_weight[idx + 1] + row_weights[idx]

    def enumerate_combinations(target_units: int) -> list[list[int]]:
        results: list[list[int]] = []
        chosen: list[int] = []

        def dfs(index: int, remaining: int) -> None:
            if remaining == 0:
                results.append([item + 1 for item in chosen])
                return
            if index >= row_count or remaining < 0 or remaining > suffix_weight[index]:
                return
            chosen.append(index)
            dfs(index + 1, remaining - row_weights[index])
            chosen.pop()
            dfs(index + 1, remaining)

        dfs(0, target_units)
        return results

    groups: list[dict] = []
    for target_units in target_unit_options:
        combos = enumerate_combinations(target_units)
        if not combos:
            continue
        groups.append(
            {
                "target_units": target_units,
                "candidate_count": len(combos),
                "combinations": combos,
            }
        )

    if not groups:
        nearest_target = int(round(exact_secondary_units))
        nearest_target = max(0, min(int(total_weight), nearest_target))
        combos = enumerate_combinations(nearest_target)
        groups.append(
            {
                "target_units": nearest_target,
                "candidate_count": len(combos),
                "combinations": combos,
            }
        )

    return {
        "assignment_index": int(assignment.get("assignment_index", 0)),
        "row_count": row_count,
        "row_weights": row_weights,
        "row_weight_total": int(total_weight),
        "material_1": material_1_name,
        "material_2": material_2_name,
        "target_material_1_ratio": round(target_ratio_1 * 100.0, 6),
        "target_material_2_ratio": round(target_ratio_2 * 100.0, 6),
        "target_secondary_units_exact": round(exact_secondary_units, 6),
        "target_unit_options": target_unit_options,
        "groups": groups,
    }


def build_assignment_step_material_candidate_analysis(
    assignment: dict,
    row_count: int = 14,
    target_ratio_2: float | None = None,
    step_index: int | None = None,
    step_progress: float | None = None,
    step_weight: float | None = None,
) -> list[dict]:
    spec = build_assignment_step_material_candidates(
        assignment,
        row_count=row_count,
        target_ratio_2=target_ratio_2,
    )
    row_weights = spec["row_weights"]
    total_weight = float(spec["row_weight_total"]) or 1.0
    material_1_name = spec["material_1"]
    material_2_name = spec["material_2"]
    target_ratio_1 = float(spec["target_material_1_ratio"]) / 100.0
    target_ratio_2 = float(spec["target_material_2_ratio"]) / 100.0
    assignment_eta = float(assignment.get("eta", 0.0))

    rows: list[dict] = []
    candidate_index = 0
    for group in spec["groups"]:
        target_units = int(group.get("target_units", 0))
        for combo in group.get("combinations", []):
            candidate_index += 1
            selected_rows = [int(v) for v in combo]
            actual_units = int(sum(row_weights[row - 1] for row in selected_rows if 1 <= row <= row_count))
            actual_ratio_2 = actual_units / total_weight if total_weight > 1e-12 else 0.0
            actual_ratio_1 = 1.0 - actual_ratio_2
            eta_info = compute_candidate_eta_proxy(row_weights, selected_rows)
            starts_with_material_1 = 1 not in selected_rows
            row_pattern = []
            selected_set = set(selected_rows)
            for row_number in range(1, row_count + 1):
                row_pattern.append(material_2_name if row_number in selected_set else material_1_name)
            rows.append(
                {
                    "assignment_index": int(assignment.get("assignment_index", 0)),
                    "step_index": None if step_index is None else int(step_index),
                    "step_progress": None if step_progress is None else round(float(step_progress), 6),
                    "step_weight": None if step_weight is None else round(float(step_weight), 6),
                    "candidate_index": candidate_index,
                    "target_units": target_units,
                    "actual_units": actual_units,
                    "target_material_1_ratio": round(target_ratio_1 * 100.0, 6),
                    "target_material_2_ratio": round(target_ratio_2 * 100.0, 6),
                    "actual_material_1_ratio": round(actual_ratio_1 * 100.0, 6),
                    "actual_material_2_ratio": round(actual_ratio_2 * 100.0, 6),
                    "ratio_error_material_1": round((actual_ratio_1 - target_ratio_1) * 100.0, 6),
                    "ratio_error_material_2": round((actual_ratio_2 - target_ratio_2) * 100.0, 6),
                    "interface_count": eta_info["interface_count"],
                    "interface_width_units": eta_info["interface_width_units"],
                    "eta_proxy": eta_info["eta_proxy"],
                    "assignment_eta": round(assignment_eta, 6),
                    "eta_error": round(eta_info["eta_proxy"] - assignment_eta, 6),
                    "starts_with_material_1": starts_with_material_1,
                    "selected_rows": selected_rows,
                    "row_pattern": row_pattern,
                }
            )
    return rows


def _candidate_sort_key(row: dict, target_eta: float) -> tuple[float, int, float, float, float, int]:
    eta_value = float(row.get("eta_proxy", 0.0))
    ratio_error_1 = abs(float(row.get("ratio_error_material_1", 0.0)))
    ratio_error_2 = abs(float(row.get("ratio_error_material_2", 0.0)))
    return (
        ratio_error_1 + ratio_error_2,
        0 if eta_value <= target_eta else 1,
        -eta_value if eta_value <= target_eta else abs(target_eta - eta_value),
        ratio_error_1,
        ratio_error_2,
        int(row.get("candidate_index", 0)),
    )


def build_top_step_material_candidates(
    assignment: dict,
    row_count: int = 14,
    target_ratio_2: float | None = None,
    keep_count: int = 1,
    require_starts_with_material_1: bool = True,
) -> list[dict]:
    """Keep only the best-ranked candidates instead of materializing the full pool."""
    spec = build_assignment_step_material_candidates(
        assignment,
        row_count=row_count,
        target_ratio_2=target_ratio_2,
    )
    row_weights = spec["row_weights"]
    total_weight = float(spec["row_weight_total"]) or 1.0
    material_1_name = spec["material_1"]
    material_2_name = spec["material_2"]
    target_ratio_1 = float(spec["target_material_1_ratio"]) / 100.0
    target_ratio_2 = float(spec["target_material_2_ratio"]) / 100.0
    assignment_eta = float(assignment.get("eta", 0.0))
    keep_count = max(1, int(keep_count))

    ranked_rows: list[dict] = []
    candidate_index = 0
    for group in spec["groups"]:
        target_units = int(group.get("target_units", 0))
        for combo in group.get("combinations", []):
            candidate_index += 1
            selected_rows = [int(v) for v in combo]
            starts_with_material_1 = 1 not in selected_rows
            if require_starts_with_material_1 and not starts_with_material_1:
                continue
            actual_units = int(sum(row_weights[row - 1] for row in selected_rows if 1 <= row <= row_count))
            actual_ratio_2 = actual_units / total_weight if total_weight > 1e-12 else 0.0
            actual_ratio_1 = 1.0 - actual_ratio_2
            eta_info = compute_candidate_eta_proxy(row_weights, selected_rows)
            selected_set = set(selected_rows)
            row_pattern = [
                material_2_name if row_number in selected_set else material_1_name
                for row_number in range(1, row_count + 1)
            ]
            row = {
                "assignment_index": int(assignment.get("assignment_index", 0)),
                "candidate_index": candidate_index,
                "target_units": target_units,
                "actual_units": actual_units,
                "target_material_1_ratio": round(target_ratio_1 * 100.0, 6),
                "target_material_2_ratio": round(target_ratio_2 * 100.0, 6),
                "actual_material_1_ratio": round(actual_ratio_1 * 100.0, 6),
                "actual_material_2_ratio": round(actual_ratio_2 * 100.0, 6),
                "ratio_error_material_1": round((actual_ratio_1 - target_ratio_1) * 100.0, 6),
                "ratio_error_material_2": round((actual_ratio_2 - target_ratio_2) * 100.0, 6),
                "interface_count": eta_info["interface_count"],
                "interface_width_units": eta_info["interface_width_units"],
                "eta_proxy": eta_info["eta_proxy"],
                "assignment_eta": round(assignment_eta, 6),
                "eta_error": round(eta_info["eta_proxy"] - assignment_eta, 6),
                "starts_with_material_1": starts_with_material_1,
                "selected_rows": selected_rows,
                "row_pattern": row_pattern,
            }
            ranked_rows.append(row)
            ranked_rows.sort(key=lambda item: _candidate_sort_key(item, assignment_eta))
            if len(ranked_rows) > keep_count:
                ranked_rows.pop()

    if ranked_rows or not require_starts_with_material_1:
        return ranked_rows
    return build_top_step_material_candidates(
        assignment,
        row_count=row_count,
        target_ratio_2=target_ratio_2,
        keep_count=keep_count,
        require_starts_with_material_1=False,
    )


def build_assignment_stepwise_material_candidate_analysis(
    assignment: dict,
    row_count: int = 14,
) -> list[dict]:
    """Enumerate row candidates separately for each step's target ratio."""
    row_count = max(1, int(row_count))
    step_count = max(1, int(assignment.get("gradient_steps", 1)))
    step_segments = assignment.get("step_segments") if isinstance(assignment.get("step_segments"), list) else []
    step_weights = [
        float(step.get("total_filament_length_mm", 0.0))
        for step in step_segments[:step_count]
        if isinstance(step, dict)
    ]
    if len(step_weights) != step_count:
        step_weights = [1.0] * step_count

    step_profile = build_stepwise_transition_profile(assignment, step_count, step_weights=step_weights)
    rows: list[dict] = []
    for step_spec in step_profile:
        target_ratio_2 = float(step_spec.get("target_material_2_ratio", 0.0)) / 100.0
        rows.extend(
            build_assignment_step_material_candidate_analysis(
                assignment,
                row_count=row_count,
                target_ratio_2=target_ratio_2,
                step_index=int(step_spec.get("step_index", 0)),
                step_progress=float(step_spec.get("step_progress", 0.0)),
                step_weight=float(step_spec.get("step_weight", 0.0)),
            )
        )
    return rows


def build_candidate_material_matrix(
    assignment: dict,
    selected_rows: list[int],
    row_count: int = 14,
) -> list[list[str]]:
    row_count = max(1, int(row_count))
    material_1_name = str(assignment.get("material_1") or "").strip()
    material_2_name = str(assignment.get("material_2") or "").strip()
    if not material_1_name and material_2_name:
        material_1_name = material_2_name
    if int(assignment.get("material_count", 1)) < 2 or not material_2_name:
        material_2_name = material_1_name

    step_count = max(1, int(assignment.get("gradient_steps", 1)))
    matrix = [[material_1_name for _ in range(step_count)] for _ in range(row_count)]
    for row_number in selected_rows:
        row_index = int(row_number) - 1
        if 0 <= row_index < row_count:
            for col_index in range(step_count):
                matrix[row_index][col_index] = material_2_name
    return matrix


def select_best_step_candidate(
    candidate_rows: list[dict],
    target_eta: float,
    candidate_rank: int = 1,
) -> dict | None:
    if not candidate_rows:
        return None

    target_eta = float(target_eta)
    eligible_rows = [
        row
        for row in candidate_rows
        if float(row.get("eta_proxy", 0.0)) <= target_eta
    ]
    pool = eligible_rows if eligible_rows else candidate_rows

    ranked = sorted(pool, key=lambda row: _candidate_sort_key(row, target_eta))
    rank_index = max(1, int(candidate_rank)) - 1
    if rank_index >= len(ranked):
        rank_index = len(ranked) - 1
    return ranked[rank_index]


def build_assignment_stepwise_material_selection(
    assignment: dict,
    row_count: int = 14,
    candidate_rank: int = 1,
) -> dict:
    row_count = max(1, int(row_count))
    material_1_name = str(assignment.get("material_1") or "").strip()
    material_2_name = str(assignment.get("material_2") or "").strip()
    if not material_1_name and material_2_name:
        material_1_name = material_2_name
    if int(assignment.get("material_count", 1)) < 2 or not material_2_name:
        material_2_name = material_1_name

    step_count = max(1, int(assignment.get("gradient_steps", 1)))
    row_weights = get_representative_row_weights(row_count)
    step_segments = assignment.get("step_segments") if isinstance(assignment.get("step_segments"), list) else []
    step_weights = [
        float(step.get("total_filament_length_mm", 0.0))
        for step in step_segments[:step_count]
        if isinstance(step, dict)
    ]
    if len(step_weights) != step_count:
        step_weights = [1.0] * step_count

    step_profile = build_stepwise_transition_profile(assignment, step_count, step_weights=step_weights)
    matrix = [[material_1_name for _ in range(step_count)] for _ in range(row_count)]
    step_choices: list[dict] = []
    assignment_eta = float(assignment.get("eta", 0.0))

    for step_position, step_spec in enumerate(step_profile):
        target_ratio_2 = float(step_spec.get("target_material_2_ratio", 0.0)) / 100.0
        target_ratio_1 = float(step_spec.get("target_material_1_ratio", 0.0)) / 100.0
        candidate_rows = build_top_step_material_candidates(
            assignment,
            row_count=row_count,
            target_ratio_2=target_ratio_2,
            keep_count=max(1, int(candidate_rank)),
            require_starts_with_material_1=True,
        )
        best_candidate = select_best_step_candidate(
            candidate_rows,
            assignment_eta,
            candidate_rank=candidate_rank,
        )
        if best_candidate is not None:
            selected_rows = [int(value) for value in best_candidate.get("selected_rows", [])]
            selected_eta_info = compute_candidate_eta_proxy(row_weights, selected_rows)
            selected_actual_ratio_1 = float(best_candidate.get("actual_material_1_ratio", 0.0)) / 100.0
            selected_actual_ratio_2 = float(best_candidate.get("actual_material_2_ratio", 0.0)) / 100.0
        else:
            selected_rows = [
                index + 1
                for index, material_name in enumerate(
                    _build_threshold_row_pattern(row_weights, target_ratio_2, material_1_name, material_2_name),
                )
                if material_name == material_2_name
            ]
            selected_eta_info = compute_candidate_eta_proxy(row_weights, selected_rows)
            selected_total_weight = float(sum(row_weights)) or 1.0
            selected_actual_units = float(sum(row_weights[row - 1] for row in selected_rows if 1 <= row <= row_count))
            selected_actual_ratio_2 = selected_actual_units / selected_total_weight
            selected_actual_ratio_1 = 1.0 - selected_actual_ratio_2
        selected_set = set(selected_rows)
        for row_index in range(1, row_count + 1):
            matrix[row_index - 1][step_position] = material_2_name if row_index in selected_set else material_1_name

        step_choices.append(
            {
                "step_index": int(step_spec.get("step_index", step_position + 1)),
                "step_progress": float(step_spec.get("step_progress", 0.0)),
                "target_material_1_ratio": float(step_spec.get("target_material_1_ratio", 0.0)),
                "target_material_2_ratio": float(step_spec.get("target_material_2_ratio", 0.0)),
                "actual_material_1_ratio": round(selected_actual_ratio_1 * 100.0, 6),
                "actual_material_2_ratio": round(selected_actual_ratio_2 * 100.0, 6),
                "ratio_error_material_1": round((selected_actual_ratio_1 - target_ratio_1) * 100.0, 6),
                "ratio_error_material_2": round((selected_actual_ratio_2 - target_ratio_2) * 100.0, 6),
                "candidate_count": len(candidate_rows),
                "selected_candidate_index": None if best_candidate is None else int(best_candidate.get("candidate_index", 0)),
                "selected_eta_proxy": None if best_candidate is None else float(best_candidate.get("eta_proxy", 0.0)),
                "selected_eta": float(selected_eta_info.get("eta", 0.0)),
                "selected_interface_width_units": float(selected_eta_info.get("interface_width_units", 0.0)),
                "selected_rows": selected_rows,
                "selected_row_pattern": None if best_candidate is None else list(best_candidate.get("row_pattern", [])),
            }
        )

    return {
        "matrix": matrix,
        "step_profile": step_profile,
        "step_choices": step_choices,
    }


def summarize_step_material_matrices(
    assignment_summary: list[dict],
    row_count: int = 14,
) -> list[list[list[str]]]:
    return [
        build_assignment_step_material_matrix(assignment, row_count=row_count)
        for assignment in assignment_summary
    ]


def summarize_step_material_analysis(
    assignment_summary: list[dict],
    row_count: int = 14,
) -> list[dict]:
    return [
        build_assignment_step_material_analysis(assignment, row_count=row_count)
        for assignment in assignment_summary
    ]


def summarize_step_material_candidates(
    assignment_summary: list[dict],
    row_count: int = 14,
) -> list[dict]:
    candidate_specs: list[dict] = []
    for assignment in assignment_summary:
        step_count = max(1, int(assignment.get("gradient_steps", 1)))
        step_segments = assignment.get("step_segments") if isinstance(assignment.get("step_segments"), list) else []
        step_weights = [
            float(step.get("total_filament_length_mm", 0.0))
            for step in step_segments[:step_count]
            if isinstance(step, dict)
        ]
        if len(step_weights) != step_count:
            step_weights = [1.0] * step_count
        step_profile = build_stepwise_transition_profile(assignment, step_count, step_weights=step_weights)
        for step_spec in step_profile:
            target_ratio_2 = float(step_spec.get("target_material_2_ratio", 0.0)) / 100.0
            spec = build_assignment_step_material_candidates(
                assignment,
                row_count=row_count,
                target_ratio_2=target_ratio_2,
            )
            spec["step_index"] = int(step_spec.get("step_index", 0))
            spec["step_progress"] = float(step_spec.get("step_progress", 0.0))
            spec["step_weight"] = float(step_spec.get("step_weight", 0.0))
            candidate_specs.append(spec)
    return candidate_specs


def summarize_step_material_candidate_matrices(
    assignment_summary: list[dict],
    row_count: int = 14,
) -> list[list[list[list[str]]]]:
    candidate_matrices: list[list[list[list[str]]]] = []
    for assignment in assignment_summary:
        assignment_candidate_matrices: list[list[list[str]]] = []
        candidate_spec = build_assignment_step_material_candidates(assignment, row_count=row_count)
        for group in candidate_spec["groups"]:
            for combo in group.get("combinations", []):
                assignment_candidate_matrices.append(
                    build_candidate_material_matrix(assignment, combo, row_count=row_count)
                )
        candidate_matrices.append(assignment_candidate_matrices)
    return candidate_matrices


def summarize_step_material_candidate_matrices_from_rows(
    assignment_summary: list[dict],
    candidate_rows: list[dict],
    row_count: int = 14,
) -> list[list[list[list[str]]]]:
    assignment_map = {
        int(assignment.get("assignment_index", idx + 1)): assignment
        for idx, assignment in enumerate(assignment_summary)
    }
    grouped: dict[int, list[list[list[str]]]] = {key: [] for key in assignment_map}
    for row in candidate_rows:
        assignment_index = int(row.get("assignment_index", 0))
        assignment = assignment_map.get(assignment_index)
        if assignment is None:
            continue
        selected_rows = [int(v) for v in row.get("selected_rows", [])]
        grouped.setdefault(assignment_index, []).append(
            build_candidate_material_matrix(assignment, selected_rows, row_count=row_count)
        )
    return [grouped.get(int(assignment.get("assignment_index", idx + 1)), []) for idx, assignment in enumerate(assignment_summary)]


def summarize_stepwise_candidate_matrices(
    assignment_summary: list[dict],
    row_count: int = 14,
    candidate_count: int | None = 10,
) -> list[list[list[list[str]]]]:
    candidate_matrices: list[list[list[list[str]]]] = []
    for assignment in assignment_summary:
        row_count = max(1, int(row_count))
        material_1_name = str(assignment.get("material_1") or "").strip()
        material_2_name = str(assignment.get("material_2") or "").strip()
        if not material_1_name and material_2_name:
            material_1_name = material_2_name
        if int(assignment.get("material_count", 1)) < 2 or not material_2_name:
            material_2_name = material_1_name

        step_count = max(1, int(assignment.get("gradient_steps", 1)))
        assignment_eta = float(assignment.get("eta", 0.0))
        step_rows: list[list[dict]] = []
        step_segments = assignment.get("step_segments") if isinstance(assignment.get("step_segments"), list) else []
        step_weights = [
            float(step.get("total_filament_length_mm", 0.0))
            for step in step_segments[:step_count]
            if isinstance(step, dict)
        ]
        if len(step_weights) != step_count:
            step_weights = [1.0] * step_count
        step_profile = build_stepwise_transition_profile(assignment, step_count, step_weights=step_weights)
        for step_spec in step_profile:
            target_ratio_2 = float(step_spec.get("target_material_2_ratio", 0.0)) / 100.0
            rows = build_assignment_step_material_candidate_analysis(
                assignment,
                row_count=row_count,
                target_ratio_2=target_ratio_2,
                step_index=int(step_spec.get("step_index", 0)),
                step_progress=float(step_spec.get("step_progress", 0.0)),
                step_weight=float(step_spec.get("step_weight", 0.0)),
            )
            eta_eligible_rows = [
                row
                for row in rows
                if float(row.get("eta_proxy", 0.0)) <= assignment_eta
            ]
            if eta_eligible_rows:
                max_eta = max(float(row.get("eta_proxy", 0.0)) for row in eta_eligible_rows)
                rows = [
                    row
                    for row in eta_eligible_rows
                    if abs(float(row.get("eta_proxy", 0.0)) - max_eta) <= 1e-9
                ]
            rows = sorted(rows, key=lambda row: _candidate_sort_key(row, assignment_eta))
            step_rows.append(rows)

        available_count = max((len(rows) for rows in step_rows), default=1)
        limit = available_count if candidate_count is None else min(max(1, int(candidate_count)), available_count)
        assignment_candidate_matrices: list[list[list[str]]] = []
        seen_matrices: set[tuple[tuple[str, ...], ...]] = set()
        for candidate_rank in range(limit):
            matrix = [[material_1_name for _ in range(step_count)] for _ in range(row_count)]
            for step_position, rows in enumerate(step_rows):
                if not rows:
                    continue
                row = rows[min(candidate_rank, len(rows) - 1)]
                selected_rows = {int(value) for value in row.get("selected_rows", [])}
                for row_index in range(1, row_count + 1):
                    matrix[row_index - 1][step_position] = (
                        material_2_name if row_index in selected_rows else material_1_name
                    )
            matrix_key = tuple(tuple(row) for row in matrix)
            if matrix_key in seen_matrices:
                continue
            seen_matrices.add(matrix_key)
            assignment_candidate_matrices.append(matrix)
        candidate_matrices.append(assignment_candidate_matrices)
    return candidate_matrices


def summarize_step_material_candidate_analysis(
    assignment_summary: list[dict],
    row_count: int = 14,
) -> list[dict]:
    rows: list[dict] = []
    for assignment in assignment_summary:
        rows.extend(build_assignment_stepwise_material_candidate_analysis(assignment, row_count=row_count))
    return rows


def select_candidate_matrix(
    candidate_matrices: list[list[list[list[str]]]] | None,
    assignment_index: int = 1,
    candidate_index: int = 1,
) -> tuple[list[list[str]] | None, int, int]:
    if not candidate_matrices:
        return None, assignment_index, candidate_index
    assignment_pos = max(1, int(assignment_index)) - 1
    if assignment_pos >= len(candidate_matrices):
        return None, assignment_index, candidate_index
    assignment_candidates = candidate_matrices[assignment_pos]
    if not assignment_candidates:
        return None, assignment_index, candidate_index
    candidate_pos = max(1, int(candidate_index)) - 1
    if candidate_pos >= len(assignment_candidates):
        return None, assignment_index, candidate_index
    return assignment_candidates[candidate_pos], assignment_index, candidate_index


def render_assignment_candidate_coverage_preview(
    output_path: Path,
    total_length_mm: float,
    filament_diameter_mm: float,
    assignment_summary: list[dict] | None,
    candidate_matrices: list[list[list[list[str]]]] | None,
    candidate_index: int = 1,
    candidate_indices_by_assignment: list[int] | None = None,
    show: bool = False,
) -> None:
    if plt is None or Rectangle is None:
        return
    if not assignment_summary:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(14.0, 2.8), dpi=180, facecolor="white")
    ax = fig.add_axes([0.06, 0.24, 0.60, 0.48], facecolor="#f8fafc")
    legend_ax = fig.add_axes([0.70, 0.14, 0.28, 0.66], facecolor="#fbfaf7")
    legend_ax.axis("off")

    # Collect labels across the selected candidates so the same material keeps the same color.
    used_labels: list[str] = []
    selected_matrices: list[list[list[str]] | None] = []
    for assignment_pos, assignment in enumerate(assignment_summary):
        matrix = None
        if candidate_matrices is not None and assignment_pos < len(candidate_matrices):
            assignment_candidates = candidate_matrices[assignment_pos]
            if assignment_candidates:
                if candidate_indices_by_assignment is not None and assignment_pos < len(candidate_indices_by_assignment):
                    candidate_pos = max(1, int(candidate_indices_by_assignment[assignment_pos])) - 1
                else:
                    candidate_pos = max(1, int(candidate_index)) - 1
                candidate_pos = min(candidate_pos, len(assignment_candidates) - 1)
                matrix = assignment_candidates[candidate_pos]
        if matrix is None:
            matrix = build_assignment_step_material_matrix(
                assignment,
                row_count=14,
                candidate_rank=max(1, int(candidate_index)),
            )
        selected_matrices.append(matrix)
        if matrix:
            for row in matrix:
                for cell in row:
                    label = str(cell)
                    if label not in used_labels:
                        used_labels.append(label)

    if not used_labels:
        used_labels = ["None"]

    palette = [
        "#38bdf8",
        "#facc15",
        "#fb7185",
        "#34d399",
        "#a78bfa",
        "#f97316",
        "#14b8a6",
        "#e879f9",
        "#60a5fa",
        "#f87171",
        "#4ade80",
        "#f59e0b",
        "#22d3ee",
        "#c084fc",
        "#84cc16",
        "#f43f5e",
    ]
    label_to_color = {label: palette[idx % len(palette)] for idx, label in enumerate(used_labels)}

    ax.add_patch(
        Rectangle(
            (0.0, 0.0),
            total_length_mm,
            filament_diameter_mm,
            facecolor="#e2e8f0",
            edgecolor="#334155",
            linewidth=2.0,
        )
    )

    legend_lines: list[str] = []
    segment_widths: list[float] = []
    for assignment in assignment_summary:
        width = float(assignment.get("total_filament_length_mm", 0.0))
        if width <= 0:
            start_fraction = float(assignment.get("start_fraction", 0.0))
            end_fraction = float(assignment.get("end_fraction", start_fraction))
            width = max(total_length_mm * max(end_fraction - start_fraction, 0.0), 0.0)
        segment_widths.append(width)

    cumulative_x = 0.0
    for idx, ((assignment, matrix), width) in enumerate(zip(zip(assignment_summary, selected_matrices), segment_widths), start=1):
        x0 = cumulative_x
        if width <= 0:
            width = max(total_length_mm * 0.001, 1e-6)
        cumulative_x += width

        ax.add_patch(
            Rectangle(
                (x0, 0.0),
                width,
                filament_diameter_mm,
                facecolor="none",
                edgecolor="#0f172a",
                linewidth=2.0,
                alpha=0.9,
            )
        )

        if matrix:
            row_count = len(matrix)
            step_count = max((len(row) for row in matrix), default=0)
            if row_count > 0 and step_count > 0:
                cell_h = filament_diameter_mm / float(row_count)
                step_segments = assignment.get("step_segments") if isinstance(assignment.get("step_segments"), list) else []
                step_bounds: list[tuple[float, float]] = []
                if step_segments:
                    for step in step_segments[:step_count]:
                        step_start = float(step.get("start_fraction", 0.0))
                        step_end = float(step.get("end_fraction", step_start))
                        step_start = max(0.0, min(1.0, step_start))
                        step_end = max(step_start, min(1.0, step_end))
                        step_bounds.append((step_start, step_end))
                if len(step_bounds) < step_count:
                    remaining = step_count - len(step_bounds)
                    for step_index in range(remaining):
                        start_fraction = step_index / float(step_count)
                        end_fraction = (step_index + 1) / float(step_count)
                        step_bounds.append((start_fraction, end_fraction))
                for col_index, (step_start_fraction, step_end_fraction) in enumerate(step_bounds[:step_count]):
                    step_x0 = x0 + width * step_start_fraction
                    col_width = max(width * (step_end_fraction - step_start_fraction), 1e-6)
                    for row_index, row in enumerate(matrix):
                        cell = row[col_index] if col_index < len(row) else row[-1]
                        color = label_to_color.get(str(cell), "#cbd5e1")
                        cell_y = filament_diameter_mm - (row_index + 1) * cell_h
                        ax.add_patch(
                            Rectangle(
                                (step_x0, cell_y),
                                col_width,
                                cell_h,
                                facecolor=color,
                                edgecolor="white",
                                linewidth=0.6,
                                alpha=0.90,
                            )
                        )
                    if col_index < step_count - 1:
                        boundary_x = x0 + width * step_end_fraction
                        ax.plot(
                            [boundary_x, boundary_x],
                            [0.0, filament_diameter_mm],
                            color="#334155",
                            linewidth=0.8,
                            alpha=0.55,
                        )
            legend_lines.append(
                f"A{idx}: {int(assignment.get('gradient_steps', 1))} step(s), eta={float(assignment.get('eta', 0.0)):.2f}"
            )
        else:
            legend_lines.append(
                f"A{idx}: no candidate matrix available, eta={float(assignment.get('eta', 0.0)):.2f}"
            )

        ax.text(
            x0 + width * 0.5,
            filament_diameter_mm * 1.04,
            f"A{idx}",
            ha="center",
            va="bottom",
            fontsize=9.5,
            fontweight="bold",
            color="#0f172a",
            bbox=dict(boxstyle="round,pad=0.18", facecolor="white", edgecolor="#94a3b8", alpha=0.95),
        )

    ax.set_xlim(0.0, max(total_length_mm, cumulative_x, 1e-6))
    ax.set_ylim(0.0, max(filament_diameter_mm, 1e-6))
    ax.set_xlabel("Filament length / extrusion amount (mm)")
    ax.set_ylabel("Diameter (mm)")
    ax.set_title("Assignment coverage preview with candidate material fill", fontsize=15, fontweight="bold", pad=14)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(False)

    legend_lines.append("")
    legend_lines.append("Materials")
    for label in used_labels:
        legend_lines.append(f"{label}: {label_to_color[label]}")
    legend_ax.text(
        0.02,
        0.98,
        "\n".join(legend_lines),
        transform=legend_ax.transAxes,
        ha="left",
        va="top",
        fontsize=8.5,
        family="monospace",
        linespacing=1.35,
        bbox=dict(boxstyle="round,pad=0.45", facecolor="white", edgecolor="#cbd5e1", linewidth=1.0),
    )

    plt.tight_layout(rect=[0.0, 0.0, 0.98, 1.0])
    fig.savefig(output_path)
    if show:
        plt.show()
    plt.close(fig)


def render_assignment_shape_gallery_preview(
    output_path: Path,
    assignment_summary: list[dict] | None,
    candidate_matrices: list[list[list[list[str]]]] | None,
    candidate_index: int = 1,
    show: bool = False,
) -> None:
    if plt is None or ListedColormap is None or BoundaryNorm is None:
        return
    if not assignment_summary:
        return

    selected_matrices: list[list[list[str]] | None] = []
    used_labels: list[str] = []
    for assignment_pos, _assignment in enumerate(assignment_summary):
        assignment_candidates = []
        if candidate_matrices is not None and assignment_pos < len(candidate_matrices):
            assignment_candidates = candidate_matrices[assignment_pos]
        if assignment_candidates:
            candidate_pos = max(1, int(candidate_index)) - 1
            if candidate_pos >= len(assignment_candidates):
                candidate_pos = 0
            matrix = assignment_candidates[candidate_pos]
        else:
            matrix = None
        selected_matrices.append(matrix)
        if matrix:
            for row in matrix:
                for cell in row:
                    label = str(cell)
                    if label not in used_labels:
                        used_labels.append(label)

    if not used_labels:
        used_labels = ["None"]

    palette = [
        "#38bdf8",
        "#facc15",
        "#fb7185",
        "#34d399",
        "#a78bfa",
        "#f97316",
        "#14b8a6",
        "#e879f9",
        "#60a5fa",
        "#f87171",
        "#4ade80",
        "#f59e0b",
        "#22d3ee",
        "#c084fc",
        "#84cc16",
        "#f43f5e",
    ]
    cmap = ListedColormap(palette[: len(used_labels)])
    norm = BoundaryNorm([i - 0.5 for i in range(len(used_labels) + 1)], cmap.N)
    label_to_index = {label: idx for idx, label in enumerate(used_labels)}

    assignment_count = len(assignment_summary)
    fig_width = max(5.0, assignment_count * 5.0)
    fig, axes = plt.subplots(1, assignment_count, figsize=(fig_width, 6.0), dpi=180, facecolor="white")
    if assignment_count == 1:
        axes = [axes]

    for idx, (axis, assignment, matrix) in enumerate(zip(axes, assignment_summary, selected_matrices), start=1):
        axis.set_facecolor("#f8fafc")
        if matrix:
            row_count = len(matrix)
            step_count = max((len(row) for row in matrix), default=0)
            if row_count > 0 and step_count > 0:
                indexed_matrix = [
                    [label_to_index.get(str(cell), 0) for cell in row]
                    for row in matrix
                ]
                image = axis.imshow(indexed_matrix, aspect="auto", cmap=cmap, norm=norm, interpolation="nearest")
                axis.set_xticks(list(range(step_count)))
                axis.set_xticklabels([f"S{col + 1}" for col in range(step_count)], fontsize=8)
                axis.set_yticks(list(range(row_count)))
                axis.set_yticklabels([f"R{row + 1}" for row in range(row_count)], fontsize=8)
                axis.set_xlabel("Step column")
                if idx == 1:
                    axis.set_ylabel("Row index")
                for row_index, row in enumerate(matrix):
                    for col_index, cell in enumerate(row):
                        axis.text(
                            col_index,
                            row_index,
                            str(cell),
                            ha="center",
                            va="center",
                            fontsize=7.5,
                            color="#0f172a",
                            fontweight="bold",
                        )
                axis.set_title(
                    f"A{idx}\neta={float(assignment.get('eta', 0.0)):.2f}, step={int(assignment.get('gradient_steps', 1))}",
                    fontsize=11,
                    fontweight="bold",
                )
                fig.colorbar(image, ax=axis, fraction=0.045, pad=0.03)
            else:
                axis.text(0.5, 0.5, "Empty matrix", ha="center", va="center")
                axis.set_axis_off()
        else:
            axis.text(0.5, 0.5, "No candidate matrix", ha="center", va="center")
            axis.set_axis_off()

    fig.suptitle("Assignment shape preview", fontsize=16, fontweight="bold")
    legend_lines = ["Materials"]
    for label in used_labels:
        legend_lines.append(f"{label}: {palette[label_to_index[label] % len(palette)]}")
    fig.text(
        0.99,
        0.02,
        "\n".join(legend_lines),
        ha="right",
        va="bottom",
        fontsize=8.5,
        family="monospace",
        bbox=dict(boxstyle="round,pad=0.45", facecolor="white", edgecolor="#cbd5e1", linewidth=1.0),
    )
    plt.tight_layout(rect=[0.0, 0.0, 0.96, 0.94])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)


def render_assignment_radial_preview(
    output_path: Path,
    assignment_summary: list[dict] | None,
    candidate_matrices: list[list[list[list[str]]]] | None,
    candidate_index: int = 1,
    row_weights: list[int] | None = None,
    show: bool = False,
) -> None:
    if plt is None:
        return
    if not assignment_summary:
        return

    weights = list(row_weights) if row_weights is not None else get_representative_row_weights(14)
    if not weights:
        weights = [1] * 14
    display_ratio = 440.0 / 125.0
    row_count = len(weights)
    max_blocks = max(max(int(weight), 1) for weight in weights) if weights else 1
    block_size = 2.0 / float(max_blocks)
    block_height = block_size / display_ratio
    total_height = float(row_count) * block_height

    selected_matrices: list[list[list[str]] | None] = []
    used_labels: list[str] = []
    for assignment_pos, _assignment in enumerate(assignment_summary):
        assignment_candidates = []
        if candidate_matrices is not None and assignment_pos < len(candidate_matrices):
            assignment_candidates = candidate_matrices[assignment_pos]
        if assignment_candidates:
            candidate_pos = max(1, int(candidate_index)) - 1
            if candidate_pos >= len(assignment_candidates):
                candidate_pos = 0
            matrix = assignment_candidates[candidate_pos]
        else:
            matrix = None
        selected_matrices.append(matrix)
        if matrix:
            for row in matrix:
                if row:
                    label = str(row[0])
                    if label not in used_labels:
                        used_labels.append(label)

    if not used_labels:
        used_labels = ["None"]

    palette = [
        "#38bdf8",
        "#facc15",
        "#fb7185",
        "#34d399",
        "#a78bfa",
        "#f97316",
        "#14b8a6",
        "#e879f9",
        "#60a5fa",
        "#f87171",
        "#4ade80",
        "#f59e0b",
        "#22d3ee",
        "#c084fc",
        "#84cc16",
        "#f43f5e",
    ]
    label_to_color = {label: palette[idx % len(palette)] for idx, label in enumerate(used_labels)}

    assignment_count = len(assignment_summary)
    fig_width = max(6.0, assignment_count * 5.0)
    fig_height = max(2.0, fig_width / display_ratio)
    fig, axes = plt.subplots(1, assignment_count, figsize=(fig_width, fig_height), dpi=180, facecolor="white")
    if assignment_count == 1:
        axes = [axes]

    for idx, (axis, assignment, matrix) in enumerate(zip(axes, assignment_summary, selected_matrices), start=1):
        axis.set_aspect("equal")
        axis.set_facecolor("#f8fafc")
        axis.set_xlim(-1.08, 1.08)
        axis.set_ylim(-total_height / 2.0 - 0.08, total_height / 2.0 + 0.08)
        axis.axis("off")

        if matrix:
            row_labels: list[str] = []
            for row in matrix[:row_count]:
                row_labels.append(str(row[0]) if row else "None")
            while len(row_labels) < row_count:
                row_labels.append(row_labels[-1] if row_labels else "None")

            y_top = total_height / 2.0
            for row_index, (weight, label) in enumerate(zip(weights, row_labels), start=1):
                block_count = max(1, int(weight))
                row_width = block_count * block_size
                x_left = -row_width / 2.0
                y_bottom = y_top - block_height
                for block_index in range(block_count):
                    block_x = x_left + block_index * block_size
                    block = Rectangle(
                        (block_x, y_bottom),
                        block_size,
                        block_height,
                        facecolor=label_to_color.get(label, "#cbd5e1"),
                        edgecolor="white",
                        linewidth=0.8,
                        alpha=0.95,
                    )
                    axis.add_patch(block)
                axis.plot([-1.0, 1.0], [y_bottom, y_bottom], color="white", linewidth=0.8)
                y_top = y_bottom

            axis.text(0.0, 0.0, f"A{idx}", ha="center", va="center", fontsize=13, fontweight="bold", color="#0f172a")
            axis.set_title(
                f"A{idx}\neta={float(assignment.get('eta', 0.0)):.2f}, step={int(assignment.get('gradient_steps', 1))}",
                fontsize=11,
                fontweight="bold",
            )
        else:
            axis.text(0.0, 0.0, "No candidate", ha="center", va="center", fontsize=11)
            axis.set_title(f"A{idx}", fontsize=11, fontweight="bold")

    fig.suptitle("Block-style assignment cross-section preview", fontsize=16, fontweight="bold")
    legend_lines = ["Row block counts", str(weights), "", "Materials"]
    for label in used_labels:
        legend_lines.append(f"{label}: {label_to_color[label]}")
    fig.text(
        0.99,
        0.02,
        "\n".join(legend_lines),
        ha="right",
        va="bottom",
        fontsize=8.5,
        family="monospace",
        bbox=dict(boxstyle="round,pad=0.45", facecolor="white", edgecolor="#cbd5e1", linewidth=1.0),
    )
    plt.tight_layout(rect=[0.0, 0.0, 0.96, 0.94])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)


def filter_candidate_rows_by_eta(
    candidate_rows: list[dict],
    eta_min: float | None = None,
    eta_max: float | None = None,
) -> list[dict]:
    filtered: list[dict] = []
    for row in candidate_rows:
        eta_proxy = float(row.get("eta_proxy", 0.0))
        if eta_min is not None and eta_proxy < eta_min:
            continue
        if eta_max is not None and eta_proxy > eta_max:
            continue
        filtered.append(row)
    return filtered


def filter_candidate_rows_by_eta_tolerance(
    candidate_rows: list[dict],
    tolerance: float,
) -> list[dict]:
    filtered: list[dict] = []
    for row in candidate_rows:
        eta_error = float(row.get("eta_error", 0.0))
        if abs(eta_error) <= tolerance:
            filtered.append(row)
    return filtered


def summarize_candidate_eta_filter(
    original_rows: list[dict],
    filtered_rows: list[dict],
) -> list[dict]:
    original_counts: dict[tuple[int, int], int] = {}
    filtered_counts: dict[tuple[int, int], int] = {}
    assignment_eta: dict[tuple[int, int], float] = {}

    for row in original_rows:
        assignment_index = int(row.get("assignment_index", 0))
        step_index = int(row.get("step_index") or 0)
        key = (assignment_index, step_index)
        original_counts[key] = original_counts.get(key, 0) + 1
        assignment_eta[key] = float(row.get("assignment_eta", row.get("eta_target", 0.0)))

    for row in filtered_rows:
        assignment_index = int(row.get("assignment_index", 0))
        step_index = int(row.get("step_index") or 0)
        key = (assignment_index, step_index)
        filtered_counts[key] = filtered_counts.get(key, 0) + 1
        assignment_eta[key] = float(row.get("assignment_eta", row.get("eta_target", 0.0)))

    summary_rows: list[dict] = []
    for assignment_index, step_index in sorted(original_counts):
        key = (assignment_index, step_index)
        before = int(original_counts.get(key, 0))
        after = int(filtered_counts.get(key, 0))
        summary_rows.append(
            {
                "assignment_index": assignment_index,
                "step_index": step_index,
                "assignment_eta": round(float(assignment_eta.get(key, 0.0)), 6),
                "before_count": before,
                "after_count": after,
                "removed_count": before - after,
                "keep_ratio": round((after / before) if before else 0.0, 6),
            }
        )
    return summary_rows


def format_material_name_matrix_raw(matrices: list[list[list[str]]]) -> str:
    lines: list[str] = ["material_name_matrix_raw = ["]
    for matrix_index, matrix in enumerate(matrices):
        lines.append("  [")
        for row_index, row in enumerate(matrix):
            row_text = ", ".join(json.dumps(str(cell), ensure_ascii=True) for cell in row)
            row_suffix = "," if row_index < len(matrix) - 1 else ""
            lines.append(f"    [{row_text}]{row_suffix}")
        matrix_suffix = "," if matrix_index < len(matrices) - 1 else ""
        lines.append(f"  ]{matrix_suffix}")
    lines.append("]")
    return "\n".join(lines) + "\n"


def format_step_material_analysis_table(analysis_rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append("step_material_analysis = [")
    lines.append(
        "  assignment_index | steps | target_ratio | actual_ratio | ratio_error | step_eta | step_ratio_error | row_pattern"
    )
    lines.append("  " + "-" * 136)
    for row in analysis_rows:
        row_pattern = ",".join(str(cell) for cell in row.get("row_pattern", []))
        step_eta_summary = ",".join(
            f"{float(step.get('selected_eta', 0.0)):.2f}"
            for step in row.get("stepwise_selection", [])
        )
        step_ratio_summary = ",".join(
            f"{float(step.get('ratio_error_material_2', 0.0)):.2f}"
            for step in row.get("stepwise_selection", [])
        )
        lines.append(
            "  "
            f"{int(row.get('assignment_index', 0)):>15} | "
            f"{int(row.get('gradient_steps', 0)):>5} | "
            f"{float(row.get('target_material_1_ratio', 0.0)):>5.1f}:{float(row.get('target_material_2_ratio', 0.0)):>5.1f} | "
            f"{float(row.get('actual_material_1_ratio', 0.0)):>5.1f}:{float(row.get('actual_material_2_ratio', 0.0)):>5.1f} | "
            f"{float(row.get('ratio_error_material_1', 0.0)):>+6.2f}:{float(row.get('ratio_error_material_2', 0.0)):>+6.2f} | "
            f"[{step_eta_summary}] | "
            f"[{step_ratio_summary}] | "
            f"[{row_pattern}]"
        )
    lines.append("]")
    return "\n".join(lines) + "\n"


def format_step_material_candidates_table(candidate_rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append("step_material_candidates = [")
    for row in candidate_rows:
        lines.append("  {")
        lines.append(f"    \"assignment_index\": {int(row.get('assignment_index', 0))},")
        if row.get("step_index") is not None:
            lines.append(f"    \"step_index\": {int(row.get('step_index', 0))},")
            lines.append(f"    \"step_progress\": {float(row.get('step_progress', 0.0)):.6f},")
            lines.append(f"    \"step_weight\": {float(row.get('step_weight', 0.0)):.6f},")
        lines.append(f"    \"row_count\": {int(row.get('row_count', 0))},")
        lines.append(f"    \"row_weights\": {json.dumps(row.get('row_weights', []), ensure_ascii=True)},")
        lines.append(
            f"    \"target_ratio\": \"{float(row.get('target_material_1_ratio', 0.0)):.1f}:{float(row.get('target_material_2_ratio', 0.0)):.1f}\","
        )
        lines.append(
            f"    \"target_secondary_units_exact\": {float(row.get('target_secondary_units_exact', 0.0)):.6f},"
        )
        lines.append(f"    \"target_unit_options\": {json.dumps(row.get('target_unit_options', []), ensure_ascii=True)},")
        lines.append("    \"groups\": [")
        for group in row.get("groups", []):
            lines.append("      {")
            lines.append(f"        \"target_units\": {int(group.get('target_units', 0))},")
            lines.append(f"        \"candidate_count\": {int(group.get('candidate_count', 0))},")
            lines.append("        \"combinations\": [")
            for combo in group.get("combinations", []):
                lines.append(f"          {json.dumps(combo, ensure_ascii=True)},")
            lines.append("        ]")
            lines.append("      },")
        lines.append("    ]")
        lines.append("  },")
    lines.append("]")
    return "\n".join(lines) + "\n"


def format_step_material_candidate_analysis_table(analysis_rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append("step_material_candidate_analysis = [")
    lines.append(
        "  assignment_index | step_index | candidate_index | target_units | actual_units | target_ratio | actual_ratio | ratio_error | eta_target | eta_proxy | eta_error | selected_rows"
    )
    lines.append("  " + "-" * 178)
    for row in analysis_rows:
        selected_rows = ",".join(str(v) for v in row.get("selected_rows", []))
        lines.append(
            "  "
            f"{int(row.get('assignment_index', 0)):>15} | "
            f"{int(row.get('step_index') or 0):>10} | "
            f"{int(row.get('candidate_index', 0)):>15} | "
            f"{int(row.get('target_units', 0)):>12} | "
            f"{int(row.get('actual_units', 0)):>11} | "
            f"{float(row.get('target_material_1_ratio', 0.0)):>5.1f}:{float(row.get('target_material_2_ratio', 0.0)):>5.1f} | "
            f"{float(row.get('actual_material_1_ratio', 0.0)):>5.1f}:{float(row.get('actual_material_2_ratio', 0.0)):>5.1f} | "
            f"{float(row.get('ratio_error_material_1', 0.0)):>+6.2f}:{float(row.get('ratio_error_material_2', 0.0)):>+6.2f} | "
            f"{float(row.get('assignment_eta', 0.0)):>5.2f} | "
            f"{float(row.get('eta_proxy', 0.0)):>7.4f} | "
            f"{float(row.get('eta_error', 0.0)):>+7.4f} | "
            f"[{selected_rows}]"
        )
    lines.append("]")
    return "\n".join(lines) + "\n"


def format_candidate_eta_summary_table(summary_rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append("candidate_eta_summary = [")
    lines.append("  assignment_index | step_index | eta_target | before_count | after_count | removed_count | keep_ratio")
    lines.append("  " + "-" * 101)
    for row in summary_rows:
        lines.append(
            "  "
            f"{int(row.get('assignment_index', 0)):>15} | "
            f"{int(row.get('step_index') or 0):>10} | "
            f"{float(row.get('assignment_eta', 0.0)):>5.2f} | "
            f"{int(row.get('before_count', 0)):>12} | "
            f"{int(row.get('after_count', 0)):>11} | "
            f"{int(row.get('removed_count', 0)):>13} | "
            f"{float(row.get('keep_ratio', 0.0)):>8.4f}"
        )
    lines.append("]")
    return "\n".join(lines) + "\n"


def format_material_name_matrix_candidates_raw(
    assignment_summary: list[dict],
    candidate_specs: list[dict],
    row_count: int = 14,
) -> str:
    lines: list[str] = ["material_name_matrix_candidates_raw = ["]
    assignment_map = {
        int(assignment.get("assignment_index", idx + 1)): assignment
        for idx, assignment in enumerate(assignment_summary)
    }
    for candidate_spec in candidate_specs:
        spec_assignment_index = int(candidate_spec.get("assignment_index", 0))
        assignment = assignment_map.get(spec_assignment_index)
        if assignment is None:
            continue
        row_weights = [int(v) for v in candidate_spec.get("row_weights", get_representative_row_weights(row_count))]
        material_1_name = str(candidate_spec.get("material_1") or assignment.get("material_1") or "").strip()
        material_2_name = str(candidate_spec.get("material_2") or assignment.get("material_2") or "").strip()
        if not material_1_name and material_2_name:
            material_1_name = material_2_name
        if int(assignment.get("material_count", 1)) < 2 or not material_2_name:
            material_2_name = material_1_name

        eta_target = float(assignment.get("eta", 0.0))
        step_index = int(candidate_spec.get("step_index") or 0)
        step_count = max(1, int(assignment.get("gradient_steps", 1)))
        total_candidates = 0
        for group in candidate_spec.get("groups", []):
            total_candidates += len(group.get("combinations", []))

        lines.append(
            f"  # assignment {spec_assignment_index} | step={step_index or 'all'} | eta_target={eta_target:.2f} | candidate_count={total_candidates}"
        )
        lines.append("  [")

        candidate_index = 0
        for group in candidate_spec.get("groups", []):
            target_units = int(group.get("target_units", 0))
            for combo in group.get("combinations", []):
                candidate_index += 1
                selected_rows = [int(v) for v in combo]
                actual_units = int(sum(row_weights[row - 1] for row in selected_rows if 1 <= row <= row_count))
                total_weight = float(sum(row_weights)) or float(row_count)
                actual_ratio_2 = actual_units / total_weight if total_weight > 1e-12 else 0.0
                actual_ratio_1 = 1.0 - actual_ratio_2
                target_ratio_1 = float(candidate_spec.get("target_material_1_ratio", 0.0)) / 100.0
                target_ratio_2 = float(candidate_spec.get("target_material_2_ratio", 0.0)) / 100.0
                eta_info = compute_candidate_eta_proxy(row_weights, selected_rows)
                matrix = [[material_1_name for _ in range(step_count)] for _ in range(row_count)]
                selected_set = set(selected_rows)
                if 1 <= step_index <= step_count:
                    for row_number in selected_set:
                        row_pos = int(row_number) - 1
                        if 0 <= row_pos < row_count:
                            matrix[row_pos][step_index - 1] = material_2_name
                else:
                    for row_number in selected_set:
                        row_pos = int(row_number) - 1
                        if 0 <= row_pos < row_count:
                            for col_pos in range(step_count):
                                matrix[row_pos][col_pos] = material_2_name
                eta_proxy = float(eta_info["eta_proxy"])
                eta_error = eta_proxy - eta_target
                lines.append(
                    f"    # candidate {candidate_index} | step={step_index or 'all'} | target_units={target_units} | actual_units={actual_units} | "
                    f"target_ratio={target_ratio_1 * 100.0:.1f}:{target_ratio_2 * 100.0:.1f} | "
                    f"actual_ratio={actual_ratio_1 * 100.0:.1f}:{actual_ratio_2 * 100.0:.1f} | "
                    f"eta={eta_proxy:.2f} | eta_error={eta_error:+.4f} | "
                    f"selected_rows={json.dumps(selected_rows, ensure_ascii=True)}"
                )
                lines.append("    [")
                for row_index, row in enumerate(matrix):
                    row_text = ", ".join(json.dumps(str(cell), ensure_ascii=True) for cell in row)
                    row_suffix = "," if row_index < len(matrix) - 1 else ""
                    lines.append(f"      [{row_text}]{row_suffix}")
                candidate_suffix = "," if candidate_index < total_candidates else ""
                lines.append(f"    ]{candidate_suffix}")
        lines.append("  ],")
    lines.append("]")
    return "\n".join(lines) + "\n"


def build_full_filament_candidate_matrix(
    candidate_matrices: list[list[list[list[str]]]],
    candidate_indices_by_assignment: list[int],
    row_count: int = 14,
) -> list[list[str]]:
    full_matrix: list[list[str]] = [[] for _ in range(max(1, int(row_count)))]
    for assignment_pos, assignment_candidates in enumerate(candidate_matrices):
        if not assignment_candidates:
            continue
        candidate_index = 1
        if assignment_pos < len(candidate_indices_by_assignment):
            candidate_index = int(candidate_indices_by_assignment[assignment_pos])
        candidate_pos = max(1, candidate_index) - 1
        if candidate_pos >= len(assignment_candidates):
            candidate_pos = len(assignment_candidates) - 1
        matrix = assignment_candidates[candidate_pos]
        for row_index in range(len(full_matrix)):
            row_values = matrix[row_index] if row_index < len(matrix) else []
            full_matrix[row_index].extend(str(value) for value in row_values)
    return full_matrix


def count_full_filament_candidate_combinations(
    candidate_matrices: list[list[list[list[str]]]] | None,
) -> int:
    if not candidate_matrices:
        return 0
    total_combo_count = 1
    for assignment_candidates in candidate_matrices:
        candidate_count = len(assignment_candidates)
        if candidate_count <= 0:
            return 0
        total_combo_count *= candidate_count
    return total_combo_count


def summarize_assignment_candidate_counts(
    candidate_matrices: list[list[list[list[str]]]] | None,
) -> list[dict]:
    if not candidate_matrices:
        return []
    return [
        {
            "assignment_index": assignment_index,
            "candidate_count": len(assignment_candidates),
        }
        for assignment_index, assignment_candidates in enumerate(candidate_matrices, start=1)
    ]


def count_exhaustive_stepwise_candidate_combinations(candidate_specs: list[dict] | None) -> int:
    if not candidate_specs:
        return 0
    total_combo_count = 1
    for candidate in candidate_specs:
        candidate_count = int(
            sum(int(group.get("candidate_count", 0)) for group in candidate.get("groups", []))
        )
        if candidate_count <= 0:
            return 0
        total_combo_count *= candidate_count
    return total_combo_count


def format_full_filament_candidate_matrices_raw(
    assignment_summary: list[dict],
    candidate_matrices: list[list[list[list[str]]]] | None,
    max_candidates: int | None = 100,
    row_count: int = 14,
) -> str:
    lines: list[str] = ["full_filament_candidate_matrices_raw = ["]
    if not assignment_summary or not candidate_matrices:
        lines.append("]")
        return "\n".join(lines) + "\n"

    candidate_ranges = [range(1, len(items) + 1) for items in candidate_matrices]
    if not candidate_ranges or any(len(rng) == 0 for rng in candidate_ranges):
        lines.append("]")
        return "\n".join(lines) + "\n"

    total_combo_count = 1
    for rng in candidate_ranges:
        total_combo_count *= len(rng)
    assignment_candidate_counts = summarize_assignment_candidate_counts(candidate_matrices)

    assignment_column_labels: list[str] = []
    for assignment_index, assignment in enumerate(assignment_summary, start=1):
        step_count = max(1, int(assignment.get("gradient_steps", 1)))
        for step_index in range(1, step_count + 1):
            assignment_column_labels.append(f"A{assignment_index}s{step_index}")

    if max_candidates is not None:
        max_candidates = int(max_candidates)
        if max_candidates <= 0:
            max_candidates = None
    output_total = total_combo_count if max_candidates is None else min(total_combo_count, max_candidates)
    lines.append(
        f"  # assignment_candidate_counts={json.dumps(assignment_candidate_counts, ensure_ascii=True)}"
    )
    combination_formula = " x ".join(
        f"A{row['assignment_index']}({row['candidate_count']})"
        for row in assignment_candidate_counts
    )
    lines.append(f"  # assignment_combination_formula={combination_formula}")
    lines.append(f"  # assignment_combination_candidate_count={total_combo_count}")
    lines.append(f"  # matrices_written={output_total}")
    emitted_count = 0
    combo_iter = product(*candidate_ranges)
    if max_candidates is not None:
        combo_iter = islice(combo_iter, max_candidates)
    for combo_index, combo in enumerate(combo_iter, start=1):
        emitted_count += 1
        matrix = build_full_filament_candidate_matrix(
            candidate_matrices,
            list(combo),
            row_count=row_count,
        )
        lines.append(
            f"  # candidate {combo_index} | candidate_indices_by_assignment={json.dumps(list(combo), ensure_ascii=True)} | "
            f"columns={json.dumps(assignment_column_labels, ensure_ascii=True)}"
        )
        lines.append("  [")
        for row_index, row in enumerate(matrix):
            row_text = ", ".join(json.dumps(str(cell), ensure_ascii=True) for cell in row)
            row_suffix = "," if row_index < len(matrix) - 1 else ""
            lines.append(f"    [{row_text}]{row_suffix}")
        candidate_suffix = "," if combo_index < output_total else ""
        lines.append(f"  ]{candidate_suffix}")

    if total_combo_count > emitted_count:
        lines.append(
            f"  # capped_at={emitted_count} of total_combination_count={total_combo_count}"
        )
    lines.append("]")
    return "\n".join(lines) + "\n"


def format_assignment_candidate_matrices_raw(
    assignment_summary: list[dict],
    candidate_matrices: list[list[list[list[str]]]] | None,
) -> str:
    lines: list[str] = ["assignment_candidate_matrices_raw = ["]
    if not assignment_summary or not candidate_matrices:
        lines.append("]")
        return "\n".join(lines) + "\n"

    for assignment_index, matrices in enumerate(candidate_matrices, start=1):
        step_count = 0
        if assignment_index <= len(assignment_summary):
            step_count = max(1, int(assignment_summary[assignment_index - 1].get("gradient_steps", 1)))
        lines.append(
            f"  # assignment {assignment_index} | candidate_count={len(matrices)} | matrix_shape=14x{step_count}"
        )
        lines.append("  [")
        for candidate_index, matrix in enumerate(matrices, start=1):
            lines.append(f"    # candidate {candidate_index}, 14 x A{assignment_index}_steps")
            lines.append("    [")
            for row_index, row in enumerate(matrix):
                row_text = ", ".join(json.dumps(str(cell), ensure_ascii=True) for cell in row)
                row_suffix = "," if row_index < len(matrix) - 1 else ""
                lines.append(f"      [{row_text}]{row_suffix}")
            candidate_suffix = "," if candidate_index < len(matrices) else ""
            lines.append(f"    ]{candidate_suffix}")
        assignment_suffix = "," if assignment_index < len(candidate_matrices) else ""
        lines.append(f"  ]{assignment_suffix}")
    lines.append("]")
    return "\n".join(lines) + "\n"


def render_step_material_candidate_preview(
    output_path: Path,
    matrix: list[list[str]],
    assignment_index: int | None = None,
    candidate_index: int | None = None,
    show: bool = False,
) -> None:
    if plt is None or ListedColormap is None or BoundaryNorm is None:
        return
    if not matrix:
        return

    row_count = len(matrix)
    step_count = max((len(row) for row in matrix), default=0)
    if step_count <= 0:
        return

    labels: list[str] = []
    for row in matrix:
        for cell in row:
            label = str(cell)
            if label not in labels:
                labels.append(label)
    if not labels:
        labels = ["None"]

    colors = [
        "#38bdf8",
        "#facc15",
        "#fb7185",
        "#34d399",
        "#a78bfa",
        "#f97316",
        "#14b8a6",
        "#e879f9",
        "#60a5fa",
        "#f87171",
        "#4ade80",
        "#f59e0b",
        "#22d3ee",
        "#c084fc",
        "#84cc16",
        "#f43f5e",
    ]
    cmap = ListedColormap(colors[: len(labels)])
    if len(labels) == 1:
        norm = BoundaryNorm([-0.5, 0.5, 1.5], cmap.N)
    else:
        norm = BoundaryNorm([i - 0.5 for i in range(len(labels) + 1)], cmap.N)
    label_to_index = {label: idx for idx, label in enumerate(labels)}
    indexed_matrix = [
        [label_to_index.get(str(cell), 0) for cell in row]
        for row in matrix
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig_width = max(9.0, step_count * 0.9)
    fig_height = max(5.0, row_count * 0.42)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=180, facecolor="white")
    ax.set_facecolor("#f8fafc")
    image = ax.imshow(indexed_matrix, aspect="auto", cmap=cmap, norm=norm, interpolation="nearest")

    ax.set_xticks(list(range(step_count)))
    ax.set_xticklabels([f"S{idx + 1}" for idx in range(step_count)], fontsize=9)
    ax.set_yticks(list(range(row_count)))
    ax.set_yticklabels([f"R{idx + 1}" for idx in range(row_count)], fontsize=9)
    ax.set_xlabel("Step column")
    ax.set_ylabel("Row index")

    ax.set_xticks([idx - 0.5 for idx in range(step_count + 1)], minor=True)
    ax.set_yticks([idx - 0.5 for idx in range(row_count + 1)], minor=True)
    ax.grid(which="minor", color="#e2e8f0", linestyle="-", linewidth=0.8)
    ax.tick_params(which="minor", bottom=False, left=False)

    for row_index, row in enumerate(matrix):
        for col_index, cell in enumerate(row):
            ax.text(
                col_index,
                row_index,
                str(cell),
                ha="center",
                va="center",
                fontsize=8.5,
                color="#0f172a",
                fontweight="bold",
            )

    preview_title = "Candidate material matrix preview"
    if assignment_index is not None and candidate_index is not None:
        preview_title = f"Candidate material matrix preview: A{assignment_index} / C{candidate_index}"
    elif assignment_index is not None:
        preview_title = f"Candidate material matrix preview: A{assignment_index}"
    ax.set_title(preview_title, fontsize=14, fontweight="bold", pad=14)

    legend_text = "\n".join(f"{label}: {idx + 1}" for idx, label in enumerate(labels))
    fig.text(
        0.82,
        0.5,
        legend_text,
        ha="left",
        va="center",
        fontsize=9,
        family="monospace",
        bbox=dict(boxstyle="round,pad=0.45", facecolor="white", edgecolor="#cbd5e1", linewidth=1.0),
    )
    fig.colorbar(image, ax=ax, fraction=0.035, pad=0.03, ticks=list(range(len(labels))))
    fig.tight_layout(rect=[0.0, 0.0, 0.79, 1.0])
    fig.savefig(output_path, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)


def render_step_material_candidate_gallery(
    output_dir: Path,
    total_length_mm: float,
    filament_diameter_mm: float,
    assignment_summary: list[dict],
    candidate_matrices: list[list[list[list[str]]]] | None,
    show: bool = False,
) -> list[dict]:
    if plt is None:
        return []
    if not assignment_summary or not candidate_matrices:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows: list[dict] = []
    max_candidate_count = max((len(items) for items in candidate_matrices), default=0)

    for candidate_pos in range(1, max_candidate_count + 1):
        candidate_path = output_dir / f"candidate_{candidate_pos:04d}.png"
        render_assignment_candidate_coverage_preview(
            candidate_path,
            total_length_mm,
            filament_diameter_mm,
            assignment_summary,
            candidate_matrices,
            candidate_index=candidate_pos,
            show=False,
        )
        manifest_rows.append(
            {
                "candidate_index": candidate_pos,
                "path": str(candidate_path),
            }
        )

    manifest_path = output_dir / "candidate_gallery_index.json"
    manifest_path.write_text(json.dumps(manifest_rows, ensure_ascii=True, indent=2), encoding="utf-8")
    if show:
        print(f"Candidate gallery written to {output_dir}")
    return manifest_rows


def render_step_material_candidate_raw_gallery(
    output_dir: Path,
    total_length_mm: float,
    filament_diameter_mm: float,
    assignment_summary: list[dict],
    row_count: int = 14,
    candidate_count: int | None = None,
    max_images: int = 100,
    show: bool = False,
) -> list[dict]:
    if plt is None:
        return []
    if not assignment_summary:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_matrices = summarize_stepwise_candidate_matrices(
        assignment_summary,
        row_count=row_count,
        candidate_count=candidate_count,
    )
    candidate_ranges = [range(1, len(items) + 1) for items in candidate_matrices]
    if not candidate_ranges or any(len(rng) == 0 for rng in candidate_ranges):
        return []

    total_combo_count = 1
    for rng in candidate_ranges:
        total_combo_count *= len(rng)

    manifest_rows: list[dict] = []
    max_images = max(1, int(max_images))
    for combo_index, combo in enumerate(islice(product(*candidate_ranges), max_images), start=1):
        candidate_path = output_dir / f"candidate_combo_{combo_index:06d}.png"
        render_assignment_candidate_coverage_preview(
            candidate_path,
            total_length_mm,
            filament_diameter_mm,
            assignment_summary,
            candidate_matrices,
            candidate_index=1,
            candidate_indices_by_assignment=list(combo),
            show=False,
        )
        manifest_rows.append(
            {
                "combo_index": combo_index,
                "candidate_indices_by_assignment": list(combo),
                "path": str(candidate_path),
            }
        )

    manifest_path = output_dir / "candidate_raw_gallery_index.json"
    manifest_path.write_text(json.dumps(manifest_rows, ensure_ascii=True, indent=2), encoding="utf-8")
    if show:
        print(f"Raw candidate gallery written to {output_dir}")
    if total_combo_count > len(manifest_rows):
        print(
            f"Raw candidate gallery capped at {len(manifest_rows)} image(s) "
            f"out of {total_combo_count} total combination(s)"
        )
    return manifest_rows


def summarize_filament_by_assignment(property_json_path: Path, voxel_summary: list[dict]) -> list[dict]:
    assignments = load_assignment_records(property_json_path)
    voxel_map = {int(voxel["voxel_id"]): voxel for voxel in voxel_summary}
    total_voxel_e = float(sum(float(voxel["voxel_e"]) for voxel in voxel_summary)) or 1.0
    prefix_by_voxel_id: dict[int, float] = {}
    running_e = 0.0
    for voxel in sorted(voxel_summary, key=lambda item: int(item["voxel_id"])):
        voxel_id = int(voxel["voxel_id"])
        prefix_by_voxel_id[voxel_id] = running_e
        running_e += float(voxel["voxel_e"])

    if not assignments:
        assignments = [
            {
                "start_voxel": 1,
                "end_voxel": len(voxel_summary),
                "material_count": 1,
                "material_1": None,
                "material_2": None,
                "gradient_steps": 1,
                "gradient_direction": "layer",
                "mat_ratio_1": 100.0,
                "mat_ratio_2": 0.0,
                "brightness": None,
            }
        ]

    results: list[dict] = []
    for assignment_index, assignment in enumerate(assignments, start=1):
        start_voxel = int(assignment.get("start_voxel", 1))
        end_voxel = int(assignment.get("end_voxel", start_voxel))
        if end_voxel < start_voxel:
            start_voxel, end_voxel = end_voxel, start_voxel

        selected_voxels = [
            voxel_map[voxel_id]
            for voxel_id in range(start_voxel, end_voxel + 1)
            if voxel_id in voxel_map
        ]
        total_e = float(sum(float(voxel["voxel_e"]) for voxel in selected_voxels))
        total_length_mm = total_e
        material_count = int(assignment.get("material_count", 1))
        ratio_1 = float(assignment.get("mat_ratio_1", assignment.get("color_ratio_1", 100)))
        ratio_2 = float(assignment.get("mat_ratio_2", assignment.get("color_ratio_2", 0)))
        ratio_sum = ratio_1 + ratio_2
        if ratio_sum <= 1e-12:
            ratio_1, ratio_2, ratio_sum = 50.0, 50.0, 100.0

        start_prefix_e = prefix_by_voxel_id.get(start_voxel, 0.0)
        end_prefix_e = prefix_by_voxel_id.get(end_voxel, total_voxel_e)
        start_fraction = max(0.0, min(1.0, start_prefix_e / total_voxel_e))
        end_fraction = max(0.0, min(1.0, (end_prefix_e + float(voxel_map.get(end_voxel, {"voxel_e": 0.0})["voxel_e"])) / total_voxel_e))

        result = {
            "assignment_index": assignment_index,
            "start_voxel": start_voxel,
            "end_voxel": end_voxel,
            "start_fraction": round(start_fraction, 6),
            "end_fraction": round(end_fraction, 6),
            "center_fraction": round((start_fraction + end_fraction) * 0.5, 6),
            "voxel_count": len(selected_voxels),
            "total_filament_e_mm": round(total_e, 6),
            "total_filament_length_mm": round(total_length_mm, 6),
            "material_count": material_count,
            "material_1": assignment.get("material_1"),
            "material_2": assignment.get("material_2") if material_count >= 2 else None,
            "start_material_slot": assignment.get("start_material_slot"),
            "end_material_slot": assignment.get("end_material_slot"),
            "transition": assignment.get("transition"),
            "gradient_steps": int(assignment.get("gradient_steps", 1)),
            "gradient_direction": str(assignment.get("gradient_direction", "layer")),
            "direction": assignment.get("direction"),
            "eta": float(assignment.get("eta", 0.5)),
            "mat_ratio_1": ratio_1,
            "mat_ratio_2": ratio_2,
            "estimated_material_1_length_mm": round(total_e * (ratio_1 / ratio_sum), 6),
            "estimated_material_2_length_mm": round(
                total_e * (ratio_2 / ratio_sum) if material_count >= 2 else 0.0,
                6,
            ),
            "step_segments": build_assignment_step_segments(assignment, voxel_summary),
            "stepwise_selection": build_assignment_stepwise_material_selection(assignment, row_count=14)["step_choices"],
        }
        results.append(result)

    return results


def build_test_assignment_summary(voxel_summary: list[dict], chunk_size: int) -> list[dict]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")
    if not voxel_summary:
        return []

    results: list[dict] = []
    chunk_index = 0
    for start in range(0, len(voxel_summary), chunk_size):
        chunk = voxel_summary[start : start + chunk_size]
        chunk_index += 1
        start_voxel = int(chunk[0]["voxel_id"])
        end_voxel = int(chunk[-1]["voxel_id"])
        total_e = float(sum(float(voxel["voxel_e"]) for voxel in chunk))
        results.append(
            {
                "assignment_index": chunk_index,
                "start_voxel": start_voxel,
                "end_voxel": end_voxel,
                "voxel_count": len(chunk),
                "total_filament_e_mm": round(total_e, 6),
                "total_filament_length_mm": round(total_e, 6),
                "source": "test_split_from_voxel_summary",
            }
        )
    return results


def split_assignment_summary_in_half(
    assignment_summary: list[dict],
    voxel_summary: list[dict],
) -> list[dict]:
    if not assignment_summary:
        return []

    voxel_map = {int(voxel["voxel_id"]): voxel for voxel in voxel_summary}
    results: list[dict] = []

    for assignment in assignment_summary:
        start_voxel = int(assignment.get("start_voxel", 1))
        end_voxel = int(assignment.get("end_voxel", start_voxel))
        selected_voxels = [
            voxel_map[voxel_id]
            for voxel_id in range(start_voxel, end_voxel + 1)
            if voxel_id in voxel_map
        ]
        if len(selected_voxels) <= 1:
            results.append(dict(assignment))
            continue

        total_e = float(sum(float(voxel["voxel_e"]) for voxel in selected_voxels))
        total_length_mm = float(assignment.get("total_filament_length_mm", total_e))
        base_left = float(assignment.get("estimated_material_1_length_mm", assignment.get("estimated_material_1_e_mm", 0.0)))
        base_right = float(assignment.get("estimated_material_2_length_mm", assignment.get("estimated_material_2_e_mm", 0.0)))

        half_target = total_e * 0.5
        left_voxels: list[dict] = []
        left_e = 0.0
        split_index = 0
        for idx, voxel in enumerate(selected_voxels):
            voxel_e = float(voxel["voxel_e"])
            if left_e < half_target and left_e + voxel_e > half_target and idx + 1 < len(selected_voxels):
                split_index = idx + 1
                break
            left_voxels.append(voxel)
            left_e += voxel_e
            split_index = idx + 1

        if split_index <= 0 or split_index >= len(selected_voxels):
            split_index = max(1, len(selected_voxels) // 2)
            left_voxels = selected_voxels[:split_index]
            left_e = float(sum(float(voxel["voxel_e"]) for voxel in left_voxels))

        right_voxels = selected_voxels[split_index:]
        right_e = float(sum(float(voxel["voxel_e"]) for voxel in right_voxels))

        left_start = int(left_voxels[0]["voxel_id"])
        left_end = int(left_voxels[-1]["voxel_id"])
        right_start = int(right_voxels[0]["voxel_id"]) if right_voxels else left_end
        right_end = int(right_voxels[-1]["voxel_id"]) if right_voxels else left_end

        start_fraction = float(assignment.get("start_fraction", 0.0))
        end_fraction = float(assignment.get("end_fraction", 1.0))
        mid_fraction = (start_fraction + end_fraction) * 0.5
        left_fraction = left_e / total_e if total_e > 0 else 0.5
        right_fraction = right_e / total_e if total_e > 0 else 0.5

        left_entry = dict(assignment)
        left_entry.update(
            {
                "split_part": "left",
                "start_voxel": left_start,
                "end_voxel": left_end,
                "voxel_count": len(left_voxels),
                "start_fraction": round(start_fraction, 6),
                "end_fraction": round(mid_fraction, 6),
                "center_fraction": round((start_fraction + mid_fraction) * 0.5, 6),
                "total_filament_e_mm": round(left_e, 6),
                "total_filament_length_mm": round(left_e, 6),
                "estimated_material_1_length_mm": round(base_left * left_fraction, 6),
                "estimated_material_2_length_mm": round(base_right * left_fraction, 6),
            }
        )

        right_entry = dict(assignment)
        right_entry.update(
            {
                "split_part": "right",
                "start_voxel": right_start,
                "end_voxel": right_end,
                "voxel_count": len(right_voxels),
                "start_fraction": round(mid_fraction, 6),
                "end_fraction": round(end_fraction, 6),
                "center_fraction": round((mid_fraction + end_fraction) * 0.5, 6),
                "total_filament_e_mm": round(right_e, 6),
                "total_filament_length_mm": round(right_e, 6),
                "estimated_material_1_length_mm": round(base_left * right_fraction, 6),
                "estimated_material_2_length_mm": round(base_right * right_fraction, 6),
            }
        )

        results.extend([left_entry, right_entry])

    return results


def build_assignment_position_preview(assignments: list[dict], voxel_count: int) -> list[dict]:
    if voxel_count <= 0:
        return []
    preview: list[dict] = []
    for assignment in assignments:
        start_voxel = int(assignment.get("start_voxel", 1))
        end_voxel = int(assignment.get("end_voxel", start_voxel))
        if end_voxel < start_voxel:
            start_voxel, end_voxel = end_voxel, start_voxel
        start_fraction = max(0.0, min(1.0, (start_voxel - 1) / voxel_count))
        end_fraction = max(0.0, min(1.0, end_voxel / voxel_count))
        preview.append(
            {
                "assignment_index": int(assignment.get("assignment_index", len(preview) + 1)),
                "start_voxel": start_voxel,
                "end_voxel": end_voxel,
                "start_fraction": round(start_fraction, 6),
                "end_fraction": round(end_fraction, 6),
                "center_fraction": round((start_fraction + end_fraction) * 0.5, 6),
            }
        )
    return preview


def render_filament_rectangle_preview(
    output_path: Path,
    total_length_mm: float,
    filament_diameter_mm: float,
    assignment_summary: list[dict] | None,
    show: bool = False,
) -> None:
    if plt is None or Rectangle is None:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 6.2), dpi=180)
    ax.set_facecolor("#f8fafc")
    ax.set_position([0.07, 0.64, 0.90, 0.26])
    legend_ax = fig.add_axes([0.07, 0.06, 0.90, 0.22], facecolor="white")
    legend_ax.axis("off")

    ax.add_patch(
        Rectangle(
            (0.0, 0.0),
            total_length_mm,
            filament_diameter_mm,
            facecolor="#e2e8f0",
            edgecolor="#334155",
            linewidth=2.0,
        )
    )

    if assignment_summary:
        colors = plt.get_cmap("tab20").colors
        legend_lines: list[str] = []
        for idx, assignment in enumerate(assignment_summary):
            start_fraction = float(assignment.get("start_fraction", 0.0))
            end_fraction = float(assignment.get("end_fraction", 0.0))
            center_fraction = float(assignment.get("center_fraction", (start_fraction + end_fraction) * 0.5))
            x0 = total_length_mm * start_fraction
            width = max(total_length_mm * (end_fraction - start_fraction), 0.0)
            if width <= 0:
                width = max(total_length_mm * 0.001, 1e-6)
            color = colors[idx % len(colors)]
            material_1 = str(assignment.get("material_1") or "None")
            material_2 = str(assignment.get("material_2") or "None")
            steps = int(assignment.get("gradient_steps", 1))
            eta_value = float(assignment.get("eta", 0.5))
            assignment_id = int(assignment.get("assignment_index", idx + 1))
            step_segments = assignment.get("step_segments") if isinstance(assignment.get("step_segments"), list) else []
            ax.add_patch(
                Rectangle(
                    (x0, 0.0),
                    width,
                    filament_diameter_mm,
                    facecolor="none",
                    edgecolor="#111827",
                    linewidth=2.4,
                    alpha=0.95,
                )
            )
            ax.add_patch(
                Rectangle(
                    (x0, 0.0),
                    width,
                    filament_diameter_mm,
                    facecolor=color,
                    edgecolor="#ffffff",
                    linewidth=0.8,
                    alpha=0.35,
                )
            )
            if step_segments:
                for step in step_segments:
                    step_start = float(step.get("start_fraction", 0.0))
                    step_end = float(step.get("end_fraction", step_start))
                    step_x0 = x0 + width * step_start
                    step_width = max(width * (step_end - step_start), 1e-6)
                    ax.add_patch(
                        Rectangle(
                            (step_x0, 0.0),
                            step_width,
                            filament_diameter_mm,
                            facecolor="none",
                            edgecolor="#ffffff",
                            linewidth=0.9,
                            alpha=0.95,
                        )
                    )
                for step in step_segments:
                    step_end = float(step.get("end_fraction", 0.0))
                    x_boundary = x0 + width * step_end
                    ax.plot(
                        [x_boundary, x_boundary],
                        [0.0, filament_diameter_mm],
                        color="#0f172a",
                        linewidth=0.7,
                        alpha=0.55,
                    )
            ax.plot([x0, x0], [0.0, filament_diameter_mm], color="#111827", linewidth=1.2, alpha=0.9)
            ax.plot([x0 + width, x0 + width], [0.0, filament_diameter_mm], color="#111827", linewidth=1.2, alpha=0.9)
            ax.text(
                x0 + width * 0.5,
                filament_diameter_mm * 0.56,
                f"A{assignment_id}",
                ha="center",
                va="center",
                fontsize=9.0,
                color="#0f172a",
                fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.20", facecolor="white", edgecolor="#94a3b8", alpha=0.92),
            )
            legend_lines.append(
                f"A{assignment_id}: ({material_1}, {material_2}) "
                f"(step: {steps}) (maximum eta: {eta_value:.2f})"
            )
            if step_segments:
                for step in step_segments:
                    step_index = int(step.get("step_index", 1))
                    step_len = float(step.get("total_filament_length_mm", 0.0))
                    step_start = float(step.get("start_fraction", 0.0))
                    step_end = float(step.get("end_fraction", step_start))
                    if "layer_start" in step:
                        layer_start = int(step.get("layer_start", 0))
                        layer_end = int(step.get("layer_end", layer_start))
                        legend_lines.append(
                            f"    s{step_index}: {step_len:.1f} mm (L{layer_start}-{layer_end})"
                        )
                    else:
                        legend_lines.append(
                            f"    s{step_index}: {step_len:.1f} mm ({step_start:.2f}-{step_end:.2f})"
                        )
            stepwise_selection = assignment.get("stepwise_selection") if isinstance(assignment.get("stepwise_selection"), list) else []
            if stepwise_selection:
                legend_lines.append("    step ratios / eta")
                for step_choice in stepwise_selection:
                    step_index = int(step_choice.get("step_index", 1))
                    target_r1 = float(step_choice.get("target_material_1_ratio", 0.0))
                    target_r2 = float(step_choice.get("target_material_2_ratio", 0.0))
                    actual_r1 = float(step_choice.get("actual_material_1_ratio", 0.0))
                    actual_r2 = float(step_choice.get("actual_material_2_ratio", 0.0))
                    eta_selected = float(step_choice.get("selected_eta", 0.0))
                    legend_lines.append(
                        f"    s{step_index}: t={target_r1:.1f}:{target_r2:.1f} "
                        f"a={actual_r1:.1f}:{actual_r2:.1f} eta={eta_selected:.2f}"
                    )
        if legend_lines:
            legend_ax.text(
                0.03,
                0.97,
                "\n".join(legend_lines),
                transform=legend_ax.transAxes,
                ha="left",
                va="top",
                fontsize=8.5,
                color="#0f172a",
                family="monospace",
                linespacing=1.35,
                bbox=dict(
                    boxstyle="round,pad=0.45",
                    facecolor="white",
                    edgecolor="#cbd5e1",
                    linewidth=1.1,
                    alpha=0.98,
                ),
            )
    ax.set_xlim(0.0, max(total_length_mm, 1e-6))
    ax.set_ylim(0.0, max(filament_diameter_mm, 1e-6))
    ax.set_xlabel("Filament length / extrusion amount (mm)")
    ax.set_ylabel("Diameter (mm)")
    fig.suptitle(
        "Total filament rectangle with assignment coverage",
        fontsize=16,
        fontweight="bold",
        y=1.03,
    )
    ax.text(
        0.01 * max(total_length_mm, 1e-6),
        filament_diameter_mm * 1.16,
        f"Width = total extrusion {total_length_mm:.3f} mm",
        fontsize=9,
        color="#475569",
    )
    ax.text(
        0.01 * max(total_length_mm, 1e-6),
        filament_diameter_mm * 1.04,
        f"Height = filament diameter {filament_diameter_mm:.3f} mm",
        fontsize=9,
        color="#475569",
    )
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout(rect=[0.0, 0.0, 0.68, 0.84])
    fig.savefig(output_path, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)


def render_filament_rectangle_material_preview(
    output_path: Path,
    total_length_mm: float,
    filament_diameter_mm: float,
    assignment_summary: list[dict] | None,
    candidate_matrices: list[list[list[list[str]]]] | None,
    candidate_index: int = 1,
    candidate_indices_by_assignment: list[int] | None = None,
    show: bool = False,
) -> None:
    if plt is None or Rectangle is None:
        return
    if not assignment_summary:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 2.8), dpi=180)
    ax.set_facecolor("#f8fafc")
    ax.set_position([0.07, 0.24, 0.58, 0.52])
    legend_ax = fig.add_axes([0.69, 0.20, 0.28, 0.58], facecolor="#fbfaf7")
    legend_ax.axis("off")

    ax.add_patch(
        Rectangle(
            (0.0, 0.0),
            total_length_mm,
            filament_diameter_mm,
            facecolor="#e2e8f0",
            edgecolor="#334155",
            linewidth=2.0,
        )
    )

    colors = plt.get_cmap("tab20").colors
    used_labels: list[str] = []
    selected_matrices: list[list[list[str]] | None] = []
    candidate_rank = max(1, int(candidate_index))
    for assignment_pos, assignment in enumerate(assignment_summary):
        matrix = None
        if candidate_matrices is not None and assignment_pos < len(candidate_matrices):
            assignment_candidates = candidate_matrices[assignment_pos]
            if assignment_candidates:
                if candidate_indices_by_assignment is not None and assignment_pos < len(candidate_indices_by_assignment):
                    candidate_pos = max(1, int(candidate_indices_by_assignment[assignment_pos])) - 1
                else:
                    candidate_pos = candidate_rank - 1
                candidate_pos = min(candidate_pos, len(assignment_candidates) - 1)
                matrix = assignment_candidates[candidate_pos]
        if matrix is None:
            matrix = build_assignment_step_material_matrix(
                assignment,
                row_count=14,
                candidate_rank=candidate_rank,
            )
        selected_matrices.append(matrix)
        if matrix:
            for row in matrix:
                for cell in row:
                    label = str(cell)
                    if label not in used_labels:
                        used_labels.append(label)

    if not used_labels:
        used_labels = ["None"]
    label_to_color = {label: colors[idx % len(colors)] for idx, label in enumerate(used_labels)}

    legend_lines: list[str] = []
    for idx, (assignment, matrix) in enumerate(zip(assignment_summary, selected_matrices), start=1):
        start_fraction = float(assignment.get("start_fraction", 0.0))
        end_fraction = float(assignment.get("end_fraction", 0.0))
        x0 = total_length_mm * start_fraction
        width = max(total_length_mm * (end_fraction - start_fraction), 0.0)
        if width <= 0:
            width = max(total_length_mm * 0.001, 1e-6)
        material_1 = str(assignment.get("material_1") or "None")
        material_2 = str(assignment.get("material_2") or "None")
        steps = int(assignment.get("gradient_steps", 1))
        eta_value = float(assignment.get("eta", 0.5))
        assignment_id = int(assignment.get("assignment_index", idx + 1))
        step_segments = assignment.get("step_segments") if isinstance(assignment.get("step_segments"), list) else []

        ax.add_patch(
            Rectangle(
                (x0, 0.0),
                width,
                filament_diameter_mm,
                facecolor="none",
                edgecolor="#111827",
                linewidth=2.4,
                alpha=0.95,
            )
        )

        if matrix:
            row_count = 14
            step_count = max((len(row) for row in matrix), default=0)
            if row_count > 0 and step_count > 0:
                cell_h = filament_diameter_mm / float(row_count)
                step_bounds: list[tuple[float, float]] = []
                if step_segments:
                    for step in step_segments[:step_count]:
                        step_start = max(0.0, min(1.0, float(step.get("start_fraction", 0.0))))
                        step_end = max(step_start, min(1.0, float(step.get("end_fraction", step_start))))
                        step_bounds.append((step_start, step_end))
                if len(step_bounds) < step_count:
                    for step_index in range(len(step_bounds), step_count):
                        step_start = step_index / float(step_count)
                        step_end = (step_index + 1) / float(step_count)
                        step_bounds.append((step_start, step_end))
                for col_index, (step_start, step_end) in enumerate(step_bounds[:step_count]):
                    step_x0 = x0 + width * step_start
                    col_width = max(width * (step_end - step_start), 1e-6)
                    step_center_x = step_x0 + col_width * 0.5
                    for row_index in range(row_count):
                        row = matrix[row_index] if row_index < len(matrix) else []
                        cell = row[col_index] if col_index < len(row) else (row[-1] if row else "None")
                        cell_y = filament_diameter_mm - (row_index + 1) * cell_h
                        ax.add_patch(
                            Rectangle(
                                (step_x0, cell_y),
                                col_width,
                                cell_h,
                                facecolor=label_to_color.get(str(cell), "#cbd5e1"),
                                edgecolor="white",
                                linewidth=0.6,
                                alpha=0.90,
                            )
                        )
                    ax.text(
                        step_center_x,
                        filament_diameter_mm * 1.02,
                        f"s{col_index + 1}",
                        ha="center",
                        va="bottom",
                        fontsize=8.2,
                        color="#111827",
                        fontweight="bold",
                        clip_on=False,
                        bbox=dict(boxstyle="round,pad=0.10", facecolor="white", edgecolor="#94a3b8", alpha=0.98),
                    )
                    if col_index < step_count - 1:
                        boundary_x = x0 + width * step_end
                        ax.plot(
                            [boundary_x, boundary_x],
                            [0.0, filament_diameter_mm],
                            color="#111827",
                            linewidth=1.8,
                            alpha=0.85,
                        )
                for row_boundary in range(1, row_count):
                    y = row_boundary * cell_h
                    ax.plot(
                        [x0, x0 + width],
                        [y, y],
                        color="white",
                        linewidth=0.8,
                        alpha=0.95,
                    )
            legend_lines.append(
                f"A{assignment_id}: ({material_1}, {material_2}) (step: {steps}) (maximum eta: {eta_value:.2f})"
            )
            if step_segments:
                for step in step_segments:
                    step_index = int(step.get("step_index", 1))
                    step_len = float(step.get("total_filament_length_mm", 0.0))
                    step_start = float(step.get("start_fraction", 0.0))
                    step_end = float(step.get("end_fraction", step_start))
                    if "layer_start" in step:
                        layer_start = int(step.get("layer_start", 0))
                        layer_end = int(step.get("layer_end", layer_start))
                        legend_lines.append(
                            f"    s{step_index}: {step_len:.1f} mm (L{layer_start}-{layer_end})"
                        )
                    else:
                        legend_lines.append(
                            f"    s{step_index}: {step_len:.1f} mm ({step_start:.2f}-{step_end:.2f})"
                        )
            stepwise_selection = assignment.get("stepwise_selection") if isinstance(assignment.get("stepwise_selection"), list) else []
            if stepwise_selection:
                legend_lines.append("    step ratios / eta")
                for step_choice in stepwise_selection:
                    step_index = int(step_choice.get("step_index", 1))
                    target_r1 = float(step_choice.get("target_material_1_ratio", 0.0))
                    target_r2 = float(step_choice.get("target_material_2_ratio", 0.0))
                    actual_r1 = float(step_choice.get("actual_material_1_ratio", 0.0))
                    actual_r2 = float(step_choice.get("actual_material_2_ratio", 0.0))
                    eta_selected = float(step_choice.get("selected_eta", 0.0))
                    legend_lines.append(
                        f"    s{step_index}: t={target_r1:.1f}:{target_r2:.1f} "
                        f"a={actual_r1:.1f}:{actual_r2:.1f} eta={eta_selected:.2f}"
                    )
            ax.text(
                x0 + width * 0.5,
                filament_diameter_mm * 1.11,
                f"A{assignment_id}",
                ha="center",
                va="bottom",
                fontsize=9.0,
                color="#111827",
                fontweight="bold",
                clip_on=False,
                bbox=dict(boxstyle="round,pad=0.12", facecolor="white", edgecolor="#64748b", alpha=0.98),
            )
        else:
            legend_lines.append(
                f"A{assignment_id}: no candidate matrix available, eta={eta_value:.2f}"
            )

    if used_labels:
        legend_lines.append("")
        legend_lines.append("Materials")
        for label in used_labels:
            legend_lines.append(f"{label}: {label_to_color[label]}")
    legend_ax.text(
        0.0,
        1.0,
        "\n".join(legend_lines),
        transform=legend_ax.transAxes,
        ha="left",
        va="top",
        fontsize=7.8,
        color="#0f172a",
        family="monospace",
        linespacing=1.18,
    )
    ax.set_xlim(0.0, max(total_length_mm, 1e-6))
    ax.set_ylim(0.0, max(filament_diameter_mm, 1e-6))
    ax.set_xlabel("Filament length / extrusion amount (mm)")
    ax.set_ylabel("Diameter (mm)")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.suptitle(
        "Total filament rectangle filled with candidate materials",
        fontsize=16,
        fontweight="bold",
        y=1.03,
    )
    fig.savefig(output_path, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)


def render_filament_rectangle_material_gallery(
    output_dir: Path,
    total_length_mm: float,
    filament_diameter_mm: float,
    assignment_summary: list[dict] | None,
    candidate_matrices: list[list[list[list[str]]]] | None,
    candidate_count: int = 10,
    show: bool = False,
) -> list[dict]:
    if plt is None or Rectangle is None:
        return []
    if not assignment_summary:
        return []
    if not candidate_matrices:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_ranges = [range(1, len(items) + 1) for items in candidate_matrices]
    if not candidate_ranges or any(len(rng) == 0 for rng in candidate_ranges):
        return []
    limit = max(1, int(candidate_count))

    manifest_rows: list[dict] = []
    for combo_index, combo in enumerate(islice(product(*candidate_ranges), limit), start=1):
        candidate_path = output_dir / f"vase_filament_rectangle_material_candidate_{combo_index:02d}.png"
        render_filament_rectangle_material_preview(
            candidate_path,
            total_length_mm,
            filament_diameter_mm,
            assignment_summary,
            candidate_matrices,
            candidate_index=1,
            candidate_indices_by_assignment=list(combo),
            show=False,
        )
        manifest_rows.append(
            {
                "candidate_index": combo_index,
                "candidate_indices_by_assignment": list(combo),
                "path": str(candidate_path),
            }
        )

    manifest_path = output_dir / "vase_filament_rectangle_material_gallery_index.json"
    manifest_path.write_text(json.dumps(manifest_rows, ensure_ascii=True, indent=2), encoding="utf-8")
    if show:
        print(f"Rectangle candidate gallery written to {output_dir}")
    return manifest_rows


def measure_total_filament_from_gcode(gcode_path: Path) -> tuple[float, str, float | None]:
    """
    Return the total positive extrusion distance in millimeters.

    The parser supports:
    - M82 absolute extrusion mode
    - M83 relative extrusion mode
    - G92 E resets
    - slicer report comments such as `;Filament used: 1.62855m`
    """
    text = gcode_path.read_text(encoding="utf-8", errors="ignore")

    reported_mm = parse_reported_filament_used_mm(text)
    reported_g = parse_reported_filament_used_g(text)
    if reported_mm is not None:
        return reported_mm, "comment_report", reported_g

    segments, _ = parse_gcode_extrusion_segments(gcode_path)
    total_e_mm = float(sum(float(seg["delta_e"]) for seg in segments))
    if total_e_mm <= 0 and reported_mm is None:
        raise ValueError(f"No extrusion commands found in {gcode_path}")

    return (reported_mm if reported_mm is not None else total_e_mm), "comment_report" if reported_mm is not None else "parsed_e_moves", reported_g


def build_filament_stats(
    gcode_path: Path,
    filament_diameter_mm: float = 1.75,
    density_g_cm3: float | None = None,
    voxel_threshold_e: float | None = None,
    property_json_path: Path | None = None,
    test_assignment_chunk_size: int | None = None,
    split_assignment_half: bool = False,
    candidate_eta_min: float | None = None,
    candidate_eta_max: float | None = None,
    candidate_eta_tolerance: float | None = None,
    build_candidate_outputs: bool = False,
    build_exhaustive_candidate_counts: bool = False,
    assignment_candidate_count: int | None = 10,
) -> FilamentStats:
    total_extrusion_mm, source, reported_g = measure_total_filament_from_gcode(gcode_path)
    cross_section_area_mm2 = math.pi * (filament_diameter_mm / 2.0) ** 2
    filament_volume_mm3 = total_extrusion_mm * cross_section_area_mm2
    filament_length_m = total_extrusion_mm / 1000.0
    filament_mass_g = None
    if density_g_cm3 is not None:
        filament_mass_g = filament_volume_mm3 * density_g_cm3 / 1000.0

    voxel_count = None
    voxel_total_e_mm = None
    voxel_summary = None
    if voxel_threshold_e is not None:
        segments, _ = parse_gcode_extrusion_segments(gcode_path)
        voxel_summary = summarize_voxel_bundles(segments, voxel_threshold_e)
        voxel_count = len(voxel_summary)
        voxel_total_e_mm = float(sum(float(voxel["voxel_e"]) for voxel in voxel_summary))

    assignment_summary = None
    test_assignment_summary = None
    split_assignment_summary = None
    step_material_summary = None
    step_material_analysis = None
    step_material_candidates = None
    step_material_candidate_total_count = None
    step_material_candidate_total_summary = None
    stepwise_exhaustive_candidate_combination_count = None
    step_material_candidate_analysis = None
    step_material_candidate_matrices = None
    step_material_candidate_eta_summary = None
    if test_assignment_chunk_size is not None:
        if voxel_summary is None:
            segments, _ = parse_gcode_extrusion_segments(gcode_path)
            voxel_summary = summarize_voxel_bundles(segments, voxel_threshold_e if voxel_threshold_e is not None else 2.0)
            voxel_count = len(voxel_summary)
            voxel_total_e_mm = float(sum(float(voxel["voxel_e"]) for voxel in voxel_summary))
        test_assignment_summary = build_test_assignment_summary(voxel_summary, test_assignment_chunk_size)

    if property_json_path is not None:
        if voxel_summary is None:
            segments, _ = parse_gcode_extrusion_segments(gcode_path)
            voxel_summary = summarize_voxel_bundles(segments, voxel_threshold_e if voxel_threshold_e is not None else 2.0)
            voxel_count = len(voxel_summary)
            voxel_total_e_mm = float(sum(float(voxel["voxel_e"]) for voxel in voxel_summary))
        assignment_summary = summarize_filament_by_assignment(property_json_path, voxel_summary)
        step_material_summary = summarize_step_material_matrices(assignment_summary, row_count=14)
        step_material_analysis = summarize_step_material_analysis(assignment_summary, row_count=14)
        step_material_candidate_matrices = summarize_stepwise_candidate_matrices(
            assignment_summary,
            row_count=14,
            candidate_count=assignment_candidate_count,
        )
        if build_candidate_outputs or build_exhaustive_candidate_counts:
            step_material_candidates = summarize_step_material_candidates(assignment_summary, row_count=14)
            step_material_candidate_total_summary = [
                {
                    "assignment_index": int(candidate.get("assignment_index", 0)),
                    "step_index": int(candidate.get("step_index") or 0),
                    "candidate_count": int(
                        sum(int(group.get("candidate_count", 0)) for group in candidate.get("groups", []))
                    ),
                }
                for candidate in step_material_candidates
            ]
            step_material_candidate_total_count = 0
            for row in step_material_candidate_total_summary:
                step_material_candidate_total_count += int(row.get("candidate_count", 0))
            stepwise_exhaustive_candidate_combination_count = count_exhaustive_stepwise_candidate_combinations(
                step_material_candidates
            )
        if build_candidate_outputs:
            step_material_candidate_analysis = summarize_step_material_candidate_analysis(assignment_summary, row_count=14)
            if candidate_eta_tolerance is not None:
                eta_summary_before = list(step_material_candidate_analysis)
                step_material_candidate_analysis = filter_candidate_rows_by_eta_tolerance(
                    step_material_candidate_analysis,
                    tolerance=float(candidate_eta_tolerance),
                )
                step_material_candidate_eta_summary = summarize_candidate_eta_filter(
                    eta_summary_before,
                    step_material_candidate_analysis,
                )
            elif candidate_eta_min is not None or candidate_eta_max is not None:
                eta_summary_before = list(step_material_candidate_analysis)
                step_material_candidate_analysis = filter_candidate_rows_by_eta(
                    step_material_candidate_analysis,
                    eta_min=candidate_eta_min,
                    eta_max=candidate_eta_max,
                )
                candidate_analysis_for_matrices = list(step_material_candidate_analysis)
                step_material_candidate_eta_summary = summarize_candidate_eta_filter(
                    eta_summary_before,
                    step_material_candidate_analysis,
                )
        if split_assignment_half:
            split_assignment_summary = split_assignment_summary_in_half(assignment_summary, voxel_summary)

    return FilamentStats(
        gcode_path=str(gcode_path.resolve()),
        extrusion_mode=source,
        total_extrusion_mm=round(total_extrusion_mm, 6),
        source=source,
        filament_diameter_mm=round(filament_diameter_mm, 6),
        cross_section_area_mm2=round(cross_section_area_mm2, 6),
        filament_volume_mm3=round(filament_volume_mm3, 6),
        filament_length_m=round(filament_length_m, 6),
        filament_mass_g=None if filament_mass_g is None else round(filament_mass_g, 6),
        gcode_reported_filament_used_g=None if reported_g is None else round(reported_g, 6),
        voxel_threshold_e=None if voxel_threshold_e is None else round(float(voxel_threshold_e), 6),
        voxel_count=voxel_count,
        voxel_total_e_mm=None if voxel_total_e_mm is None else round(voxel_total_e_mm, 6),
        voxel_summary=voxel_summary,
        assignment_summary=assignment_summary,
        test_assignment_summary=test_assignment_summary,
        split_assignment_summary=split_assignment_summary,
        step_material_summary=step_material_summary,
        step_material_analysis=step_material_analysis,
        step_material_candidates=step_material_candidates,
        step_material_candidate_total_count=step_material_candidate_total_count,
        step_material_candidate_total_summary=step_material_candidate_total_summary,
        stepwise_exhaustive_candidate_combination_count=stepwise_exhaustive_candidate_combination_count,
        step_material_candidate_analysis=step_material_candidate_analysis,
        step_material_candidate_matrices=step_material_candidate_matrices,
        step_material_candidate_eta_summary=step_material_candidate_eta_summary,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate total filament amount from a G-code file."
    )
    parser.add_argument("gcode", type=Path, help="Path to the source G-code file")
    parser.add_argument(
        "--diameter-mm",
        type=float,
        default=1.75,
        help="Filament diameter in mm (default: 1.75)",
    )
    parser.add_argument(
        "--density-g-cm3",
        type=float,
        default=None,
        help="Optional filament density in g/cm^3 for mass estimation",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional path to save the computed summary as JSON",
    )
    parser.add_argument(
        "--output-voxels-json",
        type=Path,
        default=None,
        help="Optional path to save the voxel bundle summary as JSON",
    )
    parser.add_argument(
        "--property-json",
        type=Path,
        default=None,
        help="Optional property program JSON to estimate assignment-level filament amounts",
    )
    parser.add_argument(
        "--output-assignments-json",
        type=Path,
        default=None,
        help="Optional path to save the assignment-level summary as JSON",
    )
    parser.add_argument(
        "--output-test-assignments-json",
        type=Path,
        default=None,
        help="Optional path to save a testing assignment summary split from the voxel bundles",
    )
    parser.add_argument(
        "--test-assignment-chunk-size",
        type=int,
        default=None,
        help="Chunk size used to build the test assignment summary",
    )
    parser.add_argument(
        "--output-rectangle-png",
        type=Path,
        default=None,
        help="Optional path to save a rectangle preview of the total filament length",
    )
    parser.add_argument(
        "--output-rectangle-material-png",
        type=Path,
        default=None,
        help="Optional path to save the same rectangle preview filled with candidate materials",
    )
    parser.add_argument(
        "--output-rectangle-material-gallery-dir",
        type=Path,
        default=None,
        help="Optional directory path to save the same rectangle preview for multiple candidate combinations",
    )
    parser.add_argument(
        "--rectangle-material-gallery-count",
        type=int,
        default=10,
        help="Number of rectangle preview variants to write when gallery mode is enabled",
    )
    parser.add_argument(
        "--assignment-candidate-count",
        type=int,
        default=10,
        help="Number of completed candidate matrices to keep per assignment (default: 10; use 0 for all ranked candidates)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show the rectangle preview with plt.show() after saving it",
    )
    parser.add_argument(
        "--split-assignment-half",
        action="store_true",
        help="Split each assignment into left/right voxel halves",
    )
    parser.add_argument(
        "--output-split-assignments-json",
        type=Path,
        default=None,
        help="Optional path to save the half-split assignment summary as JSON",
    )
    parser.add_argument(
        "--output-step-material-raw",
        type=Path,
        default=None,
        help="Optional path to save the 14 x step material matrices as a copy-paste friendly text file",
    )
    parser.add_argument(
        "--output-step-material-analysis",
        type=Path,
        default=None,
        help="Optional path to save the 48-slot material ratio analysis table as text",
    )
    parser.add_argument(
        "--output-step-material-candidates",
        type=Path,
        default=None,
        help="Optional path to save the candidate row-pattern table for all explored methods",
    )
    parser.add_argument(
        "--output-step-material-candidate-raw",
        type=Path,
        default=None,
        help="Optional path to save all candidate matrices in raw copy-paste form",
    )
    parser.add_argument(
        "--output-assignment-candidate-raw",
        type=Path,
        default=None,
        help="Optional path to save assignment-level candidate matrices as text",
    )
    parser.add_argument(
        "--output-full-filament-candidate-raw",
        type=Path,
        default=None,
        help="Optional path to save full 14 x all-assignment-step candidate matrices as text",
    )
    parser.add_argument(
        "--full-filament-candidate-count",
        type=int,
        default=100,
        help="Maximum number of full-filament candidate matrices to save (default: 100; use 0 for all)",
    )
    parser.add_argument(
        "--output-step-material-candidate-analysis",
        type=Path,
        default=None,
        help="Optional path to save the candidate combination analysis table as text",
    )
    parser.add_argument(
        "--output-step-material-candidate-eta-summary",
        type=Path,
        default=None,
        help="Optional path to save the eta filter count summary as text",
    )
    parser.add_argument(
        "--output-step-material-candidate-png",
        type=Path,
        default=None,
        help="Optional path to save a PNG preview of one candidate material matrix",
    )
    parser.add_argument(
        "--output-step-material-candidate-gallery-dir",
        type=Path,
        default=None,
        help="Optional directory path to save every candidate matrix as its own PNG",
    )
    parser.add_argument(
        "--output-step-material-candidate-raw-gallery-dir",
        type=Path,
        default=None,
        help="Optional directory path to save every raw candidate combination as its own PNG",
    )
    parser.add_argument(
        "--candidate-preview-assignment-index",
        type=int,
        default=1,
        help="1-based assignment index used for the candidate preview (default: 1)",
    )
    parser.add_argument(
        "--candidate-preview-candidate-index",
        type=int,
        default=1,
        help="1-based candidate index used for the candidate preview (default: 1)",
    )
    parser.add_argument(
        "--output-step-material-assignment-candidate-png",
        type=Path,
        default=None,
        help="Optional path to save a full-filament preview with assignment regions filled by candidate matrices",
    )
    parser.add_argument(
        "--output-step-material-assignment-shape-png",
        type=Path,
        default=None,
        help="Optional path to save a gallery preview showing each assignment's internal shape",
    )
    parser.add_argument(
        "--output-step-material-assignment-radial-png",
        type=Path,
        default=None,
        help="Optional path to save a radial cross-section preview for each assignment",
    )
    parser.add_argument(
        "--candidate-eta-min",
        type=float,
        default=None,
        help="Optional minimum eta_proxy used to filter candidate combinations",
    )
    parser.add_argument(
        "--candidate-eta-max",
        type=float,
        default=None,
        help="Optional maximum eta_proxy used to filter candidate combinations",
    )
    parser.add_argument(
        "--candidate-eta-tolerance",
        type=float,
        default=None,
        help="Optional absolute tolerance around assignment eta used to filter candidate combinations",
    )
    parser.add_argument(
        "--voxel-threshold-e",
        type=float,
        default=2.0,
        help="Cumulative E threshold for voxel bundling (default: 2.0)",
    )
    args = parser.parse_args()

    property_json_path = resolve_property_json_path(args.gcode, args.property_json)
    build_candidate_outputs = any(
        value is not None
        for value in (
            args.output_step_material_candidates,
            args.output_step_material_candidate_raw,
            args.output_step_material_candidate_analysis,
            args.output_step_material_candidate_eta_summary,
        )
    ) or args.candidate_eta_min is not None or args.candidate_eta_max is not None or args.candidate_eta_tolerance is not None
    build_exhaustive_candidate_counts = (
        build_candidate_outputs
    )
    assignment_candidate_count = int(args.assignment_candidate_count)
    assignment_candidate_count_arg = None if assignment_candidate_count <= 0 else assignment_candidate_count

    stats = build_filament_stats(
        args.gcode,
        args.diameter_mm,
        args.density_g_cm3,
        args.voxel_threshold_e,
        property_json_path,
        args.test_assignment_chunk_size,
        args.split_assignment_half,
        args.candidate_eta_min,
        args.candidate_eta_max,
        args.candidate_eta_tolerance,
        build_candidate_outputs,
        build_exhaustive_candidate_counts,
        assignment_candidate_count_arg,
    )

    print(f"G-code: {stats.gcode_path}")
    print(f"Source: {stats.source}")
    print(f"Total extrusion: {stats.total_extrusion_mm:.6f} mm")
    print(f"Total filament length: {stats.filament_length_m:.6f} m")
    print(f"Filament diameter: {stats.filament_diameter_mm:.3f} mm")
    print(f"Cross-section area: {stats.cross_section_area_mm2:.6f} mm^2")
    print(f"Estimated volume: {stats.filament_volume_mm3:.6f} mm^3")
    if stats.filament_mass_g is not None:
        print(f"Estimated mass: {stats.filament_mass_g:.6f} g")
    if stats.gcode_reported_filament_used_g is not None:
        print(f"G-code reported mass: {stats.gcode_reported_filament_used_g:.6f} g")
    print(f"Voxel threshold E: {stats.voxel_threshold_e:.6f}")
    print(f"Voxel count: {stats.voxel_count}")
    print(f"Voxel total E: {stats.voxel_total_e_mm:.6f} mm")
    if stats.assignment_summary is not None:
        print(f"Assignment count: {len(stats.assignment_summary)}")
    if stats.test_assignment_summary is not None:
        print(f"Test assignment count: {len(stats.test_assignment_summary)}")
    if stats.split_assignment_summary is not None:
        print(f"Split assignment count: {len(stats.split_assignment_summary)}")
    if stats.step_material_summary is not None:
        print(f"Step material matrix count: {len(stats.step_material_summary)}")
    if build_candidate_outputs and stats.step_material_candidate_total_count is not None:
        print(f"Exhaustive per-step candidate rows: {stats.step_material_candidate_total_count}")
    if build_candidate_outputs and stats.stepwise_exhaustive_candidate_combination_count is not None:
        print(
            "Exhaustive per-step full-filament combinations: "
            f"{stats.stepwise_exhaustive_candidate_combination_count}"
        )
    if build_candidate_outputs and stats.step_material_candidate_total_summary is not None:
        summary_text = ", ".join(
            f"A{row['assignment_index']}s{row.get('step_index', 0)}={row['candidate_count']}"
            for row in stats.step_material_candidate_total_summary
        )
        print(f"Candidate counts by assignment step: {summary_text}")
    if stats.step_material_candidate_matrices is not None and (
        args.output_full_filament_candidate_raw is not None
        or args.output_assignment_candidate_raw is not None
        or args.output_rectangle_material_gallery_dir is not None
    ):
        full_candidate_count = count_full_filament_candidate_combinations(stats.step_material_candidate_matrices)
        assignment_counts = summarize_assignment_candidate_counts(stats.step_material_candidate_matrices)
        assignment_counts_text = ", ".join(
            f"A{row['assignment_index']}={row['candidate_count']}" for row in assignment_counts
        )
        print(f"Assignment candidate counts: {assignment_counts_text}")
        combination_formula = " x ".join(
            f"A{row['assignment_index']}({row['candidate_count']})" for row in assignment_counts
        )
        print(f"Assignment-combination formula: {combination_formula}")
        print(f"Assignment-combination candidate count: {full_candidate_count}")
        if args.output_full_filament_candidate_raw is not None:
            requested_count = int(args.full_filament_candidate_count)
            if requested_count <= 0:
                print(f"Full-filament candidate matrices to write: {full_candidate_count} of {full_candidate_count}")
            else:
                print(
                    "Full-filament candidate matrices to write: "
                    f"{min(full_candidate_count, requested_count)} of {full_candidate_count}"
                )
        if args.output_rectangle_material_gallery_dir is not None:
            print(
                "Rectangle candidate images to write from assignment combinations: "
                f"{min(full_candidate_count, max(1, int(args.rectangle_material_gallery_count)))} of {full_candidate_count}"
            )

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(asdict(stats), indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
    if args.output_voxels_json is None and stats.voxel_summary is not None:
        args.output_voxels_json = args.gcode.with_name(f"{args.gcode.stem}_voxel_summary.json")
    if args.output_voxels_json is not None:
        if stats.voxel_summary is None:
            raise ValueError("--output-voxels-json requires --voxel-threshold-e")
        args.output_voxels_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_voxels_json.write_text(
            json.dumps(stats.voxel_summary, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
    if args.output_assignments_json is None and stats.assignment_summary is not None:
        args.output_assignments_json = args.gcode.with_name(f"{args.gcode.stem}_assignment_summary.json")
    if args.output_assignments_json is not None:
        if stats.assignment_summary is None:
            raise ValueError("--output-assignments-json requires a property JSON in DM_filament_model ver4")
        args.output_assignments_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_assignments_json.write_text(
            json.dumps(stats.assignment_summary, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
    if args.output_test_assignments_json is None and stats.test_assignment_summary is not None:
        args.output_test_assignments_json = args.gcode.with_name(f"{args.gcode.stem}_test_assignment_summary.json")
    if args.output_test_assignments_json is not None:
        if stats.test_assignment_summary is None:
            raise ValueError("--output-test-assignments-json requires --test-assignment-chunk-size")
        args.output_test_assignments_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_test_assignments_json.write_text(
            json.dumps(stats.test_assignment_summary, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
    if args.output_split_assignments_json is None and stats.split_assignment_summary is not None:
        args.output_split_assignments_json = args.gcode.with_name(f"{args.gcode.stem}_split_assignment_summary.json")
    if args.output_split_assignments_json is not None:
        if stats.split_assignment_summary is None:
            raise ValueError("--output-split-assignments-json requires --split-assignment-half")
        args.output_split_assignments_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_split_assignments_json.write_text(
            json.dumps(stats.split_assignment_summary, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
    if args.output_step_material_raw is None and stats.step_material_summary is not None:
        args.output_step_material_raw = args.gcode.with_name(f"{args.gcode.stem}_step_material_raw.txt")
    if args.output_step_material_raw is not None:
        if stats.step_material_summary is None:
            raise ValueError("--output-step-material-raw requires a property JSON")
        args.output_step_material_raw.parent.mkdir(parents=True, exist_ok=True)
        args.output_step_material_raw.write_text(
            format_material_name_matrix_raw(stats.step_material_summary),
            encoding="utf-8",
        )
    if args.output_step_material_analysis is not None:
        if stats.step_material_analysis is None:
            raise ValueError("--output-step-material-analysis requires a property JSON")
        args.output_step_material_analysis.parent.mkdir(parents=True, exist_ok=True)
        args.output_step_material_analysis.write_text(
            format_step_material_analysis_table(stats.step_material_analysis),
            encoding="utf-8",
        )
    if args.output_step_material_candidates is not None:
        if stats.step_material_candidates is None:
            raise ValueError("--output-step-material-candidates requires a property JSON")
        args.output_step_material_candidates.parent.mkdir(parents=True, exist_ok=True)
        args.output_step_material_candidates.write_text(
            format_step_material_candidates_table(stats.step_material_candidates),
            encoding="utf-8",
        )
    if args.output_step_material_candidate_raw is not None:
        if stats.step_material_candidates is None or stats.assignment_summary is None:
            raise ValueError("--output-step-material-candidate-raw requires a property JSON")
        args.output_step_material_candidate_raw.parent.mkdir(parents=True, exist_ok=True)
        args.output_step_material_candidate_raw.write_text(
            format_material_name_matrix_candidates_raw(
                stats.assignment_summary,
                stats.step_material_candidates,
                row_count=14,
            ),
            encoding="utf-8",
        )
    if args.output_assignment_candidate_raw is not None:
        if stats.step_material_candidate_matrices is None or stats.assignment_summary is None:
            raise ValueError("--output-assignment-candidate-raw requires a property JSON")
        args.output_assignment_candidate_raw.parent.mkdir(parents=True, exist_ok=True)
        args.output_assignment_candidate_raw.write_text(
            format_assignment_candidate_matrices_raw(
                stats.assignment_summary,
                stats.step_material_candidate_matrices,
            ),
            encoding="utf-8",
        )
    if args.output_full_filament_candidate_raw is not None:
        if stats.step_material_candidate_matrices is None or stats.assignment_summary is None:
            raise ValueError("--output-full-filament-candidate-raw requires a property JSON")
        args.output_full_filament_candidate_raw.parent.mkdir(parents=True, exist_ok=True)
        args.output_full_filament_candidate_raw.write_text(
            format_full_filament_candidate_matrices_raw(
                stats.assignment_summary,
                stats.step_material_candidate_matrices,
                max_candidates=args.full_filament_candidate_count,
                row_count=14,
            ),
            encoding="utf-8",
        )
    if args.output_step_material_candidate_analysis is not None:
        if stats.step_material_candidate_analysis is None:
            raise ValueError("--output-step-material-candidate-analysis requires a property JSON")
        args.output_step_material_candidate_analysis.parent.mkdir(parents=True, exist_ok=True)
        args.output_step_material_candidate_analysis.write_text(
            format_step_material_candidate_analysis_table(stats.step_material_candidate_analysis),
            encoding="utf-8",
        )
    if args.output_step_material_candidate_eta_summary is not None:
        if stats.step_material_candidate_eta_summary is None:
            raise ValueError("--output-step-material-candidate-eta-summary requires eta filtering")
        args.output_step_material_candidate_eta_summary.parent.mkdir(parents=True, exist_ok=True)
        args.output_step_material_candidate_eta_summary.write_text(
            format_candidate_eta_summary_table(stats.step_material_candidate_eta_summary),
            encoding="utf-8",
        )
    if args.output_step_material_candidate_png is not None:
        if stats.step_material_candidate_matrices is None:
            raise ValueError("--output-step-material-candidate-png requires a property JSON")
        candidate_matrix, assignment_index, candidate_index = select_candidate_matrix(
            stats.step_material_candidate_matrices,
            assignment_index=args.candidate_preview_assignment_index,
            candidate_index=args.candidate_preview_candidate_index,
        )
        if candidate_matrix is None:
            raise ValueError("--output-step-material-candidate-png could not find the requested candidate")
        render_step_material_candidate_preview(
            args.output_step_material_candidate_png,
            candidate_matrix,
            assignment_index=assignment_index,
            candidate_index=candidate_index,
            show=args.show,
        )
    if args.output_step_material_candidate_gallery_dir is not None:
        if stats.step_material_candidate_matrices is None or stats.assignment_summary is None:
            raise ValueError("--output-step-material-candidate-gallery-dir requires a property JSON")
        gallery_manifest = render_step_material_candidate_gallery(
            args.output_step_material_candidate_gallery_dir,
            stats.total_extrusion_mm,
            stats.filament_diameter_mm,
            stats.assignment_summary,
            stats.step_material_candidate_matrices,
            show=args.show,
        )
        print(f"Candidate gallery images: {len(gallery_manifest)}")
    if args.output_step_material_candidate_raw_gallery_dir is not None:
        if stats.assignment_summary is None:
            raise ValueError("--output-step-material-candidate-raw-gallery-dir requires a property JSON")
        raw_gallery_manifest = render_step_material_candidate_raw_gallery(
            args.output_step_material_candidate_raw_gallery_dir,
            stats.total_extrusion_mm,
            stats.filament_diameter_mm,
            stats.assignment_summary,
            row_count=14,
            show=args.show,
        )
        print(f"Raw candidate gallery images: {len(raw_gallery_manifest)}")
    if args.output_step_material_assignment_candidate_png is not None:
        if stats.step_material_candidate_matrices is None or stats.assignment_summary is None:
            raise ValueError("--output-step-material-assignment-candidate-png requires a property JSON")
        render_assignment_candidate_coverage_preview(
            args.output_step_material_assignment_candidate_png,
            stats.total_extrusion_mm,
            stats.filament_diameter_mm,
            stats.assignment_summary,
            stats.step_material_candidate_matrices,
            candidate_index=args.candidate_preview_candidate_index,
            show=args.show,
        )
    if args.output_step_material_assignment_shape_png is not None:
        if stats.step_material_candidate_matrices is None or stats.assignment_summary is None:
            raise ValueError("--output-step-material-assignment-shape-png requires a property JSON")
        render_assignment_shape_gallery_preview(
            args.output_step_material_assignment_shape_png,
            stats.assignment_summary,
            stats.step_material_candidate_matrices,
            candidate_index=args.candidate_preview_candidate_index,
            show=args.show,
        )
    if args.output_step_material_assignment_radial_png is not None:
        if stats.step_material_candidate_matrices is None or stats.assignment_summary is None:
            raise ValueError("--output-step-material-assignment-radial-png requires a property JSON")
        render_assignment_radial_preview(
            args.output_step_material_assignment_radial_png,
            stats.assignment_summary,
            stats.step_material_candidate_matrices,
            candidate_index=args.candidate_preview_candidate_index,
            row_weights=get_representative_row_weights(14),
            show=args.show,
        )
    if args.output_rectangle_png is not None:
        render_filament_rectangle_preview(
            args.output_rectangle_png,
            stats.total_extrusion_mm,
            stats.filament_diameter_mm,
            stats.assignment_summary,
            show=args.show,
        )
    if args.output_rectangle_material_png is not None:
        if stats.step_material_candidate_matrices is None or stats.assignment_summary is None:
            raise ValueError("--output-rectangle-material-png requires a property JSON")
        render_filament_rectangle_material_preview(
            args.output_rectangle_material_png,
            stats.total_extrusion_mm,
            stats.filament_diameter_mm,
            stats.assignment_summary,
            stats.step_material_candidate_matrices,
            candidate_index=args.candidate_preview_candidate_index,
            show=args.show,
        )
    if args.output_rectangle_material_gallery_dir is not None:
        if stats.step_material_candidate_matrices is None or stats.assignment_summary is None:
            raise ValueError("--output-rectangle-material-gallery-dir requires a property JSON")
        gallery_manifest = render_filament_rectangle_material_gallery(
            args.output_rectangle_material_gallery_dir,
            stats.total_extrusion_mm,
            stats.filament_diameter_mm,
            stats.assignment_summary,
            stats.step_material_candidate_matrices,
            candidate_count=args.rectangle_material_gallery_count,
            show=args.show,
        )
        print(f"Rectangle candidate images: {len(gallery_manifest)}")


if __name__ == "__main__":
    main()
