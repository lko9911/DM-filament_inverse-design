from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re

import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
from tqdm import tqdm

from scripts.utils.property_program_utils import resolve_assignment_material_pair, resolve_property_program_path

KNOWN_MATERIALS = {"PLA", "CPLA", "TPU", "PETG", "SMP", "CYAN", "MAGENTA", "YELLOW", "WHITE", "BLACK"}
MATERIAL_COLORS = {
    "PLA": "#2563eb",
    "CPLA": "#f97316",
    "TPU": "#10b981",
    "PETG": "#8b5cf6",
    "SMP": "#ef4444",
    "CYAN": "#06b6d4",
    "MAGENTA": "#d946ef",
    "YELLOW": "#eab308",
    "WHITE": "#e5e7eb",
    "BLACK": "#111827",
    "Other": "#9ca3af",
}
BEAM_TEXT_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency.txt")
PROPERTY_PROGRAM_PATH = resolve_property_program_path()
MATERIAL_DICTIONARY_PATH = Path(os.environ.get("B_FDM_MATERIAL_DICTIONARY_PATH", "input/config/material_dictionary.json"))
OUTPUT_JSON_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency_clusters_best.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency_clusters_best.txt")
OUTPUT_PNG_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency_clusters_best.png")
OUTPUT_IMAGE_DIR = Path("test_sample/derived/adjacency/beam_step_adjacency_cluster_images_top100")
OUTPUT_IMAGE_INDEX_PATH = OUTPUT_IMAGE_DIR / "index.txt"
GENERATE_REPRESENTATIVE_IMAGE = False
GENERATE_CANDIDATE_IMAGES = False
MAX_CANDIDATE_IMAGES = 100

STEP_RE = re.compile(
    r"^step_(?P<step>\d+)\s+\|\s+assignment\s+(?P<assignment>\d+)\s+\|\s+local_step\s+(?P<local_step>\d+)"
    r"(?:\s+\|\s+materials.*)?\s+\|\s+eta<=\s+(?P<eta>[0-9.]+|None)\s+\|"
    r"\s+candidate_count\s+(?P<count>\d+)\s*$"
)
CANDIDATES_RE = re.compile(r"^\s*candidates:\s*(?P<candidates>.*)\s*$")
BEST_RE = re.compile(
    r"^\d+\.\s+score\s+(?P<score>\d+)"
    r"(?:\s+\|\s+eta_sum\s+(?P<eta_sum>[0-9.]+))?"
    r"(?:\s+\|\s+material_switch_count\s+(?P<material_switch_count>\d+))?"
    r"\s+\|\s+step_scores\s+\[(?P<step_scores>[^\]]*)\]\s+\|\s+"
    r"selected_case_keys\s+(?P<keys>.*)\s*$"
)


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_text_lines(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Text file not found: {path}")
    return path.read_text(encoding="utf-8-sig").splitlines()


def materialize_case_rows(case_rows: list[str], start_material: str, end_material: str) -> list[str]:
    rows: list[str] = []
    for label in case_rows:
        if label == "Material_start":
            rows.append(start_material)
        elif label == "Material_end":
            rows.append(end_material)
        elif label == "White":
            rows.append("WHITE")
        else:
            raise ValueError(f"Unknown material label: {label}")
    return rows


def normalize_material_name(name: str) -> str:
    normalized = str(name).strip().upper()
    return normalized if normalized in KNOWN_MATERIALS else "Other"


def material_abbreviation(name: str) -> str:
    abbreviations = {
        "PLA": "P",
        "CPLA": "CP",
        "TPU": "T",
        "PETG": "PG",
        "SMP": "S",
        "CYAN": "C",
        "MAGENTA": "M",
        "YELLOW": "Y",
        "WHITE": "W",
        "BLACK": "B",
        "Other": "O",
    }
    return abbreviations.get(name, "O")


def build_case_lookup(material_dictionary: dict[str, dict[str, object]]) -> dict[str, list[str]]:
    case_lookup: dict[str, list[str]] = {}
    for case_key, case_info in material_dictionary.items():
        case_lookup[case_key] = [str(row) for row in case_info["case_rows"]]
    return case_lookup


def build_case_eta_lookup(material_dictionary: dict[str, dict[str, object]]) -> dict[str, float]:
    case_eta_lookup: dict[str, float] = {}
    for case_key, case_info in material_dictionary.items():
        case_eta_lookup[case_key] = float(case_info["eta"])
    return case_eta_lookup


def build_assignment_materials(property_program: dict) -> dict[int, tuple[str, str]]:
    assignment_materials: dict[int, tuple[str, str]] = {}
    for assignment in property_program.get("assignments", []):
        assignment_index = int(assignment["assignment_index"])
        start_material, end_material = resolve_assignment_material_pair(property_program, assignment)
        assignment_materials[assignment_index] = (
            normalize_material_name(start_material),
            normalize_material_name(end_material),
        )
    return assignment_materials


@dataclass
class StepInfo:
    step_index: int
    assignment_index: int
    local_step_index: int
    eta_limit: float | None
    candidate_keys: list[str]


@dataclass
class CandidateState:
    score: int
    step_scores: list[int]
    selected_case_keys: list[str]
    material_name_matrix: list[list[str]]
    eta_sum: float


def parse_steps(lines: list[str]) -> list[StepInfo]:
    steps: list[StepInfo] = []
    pending: StepInfo | None = None

    for line in lines:
        step_match = STEP_RE.match(line)
        if step_match:
            pending = StepInfo(
                step_index=int(step_match.group("step")),
                assignment_index=int(step_match.group("assignment")),
                local_step_index=int(step_match.group("local_step")),
                eta_limit=(
                    None
                    if step_match.group("eta") == "None"
                    else float(step_match.group("eta"))
                ),
                candidate_keys=[],
            )
            steps.append(pending)
            continue

        if pending is None:
            continue

        candidates_match = CANDIDATES_RE.match(line)
        if candidates_match:
            raw = candidates_match.group("candidates").strip()
            pending.candidate_keys = [item.strip() for item in raw.split(",") if item.strip()] if raw else []

    if not steps:
        raise ValueError(f"No parsed steps found in {BEAM_TEXT_PATH}")

    return steps


def parse_best_candidates(lines: list[str]) -> list[tuple[int, list[int], list[str]]]:
    in_best_candidates = False
    candidates: list[tuple[int, list[int], list[str]]] = []

    for line in lines:
        if line.strip() == "best_candidates:":
            in_best_candidates = True
            continue
        if not in_best_candidates:
            continue

        match = BEST_RE.match(line.strip())
        if not match:
            continue

        score = int(match.group("score"))
        step_scores_raw = match.group("step_scores").strip()
        step_scores = [int(item.strip()) for item in step_scores_raw.split(",") if item.strip()] if step_scores_raw else []
        selected_case_keys = [item.strip() for item in match.group("keys").split(",") if item.strip()]
        candidates.append((score, step_scores, selected_case_keys))

    if not candidates:
        raise ValueError(f"No best candidates found in {BEAM_TEXT_PATH}")

    return candidates


def count_row_groups(row: list[str]) -> int:
    if not row:
        return 0
    groups = 1
    for left, right in zip(row, row[1:]):
        if left != right:
            groups += 1
    return groups


def total_row_group_score(matrix: list[list[str]]) -> int:
    return sum(count_row_groups(row) for row in matrix)


def row_group_counts(matrix: list[list[str]]) -> list[int]:
    return [count_row_groups(row) for row in matrix]


def build_matrix_from_selection(
    selected_case_keys: list[str],
    case_lookup: dict[str, list[str]],
    start_material: str,
    end_material: str,
) -> list[list[str]]:
    selected_rows_per_step: list[list[str]] = []
    for case_key in selected_case_keys:
        rows = case_lookup[case_key]
        if start_material and end_material:
            selected_rows_per_step.append(materialize_case_rows(rows, start_material, end_material))
        else:
            selected_rows_per_step.append(rows)

    row_count = len(selected_rows_per_step[0])
    for rows in selected_rows_per_step:
        if len(rows) != row_count:
            raise ValueError("Inconsistent case row count")

    return [
        [selected_rows_per_step[step_index][row_index] for step_index in range(len(selected_rows_per_step))]
        for row_index in range(row_count)
    ]


def build_numeric_matrix(material_name_matrix: list[list[str]]) -> tuple[list[list[int]], list[str]]:
    ordered_materials: list[str] = []
    for row in material_name_matrix:
        for value in row:
            normalized = normalize_material_name(value)
            if normalized not in ordered_materials:
                ordered_materials.append(normalized)
    numeric_matrix = [
        [ordered_materials.index(normalize_material_name(value)) for value in row]
        for row in material_name_matrix
    ]
    return numeric_matrix, ordered_materials


def save_visualization(
    material_name_matrix: list[list[str]],
    score: int,
    selected_case_keys: list[str],
    steps: list[StepInfo],
    step_etas: list[float],
    row_groups: list[int],
    output_path: Path,
    title: str,
    dpi: int = 200,
) -> None:
    numeric_matrix, ordered_materials = build_numeric_matrix(material_name_matrix)

    fig, (ax_heat, ax_text) = plt.subplots(
        1,
        2,
        figsize=(18, 10),
        gridspec_kw={"width_ratios": [1.35, 0.75]},
        constrained_layout=True,
    )

    cmap = ListedColormap([MATERIAL_COLORS.get(name, MATERIAL_COLORS["Other"]) for name in ordered_materials])
    im = ax_heat.imshow(
        numeric_matrix,
        aspect="auto",
        cmap=cmap,
        interpolation="nearest",
        vmin=-0.5,
        vmax=len(ordered_materials) - 0.5,
    )
    ax_heat.set_title(title, fontsize=14, weight="bold")
    ax_heat.set_xlabel("Step Index")
    ax_heat.set_ylabel("Layer Index")
    ax_heat.set_xticks(range(len(material_name_matrix[0])))
    ax_heat.set_xticklabels([str(i) for i in range(1, len(material_name_matrix[0]) + 1)])
    ax_heat.set_yticks(range(len(material_name_matrix)))
    ax_heat.set_yticklabels(
        [f"{i} (g={row_groups[i - 1]})" for i in range(1, len(material_name_matrix) + 1)]
    )

    top_ax = ax_heat.twiny()
    top_ax.set_xlim(ax_heat.get_xlim())
    top_ax.set_xticks(range(len(steps)))
    top_ax.set_xticklabels([f"{eta:g}" for eta in step_etas], fontsize=8, rotation=45, ha="left")
    top_ax.tick_params(axis="x", length=0, pad=10)
    top_ax.set_xlabel("Step eta", labelpad=18, fontsize=10, weight="bold")

    cbar = fig.colorbar(im, ax=ax_heat, fraction=0.046, pad=0.04, ticks=range(len(ordered_materials)))
    cbar.ax.set_yticklabels(ordered_materials)

    for y, row in enumerate(material_name_matrix):
        for x, value in enumerate(row):
            ax_heat.text(
                x,
                y,
                material_abbreviation(normalize_material_name(value)),
                ha="center",
                va="center",
                fontsize=7,
                color="black" if normalize_material_name(value) in {"WHITE", "YELLOW"} else "white",
            )

    ax_text.axis("off")
    summary_lines = [
        f"score: {score}",
        "",
        "selected_case_keys:",
    ]
    summary_lines.extend(f"- {case_key}" for case_key in selected_case_keys)
    ax_text.text(
        0.0,
        1.0,
        "\n".join(summary_lines),
        va="top",
        ha="left",
        family="monospace",
        fontsize=10,
    )

    fig.suptitle(title, fontsize=18, weight="bold")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    property_program = load_json(PROPERTY_PROGRAM_PATH)
    material_dictionary = load_json(MATERIAL_DICTIONARY_PATH)
    case_lookup = build_case_lookup(material_dictionary)
    case_eta_lookup = build_case_eta_lookup(material_dictionary)
    assignment_materials = build_assignment_materials(property_program)
    lines = load_text_lines(BEAM_TEXT_PATH)
    steps = parse_steps(lines)
    raw_candidates = parse_best_candidates(lines)

    ranked: list[CandidateState] = []
    for _, step_scores, selected_case_keys in tqdm(
        raw_candidates,
        desc="Evaluate best adjacency candidates",
        unit="candidate",
    ):
        material_name_matrix_rows: list[list[str]] = []
        for step_index, case_key in enumerate(selected_case_keys):
            # Use the assignment that was used when generating the step text.
            assignment_index = steps[step_index].assignment_index
            start_material, end_material = assignment_materials.get(assignment_index, ("", ""))
            rows = case_lookup[case_key]
            material_name_matrix_rows.append(
                materialize_case_rows(rows, start_material, end_material)
                if start_material and end_material
                else rows
            )
        matrix = [
            [material_name_matrix_rows[step_index][row_index] for step_index in range(len(material_name_matrix_rows))]
            for row_index in range(len(material_name_matrix_rows[0]))
        ]
        ranked.append(
            CandidateState(
                score=total_row_group_score(matrix),
                step_scores=step_scores,
                selected_case_keys=selected_case_keys,
                material_name_matrix=list(reversed(matrix)),
                eta_sum=sum(case_eta_lookup.get(case_key, 0.0) for case_key in selected_case_keys),
            )
        )

    ranked.sort(key=lambda item: (item.score, item.selected_case_keys))
    best = ranked[0]
    best_ties = [item for item in ranked if item.score == best.score]
    best_step_etas = [case_eta_lookup.get(case_key, 0.0) for case_key in best.selected_case_keys]
    best_row_groups = row_group_counts(best.material_name_matrix)

    report = {
        "source_text_path": str(BEAM_TEXT_PATH),
        "best_score": best.score,
        "best_tie_count": len(best_ties),
        "best_selected_case_keys": best.selected_case_keys,
        "best_material_name_matrix": best.material_name_matrix,
        "generated_representative_image": GENERATE_REPRESENTATIVE_IMAGE,
        "generated_candidate_images": GENERATE_CANDIDATE_IMAGES,
        "candidate_image_limit": MAX_CANDIDATE_IMAGES if GENERATE_CANDIDATE_IMAGES else 0,
        "best_tie_image_dir": str(OUTPUT_IMAGE_DIR),
        "best_tie_image_index_path": str(OUTPUT_IMAGE_INDEX_PATH),
        "best_ties": [
            {
                "score": item.score,
                "step_scores": item.step_scores,
                "eta_sum": item.eta_sum,
                "selected_case_keys": item.selected_case_keys,
                "image_path": str(OUTPUT_IMAGE_DIR / f"candidate_{rank:03d}.png")
                if GENERATE_CANDIDATE_IMAGES and rank <= MAX_CANDIDATE_IMAGES
                else None,
            }
            for rank, item in enumerate(best_ties, start=1)
        ],
    }

    OUTPUT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_TXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    text_lines: list[str] = []
    text_lines.append(f"source_text_path: {report['source_text_path']}")
    text_lines.append(f"best_score: {report['best_score']}")
    text_lines.append(f"best_tie_count: {report['best_tie_count']}")
    text_lines.append("")
    text_lines.append("best_candidates:")
    for rank, item in enumerate(best_ties, start=1):
        text_lines.append(
            f"{rank:02d}. score {item.score} | eta_sum {item.eta_sum:.6f} | step_scores {item.step_scores} | "
            f"selected_case_keys {', '.join(item.selected_case_keys)}"
        )
    text_lines.append("")
    text_lines.append("best_material_name_matrix:")
    text_lines.extend(
        "  " + line
        for line in "\n".join(
            f"row_{row_index:02d}: " + " ".join(row) for row_index, row in enumerate(best.material_name_matrix, start=1)
        ).splitlines()
    )

    OUTPUT_TXT_PATH.write_text("\n".join(text_lines), encoding="utf-8")

    if GENERATE_REPRESENTATIVE_IMAGE:
        save_visualization(
            best.material_name_matrix,
            best.score,
            best.selected_case_keys,
            steps,
            best_step_etas,
            best_row_groups,
            OUTPUT_PNG_PATH,
            "Beam Step Adjacency Best Pattern",
        )

    if GENERATE_CANDIDATE_IMAGES:
        image_index_lines = [
            f"source_text_path: {BEAM_TEXT_PATH}",
            f"best_score: {best.score}",
            f"best_tie_count: {len(best_ties)}",
            "",
            "images:",
        ]
        limited_best_ties = best_ties[:MAX_CANDIDATE_IMAGES]
        for rank, item in enumerate(
            tqdm(
                limited_best_ties,
                desc="Render candidate images",
                unit="image",
            ),
            start=1,
        ):
            image_path = OUTPUT_IMAGE_DIR / f"candidate_{rank:03d}.png"
            step_etas = [case_eta_lookup.get(case_key, 0.0) for case_key in item.selected_case_keys]
            row_groups = row_group_counts(item.material_name_matrix)
            save_visualization(
                item.material_name_matrix,
                item.score,
                item.selected_case_keys,
                steps,
                step_etas,
                row_groups,
                image_path,
                f"Candidate {rank:03d} | score {item.score}",
                dpi=120,
            )
            image_index_lines.append(
                f"{rank:03d}. {image_path.name} | score {item.score} | eta_sum {item.eta_sum:.6f} | "
                f"selected_case_keys {', '.join(item.selected_case_keys)}"
            )

        OUTPUT_IMAGE_INDEX_PATH.write_text("\n".join(image_index_lines), encoding="utf-8")

    print(f"Saved JSON to: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT to: {OUTPUT_TXT_PATH}")
    if GENERATE_REPRESENTATIVE_IMAGE:
        print(f"Saved PNG to: {OUTPUT_PNG_PATH}")
    else:
        print("Skipped representative PNG generation")
    if GENERATE_CANDIDATE_IMAGES:
        print(f"Saved candidate PNGs to: {OUTPUT_IMAGE_DIR}")
        print(f"Saved candidate image index to: {OUTPUT_IMAGE_INDEX_PATH}")
    else:
        print("Skipped candidate PNG generation")
    print(f"Best score: {best.score}")
    print(f"Best tie count: {len(best_ties)}")


if __name__ == "__main__":
    main()
