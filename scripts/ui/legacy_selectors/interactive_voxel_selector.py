"""
Interactive G-code Voxel Selector
완벽히 참조 인터페이스를 따르는 구현 (Gcode_Property_Program_Model_Designer 호환)
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Set

import numpy as np
import matplotlib.pyplot as plt
from matplotlib import colors as mcolors
from matplotlib.widgets import TextBox, Button
from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection


# ============================================================================
# G-code Parsing (from Gcode_Voxel_Analyzer)
# ============================================================================

MOVE_PATTERN = re.compile(r"^G(?:0|1)\b")
COORD_PATTERN = re.compile(r"([XYZE])([-+]?\d*\.?\d+)")
LAYER_INDEX_PATTERN = re.compile(r"^;LAYER:(-?\d+)\s*$")
LAYER_CHANGE_PATTERN = re.compile(r"^;LAYER_CHANGE\b")
Z_LAYER_PATTERN = re.compile(r"^;Z:([-+]?\d*\.?\d+)\s*$")


def parse_gcode_extrusion_segments(file_path: str) -> Tuple[List[Dict], float]:
    """Parse G-code file and extract extrusion segments"""
    segments: List[Dict] = []
    preprint_e = 0.0

    last_x = 0.0
    last_y = 0.0
    last_z = 0.0
    last_e = 0.0
    extrusion_absolute = True
    in_print_block = False
    current_layer = 0

    with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue

            # Track layer markers
            layer_index_match = LAYER_INDEX_PATTERN.search(line)
            if layer_index_match:
                current_layer = int(layer_index_match.group(1))
                if current_layer >= 0:
                    in_print_block = True
                continue
            if LAYER_CHANGE_PATTERN.match(line):
                in_print_block = True
                continue
            if Z_LAYER_PATTERN.match(line):
                in_print_block = True
                continue

            # Handle extrusion mode
            if line.startswith("M82"):
                extrusion_absolute = True
                continue
            if line.startswith("M83"):
                extrusion_absolute = False
                continue
            if line.startswith("G92"):
                coords = {axis: float(val) for axis, val in COORD_PATTERN.findall(line)}
                if "E" in coords:
                    last_e = coords["E"]
                if "X" in coords:
                    last_x = coords["X"]
                if "Y" in coords:
                    last_y = coords["Y"]
                if "Z" in coords:
                    last_z = coords["Z"]
                continue

            # Parse movement commands
            code = line.partition(";")[0].strip()
            if not code or not MOVE_PATTERN.match(code):
                continue

            command = code.partition(" ")[0]
            coords = {axis: float(val) for axis, val in COORD_PATTERN.findall(code)}

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

            moved = (abs(new_x - last_x) > 1e-9 or abs(new_y - last_y) > 1e-9 or abs(new_z - last_z) > 1e-9)
            xy_moved = (abs(new_x - last_x) > 1e-9 or abs(new_y - last_y) > 1e-9)

            if delta_e > 0 and moved and xy_moved:
                if in_print_block:
                    segment = {
                        "line_no": line_no,
                        "command": command,
                        "start": [float(last_x), float(last_y), float(last_z)],
                        "end": [float(new_x), float(new_y), float(new_z)],
                        "delta_e": float(delta_e),
                        "segment_index": len(segments),
                        "layer": current_layer,
                    }
                    segments.append(segment)
                else:
                    preprint_e += float(delta_e)

            last_x = new_x
            last_y = new_y
            last_z = new_z

    return segments, preprint_e


def finalize_voxel(
    voxel_id: int,
    voxel_segments: List[Dict],
    threshold_e: Optional[float],
    cumulative_before: float,
) -> Dict:
    """Create finalized voxel from segments"""
    voxel_e = float(sum(seg["delta_e"] for seg in voxel_segments))
    cumulative_after = cumulative_before + voxel_e

    first_seg = voxel_segments[0]
    last_seg = voxel_segments[-1]
    
    # Use the layer from the last segment (most updated layer for this voxel)
    # gcode layer 0-198 -> user layer 1-199
    gcode_layer = int(last_seg.get("layer", 0))
    voxel_layer = gcode_layer + 1

    return {
        "voxel_id": int(voxel_id),
        "threshold_e": None if threshold_e is None else float(threshold_e),
        "voxel_e": voxel_e,
        "cumulative_e_before": float(cumulative_before),
        "cumulative_e_after": float(cumulative_after),
        "segment_count": len(voxel_segments),
        "line_start": int(first_seg["line_no"]),
        "line_end": int(last_seg["line_no"]),
        "x_start": float(first_seg["start"][0]),
        "y_start": float(first_seg["start"][1]),
        "z_start": float(first_seg["start"][2]),
        "x_end": float(last_seg["end"][0]),
        "y_end": float(last_seg["end"][1]),
        "z_end": float(last_seg["end"][2]),
        "layer_num": voxel_layer,  # 1-indexed layer
        "segments": voxel_segments.copy(),
    }


def group_segments_into_voxels(segments: List[Dict], threshold_e: float) -> Tuple[List[Dict], np.ndarray]:
    """Group segments into voxels based on extrusion threshold"""
    if threshold_e <= 0:
        raise ValueError("threshold_e must be positive. Use minimal voxel mode for segment-level voxels.")

    voxels: List[Dict] = []
    flat_rows: List[List[float]] = []

    pending: List[Dict] = []
    pending_sum = 0.0
    cumulative_before = 0.0

    for segment in segments:
        pending.append(segment)
        pending_sum += float(segment["delta_e"])

        if pending_sum + 1e-12 >= threshold_e:
            voxel_id = len(voxels) + 1
            voxel = finalize_voxel(voxel_id, pending, threshold_e, cumulative_before)
            voxels.append(voxel)

            for seg in pending:
                flat_rows.append([
                    float(voxel_id),
                    float(seg["segment_index"]),
                    float(seg["line_no"]),
                    float(seg["start"][0]),
                    float(seg["start"][1]),
                    float(seg["start"][2]),
                    float(seg["end"][0]),
                    float(seg["end"][1]),
                    float(seg["end"][2]),
                    float(seg["delta_e"]),
                ])
            cumulative_before = voxel["cumulative_e_after"]
            pending = []
            pending_sum = 0.0

    if pending:
        voxel_id = len(voxels) + 1
        voxel = finalize_voxel(voxel_id, pending, threshold_e, cumulative_before)
        voxels.append(voxel)

        for seg in pending:
            flat_rows.append([
                float(voxel_id),
                float(seg["segment_index"]),
                float(seg["line_no"]),
                float(seg["start"][0]),
                float(seg["start"][1]),
                float(seg["start"][2]),
                float(seg["end"][0]),
                float(seg["end"][1]),
                float(seg["end"][2]),
                float(seg["delta_e"]),
            ])

    flat_array = np.array(flat_rows, dtype=float) if flat_rows else np.empty((0, 10), dtype=float)
    
    # Sort voxels by voxel_id for consistency
    voxels = sorted(voxels, key=lambda v: int(v["voxel_id"]))
    
    return voxels, flat_array


def group_segments_into_minimal_voxels(segments: List[Dict]) -> Tuple[List[Dict], np.ndarray]:
    """Create the smallest available voxels: one extrusion segment per voxel."""
    voxels: List[Dict] = []
    flat_rows: List[List[float]] = []
    cumulative_before = 0.0

    for segment in segments:
        voxel_id = len(voxels) + 1
        voxel = finalize_voxel(voxel_id, [segment], None, cumulative_before)
        voxels.append(voxel)
        flat_rows.append([
            float(voxel_id),
            float(segment["segment_index"]),
            float(segment["line_no"]),
            float(segment["start"][0]),
            float(segment["start"][1]),
            float(segment["start"][2]),
            float(segment["end"][0]),
            float(segment["end"][1]),
            float(segment["end"][2]),
            float(segment["delta_e"]),
        ])
        cumulative_before = voxel["cumulative_e_after"]

    flat_array = np.array(flat_rows, dtype=float) if flat_rows else np.empty((0, 10), dtype=float)
    return voxels, flat_array


def build_segment_path(
    segment_rows: np.ndarray,
    gap_tolerance: float = 1e-6,
) -> Tuple[List[float], List[float], List[float]]:
    """Build 3D path from segment rows"""
    if segment_rows.size == 0:
        return [], [], []

    x_path = [float(segment_rows[0, 3]), float(segment_rows[0, 6])]
    y_path = [float(segment_rows[0, 4]), float(segment_rows[0, 7])]
    z_path = [float(segment_rows[0, 5]), float(segment_rows[0, 8])]
    prev_end_x = float(segment_rows[0, 6])
    prev_end_y = float(segment_rows[0, 7])
    prev_end_z = float(segment_rows[0, 8])

    for row in segment_rows[1:]:
        start_x = float(row[3])
        start_y = float(row[4])
        start_z = float(row[5])
        end_x = float(row[6])
        end_y = float(row[7])
        end_z = float(row[8])

        # Check for gaps
        dx = abs(start_x - prev_end_x)
        dy = abs(start_y - prev_end_y)
        dz = abs(start_z - prev_end_z)
        gap = np.sqrt(dx * dx + dy * dy + dz * dz)

        if gap > gap_tolerance:
            x_path.append(float('nan'))
            y_path.append(float('nan'))
            z_path.append(float('nan'))

        x_path.append(start_x)
        y_path.append(start_y)
        z_path.append(start_z)
        x_path.append(end_x)
        y_path.append(end_y)
        z_path.append(end_z)

        prev_end_x = end_x
        prev_end_y = end_y
        prev_end_z = end_z

    return x_path, y_path, z_path


def set_axes_equal(ax) -> None:
    """Set 3D axes to equal scale"""
    x_limits = ax.get_xlim3d()
    y_limits = ax.get_ylim3d()
    z_limits = ax.get_zlim3d()

    x_range = abs(x_limits[1] - x_limits[0])
    y_range = abs(y_limits[1] - y_limits[0])
    z_range = abs(z_limits[1] - z_limits[0])

    max_range = max(x_range, y_range, z_range)
    mid_x = (x_limits[0] + x_limits[1]) * 0.5
    mid_y = (y_limits[0] + y_limits[1]) * 0.5
    mid_z = (z_limits[0] + z_limits[1]) * 0.5

    offset = max_range * 0.5
    ax.set_xlim3d(mid_x - offset, mid_x + offset)
    ax.set_ylim3d(mid_y - offset, mid_y + offset)
    ax.set_zlim3d(mid_z - offset, mid_z + offset)


# ============================================================================
# Voxel Data Management
# ============================================================================

def annotate_voxels_with_layers(voxels: List[Dict]) -> None:
    """Annotate voxels with layer information based on segment layer data"""
    # First try to use layer info from segments (highest priority)
    voxels_with_layer = [v for v in voxels if v.get("layer_num", 0) > 0]
    
    if len(voxels_with_layer) == len(voxels):
        # All voxels have layer info from segments
        max_layer = max(int(v.get("layer_num", 0)) for v in voxels)
        unique_layers = len(set(int(v.get("layer_num", 0)) for v in voxels))
        print(f"  Layer annotation: {unique_layers} unique layers from GCODE -> 1-{max_layer} layers")
        return
    
    # Fallback: use Z-center based layer annotation (for compatibility)
    z_centers = [
        round((float(voxel["z_start"]) + float(voxel["z_end"])) * 0.5, 8)
        for voxel in voxels
    ]
    unique_z_levels = sorted(set(z_centers))
    layer_lookup = {z_value: index + 1 for index, z_value in enumerate(unique_z_levels)}
    
    for voxel, z_center in zip(voxels, z_centers):
        if voxel.get("layer_num", 0) == 0:  # Only fill if not already set
            voxel["layer_num"] = int(layer_lookup[z_center])
    
    max_layer = max(int(v.get("layer_num", 0)) for v in voxels)
    print(f"  Layer annotation: {len(unique_z_levels)} unique Z levels -> 1-{max_layer} layers")


def build_voxel_lookup(voxels: List[Dict]) -> Dict[int, Dict]:
    """Create lookup dictionary for voxels by ID"""
    return {int(voxel["voxel_id"]): voxel for voxel in voxels}


def build_voxel_selection_cache(voxels: List[Dict]) -> Dict[str, np.ndarray]:
    """Build optimized selection cache"""
    sorted_voxels = sorted(voxels, key=lambda voxel: int(voxel["voxel_id"]))
    voxel_ids = np.array([int(voxel["voxel_id"]) for voxel in sorted_voxels], dtype=int)
    voxel_e = np.array([float(voxel["voxel_e"]) for voxel in sorted_voxels], dtype=float)
    cumulative_before = np.array([float(voxel["cumulative_e_before"]) for voxel in sorted_voxels], dtype=float)
    cumulative_after = np.array([float(voxel["cumulative_e_after"]) for voxel in sorted_voxels], dtype=float)
    layer_nums = np.array([int(voxel.get("layer_num", 0)) for voxel in sorted_voxels], dtype=int)
    prefix_e = np.concatenate(([0.0], np.cumsum(voxel_e, dtype=float)))

    return {
        "voxel_ids": voxel_ids,
        "voxel_e": voxel_e,
        "prefix_e": prefix_e,
        "cumulative_before": cumulative_before,
        "cumulative_after": cumulative_after,
        "layer_nums": layer_nums,
    }


def compute_selection_filament_e(
    selection_cache: Dict[str, np.ndarray],
    voxel_range: Tuple[int, int]
) -> Tuple[float, float, float]:
    """Compute filament amount for voxel range"""
    low, high = voxel_range
    voxel_ids = selection_cache["voxel_ids"]
    start_idx = int(np.searchsorted(voxel_ids, low, side="left"))
    end_idx = int(np.searchsorted(voxel_ids, high, side="right"))

    if start_idx >= end_idx:
        return 0.0, 0.0, 0.0

    prefix_e = selection_cache["prefix_e"]
    selected_e = float(prefix_e[end_idx] - prefix_e[start_idx])
    start_cumulative = float(selection_cache["cumulative_before"][start_idx])
    end_cumulative = float(selection_cache["cumulative_after"][end_idx - 1])

    return selected_e, start_cumulative, end_cumulative


def compute_selected_voxel_filament_e(
    selection_cache: Dict[str, np.ndarray],
    selected_voxel_ids: Set[int],
) -> Tuple[float, float, float]:
    """Compute filament amount for an arbitrary voxel ID set."""
    if not selected_voxel_ids:
        return 0.0, 0.0, 0.0

    voxel_ids = selection_cache["voxel_ids"]
    selected_ids = np.array(sorted(selected_voxel_ids), dtype=int)
    indices = np.searchsorted(voxel_ids, selected_ids, side="left")
    in_bounds = indices < voxel_ids.size
    valid = np.zeros(indices.shape, dtype=bool)
    valid[in_bounds] = voxel_ids[indices[in_bounds]] == selected_ids[in_bounds]

    if not np.any(valid):
        return 0.0, 0.0, 0.0

    valid_indices = indices[valid]
    selected_e = float(np.sum(selection_cache["voxel_e"][valid_indices], dtype=float))
    start_cumulative = float(selection_cache["cumulative_before"][valid_indices[0]])
    end_cumulative = float(selection_cache["cumulative_after"][valid_indices[-1]])

    return selected_e, start_cumulative, end_cumulative


def layers_from_voxel_range_cached(
    selection_cache: Dict[str, np.ndarray],
    voxel_range: Tuple[int, int]
) -> Optional[Tuple[int, int]]:
    """Get layer range from voxel range using cache"""
    low, high = voxel_range
    voxel_ids = selection_cache["voxel_ids"]
    start_idx = int(np.searchsorted(voxel_ids, low, side="left"))
    end_idx = int(np.searchsorted(voxel_ids, high, side="right"))

    if start_idx >= end_idx:
        return None

    layer_nums = selection_cache["layer_nums"][start_idx:end_idx]
    positive_layers = layer_nums[layer_nums > 0]

    if positive_layers.size == 0:
        return None

    return int(positive_layers.min()), int(positive_layers.max())


def layers_from_selected_voxels_cached(
    selection_cache: Dict[str, np.ndarray],
    selected_voxel_ids: Set[int],
) -> Optional[Tuple[int, int]]:
    """Get layer range from an arbitrary voxel ID set using cache."""
    if not selected_voxel_ids:
        return None

    voxel_ids = selection_cache["voxel_ids"]
    selected_ids = np.array(sorted(selected_voxel_ids), dtype=int)
    indices = np.searchsorted(voxel_ids, selected_ids, side="left")
    in_bounds = indices < voxel_ids.size
    valid = np.zeros(indices.shape, dtype=bool)
    valid[in_bounds] = voxel_ids[indices[in_bounds]] == selected_ids[in_bounds]

    if not np.any(valid):
        return None

    layer_nums = selection_cache["layer_nums"][indices[valid]]
    positive_layers = layer_nums[layer_nums > 0]

    if positive_layers.size == 0:
        return None

    return int(positive_layers.min()), int(positive_layers.max())


def voxel_bounds(voxel: Dict) -> Tuple[float, float, float, float, float, float]:
    """Return voxel path bounds as x_min, x_max, y_min, y_max, z_min, z_max."""
    x0 = float(voxel["x_start"])
    x1 = float(voxel["x_end"])
    y0 = float(voxel["y_start"])
    y1 = float(voxel["y_end"])
    z0 = float(voxel["z_start"])
    z1 = float(voxel["z_end"])
    return min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1), min(z0, z1), max(z0, z1)


def voxel_center(voxel: Dict) -> Tuple[float, float, float]:
    """Return center point for a voxel path."""
    x_min, x_max, y_min, y_max, z_min, z_max = voxel_bounds(voxel)
    return (
        (x_min + x_max) * 0.5,
        (y_min + y_max) * 0.5,
        (z_min + z_max) * 0.5,
    )


def spatial_box_bounds(
    first_voxel: Dict,
    second_voxel: Dict,
    tolerance: float = 1e-9,
) -> Tuple[float, float, float, float, float, float]:
    """Return x/y/z bounds for the rectangular region defined by two voxels."""
    first_bounds = voxel_bounds(first_voxel)
    second_bounds = voxel_bounds(second_voxel)
    return (
        min(first_bounds[0], second_bounds[0]) - tolerance,
        max(first_bounds[1], second_bounds[1]) + tolerance,
        min(first_bounds[2], second_bounds[2]) - tolerance,
        max(first_bounds[3], second_bounds[3]) + tolerance,
        min(first_bounds[4], second_bounds[4]) - tolerance,
        max(first_bounds[5], second_bounds[5]) + tolerance,
    )


def select_voxels_in_spatial_box(
    voxels: List[Dict],
    first_voxel: Dict,
    second_voxel: Dict,
    virtual_sample_cache: Optional[Dict[str, np.ndarray]] = None,
    tolerance: float = 1e-9,
) -> Set[int]:
    """Select voxels inside the box defined by two clicked voxels."""
    x_min, x_max, y_min, y_max, z_min, z_max = spatial_box_bounds(
        first_voxel,
        second_voxel,
        tolerance,
    )

    if virtual_sample_cache is not None and virtual_sample_cache.get("points", np.empty((0, 3))).size > 0:
        points = virtual_sample_cache["points"]
        voxel_ids = virtual_sample_cache["voxel_ids"]
        mask = (
            (x_min <= points[:, 0]) & (points[:, 0] <= x_max)
            & (y_min <= points[:, 1]) & (points[:, 1] <= y_max)
            & (z_min <= points[:, 2]) & (points[:, 2] <= z_max)
        )
        if np.any(mask):
            return set(int(voxel_id) for voxel_id in np.unique(voxel_ids[mask]))

    selected: Set[int] = set()
    for voxel in voxels:
        center_x, center_y, center_z = voxel_center(voxel)
        if (
            x_min <= center_x <= x_max
            and y_min <= center_y <= y_max
            and z_min <= center_z <= z_max
        ):
            selected.add(int(voxel["voxel_id"]))

    return selected


def build_virtual_voxel_sample_cache(
    voxels: List[Dict],
    sample_spacing_mm: float = 0.2,
) -> Dict[str, np.ndarray]:
    """Sample virtual print positions along voxel paths for denser area selection."""
    sample_spacing_mm = max(float(sample_spacing_mm), 1e-6)
    sample_points: List[Tuple[float, float, float]] = []
    sample_voxel_ids: List[int] = []

    for voxel in voxels:
        voxel_id = int(voxel["voxel_id"])
        segments = voxel.get("segments", [])
        if not segments:
            sample_points.append(voxel_center(voxel))
            sample_voxel_ids.append(voxel_id)
            continue

        for segment in segments:
            start_point = np.array(segment["start"], dtype=float)
            end_point = np.array(segment["end"], dtype=float)
            segment_vector = end_point - start_point
            segment_length = float(np.linalg.norm(segment_vector))
            step_count = max(1, int(np.ceil(segment_length / sample_spacing_mm)))

            for step_index in range(step_count + 1):
                ratio = step_index / step_count
                point = start_point + segment_vector * ratio
                sample_points.append((float(point[0]), float(point[1]), float(point[2])))
                sample_voxel_ids.append(voxel_id)

    points_array = (
        np.array(sample_points, dtype=float)
        if sample_points
        else np.empty((0, 3), dtype=float)
    )
    ids_array = (
        np.array(sample_voxel_ids, dtype=int)
        if sample_voxel_ids
        else np.empty((0,), dtype=int)
    )
    return {"points": points_array, "voxel_ids": ids_array}


def build_rectangular_region_grid(
    virtual_sample_cache: Dict[str, np.ndarray],
    box_size_mm: Tuple[float, float, float],
) -> Tuple[List[Dict], Dict[int, Dict], Dict[int, int]]:
    """Split the occupied shape into rectangular grid boxes and collect voxel IDs per box."""
    points = virtual_sample_cache.get("points", np.empty((0, 3), dtype=float))
    voxel_ids = virtual_sample_cache.get("voxel_ids", np.empty((0,), dtype=int))
    if points.size == 0 or voxel_ids.size == 0:
        return [], {}, {}

    box_size = np.array(box_size_mm, dtype=float)
    box_size = np.maximum(box_size, 1e-6)
    origin = np.min(points, axis=0)
    indices = np.floor((points - origin) / box_size).astype(int)

    region_voxels: Dict[Tuple[int, int, int], Set[int]] = {}
    voxel_region_hits: Dict[int, Dict[Tuple[int, int, int], int]] = {}

    for index_tuple, voxel_id in zip(map(tuple, indices), voxel_ids):
        voxel_id = int(voxel_id)
        region_voxels.setdefault(index_tuple, set()).add(voxel_id)
        voxel_region_hits.setdefault(voxel_id, {})
        voxel_region_hits[voxel_id][index_tuple] = voxel_region_hits[voxel_id].get(index_tuple, 0) + 1

    regions: List[Dict] = []
    region_lookup: Dict[int, Dict] = {}
    index_to_region_id: Dict[Tuple[int, int, int], int] = {}

    for region_id, index_tuple in enumerate(sorted(region_voxels), start=1):
        index_array = np.array(index_tuple, dtype=float)
        min_point = origin + index_array * box_size
        max_point = min_point + box_size
        voxel_id_list = sorted(region_voxels[index_tuple])
        region = {
            "region_id": int(region_id),
            "region_type": "grid_box",
            "grid_index": [int(value) for value in index_tuple],
            "box_bounds": {
                "x_min": float(min_point[0]),
                "x_max": float(max_point[0]),
                "y_min": float(min_point[1]),
                "y_max": float(max_point[1]),
                "z_min": float(min_point[2]),
                "z_max": float(max_point[2]),
            },
            "voxel_ids": voxel_id_list,
            "voxel_count": len(voxel_id_list),
        }
        regions.append(region)
        region_lookup[int(region_id)] = region
        index_to_region_id[index_tuple] = int(region_id)

    voxel_to_region: Dict[int, int] = {}
    for voxel_id, hits in voxel_region_hits.items():
        best_index = max(hits.items(), key=lambda item: item[1])[0]
        voxel_to_region[voxel_id] = index_to_region_id[best_index]

    return regions, region_lookup, voxel_to_region


def estimate_rectangular_region_size(voxels: List[Dict]) -> Tuple[float, float, float]:
    """Estimate region size from the current path-voxel footprint and layer pitch."""
    if not voxels:
        return 1.0, 1.0, 1.0

    x_spans = []
    y_spans = []
    z_centers = []
    z_by_layer: Dict[int, List[float]] = {}

    for voxel in voxels:
        x_min, x_max, y_min, y_max, z_min, z_max = voxel_bounds(voxel)
        x_span = x_max - x_min
        y_span = y_max - y_min
        if x_span > 1e-9:
            x_spans.append(float(x_span))
        if y_span > 1e-9:
            y_spans.append(float(y_span))
        z_center = round((z_min + z_max) * 0.5, 8)
        z_centers.append(z_center)
        layer_num = int(voxel.get("layer_num", 0))
        if layer_num > 0:
            z_by_layer.setdefault(layer_num, []).append(z_center)

    xy_samples = x_spans + y_spans
    xy_size = float(np.median(xy_samples)) if xy_samples else 1.0
    xy_size = float(np.clip(xy_size, 0.5, 4.0))

    if len(z_by_layer) > 1:
        unique_z = np.array(
            [float(np.median(values)) for _, values in sorted(z_by_layer.items())],
            dtype=float,
        )
    else:
        unique_z = np.array(sorted(set(z_centers)), dtype=float)
    if unique_z.size > 1:
        z_diffs = np.diff(unique_z)
        positive_diffs = z_diffs[z_diffs > 1e-9]
        z_size = float(np.median(positive_diffs)) if positive_diffs.size else 0.2
    else:
        z_size = 0.2
    z_size = float(np.clip(z_size * 4.0, 0.15, 1.0))

    return xy_size, xy_size, z_size


def estimate_face_selection_tolerance(voxels: List[Dict]) -> float:
    """Estimate a distance tolerance for selecting voxels close to a picked face."""
    if not voxels:
        return 1e-6

    sample_count = min(len(voxels), 2000)
    sample_indices = np.linspace(0, len(voxels) - 1, sample_count, dtype=int)
    diagonals = []

    for index in sample_indices:
        x_min, x_max, y_min, y_max, z_min, z_max = voxel_bounds(voxels[int(index)])
        diagonal = np.sqrt(
            (x_max - x_min) ** 2
            + (y_max - y_min) ** 2
            + (z_max - z_min) ** 2
        )
        if diagonal > 1e-9:
            diagonals.append(float(diagonal))

    if not diagonals:
        return 1e-6

    return max(1e-6, float(np.median(diagonals)) * 0.75)


def point_in_triangle_3d(
    point: np.ndarray,
    tri_a: np.ndarray,
    tri_b: np.ndarray,
    tri_c: np.ndarray,
    tolerance: float = 1e-6,
) -> bool:
    """Return True if a coplanar 3D point lies inside a triangle."""
    edge_0 = tri_b - tri_a
    edge_1 = tri_c - tri_a
    point_vec = point - tri_a

    dot_00 = float(np.dot(edge_0, edge_0))
    dot_01 = float(np.dot(edge_0, edge_1))
    dot_11 = float(np.dot(edge_1, edge_1))
    dot_20 = float(np.dot(point_vec, edge_0))
    dot_21 = float(np.dot(point_vec, edge_1))
    denom = dot_00 * dot_11 - dot_01 * dot_01

    if abs(denom) < 1e-12:
        return False

    bary_b = (dot_11 * dot_20 - dot_01 * dot_21) / denom
    bary_c = (dot_00 * dot_21 - dot_01 * dot_20) / denom
    bary_a = 1.0 - bary_b - bary_c

    return (
        bary_a >= -tolerance
        and bary_b >= -tolerance
        and bary_c >= -tolerance
        and bary_a <= 1.0 + tolerance
        and bary_b <= 1.0 + tolerance
        and bary_c <= 1.0 + tolerance
    )


def select_voxels_on_triangle_face(
    voxels: List[Dict],
    first_voxel: Dict,
    second_voxel: Dict,
    third_voxel: Dict,
    plane_tolerance: Optional[float] = None,
) -> Set[int]:
    """Select voxels near the triangular face defined by three clicked voxel centers."""
    tri_a = np.array(voxel_center(first_voxel), dtype=float)
    tri_b = np.array(voxel_center(second_voxel), dtype=float)
    tri_c = np.array(voxel_center(third_voxel), dtype=float)

    normal = np.cross(tri_b - tri_a, tri_c - tri_a)
    normal_norm = float(np.linalg.norm(normal))
    if normal_norm < 1e-12:
        return {
            int(first_voxel["voxel_id"]),
            int(second_voxel["voxel_id"]),
            int(third_voxel["voxel_id"]),
        }

    normal = normal / normal_norm
    tolerance = estimate_face_selection_tolerance(voxels) if plane_tolerance is None else plane_tolerance
    selected: Set[int] = set()

    for voxel in voxels:
        point = np.array(voxel_center(voxel), dtype=float)
        signed_distance = float(np.dot(point - tri_a, normal))
        if abs(signed_distance) > tolerance:
            continue

        projected = point - signed_distance * normal
        if point_in_triangle_3d(projected, tri_a, tri_b, tri_c):
            selected.add(int(voxel["voxel_id"]))

    return selected


def build_voxel_plot_cache(
    flat_segments: np.ndarray
) -> Tuple[np.ndarray, Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]]]:
    """Build rendering cache for voxel paths"""
    if flat_segments.size == 0:
        return np.empty((0,), dtype=int), {}

    voxel_ids = flat_segments[:, 0].astype(int)
    unique_ids = np.unique(voxel_ids)
    path_cache: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

    for voxel_id in unique_ids:
        voxel_rows = flat_segments[voxel_ids == voxel_id]
        x_path, y_path, z_path = build_segment_path(voxel_rows)
        path_cache[int(voxel_id)] = (np.array(x_path), np.array(y_path), np.array(z_path))

    return unique_ids, path_cache


def build_assignment_color_map(
    assignments: List[Dict]
) -> Dict[int, Tuple[float, float, float]]:
    """Build color map for assignments"""
    color_map: Dict[int, Tuple[float, float, float]] = {}

    accent_palette = [
        "#ef4444", "#3b82f6", "#10b981", "#f59e0b",
        "#8b5cf6", "#ec4899", "#14b8a6", "#f97316",
    ]

    for assignment_index, assignment in enumerate(assignments, start=1):
        accent = np.array(mcolors.to_rgb(
            accent_palette[(assignment_index - 1) % len(accent_palette)]
        ), dtype=float)

        # Apply slight brightness adjustment
        color = np.clip(accent * 0.95, 0.0, 1.0)
        color_tuple = (float(color[0]), float(color[1]), float(color[2]))

        if "voxel_ids" in assignment:
            voxel_ids = [int(voxel_id) for voxel_id in assignment.get("voxel_ids", [])]
        else:
            low = int(assignment.get("start_voxel", 0))
            high = int(assignment.get("end_voxel", -1))
            if high < low:
                low, high = high, low
            voxel_ids = list(range(low, high + 1))

        for voxel_id in voxel_ids:
            color_map[voxel_id] = color_tuple

    return color_map


def bounds_from_voxel_ids(
    voxel_lookup: Dict[int, Dict],
    voxel_ids: Set[int],
) -> Optional[Dict[str, float]]:
    """Compute a rectangular box that contains the given voxel paths."""
    bounds_list = [
        voxel_bounds(voxel_lookup[int(voxel_id)])
        for voxel_id in voxel_ids
        if int(voxel_id) in voxel_lookup
    ]

    if not bounds_list:
        return None

    return {
        "x_min": float(min(bounds[0] for bounds in bounds_list)),
        "x_max": float(max(bounds[1] for bounds in bounds_list)),
        "y_min": float(min(bounds[2] for bounds in bounds_list)),
        "y_max": float(max(bounds[3] for bounds in bounds_list)),
        "z_min": float(min(bounds[4] for bounds in bounds_list)),
        "z_max": float(max(bounds[5] for bounds in bounds_list)),
    }


def box_faces_from_bounds(bounds: Dict[str, float]) -> List[List[Tuple[float, float, float]]]:
    """Build 6 rectangular faces for a 3D box."""
    x0 = float(bounds["x_min"])
    x1 = float(bounds["x_max"])
    y0 = float(bounds["y_min"])
    y1 = float(bounds["y_max"])
    z0 = float(bounds["z_min"])
    z1 = float(bounds["z_max"])

    vertices = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    return [
        [vertices[index] for index in [0, 1, 2, 3]],
        [vertices[index] for index in [4, 5, 6, 7]],
        [vertices[index] for index in [0, 1, 5, 4]],
        [vertices[index] for index in [2, 3, 7, 6]],
        [vertices[index] for index in [1, 2, 6, 5]],
        [vertices[index] for index in [0, 3, 7, 4]],
    ]


def box_edges_from_bounds(bounds: Dict[str, float]) -> List[List[Tuple[float, float, float]]]:
    """Build 12 edge segments for a 3D box."""
    x0 = float(bounds["x_min"])
    x1 = float(bounds["x_max"])
    y0 = float(bounds["y_min"])
    y1 = float(bounds["y_max"])
    z0 = float(bounds["z_min"])
    z1 = float(bounds["z_max"])

    vertices = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    return [
        [vertices[start], vertices[end]]
        for start, end in [
            (0, 1), (1, 2), (2, 3), (3, 0),
            (4, 5), (5, 6), (6, 7), (7, 4),
            (0, 4), (1, 5), (2, 6), (3, 7),
        ]
    ]


def draw_rectangular_wire_boxes(
    ax,
    regions: List[Dict],
    color: str = "#6b7280",
    alpha: float = 0.16,
    linewidth: float = 0.25,
) -> None:
    """Draw many rectangular boxes as one lightweight wireframe collection."""
    segments: List[List[Tuple[float, float, float]]] = []
    for region in regions:
        segments.extend(box_edges_from_bounds(region["box_bounds"]))

    if not segments:
        return

    collection = Line3DCollection(
        segments,
        colors=[mcolors.to_rgba(color, alpha)],
        linewidths=linewidth,
    )
    ax.add_collection3d(collection)


def draw_rectangular_filled_boxes(
    ax,
    regions: List[Dict],
    color: str = "#6b7280",
    alpha: float = 0.32,
    edge_alpha: float = 0.10,
    linewidth: float = 0.08,
    facecolors: Optional[List] = None,
) -> None:
    """Draw many rectangular boxes as one translucent filled collection."""
    faces: List[List[Tuple[float, float, float]]] = []
    colors = []
    for region_index, region in enumerate(regions):
        faces.extend(box_faces_from_bounds(region["box_bounds"]))
        if facecolors is not None:
            colors.extend([facecolors[region_index]] * 6)

    if not faces:
        return

    if facecolors is None:
        colors = [mcolors.to_rgba(color, alpha)] * len(faces)

    collection = Poly3DCollection(
        faces,
        facecolors=colors,
        edgecolors=mcolors.to_rgba(color, edge_alpha),
        linewidths=linewidth,
    )
    ax.add_collection3d(collection)


def bounds_from_region_list(regions: List[Dict]) -> Optional[Dict[str, float]]:
    """Compute total bounds for a list of rectangular regions."""
    if not regions:
        return None

    return {
        "x_min": float(min(region["box_bounds"]["x_min"] for region in regions)),
        "x_max": float(max(region["box_bounds"]["x_max"] for region in regions)),
        "y_min": float(min(region["box_bounds"]["y_min"] for region in regions)),
        "y_max": float(max(region["box_bounds"]["y_max"] for region in regions)),
        "z_min": float(min(region["box_bounds"]["z_min"] for region in regions)),
        "z_max": float(max(region["box_bounds"]["z_max"] for region in regions)),
    }


def set_axes_from_bounds(ax, bounds: Dict[str, float]) -> None:
    """Set 3D axis limits from a bounds dictionary."""
    ax.set_xlim3d(float(bounds["x_min"]), float(bounds["x_max"]))
    ax.set_ylim3d(float(bounds["y_min"]), float(bounds["y_max"]))
    ax.set_zlim3d(float(bounds["z_min"]), float(bounds["z_max"]))
    set_axes_equal(ax)


def draw_rectangular_box(
    ax,
    bounds: Dict[str, float],
    color: str = "#ef4444",
    alpha: float = 0.14,
    linewidth: float = 1.8,
    label: Optional[str] = None,
    picker: Optional[float] = None,
):
    """Draw a translucent rectangular selection box on a 3D axis."""
    faces = box_faces_from_bounds(bounds)
    face_collection = Poly3DCollection(
        faces,
        facecolors=mcolors.to_rgba(color, alpha),
        edgecolors=mcolors.to_rgba(color, 0.95),
        linewidths=linewidth,
    )
    if picker is not None:
        face_collection.set_picker(picker)
    ax.add_collection3d(face_collection)

    if label:
        x_mid = (float(bounds["x_min"]) + float(bounds["x_max"])) * 0.5
        y_mid = (float(bounds["y_min"]) + float(bounds["y_max"])) * 0.5
        z_mid = float(bounds["z_max"])
        ax.text(x_mid, y_mid, z_mid, label, color=color, fontsize=9, fontweight="bold")

    return face_collection


def plot_voxels_on_axis(
    ax,
    voxel_plot_ids: np.ndarray,
    voxel_path_cache: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]],
    voxel_range: Tuple[int, int],
    color_range: Tuple[int, int],
    assignment_color_map: Optional[Dict[int, Tuple[float, float, float]]] = None,
    selected_voxel_ids: Optional[Set[int]] = None,
    selection_label: str = "Voxel range",
) -> Dict:
    """Plot voxels on 3D axis with proper styling. Returns dict mapping line artist to voxel_id"""
    ax.cla()
    line_to_voxel = {}  # Map from line artist to voxel_id

    if voxel_plot_ids.size == 0:
        ax.set_title("No voxel segments")
        return line_to_voxel

    min_voxel, max_voxel = voxel_range
    in_range = (voxel_plot_ids >= min_voxel) & (voxel_plot_ids <= max_voxel)
    explicit_selection = selected_voxel_ids is not None and len(selected_voxel_ids) > 0

    norm = mcolors.Normalize(vmin=color_range[0], vmax=max(color_range[1], color_range[0] + 1))
    cmap = plt.get_cmap("viridis")

    # Plot background voxels
    if explicit_selection:
        selected_id_array = np.array(sorted(selected_voxel_ids), dtype=int)
        selected_mask = np.isin(voxel_plot_ids, selected_id_array)
        background_mask = ~selected_mask
    else:
        selected_id_array = voxel_plot_ids[in_range]
        background_mask = ~in_range

    background_ids = voxel_plot_ids[background_mask]
    background_limit = 5000 if explicit_selection else 600
    if background_ids.size > background_limit:
        step = max(1, int(np.ceil(background_ids.size / background_limit)))
        background_ids = background_ids[::step]

    for voxel_id in background_ids:
        if int(voxel_id) not in voxel_path_cache:
            continue
        x_path, y_path, z_path = voxel_path_cache[int(voxel_id)]
        if len(x_path) > 1:
            line = ax.plot(x_path, y_path, z_path, color="lightgray", linewidth=0.4, alpha=0.15, picker=5)[0]
            line_to_voxel[line] = int(voxel_id)

    # Plot selected voxels. Large minimal-unit views can contain tens of thousands
    # of segments, so rendering is sampled while selection/statistics stay exact.
    plotted_selected_ids = selected_id_array
    selected_limit = 10000 if explicit_selection else 5000
    if plotted_selected_ids.size > selected_limit:
        step = max(1, int(np.ceil(plotted_selected_ids.size / selected_limit)))
        plotted_selected_ids = plotted_selected_ids[::step]

    for voxel_id in plotted_selected_ids:
        if int(voxel_id) not in voxel_path_cache:
            continue
        x_path, y_path, z_path = voxel_path_cache[int(voxel_id)]
        if len(x_path) > 1:
            # Shadow line
            ax.plot(x_path, y_path, z_path, color="#111827", linewidth=2.4, alpha=0.18, picker=5)

            # Main line
            line_color = cmap(norm(voxel_id))
            line_width = 1.35
            line_alpha = 0.98

            if explicit_selection:
                line_color = "#ef4444"
                line_width = 2.2
                line_alpha = 1.0
            elif assignment_color_map is not None and int(voxel_id) in assignment_color_map:
                line_color = assignment_color_map[int(voxel_id)]
                line_width = 1.8
                line_alpha = 1.0

            line = ax.plot(x_path, y_path, z_path, color=line_color, linewidth=line_width, alpha=line_alpha, picker=5)[0]
            line_to_voxel[line] = int(voxel_id)

    title = f"{selection_label} {min_voxel} - {max_voxel}"
    if explicit_selection:
        title = f"{selection_label}: {len(selected_voxel_ids)} voxels (ID span {min_voxel} - {max_voxel})"

    ax.set_title(
        title,
        pad=10, fontsize=13, color="#111827", fontweight="bold"
    )
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.set_zlabel("")
    set_axes_equal(ax)
    
    return line_to_voxel


def format_assignment_stats(
    assignments: List[Dict],
    voxel_range: Tuple[int, int],
    voxel_lookup: Dict[int, Dict],
    selected_layer_range: Optional[Tuple[int, int]],
    preprint_e: float,
    selected_e: float,
    start_cumulative: float,
    end_cumulative: float,
    selected_voxel_ids: Optional[Set[int]] = None,
    selection_mode_label: str = "CURRENT RANGE",
    current_region: Optional[Dict] = None,
) -> str:
    """Format assignment statistics text"""
    low, high = voxel_range
    selected_ids = sorted(selected_voxel_ids) if selected_voxel_ids else []
    voxel_count = len(selected_ids) if selected_ids else max(0, high - low + 1)

    lines = [
        "PREHEAT/PRIME E",
        f"{preprint_e:.6f}",
        "",
        selection_mode_label,
        f"V{low} - V{high}",
        f"Voxel Count: {voxel_count}",
        f"Selected E: {selected_e:.6f}",
        f"Cumul E: {start_cumulative:.6f} - {end_cumulative:.6f}",
        "",
        "VOXEL INFO",
    ]

    if selected_ids:
        preview_ids = ", ".join(f"V{voxel_id}" for voxel_id in selected_ids[:12])
        if len(selected_ids) > 12:
            preview_ids += f", ... +{len(selected_ids) - 12}"
        lines.append(f"Selected IDs: {preview_ids}")

    if selected_layer_range is not None:
        lines.append(f"Layer Range: L{selected_layer_range[0]} - L{selected_layer_range[1]}")
    else:
        lines.append("Layer Range: none")

    if low in voxel_lookup:
        start_voxel = voxel_lookup[low]
        start_center = (
            (float(start_voxel["x_start"]) + float(start_voxel["x_end"])) * 0.5,
            (float(start_voxel["y_start"]) + float(start_voxel["y_end"])) * 0.5,
            (float(start_voxel["z_start"]) + float(start_voxel["z_end"])) * 0.5,
        )
        lines.extend([
            f"Start Center: ({start_center[0]:.3f}, {start_center[1]:.3f}, {start_center[2]:.3f})",
            f"Start Voxel E: {float(start_voxel['voxel_e']):.6f}",
        ])

    if high != low and high in voxel_lookup:
        end_voxel = voxel_lookup[high]
        end_center = (
            (float(end_voxel["x_start"]) + float(end_voxel["x_end"])) * 0.5,
            (float(end_voxel["y_start"]) + float(end_voxel["y_end"])) * 0.5,
            (float(end_voxel["z_start"]) + float(end_voxel["z_end"])) * 0.5,
        )
        lines.extend([
            f"End Center:   ({end_center[0]:.3f}, {end_center[1]:.3f}, {end_center[2]:.3f})",
            f"End Voxel E:  {float(end_voxel['voxel_e']):.6f}",
        ])

    if current_region is not None:
        bounds = current_region.get("box_bounds", {})
        region_label = (
            f"Region: R{current_region['region_id']}"
            if "region_id" in current_region
            else "Region: pending"
        )
        lines.extend([
            "",
            "SELECTED RECTANGLE",
            region_label,
            f"Voxels: {int(current_region.get('voxel_count', 0))}",
            f"X: {float(bounds.get('x_min', 0.0)):.3f} - {float(bounds.get('x_max', 0.0)):.3f}",
            f"Y: {float(bounds.get('y_min', 0.0)):.3f} - {float(bounds.get('y_max', 0.0)):.3f}",
            f"Z: {float(bounds.get('z_min', 0.0)):.3f} - {float(bounds.get('z_max', 0.0)):.3f}",
            "Press A or Add Region",
        ])

    lines.extend(["", "RECTANGLE REGIONS"])

    if not assignments:
        lines.append("  none")
        return "\n".join(lines)

    display_limit = 8
    for assignment in assignments[:display_limit]:
        steps = int(assignment.get("gradient_steps", 1))
        eta = float(assignment.get("eta", 0.5))
        direction = str(assignment.get("gradient_direction", "layer"))
        voxel_count_text = str(int(assignment.get("voxel_count", 0))) if "voxel_count" in assignment else "range"
        lines.append(
            f"  R{int(assignment['assignment_index'])}: "
            f"V{int(assignment['start_voxel'])}-{int(assignment['end_voxel'])} "
            f"({voxel_count_text} voxels) "
            f"S{steps} η{eta:.2f} {direction}"
        )

    remaining = len(assignments) - display_limit
    if remaining > 0:
        lines.append(f"  ... +{remaining} more")

    return "\n".join(lines)


# ============================================================================
# Interactive Selector Class
# ============================================================================

class InteractiveVoxelSelector:
    """Interactive 3D voxel selector matching reference interface"""

    def __init__(
        self,
        gcode_path: str,
        voxel_threshold_e: Optional[float] = 0.1,
        output_dir: Optional[str] = None,
        virtual_sample_spacing_mm: float = 0.2,
        rectangular_region_size_mm: Optional[Tuple[float, float, float]] = None,
    ):
        self.gcode_path = Path(gcode_path)
        self.voxel_threshold_e = voxel_threshold_e
        self.output_dir = Path(output_dir) if output_dir else self.gcode_path.parent
        self.virtual_sample_spacing_mm = virtual_sample_spacing_mm
        self.rectangular_region_size_mm = rectangular_region_size_mm

        # Initialize data structures
        self.segments: List[Dict] = []
        self.voxels: List[Dict] = []
        self.flat_segments: np.ndarray = np.array([])
        self.preprint_e: float = 0.0
        self.virtual_sample_cache: Dict[str, np.ndarray] = {}
        self.region_grid: List[Dict] = []
        self.region_lookup: Dict[int, Dict] = {}
        self.voxel_to_region: Dict[int, int] = {}

        # Caches
        self.voxel_lookup: Dict[int, Dict] = {}
        self.selection_cache: Dict[str, np.ndarray] = {}
        self.voxel_plot_ids: np.ndarray = np.array([])
        self.voxel_path_cache: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

        # Assignments
        self.assignments: List[Dict] = []
        self.assignment_color_map: Dict[int, Tuple[float, float, float]] = {}
        self.current_region: Optional[Dict] = None

        # UI state
        self.current_voxel_range: Tuple[int, int] = (1, 1)
        self.selection_source = "range"

        # Selection mode state
        self.selection_mode = False
        self.start_voxel: Optional[int] = None
        self.end_voxel: Optional[int] = None
        self.selected_voxel_ids: Set[int] = set()

        # Click-based selection state
        self.click_select_first: Optional[int] = None
        self.click_select_second: Optional[int] = None
        self.click_select_third: Optional[int] = None

        # Matplotlib
        self.fig = None
        self.ax_region = None
        self.ax_3d = None
        self.ax_stats_text = None
        self.ax_start_label = None
        self.textbox_low = None
        self.textbox_high = None
        self.textbox_start = None
        self.textbox_end = None
        self.btn_selection_mode = None
        self.btn_add_region = None
        self.btn_save_regions = None
        self.cid_key = None
        self.cid_pick = None
        self.line_to_voxel_map: Dict = {}  # Maps line artist to voxel_id
        self.region_artist_map: Dict = {}  # Maps region box artist to region_id
        self.region_center_artist = None
        self.region_center_ids: List[int] = []

    def parse(self) -> None:
        """Parse G-code and build voxel structures"""
        print("=" * 60)
        print("G-code Voxel Selector (Reference Interface)")
        print("=" * 60)

        start = time.time()
        print("G-code 파싱 시작...")

        self.segments, self.preprint_e = parse_gcode_extrusion_segments(str(self.gcode_path))
        if self.voxel_threshold_e is None or self.voxel_threshold_e <= 0:
            print("Voxel resolution: minimal extrusion segment unit")
            self.voxels, self.flat_segments = group_segments_into_minimal_voxels(self.segments)
        else:
            print(f"Voxel resolution: E threshold {self.voxel_threshold_e:.6f}")
            self.voxels, self.flat_segments = group_segments_into_voxels(
                self.segments,
                self.voxel_threshold_e,
            )

        annotate_voxels_with_layers(self.voxels)

        parse_time = time.time() - start
        print(f"G-code 파싱 완료: {parse_time:.2f}초")
        print(f"생성된 복셀: {len(self.voxels):,}개")

        # Build caches
        self.voxel_lookup = build_voxel_lookup(self.voxels)
        self.selection_cache = build_voxel_selection_cache(self.voxels)
        self.voxel_plot_ids, self.voxel_path_cache = build_voxel_plot_cache(self.flat_segments)
        self.virtual_sample_cache = build_virtual_voxel_sample_cache(
            self.voxels,
            self.virtual_sample_spacing_mm,
        )
        print(
            f"  Virtual voxel samples: {len(self.virtual_sample_cache['voxel_ids']):,} "
            f"points @ {self.virtual_sample_spacing_mm:.3f} mm"
        )
        if self.rectangular_region_size_mm is None:
            self.rectangular_region_size_mm = estimate_rectangular_region_size(self.voxels)
            print(
                f"  Auto rectangular region size: "
                f"{self.rectangular_region_size_mm[0]:.3f} x "
                f"{self.rectangular_region_size_mm[1]:.3f} x "
                f"{self.rectangular_region_size_mm[2]:.3f} mm"
            )

        self.region_grid, self.region_lookup, self.voxel_to_region = build_rectangular_region_grid(
            self.virtual_sample_cache,
            self.rectangular_region_size_mm,
        )
        print(
            f"  Rectangular regions: {len(self.region_grid):,} boxes "
            f"@ {self.rectangular_region_size_mm[0]:.2f} x "
            f"{self.rectangular_region_size_mm[1]:.2f} x "
            f"{self.rectangular_region_size_mm[2]:.2f} mm"
        )

        # Print summary
        if self.voxels:
            min_x = min(min(float(v["x_start"]), float(v["x_end"])) for v in self.voxels)
            max_x = max(max(float(v["x_start"]), float(v["x_end"])) for v in self.voxels)
            min_y = min(min(float(v["y_start"]), float(v["y_end"])) for v in self.voxels)
            max_y = max(max(float(v["y_start"]), float(v["y_end"])) for v in self.voxels)
            min_z = min(min(float(v["z_start"]), float(v["z_end"])) for v in self.voxels)
            max_z = max(max(float(v["z_start"]), float(v["z_end"])) for v in self.voxels)

            layer_nums = [int(v.get("layer_num", 0)) for v in self.voxels]
            max_layer = max(layer_nums) if layer_nums else 0

            print(f"  좌표: X={min_x:.2f}-{max_x:.2f}, Y={min_y:.2f}-{max_y:.2f}, Z={min_z:.2f}-{max_z:.2f}")
            print(f"  레이어: 1-{max_layer}")

        self.current_voxel_range = (1, len(self.voxels))

    def setup_visualization(self) -> bool:
        """Setup interactive 3D visualization"""
        if not self.voxels or self.voxel_plot_ids.size == 0:
            print("Voxel data not found")
            return False

        start = time.time()
        print("3D visualization setup...")

        # Create side-by-side 3D views and stats textbox
        self.fig = plt.figure(figsize=(20, 10))
        gs = self.fig.add_gridspec(1, 3, width_ratios=[1.35, 1.65, 0.9])
        self.ax_region = self.fig.add_subplot(gs[0], projection="3d")
        self.ax_3d = self.fig.add_subplot(gs[1], projection="3d")
        ax_stats = self.fig.add_subplot(gs[2])
        ax_stats.axis("off")
        self.ax_stats_text = ax_stats

        # Initial visualization
        self._update_visualization()

        # Add controls
        # Selection mode toggle button
        ax_btn = self.fig.add_axes([0.05, 0.05, 0.08, 0.04])
        self.btn_selection_mode = Button(ax_btn, "ROTATE ON")
        self.btn_selection_mode.on_clicked(self._toggle_selection_mode)

        # Start/End voxel selection textboxes (only visible when in selection mode)
        ax_start_label = self.fig.add_axes([0.15, 0.09, 0.1, 0.03])
        ax_start_label.axis("off")
        ax_start_label.text(0.5, 0.5, "Selection Mode (Active: OFF)", ha="center", fontsize=10, 
                          transform=ax_start_label.transAxes, fontweight="bold")
        self.ax_start_label = ax_start_label
        
        # Start voxel textbox
        ax_start = self.fig.add_axes([0.15, 0.055, 0.1, 0.04])
        self.textbox_start = TextBox(ax_start, "Start V:", initial="1")
        self.textbox_start.on_submit(lambda text: self._on_selection_submit())
        
        # End voxel textbox
        ax_end = self.fig.add_axes([0.15, 0.01, 0.1, 0.04])
        self.textbox_end = TextBox(ax_end, "End V:", initial=str(len(self.voxels)))
        self.textbox_end.on_submit(lambda text: self._on_selection_submit())
        
        # Range textboxes (always visible)
        ax_low = self.fig.add_axes([0.3, 0.05, 0.1, 0.04])
        ax_high = self.fig.add_axes([0.45, 0.05, 0.1, 0.04])

        self.textbox_low = TextBox(ax_low, "View Low:", initial=str(self.current_voxel_range[0]))
        self.textbox_high = TextBox(ax_high, "View High:", initial=str(self.current_voxel_range[1]))

        self.textbox_low.on_submit(lambda text: self._on_range_change())
        self.textbox_high.on_submit(lambda text: self._on_range_change())

        # Region action buttons
        ax_add_region = self.fig.add_axes([0.58, 0.05, 0.11, 0.04])
        self.btn_add_region = Button(ax_add_region, "ADD REGION")
        self.btn_add_region.on_clicked(lambda event: self.add_current_region_assignment())

        ax_save_regions = self.fig.add_axes([0.71, 0.05, 0.11, 0.04])
        self.btn_save_regions = Button(ax_save_regions, "SAVE JSON")
        self.btn_save_regions.on_clicked(lambda event: self.save_all_assignments_to_json())

        # Register keyboard event
        self.cid_key = self.fig.canvas.mpl_connect('key_press_event', self._on_key_press)
        
        # Register pick event (for click-based selection on voxel paths)
        self.cid_pick = self.fig.canvas.mpl_connect('pick_event', self._on_pick)
        
        # Initial mode update
        self._update_mode_ui()

        setup_time = time.time() - start
        print(f"Visualization setup completed: {setup_time:.2f}sec")
        print("\nControls:")
        print("  Space: Toggle selection mode (Rotate ON/OFF)")
        print("  S: Apply Start/End voxel IDs from textbox")
        print("  Left view: rectangular box regions")
        print("  Right view: existing path voxels")
        print("  Click: Select a box region or a voxel inside it")
        print("  A: Add selected rectangular region")
        print("  W: Save all regions to JSON")
        print(f"  R: Reset to all voxels (1-{len(self.voxels)})")
        print("  ESC: Cancel current selection")

        return True

    def _clamp_voxel_range(self, low: int, high: int) -> Tuple[int, int]:
        """Normalize a voxel range to the existing voxel ID bounds."""
        if low > high:
            low, high = high, low

        max_voxel = len(self.voxels)
        low = max(1, min(max_voxel, low))
        high = max(1, min(max_voxel, high))
        return low, high

    @staticmethod
    def _set_textbox_value(textbox, value: int) -> None:
        """Set a TextBox value without firing submit callbacks."""
        eventson = textbox.eventson
        textbox.eventson = False
        try:
            textbox.set_val(str(value))
        finally:
            textbox.eventson = eventson

    def _set_current_voxel_range(
        self,
        low: int,
        high: int,
        *,
        sync_view_boxes: bool = True,
        sync_selection_boxes: bool = False,
        mark_selected: bool = False,
        selection_source: str = "range",
        redraw: bool = True,
    ) -> Tuple[int, int]:
        """Apply a voxel range and keep the UI widgets in sync."""
        low, high = self._clamp_voxel_range(low, high)
        self.current_voxel_range = (low, high)

        if sync_view_boxes and self.textbox_low is not None and self.textbox_high is not None:
            self._set_textbox_value(self.textbox_low, low)
            self._set_textbox_value(self.textbox_high, high)

        if sync_selection_boxes and self.textbox_start is not None and self.textbox_end is not None:
            self._set_textbox_value(self.textbox_start, low)
            self._set_textbox_value(self.textbox_end, high)

        self.selected_voxel_ids = set(range(low, high + 1)) if mark_selected else set()
        self.selection_source = selection_source if mark_selected else "range"
        self.current_region = None

        if redraw and self.fig is not None:
            self._update_visualization()
            self.fig.canvas.draw_idle()

        return low, high

    def _clear_click_selection(self) -> None:
        """Reset click selection endpoints."""
        self.click_select_first = None
        self.click_select_second = None
        self.click_select_third = None

    def _set_region_selection(
        self,
        region: Dict,
    ) -> Tuple[int, int]:
        """Select a precomputed rectangular grid region."""
        selected_voxel_ids = set(int(voxel_id) for voxel_id in region.get("voxel_ids", []))
        if not selected_voxel_ids:
            return self.current_voxel_range

        low = min(selected_voxel_ids)
        high = max(selected_voxel_ids)
        self.current_voxel_range = (low, high)
        self.selected_voxel_ids = set(selected_voxel_ids)
        self.selection_source = "region"
        self.current_region = {
            "region_id": int(region["region_id"]),
            "region_type": str(region.get("region_type", "grid_box")),
            "grid_index": list(region.get("grid_index", [])),
            "box_bounds": dict(region["box_bounds"]),
            "voxel_ids": sorted(selected_voxel_ids),
            "voxel_count": len(selected_voxel_ids),
        }

        if self.textbox_low is not None and self.textbox_high is not None:
            self._set_textbox_value(self.textbox_low, low)
            self._set_textbox_value(self.textbox_high, high)

        if self.textbox_start is not None and self.textbox_end is not None:
            self._set_textbox_value(self.textbox_start, low)
            self._set_textbox_value(self.textbox_end, high)

        return low, high

    def _on_range_change(self) -> None:
        """Handle range change from textbox"""
        try:
            low = int(self.textbox_low.text)
            high = int(self.textbox_high.text)
            self._set_current_voxel_range(low, high, sync_view_boxes=True)
        except ValueError:
            print("Invalid voxel range")

    def _toggle_selection_mode(self, event) -> None:
        """Toggle between selection mode and rotation mode"""
        self.selection_mode = not self.selection_mode
        self._clear_click_selection()
        self._update_mode_ui()

    def _update_mode_ui(self) -> None:
        """Update UI based on selection mode and region state."""
        if self.btn_add_region is not None:
            self.btn_add_region.color = "#dcfce7" if self.current_region else "0.85"
            self.btn_add_region.hovercolor = "#bbf7d0" if self.current_region else "0.95"

        if self.btn_save_regions is not None:
            self.btn_save_regions.color = "#dbeafe" if self.assignments else "0.85"
            self.btn_save_regions.hovercolor = "#bfdbfe" if self.assignments else "0.95"

        if self.selection_mode:
            self.btn_selection_mode.label.set_text("ROTATE OFF")
            self.btn_selection_mode.color = "#ffcccc"
            self.btn_selection_mode.hovercolor = "#ff9999"
            self.ax_start_label.clear()
            self.ax_start_label.axis("off")

            if self.current_region is not None:
                status = f"R{self.current_region['region_id']} selected - Press A"
            else:
                status = "Click a rectangular box to select its voxels"

            self.ax_start_label.text(
                0.5, 0.5, status,
                ha="center",
                fontsize=9,
                transform=self.ax_start_label.transAxes,
                fontweight="bold",
                color="#ef4444",
            )
            self.ax_3d.disable_mouse_rotation()
            if self.ax_region is not None:
                self.ax_region.disable_mouse_rotation()
        else:
            self.btn_selection_mode.label.set_text("ROTATE ON")
            self.btn_selection_mode.color = "0.85"
            self.btn_selection_mode.hovercolor = "0.95"
            self.ax_start_label.clear()
            self.ax_start_label.axis("off")
            self.ax_start_label.text(
                0.5, 0.5,
                "Selection Mode (Active: OFF)",
                ha="center",
                fontsize=10,
                transform=self.ax_start_label.transAxes,
                fontweight="bold",
            )
            self.ax_3d.mouse_init()
            if self.ax_region is not None:
                self.ax_region.mouse_init()

        self.fig.canvas.draw_idle()

    def _on_selection_submit(self) -> None:
        """Handle selection textbox submission"""
        try:
            start = int(self.textbox_start.text)
            end = int(self.textbox_end.text)
            start, end = self._set_current_voxel_range(
                start,
                end,
                sync_view_boxes=True,
                sync_selection_boxes=True,
                mark_selected=True,
                selection_source="range",
            )
            print(f"Selection applied: V{start} - V{end} ({end - start + 1} voxels)")
        except ValueError:
            print("Invalid voxel ID")

    def _on_pick(self, event) -> None:
        """Handle pick event when clicking on a rectangular region or voxel path."""
        if not self.selection_mode:
            return

        region_id = self.region_artist_map.get(event.artist)
        if region_id is None and event.artist is self.region_center_artist and getattr(event, "ind", None) is not None:
            if len(event.ind) > 0:
                center_index = int(event.ind[0])
                if 0 <= center_index < len(self.region_center_ids):
                    region_id = self.region_center_ids[center_index]

        if region_id is None and event.artist in self.line_to_voxel_map:
            voxel_id = self.line_to_voxel_map[event.artist]
            region_id = self.voxel_to_region.get(int(voxel_id))

        if region_id is None or int(region_id) not in self.region_lookup:
            return

        region = self.region_lookup[int(region_id)]
        low, high = self._set_region_selection(region)
        print(
            f"Region R{region_id} selected: {len(self.selected_voxel_ids)} voxels "
            f"(ID span V{low} - V{high})"
        )
        self._clear_click_selection()
        self._update_mode_ui()
        self._update_visualization()
        self.fig.canvas.draw_idle()

    def _on_key_press(self, event) -> None:
        """Handle keyboard events"""
        if event.key == 'escape':
            # Cancel/clear current selection
            self.selected_voxel_ids.clear()
            self.selection_source = "range"
            self.current_region = None
            self.start_voxel = None
            self.end_voxel = None
            self._clear_click_selection()
            print("Selection cleared")
            self._update_mode_ui()
            self._update_visualization()
            self.fig.canvas.draw_idle()
        elif event.key == ' ':  # Space bar
            # Toggle selection mode
            self._toggle_selection_mode(None)
        elif event.key == 's':
            # Apply selection from Start/End textboxes
            self._on_selection_submit()
        elif event.key == 'a':
            # Add current rectangular region as a material assignment unit
            self.add_current_region_assignment()
        elif event.key == 'w':
            # Save all rectangular region assignments
            self.save_all_assignments_to_json()
        elif event.key == 'r':
            # Reset to all voxels
            self._clear_click_selection()
            self._set_current_voxel_range(
                1,
                len(self.voxels),
                sync_view_boxes=True,
                sync_selection_boxes=True,
                selection_source="range",
            )
            print(f"Reset to all voxels (1-{len(self.voxels)})")

    def _update_visualization(self) -> None:
        """Update 3D visualization"""
        low, high = self.current_voxel_range

        # Plot voxels and get line-to-voxel mapping for pick events
        self.line_to_voxel_map = plot_voxels_on_axis(
            self.ax_3d,
            self.voxel_plot_ids,
            self.voxel_path_cache,
            (low, high),
            (1, len(self.voxels)),
            self.assignment_color_map if self.assignments else None,
            self.selected_voxel_ids if self.selected_voxel_ids else None,
            "Path voxel view" if self.selection_source != "region" else "Selected path voxels",
        )

        self._draw_region_boxes()

        # Add status text
        if self.selection_mode:
            status_text = "SELECTION MODE - left: box regions, right: path voxels"
            self.ax_3d.text2D(0.05, 0.95, status_text, transform=self.ax_3d.transAxes,
                            fontsize=11, color="#ef4444", fontweight="bold",
                            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.8))

        # Update stats
        if self.selected_voxel_ids:
            selected_e, start_cum, end_cum = compute_selected_voxel_filament_e(
                self.selection_cache,
                self.selected_voxel_ids,
            )
            selected_layers = layers_from_selected_voxels_cached(
                self.selection_cache,
                self.selected_voxel_ids,
            )
            stats_label = "REGION SELECTION" if self.selection_source == "region" else "CURRENT SELECTION"
        else:
            selected_e, start_cum, end_cum = compute_selection_filament_e(
                self.selection_cache,
                (low, high)
            )
            selected_layers = layers_from_voxel_range_cached(self.selection_cache, (low, high))
            stats_label = "CURRENT RANGE"

        stats_text = format_assignment_stats(
            self.assignments,
            (low, high),
            self.voxel_lookup,
            selected_layers,
            self.preprint_e,
            selected_e,
            start_cum,
            end_cum,
            self.selected_voxel_ids if self.selected_voxel_ids else None,
            stats_label,
            self.current_region,
        )

        self.ax_stats_text.clear()
        self.ax_stats_text.axis("off")
        self.ax_stats_text.text(
            0.05, 0.95, stats_text,
            transform=self.ax_stats_text.transAxes,
            fontfamily="monospace",
            fontsize=9,
            verticalalignment="top",
        )

    def _draw_region_boxes(self) -> None:
        """Draw rectangular UI regions as translucent boxes in the 3D view."""
        if self.ax_region is None:
            return

        ax = self.ax_region
        ax.cla()
        self.region_artist_map = {}
        self.region_center_artist = None
        self.region_center_ids = []
        accent_palette = [
            "#ef4444", "#3b82f6", "#10b981", "#f59e0b",
            "#8b5cf6", "#ec4899", "#14b8a6", "#f97316",
        ]
        assigned_region_ids = {
            int(assignment["region_id"])
            for assignment in self.assignments
            if "region_id" in assignment
        }
        current_region_id = int(self.current_region["region_id"]) if self.current_region and "region_id" in self.current_region else None

        inactive_regions = [
            region
            for region in self.region_grid
            if int(region["region_id"]) not in assigned_region_ids
            and int(region["region_id"]) != current_region_id
        ]
        if len(inactive_regions) > 0:
            norm = mcolors.Normalize(vmin=1, vmax=max(len(self.voxels), 2))
            cmap = plt.get_cmap("viridis")
            region_facecolors = []
            for region in inactive_regions:
                voxel_ids = region.get("voxel_ids", [])
                representative_id = int(np.median(voxel_ids)) if voxel_ids else 1
                rgba = list(cmap(norm(representative_id)))
                rgba[3] = 0.42
                region_facecolors.append(tuple(rgba))

            draw_rectangular_filled_boxes(
                ax,
                inactive_regions,
                color="#6b7280",
                alpha=0.42,
                edge_alpha=0.08,
                linewidth=0.08,
                facecolors=region_facecolors,
            )
            centers = []
            for region in inactive_regions:
                bounds = region["box_bounds"]
                centers.append((
                    (float(bounds["x_min"]) + float(bounds["x_max"])) * 0.5,
                    (float(bounds["y_min"]) + float(bounds["y_max"])) * 0.5,
                    (float(bounds["z_min"]) + float(bounds["z_max"])) * 0.5,
                ))
                self.region_center_ids.append(int(region["region_id"]))
            if centers:
                centers_array = np.array(centers, dtype=float)
                self.region_center_artist = ax.scatter(
                    centers_array[:, 0],
                    centers_array[:, 1],
                    centers_array[:, 2],
                    s=5,
                    c="#6b7280",
                    alpha=0.12,
                    depthshade=False,
                    picker=6,
                )

        for assignment_index, assignment in enumerate(self.assignments, start=1):
            bounds = assignment.get("box_bounds")
            if not bounds:
                continue
            color = accent_palette[(assignment_index - 1) % len(accent_palette)]
            artist = draw_rectangular_box(
                ax,
                bounds,
                color=color,
                alpha=0.45,
                linewidth=0.8,
                label=f"R{assignment_index}",
                picker=5,
            )
            self.region_artist_map[artist] = int(assignment.get("region_id", -assignment_index))

        if self.current_region is not None:
            artist = draw_rectangular_box(
                ax,
                self.current_region["box_bounds"],
                color="#ef4444",
                alpha=0.60,
                linewidth=1.4,
                label=f"R{self.current_region['region_id']}",
                picker=5,
            )
            self.region_artist_map[artist] = int(self.current_region["region_id"])

        if self.current_region is None and self.selected_voxel_ids:
            bounds = bounds_from_voxel_ids(self.voxel_lookup, self.selected_voxel_ids)
            if bounds is not None:
                draw_rectangular_box(
                    ax,
                    bounds,
                    color="#ef4444",
                    alpha=0.45,
                    linewidth=1.0,
                    label="SELECTED",
                )

        total_bounds = bounds_from_region_list(self.region_grid)
        if total_bounds is not None:
            set_axes_from_bounds(ax, total_bounds)
        ax.set_title(
            "Box-region view",
            pad=10,
            fontsize=13,
            color="#111827",
            fontweight="bold",
        )
        ax.set_xlabel("")
        ax.set_ylabel("")
        ax.set_zlabel("")

    def add_assignment(self, start_voxel: int, end_voxel: int, **kwargs) -> None:
        """Add assignment"""
        if start_voxel > end_voxel:
            start_voxel, end_voxel = end_voxel, start_voxel

        start_voxel = max(1, min(len(self.voxels), start_voxel))
        end_voxel = max(1, min(len(self.voxels), end_voxel))

        assignment_index = len(self.assignments) + 1
        assignment = {
            "assignment_index": assignment_index,
            "start_voxel": start_voxel,
            "end_voxel": end_voxel,
            "gradient_steps": int(kwargs.get("gradient_steps", 1)),
            "gradient_direction": str(kwargs.get("gradient_direction", "layer")),
            "eta": float(kwargs.get("eta", 0.5)),
        }
        self.assignments.append(assignment)
        self.assignment_color_map = build_assignment_color_map(self.assignments)

        if self.fig is not None:
            self._update_visualization()
            self.fig.canvas.draw_idle()

    def add_current_region_assignment(self, **kwargs) -> None:
        """Add the current rectangular UI region as a material assignment unit."""
        if not self.current_region or not self.selected_voxel_ids:
            print("No rectangular region is selected. Click a grid box in selection mode first.")
            return

        voxel_ids = sorted(int(voxel_id) for voxel_id in self.selected_voxel_ids)
        selected_e, start_cum, end_cum = compute_selected_voxel_filament_e(
            self.selection_cache,
            set(voxel_ids),
        )
        layer_range = layers_from_selected_voxels_cached(self.selection_cache, set(voxel_ids))

        assignment_index = len(self.assignments) + 1
        assignment = {
            "assignment_index": assignment_index,
            "assignment_type": "rectangular_region",
            "region_id": int(self.current_region["region_id"]),
            "region_type": self.current_region["region_type"],
            "grid_index": self.current_region.get("grid_index", []),
            "box_bounds": self.current_region["box_bounds"],
            "start_voxel": int(voxel_ids[0]),
            "end_voxel": int(voxel_ids[-1]),
            "voxel_ids": voxel_ids,
            "voxel_count": len(voxel_ids),
            "selected_e": round(float(selected_e), 6),
            "cumulative_e_before": round(float(start_cum), 6),
            "cumulative_e_after": round(float(end_cum), 6),
            "layer_range": list(layer_range) if layer_range is not None else None,
            "gradient_steps": int(kwargs.get("gradient_steps", 1)),
            "gradient_direction": str(kwargs.get("gradient_direction", "region")),
            "eta": float(kwargs.get("eta", 0.5)),
        }
        self.assignments.append(assignment)
        self.assignment_color_map = build_assignment_color_map(self.assignments)
        self.current_region = None
        self.selected_voxel_ids.clear()
        self.selection_source = "range"
        self._clear_click_selection()

        print(
            f"Region R{assignment_index} added: {len(voxel_ids)} voxels "
            f"(ID span V{voxel_ids[0]} - V{voxel_ids[-1]})"
        )

        if self.fig is not None:
            self._update_mode_ui()
            self._update_visualization()
            self.fig.canvas.draw_idle()

    def save_assignment_to_json(self, assignment_index: int, output_path: Optional[str] = None) -> None:
        """Save assignment to JSON"""
        if not (1 <= assignment_index <= len(self.assignments)):
            print(f"Invalid assignment index: {assignment_index}")
            return

        assignment = self.assignments[assignment_index - 1]
        output_path = Path(output_path) if output_path else (
            self.output_dir / f"assignment_{assignment_index}_property_program.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Prepare output
        payload = {
            "source_gcode": str(self.gcode_path),
            "voxel_threshold_e": None if self.voxel_threshold_e is None else float(self.voxel_threshold_e),
            "voxel_resolution_mode": "minimal_segment" if self.voxel_threshold_e is None or self.voxel_threshold_e <= 0 else "e_threshold",
            "virtual_sample_spacing_mm": float(self.virtual_sample_spacing_mm),
            "rectangular_region_size_mm": [float(value) for value in self.rectangular_region_size_mm],
            "preheat_prime_e": round(float(self.preprint_e), 6),
            "assignments": [assignment],
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        print(f"Assignment saved: {output_path}")

    def save_all_assignments_to_json(self, output_path: Optional[str] = None) -> None:
        """Save all rectangular region assignments to JSON."""
        output_path = Path(output_path) if output_path else (
            self.output_dir / "rectangular_region_property_program.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "source_gcode": str(self.gcode_path),
            "voxel_threshold_e": None if self.voxel_threshold_e is None else float(self.voxel_threshold_e),
            "voxel_resolution_mode": "minimal_segment" if self.voxel_threshold_e is None or self.voxel_threshold_e <= 0 else "e_threshold",
            "virtual_sample_spacing_mm": float(self.virtual_sample_spacing_mm),
            "rectangular_region_size_mm": [float(value) for value in self.rectangular_region_size_mm],
            "preheat_prime_e": round(float(self.preprint_e), 6),
            "assignment_count": len(self.assignments),
            "assignments": self.assignments,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        print(f"All assignments saved: {output_path}")

    def show(self) -> None:
        """Show interactive interface"""
        if self.fig is None:
            print("Visualization not set up")
            return

        plt.show()


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    # Test with vase.gcode
    gcode_file = Path(__file__).parent / "test_sample" / "vase.gcode"
    output_dir = Path(__file__).parent / "test_sample"

    if not gcode_file.exists():
        print(f"G-code file not found: {gcode_file}")
        sys.exit(1)

    # Create selector
    selector = InteractiveVoxelSelector(
        gcode_path=str(gcode_file),
        voxel_threshold_e=0.1,
        output_dir=str(output_dir),
    )

    # Parse and visualize
    selector.parse()

    if selector.setup_visualization():
        selector.show()
