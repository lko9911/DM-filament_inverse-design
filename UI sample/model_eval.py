import io
import json
import os
import re
import tempfile
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from scipy.io import savemat
from model_train_full_train import HybridVoxelPredictor as TrainHybridVoxelPredictor

BASE_DIR = Path(__file__).resolve().parent

from dataset_io_ver2 import MATERIAL_CODEBOOK
from dataset_io_ver2 import MATERIAL_CODE_TO_CLASS
from dataset_io_ver2 import get_assignment_for_output_index
from dataset_io_ver2 import MATERIAL_CLASS_CODES
from dataset_io_ver2 import build_samples
from dataset_io_ver2 import build_allowed_material_class_mask
from dataset_io_ver2 import class_index_to_length
from dataset_io_ver2 import compute_max_material_ratio_vector_from_code_matrix
from dataset_io_ver2 import decode_material_class_matrix
from dataset_io_ver2 import filter_variable_target_samples
from dataset_io_ver2 import get_max_target_length
from dataset_io_ver2 import load_property_payload
from dataset_io_ver2 import load_sample_tensors
from dataset_io_ver2 import load_total_length_from_q
from dataset_io_ver2 import reverse_matrix_columns_to_sequence_order
from dataset_io_ver2 import resolve_assignment_material_code

try:
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm
    from matplotlib.colors import ListedColormap
except ModuleNotFoundError:
    plt = None
    BoundaryNorm = None
    ListedColormap = None


PROPERTY_FEATURE_NAMES = [
    "start_voxel",
    "end_voxel",
    "start_material",
    "end_material",
    "pair_min_material",
    "pair_max_material",
    "transition",
    "color_ratio_1",
    "color_ratio_2",
    "brightness",
    "direction",
    "assignment_order",
]


def make_padding_mask(lengths, max_len):
    positions = torch.arange(max_len, device=lengths.device).unsqueeze(0)
    return positions >= lengths.unsqueeze(1)


def masked_softmax(logits, lengths):
    mask = make_padding_mask(lengths, logits.size(1))
    logits_fp32 = logits.float().masked_fill(mask, -1e9)
    logits_fp32 = logits_fp32 - logits_fp32.max(dim=-1, keepdim=True).values
    exp_logits = torch.exp(logits_fp32).masked_fill(mask, 0.0)
    denom = exp_logits.sum(dim=-1, keepdim=True).clamp_min(1e-12)
    probs = exp_logits / denom
    return probs.to(logits.dtype)


def compute_ratio_vector_from_code_matrix(material_code_matrix: np.ndarray, property_json_path: str) -> np.ndarray:
    matrix = np.asarray(material_code_matrix, dtype=np.int32)
    if matrix.ndim != 2:
        raise ValueError(f"material_code_matrix must be 2D, got {matrix.shape}")
    return compute_max_material_ratio_vector_from_code_matrix(matrix)


class HybridVoxelPredictor(nn.Module):
    def __init__(self, input_dim=5, text_dim=9, max_target_length=36, ratio_rows=1, length_classes=None, embed_dim=128):
        super().__init__()
        self.max_target_length = int(max_target_length)
        self.ratio_rows = int(ratio_rows)
        self.text_dim = int(text_dim)
        self.length_classes = tuple(int(v) for v in (length_classes or [self.max_target_length]))
        self.register_buffer("length_class_values", torch.tensor(self.length_classes, dtype=torch.long), persistent=False)

        self.input_proj = nn.Linear(input_dim, embed_dim)
        self.text_value_proj = nn.Linear(1, embed_dim)
        self.feature_embedding = nn.Embedding(self.text_dim, embed_dim)
        self.x_pos_proj = nn.Sequential(nn.Linear(2, embed_dim), nn.Tanh())
        self.q_pos_proj = nn.Sequential(nn.Linear(1, embed_dim), nn.Tanh())
        self.compressor = nn.Sequential(
            nn.Conv1d(embed_dim, embed_dim, kernel_size=11, stride=10, padding=5),
            nn.BatchNorm1d(embed_dim),
            nn.ReLU(),
        )

        self.fusion_attn = nn.MultiheadAttention(embed_dim, num_heads=8, batch_first=True)
        self.norm1 = nn.LayerNorm(embed_dim)
        self.length_count_head = nn.Sequential(
            nn.Linear(embed_dim * 2, embed_dim),
            nn.LayerNorm(embed_dim),
            nn.ReLU(),
            nn.Linear(embed_dim, len(self.length_classes)),
        )

        self.queries_len = nn.Parameter(torch.randn(1, self.max_target_length, embed_dim))
        self.queries_rat = nn.Parameter(torch.randn(1, self.max_target_length, embed_dim))

        self.decoder_attn_len = nn.MultiheadAttention(embed_dim, num_heads=8, batch_first=True)
        self.decoder_attn_rat = nn.MultiheadAttention(embed_dim, num_heads=8, batch_first=True)

        self.ffn_len = nn.Sequential(
            nn.Linear(embed_dim, embed_dim * 2),
            nn.ReLU(),
            nn.Linear(embed_dim * 2, embed_dim),
        )
        self.ffn_rat = nn.Sequential(
            nn.Linear(embed_dim, embed_dim * 2),
            nn.ReLU(),
            nn.Linear(embed_dim * 2, embed_dim),
        )

        self.norm_l1 = nn.LayerNorm(embed_dim)
        self.norm_l2 = nn.LayerNorm(embed_dim)
        self.norm_r1 = nn.LayerNorm(embed_dim)
        self.norm_r2 = nn.LayerNorm(embed_dim)

        self.mlp = nn.Sequential(
            nn.Linear(embed_dim, embed_dim),
            nn.LayerNorm(embed_dim),
            nn.ReLU(),
        )
        self.ratio_context_proj = nn.Sequential(
            nn.Linear(embed_dim * 2, embed_dim),
            nn.LayerNorm(embed_dim),
            nn.ReLU(),
        )

        self.head_length = nn.Sequential(
            nn.Linear(embed_dim, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
        )
        self.head_ratio = nn.Linear(embed_dim, 1)
        self.row_embedding = nn.Embedding(self.ratio_rows, embed_dim)
        self.material_head = nn.Sequential(
            nn.Linear(embed_dim, embed_dim),
            nn.LayerNorm(embed_dim),
            nn.ReLU(),
            nn.Linear(embed_dim, len(MATERIAL_CLASS_CODES)),
        )
        self.locality_scale = nn.Parameter(torch.tensor(12.0))

    @staticmethod
    def compressed_lengths(q_lengths):
        return torch.div(q_lengths + 9, 10, rounding_mode="floor")

    def forward(self, q, x, q_lengths=None, x_lengths=None, target_lengths=None):
        batch_size = q.size(0)

        if x.dim() > 3:
            x = x.view(batch_size, -1, x.size(-1))
        feature_count = x.size(-1)

        query_positions = torch.linspace(0.0, 1.0, self.max_target_length, device=q.device, dtype=q.dtype)
        query_positions = query_positions.view(1, self.max_target_length, 1).expand(batch_size, -1, -1)
        assignment_intervals = x[..., :2]
        feature_ids = torch.arange(feature_count, device=x.device)

        v = self.input_proj(q).transpose(1, 2)
        v = self.compressor(v).transpose(1, 2)
        x_values = x.unsqueeze(-1)
        x_feature_embed = self.text_value_proj(x_values)
        feature_embed = self.feature_embedding(feature_ids).view(1, 1, feature_count, -1)
        interval_embed = self.x_pos_proj(assignment_intervals).unsqueeze(2)
        x_embed = x_feature_embed + feature_embed + interval_embed
        assignment_embed = x_embed.mean(dim=2)
        x_embed = x_embed.view(batch_size, -1, x_embed.size(-1))

        x_padding_mask = None
        v_padding_mask = None
        if q_lengths is not None:
            compressed_q_lengths = self.compressed_lengths(q_lengths)
            v_padding_mask = make_padding_mask(compressed_q_lengths, v.size(1)).to(v.device)
        if x_lengths is not None:
            x_token_lengths = x_lengths * feature_count
            x_padding_mask = make_padding_mask(x_token_lengths, x_embed.size(1)).to(x.device)

        fused_attn_out, v_t_attn = self.fusion_attn(v, x_embed, x_embed, key_padding_mask=x_padding_mask)
        fused_feat = self.norm1(v + fused_attn_out)
        if v_padding_mask is not None:
            fused_feat = fused_feat.masked_fill(v_padding_mask.unsqueeze(-1), 0.0)
        fused_pool = fused_feat.sum(dim=1) / compressed_q_lengths.clamp_min(1).unsqueeze(1).to(fused_feat.dtype)
        assignment_pool = assignment_embed.sum(dim=1) / x_lengths.clamp_min(1).unsqueeze(1).to(assignment_embed.dtype)
        target_length_logits = self.length_count_head(torch.cat([fused_pool, assignment_pool], dim=-1))
        if target_lengths is None:
            pred_length_class = torch.argmax(target_length_logits, dim=-1)
            decode_lengths = self.length_class_values[pred_length_class]
        else:
            decode_lengths = target_lengths

        q_l = self.queries_len.expand(batch_size, -1, -1) + self.q_pos_proj(query_positions)
        l_attn_out, l_attn = self.decoder_attn_len(q_l, fused_feat, fused_feat, key_padding_mask=v_padding_mask)
        l_hidden = self.norm_l1(q_l + l_attn_out)
        context_l = self.norm_l2(l_hidden + self.ffn_len(l_hidden))
        l_logits = self.head_length(self.mlp(context_l)).squeeze(-1)
        l_pred = masked_softmax(l_logits, decode_lengths)

        q_r = self.queries_rat.expand(batch_size, -1, -1) + self.q_pos_proj(query_positions)
        r_attn_out, r_attn = self.decoder_attn_rat(q_r, fused_feat, fused_feat, key_padding_mask=v_padding_mask)
        r_hidden = self.norm_r1(q_r + r_attn_out)
        assignment_centers = assignment_intervals.mean(dim=-1)
        local_distance = torch.abs(query_positions.squeeze(-1).unsqueeze(-1) - assignment_centers.unsqueeze(1))
        local_logits = -torch.abs(self.locality_scale) * local_distance
        assignment_padding_mask = make_padding_mask(x_lengths, assignment_embed.size(1)).to(x.device)
        local_logits = local_logits.masked_fill(assignment_padding_mask.unsqueeze(1), -1e9)
        local_attn = torch.softmax(local_logits, dim=-1)
        local_context = torch.bmm(local_attn, assignment_embed)
        context_r = self.norm_r2(r_hidden + self.ffn_rat(r_hidden))
        ratio_context = self.ratio_context_proj(torch.cat([context_r, local_context], dim=-1))
        ratio_hidden = self.mlp(ratio_context)
        r_pred = torch.sigmoid(self.head_ratio(ratio_hidden)).squeeze(-1)
        row_ids = torch.arange(self.ratio_rows, device=q.device)
        row_embed = self.row_embedding(row_ids).view(1, 1, self.ratio_rows, -1)
        material_hidden = ratio_hidden.unsqueeze(2) + row_embed
        material_logits = self.material_head(material_hidden)

        return l_pred, r_pred, material_logits, target_length_logits, v_t_attn, l_attn, r_attn


def make_safe_sample_name(sample_name: str) -> str:
    safe = re.sub(r'[<>:"/\\\\|?*]+', "_", str(sample_name)).strip()
    safe = re.sub(r"\s+", "_", safe)
    return safe or "sample"


def get_output_paths(sample_name: str, output_dir: Path) -> dict[str, Path]:
    if not output_dir.is_absolute():
        output_dir = BASE_DIR / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    sample_name = make_safe_sample_name(sample_name)
    return {
        "length_ratio_npy": output_dir / f"{sample_name}_pred_length_ratio.npy",
        "length_absolute_npy": output_dir / f"{sample_name}_pred_length_absolute.npy",
        "ratio_vector_npy": output_dir / f"{sample_name}_pred_ratio_vector.npy",
        "codes_npy": output_dir / f"{sample_name}_pred_material_code_matrix.npy",
        "bundle_npz": output_dir / f"{sample_name}_pred_result_vector_bundle.npz",
        "meta_json": output_dir / f"{sample_name}_pred_result_vector_metadata.json",
        "preview_png": output_dir / f"{sample_name}_pred_result_vector_preview.png",
        "presence_png": output_dir / f"{sample_name}_material_presence_preview.png",
        "attention_png": output_dir / f"{sample_name}_attention_preview.png",
        "report_txt": output_dir / f"{sample_name}_evaluation_report.txt",
        "pred_length_txt": output_dir / f"{sample_name}_pred_length_vector.txt",
        "pred_ratio_txt": output_dir / f"{sample_name}_pred_ratio_vector.txt",
        "pred_matrix_txt": output_dir / f"{sample_name}_pred_material_code_matrix.txt",
        "gt_length_txt": output_dir / f"{sample_name}_gt_length_vector.txt",
        "gt_ratio_txt": output_dir / f"{sample_name}_gt_ratio_vector.txt",
        "gt_matrix_txt": output_dir / f"{sample_name}_gt_material_code_matrix.txt",
        "assignment_map_txt": output_dir / f"{sample_name}_assignment_column_mapping.txt",
        "assignment_map_json": output_dir / f"{sample_name}_assignment_column_mapping.json",
        "mat_file": output_dir / f"{sample_name}.mat",
    }


def save_figure_png_safely(fig, output_path: Path, **savefig_kwargs) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", **savefig_kwargs)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=str(output_path.parent), suffix=".png") as handle:
            tmp_path = Path(handle.name)
            handle.write(buffer.getvalue())
        os.replace(str(tmp_path), str(output_path))
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def write_bytes_safely(output_path: Path, content: bytes, suffix: str) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=str(output_path.parent), suffix=suffix) as handle:
            tmp_path = Path(handle.name)
            handle.write(content)
        os.replace(str(tmp_path), str(output_path))
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def save_numpy_safely(output_path: Path, array) -> None:
    buffer = io.BytesIO()
    np.save(buffer, array)
    write_bytes_safely(output_path, buffer.getvalue(), ".npy")


def save_numpyz_safely(output_path: Path, **arrays) -> None:
    buffer = io.BytesIO()
    np.savez(buffer, **arrays)
    write_bytes_safely(output_path, buffer.getvalue(), ".npz")


def save_json_safely(output_path: Path, payload) -> None:
    content = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    write_bytes_safely(output_path, content, ".json")


def save_text_safely(output_path: Path, text: str) -> None:
    write_bytes_safely(output_path, text.encode("utf-8"), ".txt")


def save_mat_safely(output_path: Path, payload) -> None:
    buffer = io.BytesIO()
    savemat(buffer, payload)
    write_bytes_safely(output_path, buffer.getvalue(), ".mat")


def build_assignment_column_mapping(sample: dict, column_count: int) -> tuple[list[dict], str]:
    payload = load_property_payload(sample["x"])
    assignments = list(payload.get("assignments", []))
    rows: list[dict] = []
    lines: list[str] = []
    lines.append("=" * 140)
    lines.append(f"Assignment to DM Filament Column Mapping: {sample.get('id', Path(sample['x']).stem)}")
    lines.append("=" * 140)
    lines.append("Rule: seq 0 is the first DM filament column; raw matrix last column -> seq 0")
    lines.append(
        f"{'SeqIdx':>6} | {'RawCol':>6} | {'Assign':>6} | {'Transition':>12} | {'Brightness':>10} | "
        f"{'Mat1':>8} | {'Mat2':>8} | {'StartVoxel':>10} | {'EndVoxel':>8}"
    )
    lines.append("-" * 140)
    for seq_idx in range(int(column_count)):
        raw_col = int(column_count) - 1 - int(seq_idx)
        assignment = get_assignment_for_output_index(assignments, seq_idx, int(column_count))
        assignment_index = int(assignments.index(assignment)) if assignment in assignments else -1
        row = {
            "seq_index": int(seq_idx),
            "raw_matrix_col": int(raw_col),
            "assignment_index": int(assignment_index),
            "assignment_number": int(assignment_index + 1) if assignment_index >= 0 else None,
            "transition": str(assignment.get("transition", "")) if assignment else "",
            "brightness": str(assignment.get("brightness", "")) if assignment else "",
            "material_1": str(assignment.get("material_1", "")) if assignment else "",
            "material_2": str(assignment.get("material_2", "")) if assignment else "",
            "start_voxel": int(assignment.get("start_voxel", 0)) if assignment else 0,
            "end_voxel": int(assignment.get("end_voxel", 0)) if assignment else 0,
            "start_material_slot": str(assignment.get("start_material_slot", "")) if assignment else "",
            "end_material_slot": str(assignment.get("end_material_slot", "")) if assignment else "",
        }
        rows.append(row)
        lines.append(
            f"{row['seq_index']:6d} | {row['raw_matrix_col']:6d} | "
            f"{(row['assignment_number'] if row['assignment_number'] is not None else '-'):>6} | "
            f"{row['transition']:>12} | {row['brightness']:>10} | {row['material_1']:>8} | "
            f"{row['material_2']:>8} | {row['start_voxel']:10d} | {row['end_voxel']:8d}"
        )
    return rows, "\n".join(lines)


def infer_sample(model, device, sample, target_length):
    q, x, _, _, _ = load_sample_tensors(sample)
    q = q.unsqueeze(0).to(device)
    x = x.unsqueeze(0).to(device)
    q_lengths = torch.tensor([q.size(1)], dtype=torch.long, device=device)
    x_lengths = torch.tensor([x.size(1)], dtype=torch.long, device=device)
    decode_target_lengths = torch.tensor([int(target_length)], dtype=torch.long, device=device)
    with torch.no_grad():
        l_pred, r_pred, material_logits, n_logits, fusion_attn, len_attn, ratio_attn = model(
            q,
            x,
            q_lengths,
            x_lengths,
            decode_target_lengths,
        )

    pred_n = class_index_to_length(int(torch.argmax(n_logits.squeeze(0)).item()), model.length_classes)
    effective_target_length = min(int(target_length), int(l_pred.size(1)), int(material_logits.size(1)))

    return (
        l_pred.squeeze(0).cpu()[:effective_target_length],
        r_pred.squeeze(0).cpu()[:effective_target_length],
        material_logits.squeeze(0).cpu()[:effective_target_length],
        pred_n,
        fusion_attn.squeeze(0).cpu(),
        len_attn.squeeze(0).cpu(),
        ratio_attn.squeeze(0).cpu(),
        x.squeeze(0).cpu(),
        int(x_lengths.item()),
    )


def apply_property_material_mask(material_logits: torch.Tensor, property_json_path: str) -> torch.Tensor:
    output_count = int(material_logits.size(0))
    class_count = int(material_logits.size(-1))
    allowed_mask = build_allowed_material_class_mask(property_json_path, output_count, class_count).to(material_logits.device)
    allowed_mask = allowed_mask.unsqueeze(1).expand(-1, material_logits.size(1), -1)
    constrained_logits = material_logits.clone()
    constrained_logits = constrained_logits.masked_fill(~allowed_mask, -1e9)
    return constrained_logits


def load_ground_truth(sample):
    _, _, gt_len_ratio, gt_ratio_vector, _ = load_sample_tensors(sample)
    gt_len_abs = np.array(np.load(sample["l"]), dtype=np.float32).reshape(-1).copy()
    gt_code_matrix = reverse_matrix_columns_to_sequence_order(np.array(np.load(sample["r"]), dtype=np.int32))
    return gt_len_abs, gt_len_ratio.numpy(), gt_ratio_vector.numpy(), gt_code_matrix


def build_display_color_matrix(material_code_matrix: np.ndarray, codebook: dict[str, int]):
    ordered_items = sorted(codebook.items(), key=lambda item: int(item[1]))
    codes = [int(code) for _, code in ordered_items]
    labels = [name for name, _ in ordered_items]
    color_lookup = {
        "PLA": "#b0bec5",
        "CPLA": "#455a64",
        "TPU": "#43a047",
        "PETG": "#fb8c00",
        "SMP": "#7e57c2",
        "CYAN": "#1d4ed8",
        "MAGENTA": "#d81b60",
        "YELLOW": "#facc15",
        "WHITE": "#fafafa",
        "": "#e5e7eb",
    }
    palette = [color_lookup.get(name, "#111827") for name in labels]
    code_to_display_index = {code: index for index, code in enumerate(codes)}
    display_color_matrix = np.vectorize(lambda value: code_to_display_index.get(int(value), 0))(material_code_matrix)
    bounds = np.arange(len(labels) + 1) - 0.5
    cmap = ListedColormap(palette)
    norm = BoundaryNorm(bounds, cmap.N)
    return display_color_matrix, codes, labels, cmap, norm


def build_material_presence_heatmaps(pred_material_logits: torch.Tensor, gt_codes: np.ndarray):
    gt_codes = np.asarray(gt_codes, dtype=np.int32)
    col_count = int(gt_codes.shape[1])
    display_codes = [int(code) for code in MATERIAL_CLASS_CODES if int(code) != 0]
    display_labels = [
        next((name for name, code in MATERIAL_CODEBOOK.items() if int(code) == display_code), str(display_code))
        for display_code in display_codes
    ]

    pred_class_matrix = torch.argmax(pred_material_logits, dim=-1).cpu().numpy().T
    pred_code_matrix = decode_material_class_matrix(pred_class_matrix)
    pred_rows = []
    gt_rows = []
    for display_code in display_codes:
        pred_rows.append(
            np.array([(pred_code_matrix[:, col_idx] == display_code).any() for col_idx in range(col_count)], dtype=np.float32)
        )
        gt_rows.append(
            np.array([(gt_codes[:, col_idx] == display_code).any() for col_idx in range(col_count)], dtype=np.float32)
        )

    pred_presence = np.asarray(pred_rows, dtype=np.float32)
    gt_presence = np.asarray(gt_rows, dtype=np.float32)
    return pred_presence, gt_presence, display_labels
def plot_result_vector(ax_matrix, ax_length, ax_ratio, title_prefix, length_vector, ratio_vector, material_code_matrix, codebook):
    raw_display_matrix_codes = np.asarray(material_code_matrix, dtype=np.int32)[:, ::-1]
    display_color_matrix, codes, labels, cmap, norm = build_display_color_matrix(
        raw_display_matrix_codes, codebook
    )
    display_matrix = np.flipud(display_color_matrix)
    row_count, col_count = material_code_matrix.shape
    length_vector = np.asarray(length_vector, dtype=float).reshape(-1)
    ratio_vector = compute_max_material_ratio_vector_from_code_matrix(raw_display_matrix_codes)
    display_length = length_vector[::-1].copy()
    display_ratio = np.asarray(ratio_vector, dtype=float).reshape(-1)
    width_weights = display_length.astype(float)
    width_weights = width_weights / max(float(np.sum(width_weights)), 1e-12)
    x_edges = np.concatenate(([0.0], np.cumsum(width_weights)))
    x_centers = 0.5 * (x_edges[:-1] + x_edges[1:])
    y_edges = np.arange(row_count + 1)
    y_centers = np.arange(row_count) + 0.5

    image = ax_matrix.pcolormesh(x_edges, y_edges, display_matrix, cmap=cmap, norm=norm, shading="flat")
    ax_matrix.set_title(f"{title_prefix} Material Matrix", fontsize=12, fontweight="bold")
    ax_matrix.set_ylabel("Row Index")
    ax_matrix.set_yticks(y_centers)
    ax_matrix.set_yticklabels([str(i + 1) for i in range(row_count)])
    ax_matrix.set_xticks(x_centers)
    ax_matrix.set_xticklabels([str(col_count - 1 - i) for i in range(col_count)], fontsize=8)
    ax_matrix.set_xlim(x_edges[0], x_edges[-1])
    ax_matrix.set_ylim(y_edges[0], y_edges[-1])
    ax_matrix.set_xticks(x_edges, minor=True)
    ax_matrix.set_yticks(y_edges, minor=True)
    ax_matrix.grid(which="minor", color="#475569", linewidth=0.7, alpha=0.45)
    ax_matrix.tick_params(which="minor", bottom=False, left=False)

    ax_length.bar(
        x_edges[:-1],
        display_length,
        width=np.diff(x_edges),
        align="edge",
        color="#cbd5e1",
        edgecolor="#111827",
        linewidth=1.0,
        alpha=0.8,
    )
    ax_length.plot(x_centers, display_length, color="#111827", linewidth=1.6, marker="o", markersize=3.5)
    ax_length.set_title(f"{title_prefix} Length Vector", fontsize=11, fontweight="bold")
    ax_length.set_ylabel("Length")
    ax_length.grid(alpha=0.25, linewidth=0.6)

    ax_ratio.bar(
        x_edges[:-1],
        display_ratio,
        width=np.diff(x_edges),
        align="edge",
        color="#93c5fd",
        edgecolor="#1d4ed8",
        linewidth=1.0,
        alpha=0.85,
    )
    ax_ratio.plot(x_centers, display_ratio, color="#1d4ed8", linewidth=1.6, marker="o", markersize=3.5)
    ax_ratio.set_title(f"{title_prefix} Ratio Vector", fontsize=11, fontweight="bold")
    ax_ratio.set_xlabel("Column Index (n)")
    ax_ratio.set_ylabel("Max-Material Ratio")
    ax_ratio.set_ylim(0.0, 1.0)
    ax_ratio.grid(alpha=0.25, linewidth=0.6)
    ax_ratio.set_xticks(x_centers)
    ax_ratio.set_xticklabels([str(col_count - 1 - i) for i in range(col_count)], fontsize=8)

    for axis in (ax_length, ax_ratio):
        axis.set_xlim(x_edges[0], x_edges[-1])
        for edge in x_edges:
            axis.axvline(edge, color="#94a3b8", linewidth=0.8, alpha=0.45, zorder=0)

    return image, codes, labels


def build_x_token_labels(x_tensor: torch.Tensor, assignment_count: int) -> list[str]:
    feature_count = x_tensor.size(-1)
    feature_names = PROPERTY_FEATURE_NAMES[:feature_count]
    labels: list[str] = []
    for assignment_idx in range(assignment_count):
        for feature_name in feature_names:
            labels.append(f"A{assignment_idx + 1}:{feature_name}")
    return labels


def save_attention_preview(sample_name, output_dir, fusion_attn, len_attn, ratio_attn, x_tensor, assignment_count, show_plot=False):
    if plt is None:
        return None

    output_paths = get_output_paths(sample_name, output_dir)
    x_token_labels = build_x_token_labels(x_tensor, assignment_count)
    x_token_count = len(x_token_labels)
    fusion_map = np.asarray(fusion_attn[:, :x_token_count], dtype=float)
    len_map = np.asarray(len_attn, dtype=float)
    ratio_map = np.asarray(ratio_attn, dtype=float)

    fig, axes = plt.subplots(3, 1, figsize=(max(14, x_token_count * 0.65), 14), dpi=150, constrained_layout=True)
    plot_specs = [
        (axes[0], fusion_map, "Fusion Attention (Q -> X Feature Tokens)", "Compressed Q Index", x_token_labels),
        (axes[1], len_map, "Length Decoder Attention", "Length Query Index", None),
        (axes[2], ratio_map, "Ratio Decoder Attention", "Ratio Query Index", None),
    ]

    for axis, matrix, title, ylabel, xticklabels in plot_specs:
        image = axis.imshow(matrix, aspect="auto", cmap="magma")
        axis.set_title(title, fontsize=12, fontweight="bold")
        axis.set_ylabel(ylabel)
        if xticklabels is not None:
            axis.set_xticks(np.arange(len(xticklabels)))
            axis.set_xticklabels(xticklabels, rotation=60, ha="right", fontsize=8)
            axis.set_xlabel("X Tokens (assignment-feature)")
        else:
            axis.set_xlabel("Compressed Q Index")
        fig.colorbar(image, ax=axis, fraction=0.025, pad=0.02)

    fig.suptitle(f"Attention Preview: {sample_name}", fontsize=15, fontweight="bold")
    save_figure_png_safely(fig, output_paths["attention_png"], bbox_inches="tight")
    if show_plot:
        plt.show()
    else:
        plt.close(fig)
    return output_paths["attention_png"]


def save_result_vector_preview(sample_name, output_dir, pred_length_abs, pred_ratio_vector, pred_codes, gt_length_abs, gt_ratio_vector, gt_codes, codebook, show_plot=False):
    if plt is None or ListedColormap is None or BoundaryNorm is None:
        return None

    output_paths = get_output_paths(sample_name, output_dir)
    fig = plt.figure(figsize=(18, 12), dpi=150, facecolor="white")
    gs = fig.add_gridspec(3, 2, height_ratios=[5, 1.5, 1.4], hspace=0.32, wspace=0.32)

    ax_pred_matrix = fig.add_subplot(gs[0, 0])
    ax_gt_matrix = fig.add_subplot(gs[0, 1])
    ax_pred_length = fig.add_subplot(gs[1, 0], sharex=ax_pred_matrix)
    ax_gt_length = fig.add_subplot(gs[1, 1], sharex=ax_gt_matrix)
    ax_pred_ratio = fig.add_subplot(gs[2, 0], sharex=ax_pred_matrix)
    ax_gt_ratio = fig.add_subplot(gs[2, 1], sharex=ax_gt_matrix)

    pred_image, pred_codes_list, pred_labels = plot_result_vector(
        ax_pred_matrix, ax_pred_length, ax_pred_ratio, "Predicted", pred_length_abs, pred_ratio_vector, pred_codes, codebook
    )
    gt_image, gt_codes_list, gt_labels = plot_result_vector(
        ax_gt_matrix, ax_gt_length, ax_gt_ratio, "Ground Truth", gt_length_abs, gt_ratio_vector, gt_codes, codebook
    )

    fig.suptitle(f"Result Vector Evaluation: {sample_name}", fontsize=15, fontweight="bold")
    cbar_pred = fig.colorbar(pred_image, ax=ax_pred_matrix, fraction=0.03, pad=0.03)
    cbar_pred.set_ticks(np.arange(len(pred_labels)))
    cbar_pred.ax.set_yticklabels([f"{label} ({code})" for label, code in zip(pred_labels, pred_codes_list)])

    cbar_gt = fig.colorbar(gt_image, ax=ax_gt_matrix, fraction=0.03, pad=0.03)
    cbar_gt.set_ticks(np.arange(len(gt_labels)))
    cbar_gt.ax.set_yticklabels([f"{label} ({code})" for label, code in zip(gt_labels, gt_codes_list)])

    fig.tight_layout()
    save_figure_png_safely(fig, output_paths["preview_png"], bbox_inches="tight")
    if show_plot:
        plt.show()
    else:
        plt.close(fig)
    return output_paths["preview_png"]


def save_material_presence_preview(sample_name, output_dir, pred_material_logits, gt_codes, show_plot=False):
    if plt is None:
        return None

    output_paths = get_output_paths(sample_name, output_dir)
    pred_presence, gt_presence, presence_labels = build_material_presence_heatmaps(pred_material_logits, gt_codes)

    fig, axes = plt.subplots(2, 1, figsize=(16, 5.5), dpi=150, facecolor="white", sharex=True)
    images = [
        axes[0].imshow(pred_presence, aspect="auto", cmap="Blues", vmin=0.0, vmax=1.0),
        axes[1].imshow(gt_presence, aspect="auto", cmap="Blues", vmin=0.0, vmax=1.0),
    ]
    titles = ["Predicted Material Presence Prob.", "GT Material Presence"]

    for axis, image, title, matrix in zip(axes, images, titles, [pred_presence, gt_presence]):
        axis.set_title(title, fontsize=11, fontweight="bold")
        axis.set_yticks(np.arange(len(presence_labels)))
        axis.set_yticklabels(presence_labels, fontsize=8)
        axis.set_ylabel("Material")
        axis.set_xticks(np.arange(matrix.shape[1]))
        axis.set_xticklabels([str(matrix.shape[1] - 1 - i) for i in range(matrix.shape[1])], fontsize=8)
        axis.set_xlabel("Column Index (n)")
        fig.colorbar(image, ax=axis, fraction=0.02, pad=0.02)

    fig.suptitle(f"Material Presence Preview: {sample_name}", fontsize=14, fontweight="bold")
    fig.tight_layout()
    save_figure_png_safely(fig, output_paths["presence_png"], bbox_inches="tight")
    if show_plot:
        plt.show()
    else:
        plt.close(fig)
    return output_paths["presence_png"]


def save_prediction_results(sample, sample_name, output_dir, pred_len_ratio, pred_len_abs, pred_ratio_vector, pred_codes, total_length, gt_len_abs, gt_len_ratio, gt_ratio_vector, gt_codes):
    output_paths = get_output_paths(sample_name, output_dir)
    pred_len_ratio_saved = np.asarray(pred_len_ratio, dtype=np.float32)[::-1].copy()
    pred_len_abs_saved = np.asarray(pred_len_abs, dtype=np.float32)[::-1].copy()
    pred_codes_saved = np.asarray(pred_codes, dtype=np.int32)[:, ::-1].copy()
    pred_ratio_saved = compute_max_material_ratio_vector_from_code_matrix(pred_codes_saved).astype(np.float32)

    gt_len_abs_saved = np.asarray(gt_len_abs, dtype=np.float32)[::-1].copy()
    gt_len_ratio_saved = np.asarray(gt_len_ratio, dtype=np.float32)[::-1].copy()
    gt_codes_saved = np.asarray(gt_codes, dtype=np.int32)[:, ::-1].copy()
    gt_ratio_saved = compute_max_material_ratio_vector_from_code_matrix(gt_codes_saved).astype(np.float32)

    save_numpy_safely(output_paths["length_ratio_npy"], pred_len_ratio_saved)
    save_numpy_safely(output_paths["length_absolute_npy"], pred_len_abs_saved)
    save_numpy_safely(output_paths["ratio_vector_npy"], pred_ratio_saved)
    save_numpy_safely(output_paths["codes_npy"], pred_codes_saved)
    save_numpyz_safely(
        output_paths["bundle_npz"],
        length_ratio=pred_len_ratio_saved,
        length_absolute=pred_len_abs_saved,
        ratio_vector=pred_ratio_saved,
        material_code_matrix=pred_codes_saved,
    )

    meta = {
        "sample_name": sample_name,
        "total_length_from_q": float(total_length),
        "pred_total_length": float(np.sum(pred_len_abs_saved)),
        "gt_total_length": float(np.sum(gt_len_abs_saved)),
        "pred_length_ratio_sum": float(np.sum(pred_len_ratio_saved)),
        "gt_length_ratio_sum": float(np.sum(gt_len_ratio_saved)),
        "pred_length_shape": [int(pred_len_abs_saved.shape[0])],
        "pred_ratio_shape": [int(pred_ratio_saved.shape[0])],
        "gt_length_shape": [int(gt_len_abs_saved.shape[0])],
        "gt_ratio_shape": [int(gt_ratio_saved.shape[0])],
        "saved_orientation": "display-order/rightmost-is-col0",
        "saved_files": {key: str(path) for key, path in output_paths.items()},
    }
    save_json_safely(output_paths["meta_json"], meta)
    save_mat_safely(
        output_paths["mat_file"],
        {
            "length_ratio": pred_len_ratio_saved,
            "length_absolute": pred_len_abs_saved,
            "ratio_vector": pred_ratio_saved,
            "material_code_matrix": pred_codes_saved,
        },
    )
    save_text_safely(
        output_paths["pred_length_txt"],
        np.array2string(pred_len_abs_saved, separator=", "),
    )
    save_text_safely(
        output_paths["pred_ratio_txt"],
        np.array2string(pred_ratio_saved, separator=", "),
    )
    save_text_safely(
        output_paths["pred_matrix_txt"],
        np.array2string(pred_codes_saved, separator=", "),
    )
    save_text_safely(
        output_paths["gt_length_txt"],
        np.array2string(gt_len_abs_saved, separator=", "),
    )
    save_text_safely(
        output_paths["gt_ratio_txt"],
        np.array2string(gt_ratio_saved, separator=", "),
    )
    save_text_safely(
        output_paths["gt_matrix_txt"],
        np.array2string(gt_codes_saved, separator=", "),
    )
    assignment_rows, assignment_report = build_assignment_column_mapping(sample, int(pred_codes_saved.shape[1]))
    save_text_safely(output_paths["assignment_map_txt"], assignment_report)
    save_json_safely(
        output_paths["assignment_map_json"],
        {
            "sample_name": sample_name,
            "saved_orientation": "display-order/rightmost-is-col0",
            "columns": assignment_rows,
        },
    )
    return output_paths


def renormalize_length_ratio(pred_len_ratio):
    pred_len_ratio = np.array(pred_len_ratio, dtype=np.float32).reshape(-1)
    ratio_sum = float(np.sum(pred_len_ratio))
    if ratio_sum > 1e-12:
        return pred_len_ratio / ratio_sum
    return pred_len_ratio


def analyze_length_prediction(pred_len_ratio, total_length, gt_len_abs, gt_len_ratio):
    pred_len_ratio = renormalize_length_ratio(pred_len_ratio)
    pred_len_abs = pred_len_ratio * float(total_length)
    abs_err_ratio = np.abs(pred_len_ratio - gt_len_ratio)
    abs_err_abs = np.abs(pred_len_abs - gt_len_abs)
    pred_ratio_sum = float(np.sum(pred_len_ratio))
    gt_ratio_sum = float(np.sum(gt_len_ratio))
    pred_total = float(np.sum(pred_len_abs))
    gt_total = float(np.sum(gt_len_abs))
    q_total = float(total_length)

    print("\n" + "=" * 110)
    print(f"Length Prediction Analysis ({pred_len_ratio.shape[0]} segments)")
    print("=" * 110)
    print(f"{'Idx':>3} | {'Pred Ratio':>12} | {'GT Ratio':>12} | {'|Err| Ratio':>12} | {'Pred Abs':>12} | {'GT Abs':>12} | {'|Err| Abs':>12}")
    print("-" * 110)
    for i in range(pred_len_ratio.shape[0]):
        print(
            f"{i + 1:3d} | {pred_len_ratio[i]:12.6f} | {gt_len_ratio[i]:12.6f} | {abs_err_ratio[i]:12.6f} | "
            f"{pred_len_abs[i]:12.4f} | {gt_len_abs[i]:12.4f} | {abs_err_abs[i]:12.4f}"
        )

    ratio_mae = float(abs_err_ratio.mean())
    ratio_rmse = float(np.sqrt(np.mean((pred_len_ratio - gt_len_ratio) ** 2)))
    abs_mae = float(abs_err_abs.mean())
    abs_rmse = float(np.sqrt(np.mean((pred_len_abs - gt_len_abs) ** 2)))

    print("-" * 110)
    print(f"Ratio MAE : {ratio_mae:.6f}")
    print(f"Ratio RMSE: {ratio_rmse:.6f}")
    print(f"Abs   MAE : {abs_mae:.6f}")
    print(f"Abs   RMSE: {abs_rmse:.6f}")
    print("-" * 110)
    print(f"Pred ratio sum : {pred_ratio_sum:.6f}")
    print(f"GT ratio sum   : {gt_ratio_sum:.6f}")
    print(f"Q total length : {q_total:.6f}")
    print(f"Pred total len : {pred_total:.6f}")
    print(f"GT total len   : {gt_total:.6f}")
    print(f"Pred-GT total  : {pred_total - gt_total:.6f}")
    print(f"Q-GT total     : {q_total - gt_total:.6f}")

    return {
        "pred_len_ratio": pred_len_ratio,
        "pred_len_abs": pred_len_abs,
        "gt_len_ratio": gt_len_ratio,
        "gt_len_abs": gt_len_abs,
        "ratio_mae": ratio_mae,
        "ratio_rmse": ratio_rmse,
        "abs_mae": abs_mae,
        "abs_rmse": abs_rmse,
        "pred_ratio_sum": pred_ratio_sum,
        "gt_ratio_sum": gt_ratio_sum,
        "pred_total": pred_total,
        "gt_total": gt_total,
        "q_total": q_total,
    }


def analyze_ratio_prediction(pred_ratio, gt_ratio):
    pred_ratio = np.array(pred_ratio, dtype=np.float32).reshape(-1)
    gt_ratio = np.array(gt_ratio, dtype=np.float32).reshape(-1)
    abs_err = np.abs(pred_ratio - gt_ratio)
    mae = float(abs_err.mean())
    rmse = float(np.sqrt(np.mean((pred_ratio - gt_ratio) ** 2)))

    print("\n" + "=" * 90)
    print(f"Ratio Prediction Analysis ({pred_ratio.shape[0]} positions)")
    print("=" * 90)
    print(f"{'Idx':>3} | {'Pred Ratio':>12} | {'GT Ratio':>12} | {'|Err|':>12}")
    print("-" * 60)
    for index in range(pred_ratio.shape[0]):
        print(f"{index + 1:3d} | {pred_ratio[index]:12.6f} | {gt_ratio[index]:12.6f} | {abs_err[index]:12.6f}")
    print("-" * 60)
    print(f"Ratio MAE : {mae:.6f}")
    print(f"Ratio RMSE: {rmse:.6f}")

    return {
        "pred_ratio": pred_ratio,
        "gt_ratio": gt_ratio,
        "mae": mae,
        "rmse": rmse,
    }


def print_column_sequence_alignment(matrix: np.ndarray, length_vector: np.ndarray, ratio_vector: np.ndarray, title: str) -> None:
    matrix = np.asarray(matrix)
    length_vector = np.asarray(length_vector, dtype=np.float32).reshape(-1)
    ratio_vector = np.asarray(ratio_vector, dtype=np.float32).reshape(-1)
    col_count = int(matrix.shape[1])

    print("\n" * 2 + "=" * 120)
    print(f"{title} Raw Matrix / Sequence Alignment")
    print("=" * 120)
    print("Rule: raw matrix last column -> seq 0, raw matrix first column -> seq n-1")
    print("Matrix shape:", matrix.shape)
    print("Length vector:", length_vector.tolist())
    print("Ratio vector :", ratio_vector.tolist())
    print("Matrix (raw column order left->right):")
    print(np.array2string(matrix, max_line_width=200))
    print("-" * 120)
    print(f"{'SeqIdx':>6} | {'RawCol':>6} | {'Length':>12} | {'Ratio':>12} | {'Column Values':>30}")
    print("-" * 120)
    for seq_idx in range(col_count):
        raw_col = col_count - 1 - seq_idx
        column_values = matrix[:, raw_col].tolist()
        print(
            f"{seq_idx:6d} | {raw_col:6d} | {float(length_vector[seq_idx]):12.6f} | "
            f"{float(ratio_vector[seq_idx]):12.6f} | {str(column_values):>30}"
        )
    print("\n")


def build_column_sequence_alignment_report(matrix: np.ndarray, length_vector: np.ndarray, ratio_vector: np.ndarray, title: str) -> str:
    matrix = np.asarray(matrix)
    length_vector = np.asarray(length_vector, dtype=np.float32).reshape(-1)
    ratio_vector = np.asarray(ratio_vector, dtype=np.float32).reshape(-1)
    col_count = int(matrix.shape[1])
    lines = []
    lines.append("")
    lines.append("")
    lines.append("=" * 120)
    lines.append(f"{title} Raw Matrix / Sequence Alignment")
    lines.append("=" * 120)
    lines.append("Rule: raw matrix last column -> seq 0, raw matrix first column -> seq n-1")
    lines.append(f"Matrix shape: {matrix.shape}")
    lines.append(f"Length vector: {length_vector.tolist()}")
    lines.append(f"Ratio vector : {ratio_vector.tolist()}")
    lines.append("Matrix (raw column order left->right):")
    lines.append(np.array2string(matrix, max_line_width=200))
    lines.append("-" * 120)
    lines.append(f"{'SeqIdx':>6} | {'RawCol':>6} | {'Length':>12} | {'Ratio':>12} | {'Column Values':>30}")
    lines.append("-" * 120)
    for seq_idx in range(col_count):
        raw_col = col_count - 1 - seq_idx
        column_values = matrix[:, raw_col].tolist()
        lines.append(
            f"{seq_idx:6d} | {raw_col:6d} | {float(length_vector[seq_idx]):12.6f} | "
            f"{float(ratio_vector[seq_idx]):12.6f} | {str(column_values):>30}"
        )
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def analyze_column_material_presence(pred_code_matrix, gt_code_matrix):
    pred = np.asarray(pred_code_matrix, dtype=np.int32)
    gt = np.asarray(gt_code_matrix, dtype=np.int32)
    col_count = min(pred.shape[1], gt.shape[1])
    pred_sets = []
    gt_sets = []
    exact_hits = []

    print(f"Column Material Presence Analysis ({col_count} columns)")
    print("=" * 110)
    print(f"{'Col':>3} | {'Pred Materials':>30} | {'GT Materials':>30} | {'Exact':>8}")
    print("-" * 110)

    for col_index in range(col_count):
        pred_set = sorted(set(int(v) for v in pred[:, col_index].tolist() if int(v) != 0))
        gt_set = sorted(set(int(v) for v in gt[:, col_index].tolist() if int(v) != 0))
        pred_sets.append(pred_set)
        gt_sets.append(gt_set)
        exact = pred_set == gt_set
        exact_hits.append(1.0 if exact else 0.0)
        print(f"{col_index + 1:3d} | {str(pred_set):>30} | {str(gt_set):>30} | {str(exact):>8}")

    print("-" * 110)
    exact_accuracy = float(np.mean(exact_hits)) if exact_hits else 0.0
    print(f"Presence Exact Acc: {exact_accuracy:.6f}")
    return {
        "pred_sets": pred_sets,
        "gt_sets": gt_sets,
        "exact_accuracy": exact_accuracy,
    }


def build_column_material_presence_report(pred_code_matrix, gt_code_matrix) -> str:
    pred = np.asarray(pred_code_matrix, dtype=np.int32)
    gt = np.asarray(gt_code_matrix, dtype=np.int32)
    col_count = min(pred.shape[1], gt.shape[1])
    exact_hits = []
    lines = []
    lines.append(f"Column Material Presence Analysis ({col_count} columns)")
    lines.append("=" * 110)
    lines.append(f"{'Col':>3} | {'Pred Materials':>30} | {'GT Materials':>30} | {'Exact':>8}")
    lines.append("-" * 110)
    for col_index in range(col_count):
        pred_set = sorted(set(int(v) for v in pred[:, col_index].tolist() if int(v) != 0))
        gt_set = sorted(set(int(v) for v in gt[:, col_index].tolist() if int(v) != 0))
        exact = pred_set == gt_set
        exact_hits.append(1.0 if exact else 0.0)
        lines.append(f"{col_index + 1:3d} | {str(pred_set):>30} | {str(gt_set):>30} | {str(exact):>8}")
    lines.append("-" * 110)
    exact_accuracy = float(np.mean(exact_hits)) if exact_hits else 0.0
    lines.append(f"Presence Exact Acc: {exact_accuracy:.6f}")
    return "\n".join(lines)


def compute_column_position_metrics(pred_code_matrix, gt_code_matrix):
    pred = np.asarray(pred_code_matrix)
    gt = np.asarray(gt_code_matrix)
    col_count = min(pred.shape[1], gt.shape[1])
    if col_count <= 0:
        return {
            "exact_accuracy": 0.0,
            "position_mae": 0.0,
            "best_match_indices": [],
            "position_errors": [],
        }

    pred = pred[:, :col_count]
    gt = gt[:, :col_count]
    best_match_indices = []
    position_errors = []
    exact_hits = []

    for pred_index in range(col_count):
        pred_col = pred[:, pred_index][:, None]
        similarities = np.mean(pred_col == gt, axis=0)
        best_match_index = int(np.argmax(similarities))
        best_match_indices.append(best_match_index)
        position_errors.append(abs(best_match_index - pred_index))
        exact_hits.append(1.0 if best_match_index == pred_index else 0.0)

    return {
        "exact_accuracy": float(np.mean(exact_hits)),
        "position_mae": float(np.mean(position_errors)),
        "best_match_indices": best_match_indices,
        "position_errors": position_errors,
    }


def compute_column_material_presence_metrics(pred_code_matrix, gt_code_matrix):
    pred = np.asarray(pred_code_matrix, dtype=np.int32)
    gt = np.asarray(gt_code_matrix, dtype=np.int32)
    col_count = min(pred.shape[1], gt.shape[1])
    if col_count <= 0:
        return {
            "exact_accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
        }

    pred = pred[:, :col_count]
    gt = gt[:, :col_count]
    exact_hits = []
    tp = fp = fn = 0

    for col_index in range(col_count):
        pred_set = set(int(v) for v in pred[:, col_index].tolist() if int(v) != 0)
        gt_set = set(int(v) for v in gt[:, col_index].tolist() if int(v) != 0)
        exact_hits.append(1.0 if pred_set == gt_set else 0.0)
        tp += len(pred_set & gt_set)
        fp += len(pred_set - gt_set)
        fn += len(gt_set - pred_set)

    precision = float(tp / max(tp + fp, 1))
    recall = float(tp / max(tp + fn, 1))
    f1 = float((2 * precision * recall) / max(precision + recall, 1e-12))
    return {
        "exact_accuracy": float(np.mean(exact_hits)),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def evaluate_sample(model, device, sample, save_dir, file_id):
    print("\n" + "=" * 120)
    print("Evaluating sample")
    print(sample)
    print("=" * 120)

    target_length = int(sample["target_length"])
    (
        pred_len_ratio,
        pred_ratio_vector,
        pred_material_logits,
        pred_n,
        fusion_attn,
        len_attn,
        ratio_attn,
        x_tensor,
        assignment_count,
    ) = infer_sample(model, device, sample, target_length=target_length)
    total_length = load_total_length_from_q(sample["q"], sample["x"])
    gt_len_abs, gt_len_ratio, gt_ratio_vector, gt_code_matrix = load_ground_truth(sample)
    gt_code_matrix_raw = np.array(np.load(sample["r"]), dtype=np.int32)

    pred_len_ratio_np = renormalize_length_ratio(pred_len_ratio.numpy())[::-1].copy()
    pred_len_abs = pred_len_ratio_np * total_length
    pred_material_logits = apply_property_material_mask(pred_material_logits[:, : gt_code_matrix.shape[0], :], sample["x"])
    pred_material_class = torch.argmax(pred_material_logits, dim=-1).numpy().T
    pred_code_matrix = decode_material_class_matrix(pred_material_class)
    pred_code_matrix_raw = np.asarray(pred_code_matrix, dtype=np.int32)[:, ::-1].copy()
    pred_ratio_np = compute_ratio_vector_from_code_matrix(pred_code_matrix, sample["x"])
    gt_ratio_vector = compute_ratio_vector_from_code_matrix(gt_code_matrix, sample["x"])
    column_pos_metrics = compute_column_position_metrics(pred_code_matrix, gt_code_matrix)
    column_presence_metrics = compute_column_material_presence_metrics(pred_code_matrix, gt_code_matrix)

    print("\nRecovered total length from Q")
    print(total_length)
    print(f"Predicted n: {pred_n} | Ground-truth n: {target_length}")
    print(f"Pred length ratio sum: {float(pred_len_ratio_np.sum()):.6f}")
    print(f"GT length ratio sum  : {float(gt_len_ratio.sum()):.6f}")
    print(f"GT total length      : {float(gt_len_abs.sum()):.6f}")
    print(f"\nPredicted absolute lengths [{pred_len_abs.shape[0]}]")
    print(pred_len_abs)
    print("sum:", float(pred_len_abs.sum()))

    length_metrics = analyze_length_prediction(pred_len_ratio_np, total_length, gt_len_abs, gt_len_ratio)
    ratio_metrics = analyze_ratio_prediction(pred_ratio_np, gt_ratio_vector)
    print_column_sequence_alignment(gt_code_matrix_raw, gt_len_abs, gt_ratio_vector, "Ground Truth")
    print_column_sequence_alignment(pred_code_matrix_raw, pred_len_abs, pred_ratio_np, "Predicted")
    presence_analysis = analyze_column_material_presence(pred_code_matrix_raw, gt_code_matrix_raw)
    print(f"Column position exact accuracy: {column_pos_metrics['exact_accuracy']:.6f}")
    print(f"Column position MAE          : {column_pos_metrics['position_mae']:.6f}")
    print(f"Column material presence acc : {column_presence_metrics['exact_accuracy']:.6f}")
    print(f"Column material presence F1  : {column_presence_metrics['f1']:.6f}")

    output_dir = Path(save_dir)
    output_paths = save_prediction_results(
        sample=sample,
        sample_name=file_id,
        output_dir=output_dir,
        pred_len_ratio=pred_len_ratio_np,
        pred_len_abs=pred_len_abs,
        pred_ratio_vector=pred_ratio_np,
        pred_codes=pred_code_matrix,
        total_length=total_length,
        gt_len_abs=gt_len_abs,
        gt_len_ratio=gt_len_ratio,
        gt_ratio_vector=gt_ratio_vector,
        gt_codes=gt_code_matrix,
    )
    preview_path = save_result_vector_preview(
        sample_name=file_id,
        output_dir=output_dir,
        pred_length_abs=pred_len_abs,
        pred_ratio_vector=pred_ratio_np,
        pred_codes=pred_code_matrix,
        gt_length_abs=gt_len_abs,
        gt_ratio_vector=gt_ratio_vector,
        gt_codes=gt_code_matrix,
        codebook=MATERIAL_CODEBOOK,
        show_plot=False,
    )
    presence_preview_path = save_material_presence_preview(
        sample_name=file_id,
        output_dir=output_dir,
        pred_material_logits=pred_material_logits.cpu(),
        gt_codes=gt_code_matrix,
        show_plot=False,
    )
    attention_preview_path = save_attention_preview(
        sample_name=file_id,
        output_dir=output_dir,
        fusion_attn=fusion_attn.numpy(),
        len_attn=len_attn.numpy(),
        ratio_attn=ratio_attn.numpy(),
        x_tensor=x_tensor,
        assignment_count=assignment_count,
        show_plot=False,
    )
    report_lines = [
        f"Sample ID: {file_id}",
        f"Target length: {target_length}",
        f"Predicted n: {pred_n}",
        f"Q total length: {total_length:.6f}",
        f"GT total length: {float(gt_len_abs.sum()):.6f}",
        "",
        "",
        build_column_sequence_alignment_report(gt_code_matrix_raw, gt_len_abs, gt_ratio_vector, "Ground Truth"),
        "",
        "",
        build_column_sequence_alignment_report(pred_code_matrix_raw, pred_len_abs, pred_ratio_np, "Predicted"),
        "",
        "",
        build_column_material_presence_report(pred_code_matrix_raw, gt_code_matrix_raw),
        "",
        f"Column position exact accuracy: {column_pos_metrics['exact_accuracy']:.6f}",
        f"Column position MAE: {column_pos_metrics['position_mae']:.6f}",
        f"Column material presence acc: {column_presence_metrics['exact_accuracy']:.6f}",
        f"Column material presence F1: {column_presence_metrics['f1']:.6f}",
    ]
    save_text_safely(output_paths["report_txt"], "\n".join(report_lines))

    print(f"\n[saved] ID {file_id}")
    for key, path in output_paths.items():
        print(f" - {path}")
    if preview_path is not None:
        print(f" - {preview_path}")
    if presence_preview_path is not None:
        print(f" - {presence_preview_path}")
    if attention_preview_path is not None:
        print(f" - {attention_preview_path}")

    return {
        "sample": sample,
        "total_length": total_length,
        "length": length_metrics,
        "ratio": ratio_metrics,
        "presence_analysis": presence_analysis,
        "column_position_accuracy": column_pos_metrics["exact_accuracy"],
        "column_position_mae": column_pos_metrics["position_mae"],
        "column_best_match_indices": column_pos_metrics["best_match_indices"],
        "column_position_errors": column_pos_metrics["position_errors"],
        "column_material_presence": column_presence_metrics,
        "pred_n": int(pred_n),
        "true_n": int(target_length),
        "preview_png": str(preview_path) if preview_path is not None else None,
        "presence_preview_png": str(presence_preview_path) if presence_preview_path is not None else None,
        "attention_preview_png": str(attention_preview_path) if attention_preview_path is not None else None,
    }


if __name__ == "__main__":
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("DEVICE:", device)
    print("Current working directory:", os.getcwd())
    print("Dataset exists:", os.path.exists("Dataset"))

    samples = filter_variable_target_samples(build_samples("Dataset"))
    sample_q, sample_x, _, _, sample_m = load_sample_tensors(samples[0])
    max_target_length = get_max_target_length(samples)

    ckpt_path = BASE_DIR / "Model_files" / "best_model_full_train.pth"
    if not ckpt_path.exists():
        raise FileNotFoundError(f"Checkpoint not found: {ckpt_path}")

    checkpoint = torch.load(str(ckpt_path), map_location=device)
    if isinstance(checkpoint, dict) and "state_dict" in checkpoint:
        state_dict = checkpoint["state_dict"]
        max_target_length = int(checkpoint.get("max_target_length", max_target_length))
        ratio_rows = int(checkpoint.get("ratio_rows", 1))
        length_classes = checkpoint.get("length_classes", [max_target_length])
    else:
        state_dict = checkpoint
        ratio_rows = 1
        length_classes = [max_target_length]

    model = TrainHybridVoxelPredictor(
        input_dim=sample_q.size(-1),
        text_dim=sample_x.size(-1),
        max_target_length=max_target_length,
        ratio_rows=sample_m.size(-1),
        length_classes=length_classes,
    ).to(device)
    load_result = model.load_state_dict(state_dict, strict=False)
    if load_result.missing_keys:
        print("[load] missing keys:", load_result.missing_keys)
    if load_result.unexpected_keys:
        print("[load] unexpected keys:", load_result.unexpected_keys)
    print(f"[loaded] {ckpt_path}")
    model.eval()

    all_results = []
    for i, sample in enumerate(samples):
        print(f"\n[{i + 1}/{len(samples)}] Evaluating {os.path.basename(sample['q'])}")
        result = evaluate_sample(
            model=model,
            device=device,
            sample=sample,
            save_dir=str(BASE_DIR / "Source_DM_filament" / "Output_Results"),
            file_id=sample.get("id") or Path(sample["q"]).stem,
        )
        all_results.append(result)

    print("\nAll sample evaluations finished.")
    print(f"Total evaluated samples: {len(all_results)}")
