import json
import os
import csv
from pathlib import Path

import numpy as np
import torch

TARGET_MATERIAL_CODE_SCALE = 400.0
DEFAULT_RATIO_ROW_COUNT = 14
PROPERTY_FEATURE_DIM = 12


MATERIAL_CODEBOOK = {
    "": 0,
    "PLA": 1,
    "CPLA": 2,
    "TPU": 3,
    "PETG": 4,
    "SMP": 5,
    "CYAN": 100,
    "MAGENTA": 200,
    "YELLOW": 300,
    "WHITE": 400,
}

MATERIAL_CLASS_CODES = [int(code) for _, code in sorted(MATERIAL_CODEBOOK.items(), key=lambda item: int(item[1]))]
MATERIAL_CODE_TO_CLASS = {int(code): index for index, code in enumerate(MATERIAL_CLASS_CODES)}
MATERIAL_CLASS_TO_CODE = {index: int(code) for index, code in enumerate(MATERIAL_CLASS_CODES)}

TRANSITION_CODEBOOK = {
    "linear": 1,
    "non-graded": 2,
    "1-step": 3,
    "1-setp": 3,
    "5-step": 4,
    "11-step": 5,
    "mix": 6,
}

SLOT_CODEBOOK = {
    "": 0,
    "Mat 1": 1,
    "Mat 2": 2,
}

BRIGHTNESS_CODEBOOK = {
    "off": 0,
    "on": 1,
}

DIRECTION_CODEBOOK = {
    "": 0,
    "filament_path": 1,
    "reverse_filament_path": 2,
}


def normalize_length(length_raw: torch.Tensor) -> torch.Tensor:
    total = length_raw.sum()
    if total > 0:
        return length_raw / total
    return torch.zeros_like(length_raw)


def reverse_sequence_axis_1d(values: np.ndarray | torch.Tensor):
    if isinstance(values, torch.Tensor):
        return torch.flip(values, dims=[0])
    array = np.asarray(values)
    return array[::-1].copy()


def reverse_matrix_columns_to_sequence_order(matrix: np.ndarray) -> np.ndarray:
    array = np.asarray(matrix)
    if array.ndim != 2:
        raise ValueError(f"Expected 2D matrix to reverse columns, got {array.shape}")
    return array[:, ::-1].copy()


def resolve_base_dir(base_dir: str | os.PathLike = "Dataset") -> str:
    base_path = Path(base_dir)
    if base_path.is_absolute():
        return str(base_path)

    cwd_candidate = Path.cwd() / base_path
    if cwd_candidate.exists():
        return str(cwd_candidate)

    script_candidate = Path(__file__).resolve().parent / base_path
    return str(script_candidate)


def _split_length_stem(stem: str) -> tuple[str, str]:
    if stem.endswith("_lengths"):
        return stem[: -len("_lengths")], ""
    if "_lengths_" in stem:
        prefix, variant = stem.split("_lengths_", 1)
        return prefix, variant
    raise ValueError(f"Unexpected Result_Vector length filename stem: {stem}")


def _resolve_existing_path(directory: str, candidates: list[str]) -> str | None:
    for candidate in candidates:
        path = os.path.join(directory, candidate)
        if os.path.exists(path):
            return path
    return None


def _structure_candidates(prefix: str, variant: str) -> list[str]:
    candidates: list[str] = []
    aliases = [prefix]
    lower_prefix = prefix.lower()
    if lower_prefix == "cyclinder":
        aliases.append("cylinder")

    for alias in aliases:
        if variant:
            candidates.append(f"{alias}_{variant}.npy")
        candidates.append(f"{alias}.npy")
    return candidates


def _property_candidates(prefix: str, variant: str) -> list[str]:
    candidates: list[str] = []
    aliases = [prefix]
    lower_prefix = prefix.lower()
    if lower_prefix == "cyclinder":
        aliases.append("cylinder")

    for alias in aliases:
        if variant:
            candidates.append(f"{alias}_property_program_{variant}.json")
            if alias.startswith("linear_"):
                linear_suffix = alias[len("linear_") :]
                candidates.append(f"linear_property_program_{linear_suffix}_{variant}.json")
        candidates.append(f"{alias}_property_program.json")
    return candidates


def build_samples(base_dir: str = "Dataset") -> list[dict]:
    base_dir = resolve_base_dir(base_dir)
    structure_dir = os.path.join(base_dir, "Structure_Vector")
    property_dir = os.path.join(base_dir, "Property_Vector")
    result_dir = os.path.join(base_dir, "Result_Vector")

    for required_dir in (structure_dir, property_dir, result_dir):
        if not os.path.isdir(required_dir):
            raise FileNotFoundError(f"Required dataset directory not found: {required_dir}")

    samples: list[dict] = []

    for file_name in sorted(os.listdir(result_dir)):
        if not file_name.endswith(".npy") or "_lengths" not in file_name:
            continue

        stem = os.path.splitext(file_name)[0]
        prefix, variant = _split_length_stem(stem)
        ratio_name = file_name.replace("_lengths_", "_ratios_").replace("_lengths.npy", "_ratios.npy")
        ratio_path = os.path.join(result_dir, ratio_name)
        if not os.path.exists(ratio_path):
            print(f"[skip] Missing ratio file for {file_name}")
            continue

        structure_path = _resolve_existing_path(structure_dir, _structure_candidates(prefix, variant))
        property_path = _resolve_existing_path(property_dir, _property_candidates(prefix, variant))
        if structure_path is None or property_path is None:
            missing = []
            if structure_path is None:
                missing.append("q")
            if property_path is None:
                missing.append("x")
            print(f"[skip] Missing paired files for {file_name}: {missing}")
            continue

        length_shape = tuple(np.load(os.path.join(result_dir, file_name), allow_pickle=True).shape)
        ratio_shape = tuple(np.load(ratio_path, allow_pickle=True).shape)

        sample_id = f"{prefix}_{variant}".strip("_")
        samples.append(
            {
                "id": sample_id,
                "q": structure_path,
                "x": property_path,
                "l": os.path.join(result_dir, file_name),
                "r": ratio_path,
                "length_shape": length_shape,
                "ratio_shape": ratio_shape,
            }
        )

    if not samples:
        raise RuntimeError("No valid samples were found in the current ver2 dataset layout.")

    print(f"[ok] Built {len(samples)} sample(s) from ver2 dataset")
    for index, sample in enumerate(samples):
        print(f"  sample {index}: {Path(sample['id'])}")
    return samples


def filter_variable_target_samples(
    samples: list[dict],
    expected_ratio_rows: int = DEFAULT_RATIO_ROW_COUNT,
) -> list[dict]:
    compatible: list[dict] = []

    for sample in samples:
        length_shape = tuple(sample.get("length_shape", ()))
        ratio_shape = tuple(sample.get("ratio_shape", ()))

        if len(length_shape) != 1:
            print(f"[skip-model] {sample['id']}: unsupported length shape {length_shape}")
            continue
        if len(ratio_shape) != 2:
            print(f"[skip-model] {sample['id']}: unsupported ratio shape {ratio_shape}")
            continue
        if ratio_shape[0] != expected_ratio_rows:
            print(
                f"[skip-model] {sample['id']}: ratio row count {ratio_shape[0]} "
                f"!= {expected_ratio_rows}"
            )
            continue
        if ratio_shape[1] != length_shape[0]:
            print(
                f"[skip-model] {sample['id']}: length/ratio mismatch "
                f"{length_shape[0]} vs {ratio_shape[1]}"
            )
            continue

        sample_copy = dict(sample)
        sample_copy["target_length"] = int(length_shape[0])
        compatible.append(sample_copy)

    if not compatible:
        raise RuntimeError(
            "No model-compatible samples found for variable-length training. "
            f"Expected 1D length vectors and ratio matrices with shape ({expected_ratio_rows}, n)."
        )

    report_rows = []
    report_payload = []
    report_text_blocks = []
    root_dir = Path(compatible[0]["q"]).resolve().parents[2]
    csv_path = root_dir / "dataset_sequence_alignment.csv"
    json_path = root_dir / "dataset_sequence_alignment.json"
    txt_path = root_dir / "dataset_sequence_alignment.txt"

    for sample in compatible:
        n = int(sample["target_length"])
        columns = []
        for seq_index in range(n):
            raw_matrix_col = n - 1 - seq_index
            row = {
                "sample_id": sample["id"],
                "target_length": n,
                "seq_index": seq_index,
                "raw_matrix_col": raw_matrix_col,
                "length_index": seq_index,
                "ratio_index": seq_index,
            }
            report_rows.append(row)
            columns.append(
                {
                    "seq_index": seq_index,
                    "raw_matrix_col": raw_matrix_col,
                    "length_index": seq_index,
                    "ratio_index": seq_index,
                }
            )
        report_payload.append(
            {
                "sample_id": sample["id"],
                "target_length": n,
                "sequence_alignment_rule": "raw matrix last column -> seq 0 -> length[0], ratio[0]; raw matrix first column -> seq n-1 -> length[n-1], ratio[n-1]",
                "columns": columns,
            }
        )
        report_text_blocks.append(build_gt_sequence_alignment_text(sample))

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["sample_id", "target_length", "seq_index", "raw_matrix_col", "length_index", "ratio_index"],
        )
        writer.writeheader()
        writer.writerows(report_rows)

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report_payload, handle, indent=2, ensure_ascii=False)
    txt_path.write_text("\n\n".join(report_text_blocks), encoding="utf-8")

    max_target_length = max(int(sample["target_length"]) for sample in compatible)
    print(
        f"[ok] Model-compatible variable-length samples: {len(compatible)} "
        f"(ratio rows={expected_ratio_rows}, max target length={max_target_length})"
    )
    print(f"[ok] Saved dataset sequence alignment CSV : {csv_path}")
    print(f"[ok] Saved dataset sequence alignment JSON: {json_path}")
    print(f"[ok] Saved dataset sequence alignment TXT : {txt_path}")
    print(
        "[seq-align] sequence index is anchored at the RIGHTMOST raw matrix column: "
        "matrix raw col (n-1) -> length[0], ratio[0]; matrix raw col 0 -> length[n-1], ratio[n-1]"
    )
    for index, sample in enumerate(compatible):
        print(
            f"  model sample {index}: {sample['id']} "
            f"(n={sample['target_length']}, ratio_shape={sample['ratio_shape']})"
        )
        print(
            f"    seq-map: raw matrix col {sample['target_length'] - 1} -> seq 0, "
            f"raw matrix col 0 -> seq {sample['target_length'] - 1}"
        )
    return compatible


def get_max_target_length(samples: list[dict]) -> int:
    if not samples:
        raise ValueError("Cannot infer max target length from an empty sample list.")
    return max(int(sample.get("target_length", sample.get("length_shape", (0,))[0])) for sample in samples)


def get_target_length_classes(samples: list[dict]) -> list[int]:
    if not samples:
        raise ValueError("Cannot infer target-length classes from an empty sample list.")
    return sorted({int(sample.get("target_length", sample.get("length_shape", (0,))[0])) for sample in samples})


def length_to_class_index(length: int, length_classes: list[int] | tuple[int, ...]) -> int:
    length = int(length)
    try:
        return list(length_classes).index(length)
    except ValueError as exc:
        raise ValueError(f"Target length {length} is not in length_classes={list(length_classes)}") from exc


def class_index_to_length(class_index: int, length_classes: list[int] | tuple[int, ...]) -> int:
    classes = list(length_classes)
    if class_index < 0 or class_index >= len(classes):
        raise IndexError(f"class_index {class_index} out of range for length_classes={classes}")
    return int(classes[int(class_index)])


def _safe_code(codebook: dict[str, int], value: str, default: int = 0) -> int:
    key = str(value).strip()
    return int(codebook.get(key, default))


def resolve_assignment_material_code(assignment: dict, slot_name: str) -> int:
    slot = str(slot_name).strip()
    material_1_code = _safe_code(MATERIAL_CODEBOOK, assignment.get("material_1", ""))
    material_2_code = _safe_code(MATERIAL_CODEBOOK, assignment.get("material_2", ""))
    if slot == "Mat 2":
        return material_2_code
    return material_1_code


def get_allowed_material_codes_for_assignment(assignment: dict) -> list[int]:
    if not assignment:
        return [0]

    allowed_codes: list[int] = []
    material_count = max(1, int(assignment.get("material_count", 1)))
    for material_index in range(1, material_count + 1):
        code = _safe_code(MATERIAL_CODEBOOK, assignment.get(f"material_{material_index}", ""))
        if code not in allowed_codes:
            allowed_codes.append(code)

    start_code = resolve_assignment_material_code(assignment, assignment.get("start_material_slot", "Mat 1"))
    end_code = resolve_assignment_material_code(assignment, assignment.get("end_material_slot", "Mat 1"))
    for code in (start_code, end_code):
        if code not in allowed_codes:
            allowed_codes.append(code)

    if _safe_code(BRIGHTNESS_CODEBOOK, assignment.get("brightness", "")) == 1:
        white_code = int(MATERIAL_CODEBOOK.get("WHITE", 0))
        if white_code not in allowed_codes:
            allowed_codes.append(white_code)

    if not allowed_codes:
        allowed_codes.append(0)
    return allowed_codes


def load_property_payload(property_json_path: str) -> dict:
    with open(property_json_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def get_assignment_voxel_support(property_json_path: str) -> tuple[int, int, int]:
    payload = load_property_payload(property_json_path)
    assignments = payload.get("assignments", [])
    if not assignments:
        voxel_count = max(int(payload.get("voxel_count", 0)), 1)
        return 1, voxel_count, voxel_count

    start_voxel = min(int(assignment.get("start_voxel", 1)) for assignment in assignments)
    end_voxel = max(int(assignment.get("end_voxel", start_voxel)) for assignment in assignments)
    if end_voxel < start_voxel:
        start_voxel, end_voxel = end_voxel, start_voxel
    return start_voxel, end_voxel, int(end_voxel - start_voxel + 1)


def _estimate_previous_cumulative_for_cropped_support(cumulative_e: np.ndarray) -> float:
    cumulative_e = np.asarray(cumulative_e, dtype=np.float64).reshape(-1)
    if cumulative_e.size == 0:
        return 0.0
    if cumulative_e.size == 1:
        return float(cumulative_e[0])

    local_diffs = np.diff(cumulative_e)
    positive_diffs = local_diffs[local_diffs > 0.0]
    if positive_diffs.size == 0:
        step = 0.0
    else:
        # Cropped Structure_Vector files do not contain the voxel just before the
        # first assignment voxel. Use the median local step as a stable estimate
        # of the missing first-voxel extrusion so total_length stays support-aware.
        step = float(np.median(positive_diffs))
    return float(cumulative_e[0] - step)


def compute_total_length_from_cumulative(cumulative_e: np.ndarray, initial_cumulative: float) -> float:
    cumulative_e = np.asarray(cumulative_e, dtype=np.float64).reshape(-1)
    if cumulative_e.size == 0:
        return 0.0
    diffs = np.diff(np.concatenate(([float(initial_cumulative)], cumulative_e)))
    diffs[diffs < 0.0] = 0.0
    return float(diffs.sum())


def crop_q_array_to_assignment_support(q_raw: np.ndarray, property_json_path: str | None = None) -> np.ndarray:
    q_raw = np.asarray(q_raw)
    if property_json_path is None:
        return q_raw
    if q_raw.ndim != 2 or q_raw.shape[1] < 5:
        raise ValueError(f"Unexpected Q shape: {q_raw.shape}")

    start_voxel, end_voxel, support_length = get_assignment_voxel_support(property_json_path)
    row_count = int(q_raw.shape[0])

    if row_count == support_length:
        return q_raw
    if row_count >= end_voxel:
        return q_raw[start_voxel - 1 : end_voxel]
    return q_raw


def build_assignment_output_index_map(assignments: list[dict], output_count: int) -> list[int]:
    if not assignments or output_count <= 0:
        return []
    assignment_count = int(len(assignments))
    output_count = int(output_count)

    if output_count <= assignment_count:
        mapped_indices: list[int] = []
        for output_index in range(output_count):
            position = ((float(output_index) + 0.5) * float(assignment_count)) / float(output_count)
            assignment_index = int(np.floor(position))
            assignment_index = min(max(assignment_index, 0), assignment_count - 1)
            mapped_indices.append(assignment_index)
        return mapped_indices

    transition_codes = [
        int(_safe_code(TRANSITION_CODEBOOK, assignment.get("transition", "")))
        for assignment in assignments
    ]
    span_lengths = [
        max(
            1.0,
            float(abs(int(assignment.get("end_voxel", assignment.get("start_voxel", 1))) - int(assignment.get("start_voxel", 1))) + 1),
        )
        for assignment in assignments
    ]

    allocations = [1 for _ in range(assignment_count)]
    remaining = output_count - assignment_count
    graded_indices = [index for index, code in enumerate(transition_codes) if 1 <= int(code) <= 5]
    target_indices = graded_indices if graded_indices else list(range(assignment_count))

    if remaining > 0 and target_indices:
        weights = np.array([span_lengths[index] for index in target_indices], dtype=np.float64)
        if float(weights.sum()) <= 0.0:
            weights = np.ones_like(weights)
        shares = (weights / weights.sum()) * float(remaining)
        extras = np.floor(shares).astype(int)
        leftover = int(remaining - int(extras.sum()))
        if leftover > 0:
            remainders = shares - extras
            order = np.argsort(-remainders)
            for local_index in order[:leftover]:
                extras[int(local_index)] += 1
        for local_index, assignment_index in enumerate(target_indices):
            allocations[int(assignment_index)] += int(extras[int(local_index)])

    mapped_indices: list[int] = []
    for assignment_index, allocation in enumerate(allocations):
        mapped_indices.extend([int(assignment_index)] * max(int(allocation), 0))

    if len(mapped_indices) < output_count:
        mapped_indices.extend([assignment_count - 1] * (output_count - len(mapped_indices)))
    elif len(mapped_indices) > output_count:
        mapped_indices = mapped_indices[:output_count]
    return mapped_indices


def get_assignment_for_output_index(assignments: list[dict], output_index: int, output_count: int) -> dict:
    if not assignments:
        return {}
    mapped = build_assignment_output_index_map(assignments, output_count)
    if not mapped:
        return assignments[min(max(int(output_index), 0), len(assignments) - 1)]
    mapped_index = mapped[min(max(int(output_index), 0), len(mapped) - 1)]
    return assignments[mapped_index]


def encode_property_program(property_json_path: str) -> torch.Tensor:
    payload = load_property_payload(property_json_path)
    assignments = payload.get("assignments", [])
    voxel_count = max(int(payload.get("voxel_count", 0)), 1)
    assignment_total = max(len(assignments), 1)

    rows: list[list[float]] = []
    for assignment_index, assignment in enumerate(assignments):
        start_voxel = int(assignment.get("start_voxel", 1))
        end_voxel = int(assignment.get("end_voxel", start_voxel))
        start_material_code = resolve_assignment_material_code(assignment, assignment.get("start_material_slot", "Mat 1"))
        end_material_code = resolve_assignment_material_code(assignment, assignment.get("end_material_slot", "Mat 1"))
        sorted_pair = sorted((int(start_material_code), int(end_material_code)))
        pair_min_code = int(sorted_pair[0])
        pair_max_code = int(sorted_pair[1])

        row = [
            float(start_voxel) / voxel_count,
            float(end_voxel) / voxel_count,
            float(start_material_code) / TARGET_MATERIAL_CODE_SCALE,
            float(end_material_code) / TARGET_MATERIAL_CODE_SCALE,
            float(pair_min_code) / TARGET_MATERIAL_CODE_SCALE,
            float(pair_max_code) / TARGET_MATERIAL_CODE_SCALE,
            float(_safe_code(TRANSITION_CODEBOOK, assignment.get("transition", ""))) / 6.0,
            float(int(assignment.get("color_ratio_1", 0))) / 100.0,
            float(int(assignment.get("color_ratio_2", 0))) / 100.0,
            float(_safe_code(BRIGHTNESS_CODEBOOK, assignment.get("brightness", ""))),
            float(_safe_code(DIRECTION_CODEBOOK, assignment.get("direction", ""))) / 2.0,
            float(assignment_index) / max(assignment_total - 1, 1),
        ]
        rows.append(row)

    if not rows:
        rows.append(
            [
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ]
        )

    return torch.tensor(rows, dtype=torch.float32)


def compute_max_material_ratio_vector_from_code_matrix(material_code_matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(material_code_matrix, dtype=np.int32)
    if matrix.ndim != 2:
        raise ValueError(f"Expected material code matrix with 2 dimensions, got {matrix.shape}")

    _, output_count = matrix.shape
    ratio_vector = np.zeros((output_count,), dtype=np.float32)

    for output_index in range(output_count):
        column = matrix[:, output_index]
        valid = column[column != 0]
        if valid.size == 0:
            ratio_vector[output_index] = 0.0
            continue

        _, counts = np.unique(valid, return_counts=True)
        max_fraction = float(counts.max()) / float(valid.size)
        ratio_vector[output_index] = float(np.clip(max_fraction, 0.0, 1.0))

    return ratio_vector


def build_gt_sequence_alignment_text(sample: dict) -> str:
    length_vector = np.asarray(np.load(sample["l"], allow_pickle=True), dtype=np.float32).reshape(-1)
    matrix = np.asarray(np.load(sample["r"], allow_pickle=True), dtype=np.int32)
    ratio_vector = compute_max_material_ratio_vector_from_code_matrix(matrix)[::-1]
    n = int(length_vector.shape[0])

    lines = []
    lines.append("=" * 140)
    lines.append(f"Dataset GT Sequence Alignment - {sample['id']}")
    lines.append("=" * 140)
    lines.append("Rule: raw matrix last column -> seq 0 -> length[0], ratio[0]; raw matrix first column -> seq n-1 -> length[n-1], ratio[n-1]")
    lines.append(f"Target length: {n}")
    lines.append(f"GT Length vector: {length_vector.tolist()}")
    lines.append(f"GT Ratio vector : {ratio_vector.tolist()}")
    lines.append("GT Matrix (raw column order left->right):")
    lines.append(np.array2string(matrix, max_line_width=220))
    lines.append("-" * 140)
    lines.append(f"{'SeqIdx':>6} | {'RawCol':>6} | {'Length':>12} | {'Ratio':>12} | {'Dominant':>8} | {'Materials':>25} | {'Column Values':>35}")
    lines.append("-" * 140)

    for seq_index in range(n):
        raw_col = n - 1 - seq_index
        column = matrix[:, raw_col]
        valid = column[column != 0]
        if valid.size == 0:
            dominant_code = 0
            present_codes = []
        else:
            unique_codes, counts = np.unique(valid, return_counts=True)
            dominant_code = int(unique_codes[int(np.argmax(counts))])
            present_codes = [int(v) for v in unique_codes.tolist()]
        lines.append(
            f"{seq_index:6d} | {raw_col:6d} | {float(length_vector[seq_index]):12.6f} | {float(ratio_vector[seq_index]):12.6f} | "
            f"{dominant_code:8d} | {str(present_codes):>25} | {str([int(v) for v in column.tolist()]):>35}"
        )

    lines.append("Column Material Presence Analysis")
    lines.append("=" * 110)
    lines.append(f"{'Col':>3} | {'GT Materials':>30}")
    lines.append("-" * 110)
    for seq_index in range(n):
        raw_col = n - 1 - seq_index
        column = matrix[:, raw_col]
        gt_set = sorted(set(int(v) for v in column.tolist() if int(v) != 0))
        lines.append(f"{seq_index + 1:3d} | {str(gt_set):>30}")

    return "\n".join(lines)


def build_ratio_target_vector(sample: dict) -> torch.Tensor:
    ratio_matrix = np.load(sample["r"], allow_pickle=True)
    ratio_vector = compute_max_material_ratio_vector_from_code_matrix(ratio_matrix)
    ratio_vector = reverse_sequence_axis_1d(ratio_vector)
    return torch.tensor(ratio_vector, dtype=torch.float32)


def encode_material_matrix_target(sample: dict) -> torch.Tensor:
    material_matrix = np.asarray(np.load(sample["r"], allow_pickle=True), dtype=np.int32)
    if material_matrix.ndim != 2:
        raise ValueError(f"Expected material matrix with 2 dimensions, got {material_matrix.shape} for {sample['r']}")
    material_matrix = reverse_matrix_columns_to_sequence_order(material_matrix)
    class_matrix = np.vectorize(lambda value: MATERIAL_CODE_TO_CLASS.get(int(value), 0))(material_matrix)
    return torch.tensor(class_matrix.T, dtype=torch.long)


def decode_material_class_matrix(class_matrix: np.ndarray | torch.Tensor) -> np.ndarray:
    class_array = np.asarray(class_matrix, dtype=np.int64)
    return np.vectorize(lambda value: MATERIAL_CLASS_TO_CODE.get(int(value), 0))(class_array).astype(np.int32)


def reconstruct_code_matrix_from_ratio_vector(
    ratio_vector: np.ndarray,
    property_json_path: str,
    row_count: int = DEFAULT_RATIO_ROW_COUNT,
) -> np.ndarray:
    payload = load_property_payload(property_json_path)
    assignments = payload.get("assignments", [])
    ratio_vector = np.asarray(ratio_vector, dtype=np.float32).reshape(-1)
    output_count = int(ratio_vector.shape[0])
    matrix = np.zeros((row_count, output_count), dtype=np.int32)
    mapped_indices = build_assignment_output_index_map(assignments, output_count)

    for output_index, ratio_value in enumerate(ratio_vector):
        assignment_index = mapped_indices[min(output_index, len(mapped_indices) - 1)] if mapped_indices else 0
        assignment = assignments[assignment_index] if assignments else {}
        allowed_codes = [code for code in get_allowed_material_codes_for_assignment(assignment) if int(code) != 0]
        if not allowed_codes:
            continue

        primary_code = int(allowed_codes[0])
        alternate_codes = [int(code) for code in allowed_codes[1:]]
        if not alternate_codes:
            matrix[:, output_index] = primary_code
            continue

        mixed_count = int(np.clip(np.rint(float(ratio_value) * row_count), 0, row_count))
        dominant_count = max(row_count - mixed_count, 0)
        matrix[:dominant_count, output_index] = primary_code
        for row_index in range(dominant_count, row_count):
            matrix[row_index, output_index] = alternate_codes[(row_index - dominant_count) % len(alternate_codes)]

    return matrix


def build_allowed_material_class_mask(
    property_json_path: str,
    output_count: int,
    class_count: int | None = None,
) -> torch.Tensor:
    payload = load_property_payload(property_json_path)
    assignments = payload.get("assignments", [])
    if class_count is None:
        class_count = len(MATERIAL_CLASS_CODES)

    mask = torch.zeros((int(output_count), int(class_count)), dtype=torch.bool)
    mapped_indices = build_assignment_output_index_map(assignments, int(output_count))
    for output_index in range(int(output_count)):
        assignment_index = mapped_indices[min(output_index, len(mapped_indices) - 1)] if mapped_indices else 0
        assignment = assignments[assignment_index] if assignments else {}
        allowed_codes = get_allowed_material_codes_for_assignment(assignment)
        allowed_classes = [
            MATERIAL_CODE_TO_CLASS[code]
            for code in allowed_codes
            if int(code) in MATERIAL_CODE_TO_CLASS
        ]
        if not allowed_classes:
            allowed_classes = [0]
        mask[output_index, allowed_classes] = True
    return mask


def load_sample_tensors(sample: dict) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    for key in ("q", "x", "l", "r"):
        if not os.path.exists(sample[key]):
            raise FileNotFoundError(f"Missing sample file for key '{key}': {sample[key]}")

    q_raw = np.load(sample["q"])
    q_cropped = crop_q_array_to_assignment_support(q_raw, sample["x"])
    q = torch.tensor(q_cropped, dtype=torch.float32)
    x = encode_property_program(sample["x"])

    length_raw = torch.tensor(np.load(sample["l"]), dtype=torch.float32).reshape(-1)
    length_raw = reverse_sequence_axis_1d(length_raw)
    length_ratio = normalize_length(length_raw)
    ratio = build_ratio_target_vector(sample)
    material_matrix = encode_material_matrix_target(sample)
    return q, x, length_ratio, ratio, material_matrix


def load_total_length_from_q(q_path: str, property_json_path: str | None = None) -> float:
    q_raw = np.load(q_path)
    if q_raw.ndim != 2 or q_raw.shape[1] < 5:
        raise ValueError(f"Unexpected Q shape: {q_raw.shape}")
    if property_json_path is None:
        return float(q_raw[-1, 4])

    start_voxel, end_voxel, support_length = get_assignment_voxel_support(property_json_path)
    row_count = int(q_raw.shape[0])

    if row_count >= end_voxel:
        support_slice = np.asarray(q_raw[start_voxel - 1 : end_voxel, 4], dtype=np.float64)
        previous_cumulative = float(q_raw[start_voxel - 2, 4]) if start_voxel > 1 else 0.0
        return compute_total_length_from_cumulative(support_slice, previous_cumulative)

    if row_count == support_length:
        support_slice = np.asarray(q_raw[:, 4], dtype=np.float64)
        if start_voxel > 1:
            previous_cumulative = _estimate_previous_cumulative_for_cropped_support(support_slice)
        else:
            previous_cumulative = 0.0
        return compute_total_length_from_cumulative(support_slice, previous_cumulative)

    return float(q_raw[-1, 4])
