import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

try:
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.widgets import Button
    from matplotlib.widgets import RadioButtons
    from matplotlib.widgets import TextBox
except ModuleNotFoundError:
    plt = None
    Rectangle = None
    Button = None
    RadioButtons = None
    TextBox = None

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dataset_io_ver2 import MATERIAL_CODEBOOK
from dataset_io_ver2 import build_allowed_material_class_mask
from dataset_io_ver2 import encode_property_program
from dataset_io_ver2 import load_property_payload
from dataset_io_ver2 import load_total_length_from_q
from model_eval import compute_ratio_vector_from_code_matrix
from model_eval import plot_result_vector
from model_eval import save_attention_preview
from model_train_full_train import HybridVoxelPredictor
from Gcode_Property_Program_Designer import annotate_voxels_with_layers
from Gcode_Property_Program_Designer import build_assignment_color_map
from Gcode_Property_Program_Designer import build_assignment_legend_entries
from Gcode_Property_Program_Designer import build_layer_to_voxel_ids
from Gcode_Property_Program_Designer import build_voxel_lookup
from Gcode_Property_Program_Designer import build_voxel_plot_cache
from Gcode_Property_Program_Designer import build_voxel_selection_cache
from Gcode_Property_Program_Designer import clamp_ratio_value
from Gcode_Property_Program_Designer import clamp_range
from Gcode_Property_Program_Designer import compute_selection_filament_e
from Gcode_Property_Program_Designer import format_assignment_stats
from Gcode_Property_Program_Designer import get_output_paths as get_designer_output_paths
from Gcode_Property_Program_Designer import group_segments_into_voxels
from Gcode_Property_Program_Designer import layers_from_voxel_range_cached
from Gcode_Property_Program_Designer import load_voxel_cache
from Gcode_Property_Program_Designer import MATERIAL_COUNT_OPTIONS
from Gcode_Property_Program_Designer import MATERIAL_OPTIONS
from Gcode_Property_Program_Designer import BRIGHTNESS_OPTIONS
from Gcode_Property_Program_Designer import parse_int
from Gcode_Property_Program_Designer import parse_gcode_extrusion_segments
from Gcode_Property_Program_Designer import plot_voxels_on_axis
from Gcode_Property_Program_Designer import print_summary
from Gcode_Property_Program_Designer import save_property_program
from Gcode_Property_Program_Designer import save_voxel_cache
from Gcode_Property_Program_Designer import START_END_SLOT_OPTIONS


GRADIENT_DIRECTION_OPTIONS = ["layer", "printing"]
DEFAULT_GRADIENT_STEPS = 5
DEFAULT_GRADIENT_ETA = 0.50

_BASE_BUILD_ASSIGNMENT_LEGEND_ENTRIES = build_assignment_legend_entries
_BASE_FORMAT_ASSIGNMENT_STATS = format_assignment_stats


def get_prediction_output_paths(program_json_path: Path, output_dir: Path | None) -> dict[str, Path]:
    base_dir = output_dir if output_dir else program_json_path.parent
    base_dir.mkdir(parents=True, exist_ok=True)
    stem = program_json_path.stem.replace("_property_program", "")
    return {
        "preview_png": base_dir / f"{stem}_model_preview.png",
        "attention_png": base_dir / f"{stem}_model_attention.png",
        "bundle_npz": base_dir / f"{stem}_model_prediction_bundle.npz",
        "meta_json": base_dir / f"{stem}_model_prediction_metadata.json",
        "input_bundle_npz": base_dir / f"{stem}_model_input_bundle.npz",
        "input_json": base_dir / f"{stem}_model_input_metadata.json",
    }


def clamp_gradient_steps(value: object) -> int:
    try:
        return max(1, min(99, int(float(value))))
    except (TypeError, ValueError):
        return DEFAULT_GRADIENT_STEPS


def clamp_eta_value(value: object) -> float:
    try:
        return float(max(0.0, min(1.0, float(value))))
    except (TypeError, ValueError):
        return DEFAULT_GRADIENT_ETA


def format_gradient_direction(value: object) -> str:
    value = str(value).strip().lower()
    if value == "printing":
        return "printing"
    return "layer"


def build_assignment_label_positions(
    assignments: list[dict],
    voxel_lookup: dict[int, dict],
) -> list[dict]:
    label_positions: list[dict] = []
    for assignment_index, assignment in enumerate(assignments, start=1):
        start_voxel = int(assignment.get("start_voxel", 1))
        end_voxel = int(assignment.get("end_voxel", start_voxel))
        if end_voxel < start_voxel:
            start_voxel, end_voxel = end_voxel, start_voxel

        selected_voxels = [
            voxel_lookup[voxel_id]
            for voxel_id in range(start_voxel, end_voxel + 1)
            if voxel_id in voxel_lookup
        ]
        if not selected_voxels:
            continue

        center_voxel = selected_voxels[len(selected_voxels) // 2]
        x_center = (float(center_voxel["x_start"]) + float(center_voxel["x_end"])) * 0.5
        y_center = (float(center_voxel["y_start"]) + float(center_voxel["y_end"])) * 0.5
        z_center = (float(center_voxel["z_start"]) + float(center_voxel["z_end"])) * 0.5
        total_e = float(sum(float(voxel.get("voxel_e", 0.0)) for voxel in selected_voxels))
        gradient_steps = clamp_gradient_steps(assignment.get("gradient_steps", DEFAULT_GRADIENT_STEPS))
        eta_value = clamp_eta_value(assignment.get("eta", DEFAULT_GRADIENT_ETA))
        gradient_direction = format_gradient_direction(assignment.get("gradient_direction", "layer"))

        label_positions.append(
            {
                "assignment_index": assignment_index,
                "label": f"A{assignment_index}",
                "start_voxel": start_voxel,
                "end_voxel": end_voxel,
                "total_filament_e_mm": round(total_e, 6),
                "position": (x_center, y_center, z_center),
                "gradient_steps": gradient_steps,
                "eta": eta_value,
                "gradient_direction": gradient_direction,
                "display_label": (
                    f"A{assignment_index}\n"
                    f"{total_e:.1f} mm\n"
                    f"S{gradient_steps} eta={eta_value:.2f}\n"
                    f"{gradient_direction}"
                ),
            }
        )

    return label_positions


def build_assignment_legend_entries(assignments: list[dict]) -> list[dict]:
    entries = _BASE_BUILD_ASSIGNMENT_LEGEND_ENTRIES(assignments)
    for entry, assignment in zip(entries, assignments, strict=False):
        steps = clamp_gradient_steps(assignment.get("gradient_steps", DEFAULT_GRADIENT_STEPS))
        eta_value = clamp_eta_value(assignment.get("eta", DEFAULT_GRADIENT_ETA))
        gradient_direction = format_gradient_direction(assignment.get("gradient_direction", "layer"))
        entry["label"] = (
            f"{entry['label']}  "
            f"S{steps} η{eta_value:.2f}  "
            f"{gradient_direction}"
        )
    return entries


def format_assignment_stats(
    assignments: list[dict],
    voxel_range: tuple[int, int],
    voxel_lookup: dict[int, dict],
    selected_layer_range: tuple[int, int] | None,
    preprint_e: float,
    selected_e: float,
    start_cumulative: float,
    end_cumulative: float,
) -> str:
    low, high = voxel_range
    voxel_count = max(0, high - low + 1)
    lines = [
        "PREHEAT/PRIME E",
        f"{preprint_e:.6f}",
        "",
        "CURRENT RANGE",
        f"V{low} - V{high}",
        f"Voxel Count: {voxel_count}",
        f"Selected E: {selected_e:.6f}",
    ]
    if selected_layer_range is not None:
        lines.append(f"Layer Range: L{selected_layer_range[0]} - L{selected_layer_range[1]}")
    else:
        lines.append("Layer Range: none")

    if assignments:
        lines.append("")
        lines.append("GRADIENT SETTINGS")
    else:
        lines.append("")
        lines.append("GRADIENT SETTINGS")
        lines.append("  none")
        return "\n".join(lines)

    if not assignments:
        return "\n".join(lines)

    display_limit = 8
    for assignment in assignments[:display_limit]:
        steps = clamp_gradient_steps(assignment.get("gradient_steps", DEFAULT_GRADIENT_STEPS))
        eta_value = clamp_eta_value(assignment.get("eta", DEFAULT_GRADIENT_ETA))
        gradient_direction = format_gradient_direction(assignment.get("gradient_direction", "layer"))
        lines.append(
            f"  V{int(assignment['start_voxel'])}-{int(assignment['end_voxel'])} "
            f"S{steps} η{eta_value:.2f} {gradient_direction}"
        )

    remaining = len(assignments) - display_limit
    if remaining > 0:
        lines.append(f"  ... +{remaining} more")
    return "\n".join(lines)


def format_assignment_stats_compact(
    assignments: list[dict],
    voxel_range: tuple[int, int],
    voxel_lookup: dict[int, dict],
    selected_layer_range: tuple[int, int] | None,
    preprint_e: float,
    selected_e: float,
    start_cumulative: float,
    end_cumulative: float,
) -> str:
    low, high = voxel_range
    voxel_count = max(0, high - low + 1)
    lines = [
        "PREHEAT/PRIME E",
        f"{preprint_e:.6f}",
        "",
        "CURRENT RANGE",
        f"V{low} - V{high}",
        f"Voxel Count: {voxel_count}",
        f"Selected E: {selected_e:.6f}",
        f"Cumul E: {start_cumulative:.6f} - {end_cumulative:.6f}",
    ]
    if selected_layer_range is not None:
        lines.append(f"Layer Range: L{selected_layer_range[0]} - L{selected_layer_range[1]}")
    else:
        lines.append("Layer Range: none")

    lines.append("")
    lines.append("GRADIENT SETTINGS")
    if not assignments:
        lines.append("  none")
        return "\n".join(lines)

    display_limit = 8
    for assignment in assignments[:display_limit]:
        steps = clamp_gradient_steps(assignment.get("gradient_steps", DEFAULT_GRADIENT_STEPS))
        eta_value = clamp_eta_value(assignment.get("eta", DEFAULT_GRADIENT_ETA))
        gradient_direction = format_gradient_direction(assignment.get("gradient_direction", "layer"))
        lines.append(
            f"  V{int(assignment['start_voxel'])}-{int(assignment['end_voxel'])} "
            f"S{steps} eta={eta_value:.2f} {gradient_direction}"
        )

    remaining = len(assignments) - display_limit
    if remaining > 0:
        lines.append(f"  ... +{remaining} more")
    return "\n".join(lines)


def resolve_model_designer_output_dir(gcode_path: Path, output_dir_arg: str | None) -> Path:
    if output_dir_arg:
        return Path(output_dir_arg).resolve()
    return (gcode_path.parent / f"{gcode_path.stem}_model_designer_outputs").resolve()


def resolve_checkpoint_path(checkpoint_arg: str | Path) -> Path:
    candidate = Path(checkpoint_arg)
    if candidate.is_absolute() and candidate.exists():
        return candidate.resolve()

    ver3_model_dir = PROJECT_ROOT / "DM_filament_model ver3" / "Model_files"
    search_candidates = [
        Path.cwd() / candidate,
        PROJECT_ROOT / candidate,
        WORKSPACE_ROOT / candidate,
        CURRENT_DIR / candidate,
        PROJECT_ROOT / "Model_files" / candidate.name,
        CURRENT_DIR / "Model_files" / candidate.name,
        ver3_model_dir / candidate.name,
        WORKSPACE_ROOT / "Model_files" / candidate.name,
        WORKSPACE_ROOT / "Model_files" / "best_model_full_train.pth",
        ver3_model_dir / "best_model_full_train.pth",
    ]
    for path in search_candidates:
        if path.exists():
            return path.resolve()
    raise FileNotFoundError(
        f"Checkpoint not found. Tried: {', '.join(str(path) for path in search_candidates)}"
    )


def resolve_structure_vector_path(structure_vector_arg: str | None, designer_paths: dict[str, Path]) -> Path:
    if structure_vector_arg:
        path = Path(structure_vector_arg).resolve()
    else:
        path = designer_paths["voxel_npy"]

    stem_name = path.name
    candidate_roots = [
        path,
        PROJECT_ROOT / "DM_filament_model ver3" / "Dataset" / "Overview" / "3D_Structure" / stem_name,
        PROJECT_ROOT / "DM_filament_model ver3" / "Dataset" / "Overview" / "3D_Structure" / "vase_model_designer_outputs" / stem_name,
        PROJECT_ROOT / "bFDM후속" / "Vase" / stem_name,
        WORKSPACE_ROOT / "bFDM후속" / "Vase" / stem_name,
    ]
    for candidate in candidate_roots:
        if candidate.exists():
            return candidate.resolve()

    raise FileNotFoundError(
        f"Structure vector not found: {path}. "
        "Run the interactive model designer once to generate the voxel NPY, "
        "or pass --structure-vector explicitly."
    )


def resolve_property_json_path(designer_paths: dict[str, Path]) -> Path:
    path = designer_paths["program_json"]
    stem_name = path.name
    candidate_roots = [
        path,
        PROJECT_ROOT / "DM_filament_model ver3" / "Dataset" / "Overview" / "3D_Structure" / stem_name,
        PROJECT_ROOT / "DM_filament_model ver3" / "Dataset" / "Overview" / "3D_Structure" / "vase_model_designer_outputs" / stem_name,
        PROJECT_ROOT / "DM_filament_model ver3" / "Dataset" / "Property_Vector" / stem_name,
        PROJECT_ROOT / "bFDM후속" / "Vase" / stem_name,
        WORKSPACE_ROOT / "bFDM후속" / "Vase" / stem_name,
    ]
    for candidate in candidate_roots:
        if candidate.exists():
            return candidate.resolve()

    raise FileNotFoundError(
        f"Property program JSON not found: {path}. "
        "Run the interactive model designer once to generate the property program, "
        "or pass a valid generated file path."
    )


def make_padding_mask(lengths, max_len):
    positions = torch.arange(max_len, device=lengths.device).unsqueeze(0)
    return positions >= lengths.unsqueeze(1)


def load_model(checkpoint_path: Path, structure_vector_path: Path, property_json_path: Path, target_length: int | None):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    checkpoint = torch.load(str(checkpoint_path), map_location=device)
    if isinstance(checkpoint, dict) and "state_dict" in checkpoint:
        state_dict = checkpoint["state_dict"]
        max_target_length = int(checkpoint.get("max_target_length", 36))
        ratio_rows = int(checkpoint.get("ratio_rows", 14))
        length_classes = checkpoint.get("length_classes", [max_target_length])
        input_dim = int(checkpoint.get("input_dim", np.load(structure_vector_path).shape[-1]))
        text_dim = int(checkpoint.get("text_dim", encode_property_program(str(property_json_path)).shape[-1]))
    else:
        state_dict = checkpoint
        max_target_length = 36
        ratio_rows = 14
        length_classes = [max_target_length]
        input_dim = int(np.load(structure_vector_path).shape[-1])
        text_dim = int(encode_property_program(str(property_json_path)).shape[-1])

    model = HybridVoxelPredictor(
        input_dim=input_dim,
        text_dim=text_dim,
        max_target_length=max_target_length,
        ratio_rows=ratio_rows,
        length_classes=length_classes,
    ).to(device)
    load_result = model.load_state_dict(state_dict, strict=False)
    missing = list(getattr(load_result, "missing_keys", []))
    unexpected = list(getattr(load_result, "unexpected_keys", []))
    if missing or unexpected:
        print("[checkpoint-load] missing keys:", missing)
        print("[checkpoint-load] unexpected keys:", unexpected)
    model.eval()
    return model, device, max_target_length, ratio_rows, (None if target_length is None else int(target_length))


def run_prediction(
    structure_vector_path: Path,
    property_json_path: Path,
    checkpoint_path: Path,
    output_dir: Path | None,
    target_length: int | None,
    show_plot: bool,
):
    model, device, max_target_length, ratio_rows, target_length = load_model(
        checkpoint_path, structure_vector_path, property_json_path, target_length
    )

    q_array = np.load(structure_vector_path).astype(np.float32)
    x_encoded = encode_property_program(str(property_json_path))
    property_payload = load_property_payload(str(property_json_path))
    q = torch.tensor(q_array, dtype=torch.float32).unsqueeze(0).to(device)
    x = x_encoded.unsqueeze(0).to(device)
    q_lengths = torch.tensor([q.size(1)], dtype=torch.long, device=device)
    x_lengths = torch.tensor([x.size(1)], dtype=torch.long, device=device)
    effective_target_length = None if target_length is None else min(target_length, max_target_length)
    target_lengths = None if effective_target_length is None else torch.tensor([effective_target_length], dtype=torch.long, device=device)

    with torch.no_grad():
        pred_len_ratio, pred_ratio, material_logits, n_logits, fusion_attn, len_attn, ratio_attn = model(
            q, x, q_lengths, x_lengths, target_lengths
        )

    pred_n = int(model.length_classes[int(torch.argmax(n_logits.squeeze(0)).item())])
    effective_target_length = int(effective_target_length) if effective_target_length is not None else min(pred_n, int(pred_len_ratio.size(1)))
    pred_len_ratio = pred_len_ratio.squeeze(0).cpu()[:effective_target_length].numpy()
    material_logits = material_logits.squeeze(0).cpu()[:effective_target_length]
    allowed_mask = build_allowed_material_class_mask(
        str(property_json_path),
        int(material_logits.size(0)),
        int(material_logits.size(-1)),
    )
    allowed_mask = allowed_mask.unsqueeze(1).expand(-1, material_logits.size(1), -1)
    material_logits = material_logits.masked_fill(~allowed_mask, -1e9)
    pred_material_class = torch.argmax(material_logits, dim=-1).numpy().T

    from dataset_io_ver2 import decode_material_class_matrix

    pred_code_matrix = decode_material_class_matrix(pred_material_class)
    pred_ratio = compute_ratio_vector_from_code_matrix(pred_code_matrix, str(property_json_path))
    total_length = load_total_length_from_q(str(structure_vector_path), str(property_json_path))
    pred_length_abs = pred_len_ratio * float(total_length)
    pred_len_ratio_saved = np.asarray(pred_len_ratio, dtype=np.float32)[::-1].copy()
    pred_len_abs_saved = np.asarray(pred_length_abs, dtype=np.float32)[::-1].copy()
    pred_code_matrix_saved = np.asarray(pred_code_matrix, dtype=np.int32)[:, ::-1].copy()
    pred_ratio_saved = compute_ratio_vector_from_code_matrix(pred_code_matrix_saved, str(property_json_path))

    prediction_paths = get_prediction_output_paths(property_json_path, output_dir)

    if plt is not None:
        fig = plt.figure(figsize=(10, 11), dpi=150, facecolor="white")
        gs = fig.add_gridspec(3, 1, height_ratios=[5, 1.5, 1.4], hspace=0.28)
        ax_matrix = fig.add_subplot(gs[0, 0])
        ax_length = fig.add_subplot(gs[1, 0], sharex=ax_matrix)
        ax_ratio = fig.add_subplot(gs[2, 0], sharex=ax_matrix)
        plot_result_vector(
            ax_matrix,
            ax_length,
            ax_ratio,
            "Predicted",
            pred_length_abs,
            pred_ratio,
            pred_code_matrix,
            MATERIAL_CODEBOOK,
        )
        fig.suptitle(f"Property Program Model Preview: {property_json_path.stem}", fontsize=15, fontweight="bold")
        fig.tight_layout()
        fig.savefig(prediction_paths["preview_png"], bbox_inches="tight")
        if show_plot:
            plt.show()
        else:
            plt.close(fig)

    save_attention_preview(
        property_json_path.stem,
        prediction_paths["attention_png"].parent,
        fusion_attn.squeeze(0).cpu().numpy(),
        len_attn.squeeze(0).cpu().numpy(),
        ratio_attn.squeeze(0).cpu().numpy(),
        x.squeeze(0).cpu(),
        int(x_lengths.item()),
        show_plot=False,
    )
    auto_attention_path = prediction_paths["attention_png"].parent / f"{property_json_path.stem}_attention_preview.png"
    if auto_attention_path.exists() and auto_attention_path != prediction_paths["attention_png"]:
        auto_attention_path.replace(prediction_paths["attention_png"])

    np.savez(
        prediction_paths["bundle_npz"],
        length_ratio=pred_len_ratio_saved,
        length_absolute=pred_len_abs_saved,
        ratio_vector=pred_ratio_saved,
        material_code_matrix=pred_code_matrix_saved,
        material_class_matrix=pred_material_class,
    )
    np.savez(
        prediction_paths["input_bundle_npz"],
        q_input=q_array,
        x_input=x_encoded.cpu().numpy(),
        q_length=np.array([q_array.shape[0]], dtype=np.int32),
        x_length=np.array([x_encoded.shape[0]], dtype=np.int32),
        target_length=np.array([effective_target_length], dtype=np.int32),
    )
    with open(prediction_paths["meta_json"], "w", encoding="utf-8") as handle:
        json.dump(
            {
                "structure_vector": str(structure_vector_path),
                "property_program": str(property_json_path),
                "checkpoint": str(checkpoint_path),
                "target_length": effective_target_length,
                "predicted_n": int(pred_n),
                "ratio_rows": ratio_rows,
                "total_length": float(total_length),
                "saved_orientation": "display-order/rightmost-is-col0",
                "saved_files": {key: str(path) for key, path in prediction_paths.items()},
            },
            handle,
            indent=2,
        )
    with open(prediction_paths["input_json"], "w", encoding="utf-8") as handle:
        json.dump(
            {
                "structure_vector": str(structure_vector_path),
                "property_program": str(property_json_path),
                "checkpoint": str(checkpoint_path),
                "q_shape": [int(dim) for dim in q_array.shape],
                "x_shape": [int(dim) for dim in x_encoded.shape],
                "q_dtype": str(q_array.dtype),
                "x_dtype": str(x_encoded.cpu().numpy().dtype),
                "target_length": int(effective_target_length),
                "predicted_n": int(pred_n),
                "property_program_payload": property_payload,
                "saved_files": {
                    "input_bundle_npz": str(prediction_paths["input_bundle_npz"]),
                    "prediction_bundle_npz": str(prediction_paths["bundle_npz"]),
                },
            },
            handle,
            indent=2,
        )
    return prediction_paths


def launch_model_designer(
    gcode_path: Path,
    structure_vector_path: Path,
    checkpoint_path: Path,
    output_paths: dict[str, Path],
    voxels: list[dict],
    flat_segments: np.ndarray,
    preprint_e: float,
    delta_e: float,
    target_length: int | None,
):
    if plt is None or TextBox is None or Button is None or RadioButtons is None or flat_segments.size == 0:
        return

    voxel_ids = np.array([int(voxel["voxel_id"]) for voxel in voxels], dtype=int)
    selection_cache = build_voxel_selection_cache(voxels)
    voxel_lookup = build_voxel_lookup(voxels)
    voxel_plot_ids, voxel_path_cache = build_voxel_plot_cache(flat_segments)
    min_voxel = int(voxel_ids.min())
    max_voxel = int(voxel_ids.max())
    layer_to_voxel_ids = build_layer_to_voxel_ids(voxels)
    voxel_to_layer_id: dict[int, int] = {
        int(voxel_id): int(layer_num)
        for layer_num, voxel_id_list in layer_to_voxel_ids.items()
        for voxel_id in voxel_id_list
    }
    available_layers = sorted(layer_to_voxel_ids.keys())
    min_layer = min(available_layers) if available_layers else 1
    max_layer = max(available_layers) if available_layers else 1
    assignments: list[dict] = []
    pending_assignments: list[dict] = []

    fig = plt.figure(figsize=(17, 10), dpi=120, facecolor="#f4f1ea")
    ax = fig.add_axes([0.00, 0.14, 0.48, 0.68], projection="3d")
    min_ax = fig.add_axes([0.06, 0.04, 0.05, 0.035], facecolor="#f4f1ea")
    max_ax = fig.add_axes([0.14, 0.04, 0.05, 0.035], facecolor="#f4f1ea")
    layer_min_ax = fig.add_axes([0.23, 0.04, 0.05, 0.035], facecolor="#f4f1ea")
    layer_max_ax = fig.add_axes([0.31, 0.04, 0.05, 0.035], facecolor="#f4f1ea")
    material_count_ax = fig.add_axes([0.58, 0.73, 0.10, 0.12], facecolor="#fbfaf7")
    edit_slot_ax = fig.add_axes([0.70, 0.73, 0.10, 0.12], facecolor="#fbfaf7")
    material_choice_ax = fig.add_axes([0.82, 0.58, 0.14, 0.27], facecolor="#fbfaf7")
    start_mat_ax = fig.add_axes([0.58, 0.56, 0.10, 0.10], facecolor="#fbfaf7")
    end_mat_ax = fig.add_axes([0.70, 0.56, 0.10, 0.10], facecolor="#fbfaf7")
    ratio1_ax = fig.add_axes([0.80, 0.475, 0.055, 0.04], facecolor="#f4f1ea")
    ratio2_ax = fig.add_axes([0.885, 0.475, 0.055, 0.04], facecolor="#f4f1ea")
    brightness_ax = fig.add_axes([0.58, 0.34, 0.18, 0.10], facecolor="#fbfaf7")
    gradient_steps_ax = fig.add_axes([0.58, 0.22, 0.08, 0.04], facecolor="#f4f1ea")
    eta_ax = fig.add_axes([0.68, 0.22, 0.08, 0.04], facecolor="#f4f1ea")
    gradient_direction_ax = fig.add_axes([0.58, 0.12, 0.18, 0.08], facecolor="#fbfaf7")
    add_btn_ax = fig.add_axes([0.80, 0.24, 0.12, 0.05])
    remove_btn_ax = fig.add_axes([0.80, 0.17, 0.12, 0.05])
    save_btn_ax = fig.add_axes([0.80, 0.10, 0.12, 0.05])
    stats_ax = fig.add_axes([0.58, 0.00, 0.38, 0.08], facecolor="#fbfaf7")
    stats_ax.axis("off")
    stats_text = stats_ax.text(
        0.04, 0.92, "", va="top", ha="left", family="monospace", fontsize=9.4, color="#1f2937", linespacing=1.18
    )

    min_box = TextBox(min_ax, "Start", initial=str(min_voxel), color="#ffffff", hovercolor="#eef2ff")
    max_box = TextBox(max_ax, "End", initial=str(min(min_voxel + 20, max_voxel)), color="#ffffff", hovercolor="#eef2ff")
    layer_min_box = TextBox(layer_min_ax, "L Start", initial=str(min_layer), color="#ffffff", hovercolor="#eef2ff")
    layer_max_box = TextBox(layer_max_ax, "L End", initial=str(min(min_layer + 4, max_layer)), color="#ffffff", hovercolor="#eef2ff")
    material_count_radio = RadioButtons(material_count_ax, MATERIAL_COUNT_OPTIONS, active=1)
    edit_slot_radio = RadioButtons(edit_slot_ax, START_END_SLOT_OPTIONS, active=0)
    material_choice_radio = RadioButtons(material_choice_ax, MATERIAL_OPTIONS, active=0)
    start_mat_radio = RadioButtons(start_mat_ax, START_END_SLOT_OPTIONS, active=0)
    end_mat_radio = RadioButtons(end_mat_ax, START_END_SLOT_OPTIONS, active=1)
    ratio1_box = TextBox(ratio1_ax, "", initial="50", color="#ffffff", hovercolor="#eef2ff")
    ratio2_box = TextBox(ratio2_ax, "", initial="50", color="#ffffff", hovercolor="#eef2ff")
    brightness_radio = RadioButtons(brightness_ax, BRIGHTNESS_OPTIONS, active=0)
    gradient_steps_box = TextBox(gradient_steps_ax, "", initial=str(DEFAULT_GRADIENT_STEPS), color="#ffffff", hovercolor="#eef2ff")
    eta_box = TextBox(eta_ax, "", initial=f"{DEFAULT_GRADIENT_ETA:.2f}", color="#ffffff", hovercolor="#eef2ff")
    gradient_direction_radio = RadioButtons(gradient_direction_ax, GRADIENT_DIRECTION_OPTIONS, active=0)
    ratio1_ax.text(0.5, 1.18, "Mat ratio 1", transform=ratio1_ax.transAxes, fontsize=8.8, color="#374151", ha="center", va="bottom")
    ratio2_ax.text(0.5, 1.18, "Mat ratio 2", transform=ratio2_ax.transAxes, fontsize=8.8, color="#374151", ha="center", va="bottom")
    gradient_steps_ax.text(0.5, 1.18, "step count", transform=gradient_steps_ax.transAxes, fontsize=8.8, color="#374151", ha="center", va="bottom")
    eta_ax.text(0.5, 1.18, "eta", transform=eta_ax.transAxes, fontsize=8.8, color="#374151", ha="center", va="bottom")
    gradient_direction_ax.set_title("Gradient Dir", fontsize=10, color="#111827", pad=4)
    material_choice_ax.text(0.52, 0.47, "Base Mat", transform=material_choice_ax.transAxes, fontsize=9.2, color="#374151", fontweight="bold", ha="left", va="center")
    material_choice_ax.add_patch(Rectangle((0.07, 0.05), 0.88, 0.39, transform=material_choice_ax.transAxes, facecolor="none", edgecolor="#94a3b8", linewidth=2.0, joinstyle="round"))

    add_button = Button(add_btn_ax, "Add", color="#dbeafe", hovercolor="#bfdbfe")
    remove_button = Button(remove_btn_ax, "Remove", color="#fecaca", hovercolor="#fca5a5")
    save_button = Button(save_btn_ax, "Result", color="#dbeafe", hovercolor="#bfdbfe")

    for box in [min_box, max_box, layer_min_box, layer_max_box]:
        box.label.set_fontsize(9)

    ui_state = {
        "material_count": 2,
        "material_1": "PLA",
        "material_2": "CPLA",
        "edit_slot": "Mat 1",
        "start_material_slot": "Mat 1",
        "end_material_slot": "Mat 2",
        "color_ratio_1": 50,
        "color_ratio_2": 50,
        "brightness": "off",
        "gradient_steps": DEFAULT_GRADIENT_STEPS,
        "eta": DEFAULT_GRADIENT_ETA,
        "gradient_direction": "layer",
    }
    is_syncing = {"value": False}

    def active_slots():
        return START_END_SLOT_OPTIONS[: int(ui_state["material_count"])]

    def set_radio_active(radio, labels: list[str], value: str) -> None:
        if value in labels and radio.value_selected != value:
            radio.set_active(labels.index(value))

    def sync_radios():
        is_syncing["value"] = True
        set_radio_active(material_count_radio, MATERIAL_COUNT_OPTIONS, str(int(ui_state["material_count"])))
        set_radio_active(edit_slot_radio, START_END_SLOT_OPTIONS, ui_state["edit_slot"])
        current_material = ui_state[f"material_{START_END_SLOT_OPTIONS.index(ui_state['edit_slot']) + 1}"]
        set_radio_active(material_choice_radio, MATERIAL_OPTIONS, current_material)
        set_radio_active(start_mat_radio, START_END_SLOT_OPTIONS, ui_state["start_material_slot"])
        set_radio_active(end_mat_radio, START_END_SLOT_OPTIONS, ui_state["end_material_slot"])
        set_radio_active(brightness_radio, BRIGHTNESS_OPTIONS, ui_state["brightness"])
        set_radio_active(gradient_direction_radio, GRADIENT_DIRECTION_OPTIONS, ui_state["gradient_direction"])
        ratio1_box.set_val(str(int(ui_state["color_ratio_1"])))
        ratio2_box.set_val(str(int(ui_state["color_ratio_2"])))
        gradient_steps_box.set_val(str(int(ui_state["gradient_steps"])))
        eta_box.set_val(f"{float(ui_state['eta']):.2f}")
        is_syncing["value"] = False

    def current_range():
        low = parse_int(min_box.text, min_voxel)
        high = parse_int(max_box.text, min(min_voxel + 20, max_voxel))
        return clamp_range(low, high, min_voxel, max_voxel)

    def clamp_layer_range(low: int, high: int):
        low = max(min_layer, min(max_layer, low))
        high = max(min_layer, min(max_layer, high))
        if low > high:
            low, high = high, low
        return low, high

    def current_layer_range():
        low = parse_int(layer_min_box.text, min_layer)
        high = parse_int(layer_max_box.text, min(min_layer + 4, max_layer))
        return clamp_layer_range(low, high)

    def voxel_range_from_layers(layer_range):
        low_layer, high_layer = layer_range
        selected_voxel_ids = [
            voxel_id
            for layer_num in range(low_layer, high_layer + 1)
            if layer_num in layer_to_voxel_ids
            for voxel_id in layer_to_voxel_ids[layer_num]
        ]
        if not selected_voxel_ids:
            return None
        return min(selected_voxel_ids), max(selected_voxel_ids)

    def layers_from_voxel_range(voxel_range):
        return layers_from_voxel_range_cached(selection_cache, voxel_range)

    def build_preview_assignments() -> list[dict]:
        preview = [dict(assignment) for assignment in assignments] + [dict(assignment) for assignment in pending_assignments]
        for idx, assignment in enumerate(preview, start=1):
            assignment["assignment_index"] = idx
        return preview

    def upsert_assignment(collection: list[dict], assignment: dict) -> None:
        for idx in range(len(collection) - 1, -1, -1):
            existing = collection[idx]
            if int(existing.get("start_voxel", -1)) == int(assignment["start_voxel"]) and int(existing.get("end_voxel", -1)) == int(assignment["end_voxel"]):
                collection[idx] = assignment
                return
        collection.append(assignment)

    def remove_assignment_from(collection: list[dict], assignment: dict) -> bool:
        for idx in range(len(collection) - 1, -1, -1):
            existing = collection[idx]
            if int(existing.get("start_voxel", -1)) == int(assignment["start_voxel"]) and int(existing.get("end_voxel", -1)) == int(assignment["end_voxel"]):
                collection.pop(idx)
                return True
        return False

    def build_assignment():
        low, high = current_range()
        selected_layer_range = layers_from_voxel_range((low, high))
        selected_voxels = [
            voxel_lookup[voxel_id]
            for voxel_id in range(low, high + 1)
            if voxel_id in voxel_lookup
        ]
        voxel_layer_table = [
            {
                "voxel_id": int(voxel["voxel_id"]),
                "layer_num": int(voxel_to_layer_id.get(int(voxel["voxel_id"]), -1)),
            }
            for voxel in selected_voxels
        ]
        selected_e = float(sum(float(voxel.get("voxel_e", 0.0)) for voxel in selected_voxels))
        total_length_mm = selected_e
        material_count = int(ui_state["material_count"])
        mat_ratio_1 = float(ui_state["color_ratio_1"])
        mat_ratio_2 = float(ui_state["color_ratio_2"])
        estimated_material_1_e = selected_e * (mat_ratio_1 / 100.0) if material_count >= 1 else 0.0
        estimated_material_2_e = selected_e * (mat_ratio_2 / 100.0) if material_count >= 2 else 0.0
        layer_start = -1
        layer_end = -1
        layer_count = 0
        if selected_layer_range is not None:
            layer_start, layer_end = selected_layer_range
            layer_count = max(0, layer_end - layer_start + 1)
        return {
            "assignment_index": len(assignments) + len(pending_assignments) + 1,
            "start_voxel": low,
            "end_voxel": high,
            "layer_start": int(layer_start),
            "layer_end": int(layer_end),
            "layer_count": int(layer_count),
            "voxel_count": len(selected_voxels),
            "total_filament_e_mm": round(selected_e, 6),
            "total_filament_length_mm": round(total_length_mm, 6),
            "material_count": material_count,
            "material_1": ui_state["material_1"],
            "material_2": ui_state["material_2"] if material_count == 2 else None,
            "start_material_slot": ui_state["start_material_slot"],
            "end_material_slot": ui_state["end_material_slot"],
            "voxel_layer_table": voxel_layer_table,
            "mat_ratio_1": mat_ratio_1,
            "mat_ratio_2": mat_ratio_2 if material_count >= 2 else 0.0,
            "gradient_steps": int(ui_state["gradient_steps"]),
            "gradient_direction": ui_state["gradient_direction"],
            "brightness": ui_state["brightness"],
            "eta": float(ui_state["eta"]),
            "direction": "filament_path" if ui_state["gradient_direction"] == "layer" else "reverse_filament_path",
            "estimated_material_1_length_mm": round(estimated_material_1_e, 6),
            "estimated_material_2_length_mm": round(estimated_material_2_e, 6),
        }

    def save_current_assignment(_event=None) -> None:
        preview_assignments = build_preview_assignments()
        if not preview_assignments:
            return
        finalized_assignments: list[dict] = []
        seen_ranges: set[tuple[int, int]] = set()
        for assignment in preview_assignments:
            key = (int(assignment.get("start_voxel", -1)), int(assignment.get("end_voxel", -1)))
            if key in seen_ranges:
                for idx in range(len(finalized_assignments) - 1, -1, -1):
                    existing = finalized_assignments[idx]
                    existing_key = (int(existing.get("start_voxel", -1)), int(existing.get("end_voxel", -1)))
                    if existing_key == key:
                        finalized_assignments[idx] = dict(assignment)
                        break
                continue
            seen_ranges.add(key)
            finalized_assignments.append(dict(assignment))

        for idx, assignment in enumerate(finalized_assignments, start=1):
            assignment["assignment_index"] = idx

        assignments.clear()
        assignments.extend(finalized_assignments)
        pending_assignments.clear()
        save_property_program(output_paths, gcode_path, delta_e, voxels, preprint_e, assignments)
        refresh_plot()

    def add_assignment(_event=None) -> None:
        assignment = build_assignment()
        upsert_assignment(pending_assignments, assignment)
        refresh_plot()

    def remove_assignment(_event=None) -> None:
        if not assignments and not pending_assignments:
            return
        current = build_assignment()
        if not remove_assignment_from(pending_assignments, current):
            if not remove_assignment_from(assignments, current) and assignments:
                assignments.pop(-1)
        refresh_plot()

    def refresh_plot():
        low, high = current_range()
        preview_assignments = build_preview_assignments()
        assignment_color_map = build_assignment_color_map(preview_assignments)
        legend_entries = build_assignment_legend_entries(preview_assignments)
        plot_voxels_on_axis(
            ax,
            voxel_plot_ids,
            voxel_path_cache,
            (low, high),
            (min_voxel, max_voxel),
            assignment_color_map=assignment_color_map,
        )
        label_positions = build_assignment_label_positions(preview_assignments, voxel_lookup)
        for label_info in label_positions:
            x_center, y_center, z_center = label_info["position"]
            ax.text(
                x_center,
                y_center,
                z_center,
                label_info["display_label"],
                fontsize=9.0,
                fontweight="bold",
                color="#111827",
                ha="center",
                va="center",
                bbox=dict(
                    boxstyle="round,pad=0.28",
                    facecolor="white",
                    edgecolor="#111827",
                    alpha=0.88,
                ),
            )
        selected_e, start_cumulative, end_cumulative = compute_selection_filament_e(selection_cache, (low, high))
        selected_layer_range = layers_from_voxel_range((low, high))
        program_lines = ["Programs:"]
        if not legend_entries:
            program_lines.append("  none")
        else:
            max_entries = 6
            for entry in legend_entries[:max_entries]:
                program_lines.append(f"  - {entry['label']}")
            remaining = len(legend_entries) - max_entries
            if remaining > 0:
                program_lines.append(f"  ... +{remaining} more")
        stats_text.set_text(
            "`n".join(program_lines)
            + "`n`n"
            + format_assignment_stats_compact(
                preview_assignments,
                (low, high),
                voxel_lookup,
                selected_layer_range,
                preprint_e,
                selected_e,
                start_cumulative,
                end_cumulative,
            )
        )
        if selected_layer_range is not None:
            layer_min_box.set_val(str(selected_layer_range[0]))
            layer_max_box.set_val(str(selected_layer_range[1]))
        min_box.set_val(str(low))
        max_box.set_val(str(high))
        fig.canvas.draw_idle()

    def on_material_count(label):
        if is_syncing["value"]:
            return
        ui_state["material_count"] = int(label)
        if int(ui_state["material_count"]) == 1:
            ui_state["edit_slot"] = "Mat 1"
            ui_state["start_material_slot"] = "Mat 1"
            ui_state["end_material_slot"] = "Mat 1"
        sync_radios()
        refresh_plot()

    def on_edit_slot(label):
        if is_syncing["value"]:
            return
        ui_state["edit_slot"] = label
        sync_radios()
        refresh_plot()

    def on_material_choice(label):
        if is_syncing["value"]:
            return
        slot_index = START_END_SLOT_OPTIONS.index(ui_state["edit_slot"]) + 1
        if slot_index <= int(ui_state["material_count"]):
            ui_state[f"material_{slot_index}"] = label
        sync_radios()
        refresh_plot()

    def on_start_material(label):
        if is_syncing["value"]:
            return
        ui_state["start_material_slot"] = label
        sync_radios()
        refresh_plot()

    def on_end_material(label):
        if is_syncing["value"]:
            return
        ui_state["end_material_slot"] = label
        sync_radios()
        refresh_plot()

    def on_color_ratio_1(_text):
        if is_syncing["value"]:
            return
        ratio_1 = clamp_ratio_value(parse_int(ratio1_box.text, int(ui_state["color_ratio_1"])))
        ui_state["color_ratio_1"] = ratio_1
        ui_state["color_ratio_2"] = 100 - ratio_1
        sync_radios()
        refresh_plot()

    def on_color_ratio_2(_text):
        if is_syncing["value"]:
            return
        ratio_2 = clamp_ratio_value(parse_int(ratio2_box.text, int(ui_state["color_ratio_2"])))
        ui_state["color_ratio_2"] = ratio_2
        ui_state["color_ratio_1"] = 100 - ratio_2
        sync_radios()
        refresh_plot()

    def on_brightness(label):
        if is_syncing["value"]:
            return
        ui_state["brightness"] = label
        refresh_plot()

    def on_gradient_steps(_text):
        if is_syncing["value"]:
            return
        ui_state["gradient_steps"] = clamp_gradient_steps(gradient_steps_box.text)
        sync_radios()
        refresh_plot()

    def on_eta(_text):
        if is_syncing["value"]:
            return
        ui_state["eta"] = clamp_eta_value(eta_box.text)
        sync_radios()
        refresh_plot()

    def on_gradient_direction(label):
        if is_syncing["value"]:
            return
        ui_state["gradient_direction"] = format_gradient_direction(label)
        sync_radios()
        refresh_plot()

    def submit_any(_text):
        refresh_plot()

    def submit_layer_any(_text):
        layer_range = current_layer_range()
        voxel_range = voxel_range_from_layers(layer_range)
        if voxel_range is not None:
            min_box.set_val(str(voxel_range[0]))
            max_box.set_val(str(voxel_range[1]))
        layer_min_box.set_val(str(layer_range[0]))
        layer_max_box.set_val(str(layer_range[1]))
        refresh_plot()

    for box in [min_box, max_box]:
        box.on_submit(submit_any)
    for box in [layer_min_box, layer_max_box]:
        box.on_submit(submit_layer_any)
    material_count_radio.on_clicked(on_material_count)
    edit_slot_radio.on_clicked(on_edit_slot)
    material_choice_radio.on_clicked(on_material_choice)
    start_mat_radio.on_clicked(on_start_material)
    end_mat_radio.on_clicked(on_end_material)
    ratio1_box.on_submit(on_color_ratio_1)
    ratio2_box.on_submit(on_color_ratio_2)
    brightness_radio.on_clicked(on_brightness)
    gradient_steps_box.on_submit(on_gradient_steps)
    eta_box.on_submit(on_eta)
    gradient_direction_radio.on_clicked(on_gradient_direction)
    add_button.on_clicked(add_assignment)
    remove_button.on_clicked(remove_assignment)
    save_button.on_clicked(save_current_assignment)
    material_count_ax.set_title("Material Count", fontsize=10, color="#111827", pad=4)
    edit_slot_ax.set_title("Edit Slot", fontsize=10, color="#111827", pad=4)
    material_choice_ax.set_title("Material", fontsize=10, color="#111827", pad=4)
    start_mat_ax.set_title("Start Material", fontsize=10, color="#111827", pad=4)
    end_mat_ax.set_title("End Material", fontsize=10, color="#111827", pad=4)
    brightness_ax.set_title("Brightness", fontsize=10, color="#111827", pad=4)
    gradient_direction_ax.set_title("Gradient Dir", fontsize=10, color="#111827", pad=4)
    stats_ax.set_title("Voxel / Program", loc="left", fontsize=12, color="#111827", pad=0, fontweight="bold")
    fig.text(0.50, 0.96, "b-FDM Model Designer", fontsize=18, fontweight="bold", color="#111827", ha="center")

    sync_radios()
    refresh_plot()
    plt.show()


def main():
    parser = argparse.ArgumentParser(
        description="Launch the voxel property-program assignment designer."
    )
    parser.add_argument("gcode_file", help="Path to the source G-code used for voxelized property-program design")
    parser.add_argument("--output-dir", default=None, help="Directory to save assignment designer exports")
    parser.add_argument("--delta-e", type=float, default=2.0, help="Voxel threshold based on accumulated E")
    args = parser.parse_args()

    gcode_path = Path(args.gcode_file).resolve()
    output_dir = resolve_model_designer_output_dir(gcode_path, args.output_dir)
    designer_paths = get_designer_output_paths(gcode_path, output_dir)

    cached = load_voxel_cache(designer_paths, gcode_path, args.delta_e)
    if cached is None:
        unit_segments, preprint_e = parse_gcode_extrusion_segments(str(gcode_path))
        voxels, flat_segments = group_segments_into_voxels(unit_segments, args.delta_e)
        save_voxel_cache(designer_paths, gcode_path, args.delta_e, voxels, flat_segments, preprint_e)
    else:
        voxels, flat_segments, preprint_e = cached
    annotate_voxels_with_layers(voxels, gcode_path)
    print_summary(voxels, preprint_e, designer_paths)
    launch_model_designer(
        gcode_path=gcode_path,
        structure_vector_path=None,
        checkpoint_path=None,
        output_paths=designer_paths,
        voxels=voxels,
        flat_segments=flat_segments,
        preprint_e=preprint_e,
        delta_e=args.delta_e,
        target_length=None,
    )


if __name__ == "__main__":
    main()
