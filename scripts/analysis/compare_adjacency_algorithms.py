from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = PROJECT_ROOT / "main.py"
CONFIG_DIR = PROJECT_ROOT / "input" / "config"
DEFAULT_PROPERTY_FILES = [
    "Property_1step.json",
    "Property_5step.json",
    "Property_11step.json",
    "Property_Origami gripper.json",
    "Property_sample.json",
    "Property_SNU.json",
    "Property_vase.json",
]
DEFAULT_ALGORITHMS = ["beam", "ga", "astar", "dijkstra", "bfs", "dfs"]
DEFAULT_TIMEOUT_SECONDS = 300
COMPARISON_ROOT = PROJECT_ROOT / "out" / "algorithm_comparison"
RUNS_ROOT = COMPARISON_ROOT / "runs"
SUMMARY_JSON = COMPARISON_ROOT / "algorithm_comparison.json"
SUMMARY_CSV = COMPARISON_ROOT / "algorithm_comparison.csv"
SUMMARY_MD = COMPARISON_ROOT / "algorithm_comparison.md"
PROPERTY_PATH_ENV_KEY = "B_FDM_PROPERTY_PATH"
ALGORITHM_ENV_KEY = "B_FDM_ADJACENCY_SEARCH_ALGORITHM"
RUN_SOURCE_DM_ENV_KEY = "B_FDM_RUN_SOURCE_DM_FILAMENT"
SAMPLE_INFO_PATH_ENV_KEY = "B_FDM_SAMPLE_INFO_PATH"
DEFAULT_SAMPLE_INFO_PATH = CONFIG_DIR / "sample_info.json"
PROPERTY_SAMPLE_INFO_OVERRIDES = {
    "Property_3DBenchy": CONFIG_DIR / "sample_info_3DBenchy.json",
    "Property_vase": CONFIG_DIR / "sample_info_vase.json",
    "Property_Origami gripper": CONFIG_DIR / "sample_info_origami_gripper - base^origami_gripper-2_0.15mm_ABS_MK3S_55m.json",
}


def resolve_property_paths(items: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in items:
        raw = Path(item)
        path = raw if raw.is_absolute() else CONFIG_DIR / raw
        if not path.exists():
            raise FileNotFoundError(f"Property file not found: {path}")
        paths.append(path.resolve())
    return paths


def resolve_sample_info_path(property_path: Path) -> Path:
    return PROPERTY_SAMPLE_INFO_OVERRIDES.get(property_path.stem, DEFAULT_SAMPLE_INFO_PATH).resolve()


def run_pipeline(property_path: Path, algorithm: str, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env[PROPERTY_PATH_ENV_KEY] = str(property_path)
    env[ALGORITHM_ENV_KEY] = algorithm
    env[RUN_SOURCE_DM_ENV_KEY] = "0"
    env[SAMPLE_INFO_PATH_ENV_KEY] = str(resolve_sample_info_path(property_path))
    return subprocess.run(
        [sys.executable, str(MAIN_PY)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout_seconds,
    )


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Expected JSON file was not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in: {path}")
    return payload


def copy_if_exists(source: Path, destination: Path) -> None:
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def summarize_error_text(raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        return "unknown error"
    if "PermissionError" in text and ".gif" in text:
        return "output lock: simulation gif file is in use"
    if "Selected case count does not match the step spatial metadata count." in text:
        return "shape mismatch: selected case count vs step spatial metadata"
    if "Column count mismatch:" in text:
        for line in reversed(text.splitlines()):
            stripped = line.strip()
            if "Column count mismatch:" in stripped:
                return stripped
        return "shape mismatch: column count mismatch"
    if "Voxel index range exceeds sample_info voxel count" in text:
        return "sample_info mismatch: voxel index range exceeds sample_info voxel count"
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return "unknown error"


def collect_run_metrics(property_path: Path, algorithm: str) -> dict[str, Any]:
    property_stem = property_path.stem
    out_root = PROJECT_ROOT / "out" / property_stem
    score_final_path = out_root / "simulation" / "intermediate" / "beam_step_adjacency_clusters_best_switch_eta_ranked.json"
    optimal_candidate_path = out_root / "simulation" / "intermediate" / "optimal_beam_candidate.json"
    adjacency_search_path = out_root / "adjacency" / "beam_step_adjacency.json"
    assignment_matrix_path = out_root / "matrices" / "assignment_candidate_matrix.json"

    score_final_payload = load_json(score_final_path)
    optimal_payload = load_json(optimal_candidate_path)
    adjacency_payload = load_json(adjacency_search_path) if adjacency_search_path.exists() else {}
    assignment_payload = load_json(assignment_matrix_path) if assignment_matrix_path.exists() else {}

    score_summary = score_final_payload.get("summary", {})
    assignment_summary = assignment_payload.get("assignments_summary", [])
    search_stats = adjacency_payload.get("search_stats", {})

    run_snapshot_dir = RUNS_ROOT / property_stem / algorithm
    run_snapshot_dir.mkdir(parents=True, exist_ok=True)
    copy_if_exists(score_final_path, run_snapshot_dir / score_final_path.name)
    copy_if_exists(optimal_candidate_path, run_snapshot_dir / optimal_candidate_path.name)
    copy_if_exists(adjacency_search_path, run_snapshot_dir / adjacency_search_path.name)
    copy_if_exists(assignment_matrix_path, run_snapshot_dir / assignment_matrix_path.name)

    return {
        "result_status": "ok",
        "pipeline_note": "",
        "property_file": property_path.name,
        "property_stem": property_stem,
        "algorithm": algorithm,
        "sample_info_file": resolve_sample_info_path(property_path).name,
        "candidate_count": int(score_summary.get("candidate_count", 0)),
        "min_material_switch_count": int(optimal_payload.get("min_material_switch_count", 0)),
        "eta_sum": float(optimal_payload.get("eta_sum", 0.0)),
        "eta_avg": float(optimal_payload.get("eta_avg", 0.0)),
        "eta_min": float(optimal_payload.get("eta_min", 0.0)),
        "eta_max": float(optimal_payload.get("eta_max", 0.0)),
        "best_rank": int(optimal_payload.get("rank", 0)),
        "best_score": int(optimal_payload.get("score", 0)),
        "optimal_switch_candidate_count": int(score_summary.get("min_material_switch_candidate_count", 0)),
        "max_eta_sum_at_min_material_switch": float(score_summary.get("max_eta_sum_at_min_material_switch", 0.0)),
        "search_best_tie_count": int(adjacency_payload.get("best_tie_count", 0)),
        "search_expanded_state_count": int(search_stats.get("expanded_state_count", 0)),
        "search_terminal_state_count": int(search_stats.get("terminal_state_count", 0)),
        "search_max_frontier_size": int(search_stats.get("max_frontier_size", 0)),
        "search_stopped_by_expansion_limit": bool(search_stats.get("stopped_by_expansion_limit", False)),
        "assignment_count": len(assignment_summary),
        "assignment_candidate_product_sum": int(
            sum(int(item.get("assignment_candidate_count", 0)) for item in assignment_summary)
        ),
        "snapshot_dir": str(run_snapshot_dir.relative_to(PROJECT_ROOT)),
    }


def build_failure_record(
    property_path: Path,
    algorithm: str,
    error_summary: str,
) -> dict[str, Any]:
    return {
        "result_status": "failed",
        "pipeline_note": error_summary,
        "property_file": property_path.name,
        "property_stem": property_path.stem,
        "algorithm": algorithm,
        "sample_info_file": resolve_sample_info_path(property_path).name,
        "candidate_count": "",
        "min_material_switch_count": "",
        "eta_sum": "",
        "eta_avg": "",
        "eta_min": "",
        "eta_max": "",
        "best_rank": "",
        "best_score": "",
        "optimal_switch_candidate_count": "",
        "max_eta_sum_at_min_material_switch": "",
        "search_best_tie_count": "",
        "search_expanded_state_count": "",
        "search_terminal_state_count": "",
        "search_max_frontier_size": "",
        "search_stopped_by_expansion_limit": "",
        "assignment_count": "",
        "assignment_candidate_product_sum": "",
        "snapshot_dir": "",
    }


def build_recovered_record(property_path: Path, algorithm: str, error_summary: str) -> dict[str, Any] | None:
    try:
        record = collect_run_metrics(property_path, algorithm)
    except Exception:
        return None
    record["result_status"] = "ok"
    record["pipeline_note"] = error_summary
    return record


def build_timeout_record(
    property_path: Path,
    algorithm: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    return {
        "result_status": "time limit",
        "pipeline_note": f"time limit ({timeout_seconds}s)",
        "property_file": property_path.name,
        "property_stem": property_path.stem,
        "algorithm": algorithm,
        "sample_info_file": resolve_sample_info_path(property_path).name,
        "candidate_count": "",
        "min_material_switch_count": "",
        "eta_sum": "",
        "eta_avg": "",
        "eta_min": "",
        "eta_max": "",
        "best_rank": "",
        "best_score": "",
        "optimal_switch_candidate_count": "",
        "max_eta_sum_at_min_material_switch": "",
        "search_best_tie_count": "",
        "search_expanded_state_count": "",
        "search_terminal_state_count": "",
        "search_max_frontier_size": "",
        "search_stopped_by_expansion_limit": "",
        "assignment_count": "",
        "assignment_candidate_product_sum": "",
        "snapshot_dir": "",
    }


def write_json(records: list[dict[str, Any]]) -> None:
    COMPARISON_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "property_files": sorted({record["property_file"] for record in records}),
        "algorithms": sorted({record["algorithm"] for record in records}),
        "record_count": len(records),
        "records": records,
    }
    SUMMARY_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(records: list[dict[str, Any]]) -> None:
    if not records:
        SUMMARY_CSV.write_text("", encoding="utf-8")
        return
    fieldnames = list(records[0].keys())
    with SUMMARY_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def write_markdown(records: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    lines.append("# Algorithm Comparison")
    lines.append("")
    lines.append("Primary metrics:")
    lines.append("- `candidate_count`: ranked candidate number after the final scoring stage")
    lines.append("- `eta_sum`: eta sum of the finally selected best candidate")
    lines.append("- `min_material_switch_count`: material switch count of the finally selected best candidate")
    lines.append("")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["property_file"], []).append(record)

    for property_file in sorted(grouped):
        lines.append(f"## {property_file}")
        lines.append("")
        lines.append("| Algorithm | Result | Candidate Count | Eta Sum | Material Switch Count | Best Score | Pipeline Note |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
        for record in sorted(grouped[property_file], key=lambda item: item["algorithm"]):
            if record["result_status"] == "ok":
                note = f"`{record['snapshot_dir']}`"
                if record["pipeline_note"]:
                    note += f" / {record['pipeline_note']}"
                lines.append(
                    f"| {record['algorithm']} | ok | {record['candidate_count']} | "
                    f"{record['eta_sum']:.6f} | {record['min_material_switch_count']} | "
                    f"{record['best_score']} | {note} |"
                )
            else:
                lines.append(
                    f"| {record['algorithm']} | {record['result_status']} |  |  |  |  | "
                    f"`{record['sample_info_file']}` / {record['pipeline_note'].replace('|', '/')} |"
                )
        lines.append("")

    SUMMARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the pipeline for multiple property programs and adjacency algorithms, then compare metrics."
    )
    parser.add_argument(
        "--properties",
        nargs="+",
        default=DEFAULT_PROPERTY_FILES,
        help="Property JSON filenames or paths. Default: the curated comparison list in this file.",
    )
    parser.add_argument(
        "--algorithms",
        nargs="+",
        default=DEFAULT_ALGORITHMS,
        help="Algorithms to compare. Default: beam ga astar dijkstra bfs dfs",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-run timeout in seconds. Default: 300",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    property_paths = resolve_property_paths(args.properties)
    algorithms = [item.strip().lower() for item in args.algorithms if item.strip()]
    records: list[dict[str, Any]] = []

    print("Running algorithm comparison...")
    for property_path in property_paths:
        for algorithm in algorithms:
            print(f"[RUN] property={property_path.name} algorithm={algorithm}")
            try:
                completed = run_pipeline(property_path, algorithm, args.timeout_seconds)
                if completed.stdout.strip():
                    print(completed.stdout.strip().splitlines()[-1])
                record = collect_run_metrics(property_path, algorithm)
                records.append(record)
                print(
                    "      "
                    f"candidates={record['candidate_count']} "
                    f"eta_sum={record['eta_sum']:.6f} "
                    f"switches={record['min_material_switch_count']}"
                )
            except subprocess.CalledProcessError as exc:
                stderr_text = (exc.stderr or "").strip()
                stdout_text = (exc.stdout or "").strip()
                combined_text = stderr_text if stderr_text else stdout_text
                error_summary = summarize_error_text(combined_text)
                record = build_recovered_record(property_path, algorithm, error_summary)
                if record is None:
                    record = build_failure_record(property_path, algorithm, error_summary)
                records.append(record)
                print(f"      {record['result_status']}: {record['pipeline_note']}")
            except subprocess.TimeoutExpired:
                record = build_timeout_record(property_path, algorithm, args.timeout_seconds)
                records.append(record)
                print(f"      time limit: {args.timeout_seconds}s")

            write_json(records)
            write_csv(records)
            write_markdown(records)

    print("")
    print("Saved comparison files:")
    print(f"  {SUMMARY_JSON}")
    print(f"  {SUMMARY_CSV}")
    print(f"  {SUMMARY_MD}")


if __name__ == "__main__":
    main()
