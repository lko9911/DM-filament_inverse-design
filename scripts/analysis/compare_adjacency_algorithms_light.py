from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = PROJECT_ROOT / "main.py"
CONFIG_DIR = PROJECT_ROOT / "input" / "config"
DEFAULT_PROPERTY_FILES = sorted(
    path.name for path in CONFIG_DIR.glob("Property_*.json") if path.name != "Property_3DBenchy.json"
)
DEFAULT_ALGORITHMS = ["beam", "ga", "astar", "dijkstra", "bfs", "dfs"]

OUTPUT_ROOT = PROJECT_ROOT / "out" / "algorithm_comparison_light"
OUTPUT_JSON = OUTPUT_ROOT / "algorithm_comparison_light.json"
OUTPUT_CSV = OUTPUT_ROOT / "algorithm_comparison_light.csv"
OUTPUT_MD = OUTPUT_ROOT / "algorithm_comparison_light.md"
OUTPUT_GIF_ROOT = OUTPUT_ROOT / "representative_gifs"

PROPERTY_PATH_ENV_KEY = "B_FDM_PROPERTY_PATH"
ALGORITHM_ENV_KEY = "B_FDM_ADJACENCY_SEARCH_ALGORITHM"
RUN_SOURCE_DM_ENV_KEY = "B_FDM_RUN_SOURCE_DM_FILAMENT"
SAMPLE_INFO_PATH_ENV_KEY = "B_FDM_SAMPLE_INFO_PATH"
RESULT_COUNT_ENV_KEY = "B_FDM_RESULT_COUNT"
BEAM_BEST_PER_STEP_ENV_KEY = "B_FDM_BEAM_BEST_PER_STEP"

DEFAULT_BEAM_BEST_PER_STEP_LIMIT = "50"
DEFAULT_SAMPLE_INFO_PATH = CONFIG_DIR / "sample_info.json"
PROPERTY_SAMPLE_INFO_OVERRIDES = {
    "Property_vase": CONFIG_DIR / "sample_info_vase.json",
    "Property_Origami gripper": CONFIG_DIR / "sample_info_origami_gripper - base^origami_gripper-2_0.15mm_ABS_MK3S_55m.json",
}

RUNTIME_DIRS = [
    PROJECT_ROOT / "test_sample" / "derived",
    PROJECT_ROOT / "test_sample" / "derived" / "matrices",
    PROJECT_ROOT / "test_sample" / "derived" / "adjacency",
    PROJECT_ROOT / "test_sample" / "derived" / "simulation",
]


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


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def build_run_env(property_path: Path, algorithm: str) -> dict[str, str]:
    env = os.environ.copy()
    env[PROPERTY_PATH_ENV_KEY] = str(property_path)
    env[ALGORITHM_ENV_KEY] = algorithm
    env[RUN_SOURCE_DM_ENV_KEY] = "0"
    env[SAMPLE_INFO_PATH_ENV_KEY] = str(resolve_sample_info_path(property_path))
    env[RESULT_COUNT_ENV_KEY] = "1"
    env[BEAM_BEST_PER_STEP_ENV_KEY] = DEFAULT_BEAM_BEST_PER_STEP_LIMIT
    return env


def ensure_runtime_dirs() -> None:
    for path in RUNTIME_DIRS:
        path.mkdir(parents=True, exist_ok=True)


def run_main_pipeline(property_path: Path, algorithm: str) -> None:
    ensure_runtime_dirs()
    subprocess.run(
        [sys.executable, str(MAIN_PY)],
        cwd=PROJECT_ROOT,
        env=build_run_env(property_path, algorithm),
        check=True,
        text=True,
    )


def build_empty_record(property_path: Path, algorithm: str) -> dict[str, Any]:
    return {
        "property_file": property_path.name,
        "algorithm": algorithm,
        "candidate_count": None,
        "path_score": None,
        "eta_sum": None,
        "material_switch_count": None,
        "path_search_time_seconds": None,
    }


def find_primary_result_json(property_path: Path) -> Path:
    result_root = PROJECT_ROOT / "out" / property_path.stem / "result"
    candidate_result_paths = sorted(result_root.glob("candidate_rank_*/result.json"))
    if candidate_result_paths:
        return candidate_result_paths[0]
    fallback = result_root / "result.json"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f"No result.json found under {result_root}")


def load_candidate_count(property_path: Path) -> int:
    out_root = PROJECT_ROOT / "out" / property_path.stem
    ranked_path = out_root / "simulation" / "intermediate" / "beam_step_adjacency_clusters_best_switch_eta_ranked.json"
    if ranked_path.exists():
        ranked_payload = load_json(ranked_path)
        summary = ranked_payload.get("summary", {})
        if "candidate_count" in summary:
            return int(summary["candidate_count"])

    switches_path = out_root / "simulation" / "intermediate" / "beam_step_adjacency_clusters_best_material_switches.json"
    if switches_path.exists():
        switches_payload = load_json(switches_path)
        summary = switches_payload.get("summary", {})
        if "candidate_count" in summary:
            return int(summary["candidate_count"])

    adjacency_path = out_root / "adjacency" / "beam_step_adjacency.json"
    if adjacency_path.exists():
        adjacency_payload = load_json(adjacency_path)
        best_candidates = adjacency_payload.get("best_candidates", [])
        return len(best_candidates) if isinstance(best_candidates, list) else 0

    raise FileNotFoundError(f"No candidate count source found for {property_path.name}")


def load_path_search_time_seconds(property_path: Path) -> float | None:
    adjacency_path = PROJECT_ROOT / "out" / property_path.stem / "adjacency" / "beam_step_adjacency.json"
    if not adjacency_path.exists():
        return None
    adjacency_payload = load_json(adjacency_path)
    raw_value = adjacency_payload.get("search_time_seconds")
    if raw_value is None:
        return None
    return float(raw_value)


def build_record_from_result_json(property_path: Path, algorithm: str) -> dict[str, Any]:
    result_payload = load_json(find_primary_result_json(property_path))
    return {
        "property_file": property_path.name,
        "algorithm": algorithm,
        "candidate_count": load_candidate_count(property_path),
        "path_score": int(result_payload.get("candidate_score", 0)),
        "eta_sum": float(result_payload.get("candidate_eta_sum", 0.0)),
        "material_switch_count": int(result_payload.get("material_switch_count", 0)),
        "path_search_time_seconds": load_path_search_time_seconds(property_path),
    }


def copy_representative_gif(property_path: Path, algorithm: str) -> None:
    source_path = (
        PROJECT_ROOT
        / "out"
        / property_path.stem
        / "simulation"
        / "candidate_simulations"
        / "candidate_rank_0001"
        / "candidate_rank_0001_simulation.gif"
    )
    if not source_path.exists():
        raise FileNotFoundError(f"Representative GIF not found: {source_path}")

    OUTPUT_GIF_ROOT.mkdir(parents=True, exist_ok=True)
    safe_property_name = property_path.stem.replace(" ", "_")
    target_path = OUTPUT_GIF_ROOT / f"{safe_property_name}__{algorithm}__candidate_rank_0001_simulation.gif"
    target_path.write_bytes(source_path.read_bytes())


def write_outputs(records: list[dict[str, Any]]) -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    OUTPUT_JSON.write_text(
        json.dumps(
                {
                    "property_files": sorted({record["property_file"] for record in records}),
                    "algorithms": sorted({record["algorithm"] for record in records}),
                    "record_count": len(records),
                    "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if records:
        with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "property_file",
                    "algorithm",
                    "candidate_count",
                    "path_score",
                    "eta_sum",
                    "material_switch_count",
                    "path_search_time_seconds",
                ],
            )
            writer.writeheader()
            writer.writerows(records)
    else:
        OUTPUT_CSV.write_text("", encoding="utf-8")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["property_file"], []).append(record)

    lines: list[str] = []
    lines.append("# Algorithm Comparison Light")
    lines.append("")
    for property_file in sorted(grouped):
        lines.append(f"## {property_file}")
        lines.append("")
        lines.append("| Algorithm | Candidate Count | Path Score | Eta Sum | Material Switch Count | Path Search Time (s) |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for record in sorted(grouped[property_file], key=lambda item: item["algorithm"]):
            candidate_count = "" if record["candidate_count"] is None else str(record["candidate_count"])
            path_score = "" if record["path_score"] is None else str(record["path_score"])
            eta_sum = "" if record["eta_sum"] is None else f"{record['eta_sum']:.6f}"
            material_switch_count = "" if record["material_switch_count"] is None else str(record["material_switch_count"])
            path_search_time_seconds = (
                ""
                if record["path_search_time_seconds"] is None
                else f"{record['path_search_time_seconds']:.6f}"
            )
            lines.append(
                f"| {record['algorithm']} | {candidate_count} | "
                f"{path_score} | {eta_sum} | {material_switch_count} | {path_search_time_seconds} |"
            )
        lines.append("")
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare algorithms using the same result.json basis as the main UI."
    )
    parser.add_argument(
        "--properties",
        nargs="+",
        default=DEFAULT_PROPERTY_FILES,
        help="Property JSON filenames or paths. Default: all Property_*.json files under input/config except Property_3DBenchy.json.",
    )
    parser.add_argument(
        "--algorithms",
        nargs="+",
        default=DEFAULT_ALGORITHMS,
        help="Algorithms to compare. Default: beam ga astar dijkstra bfs dfs",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    property_paths = resolve_property_paths(args.properties)
    algorithms = [item.strip().lower() for item in args.algorithms if item.strip()]

    records: list[dict[str, Any]] = []
    print("Running result.json-based algorithm comparison...")
    for property_path in property_paths:
        print(f"[PROPERTY] {property_path.name}")
        for algorithm in algorithms:
            print(f"  [RUN] algorithm={algorithm}")
            try:
                run_main_pipeline(property_path, algorithm)
                record = build_record_from_result_json(property_path, algorithm)
                copy_representative_gif(property_path, algorithm)
            except Exception:
                record = build_empty_record(property_path, algorithm)
            records.append(record)
            write_outputs(records)
            if record["candidate_count"] is None:
                print("        failed; left metrics blank")
            else:
                print(
                    "        "
                    f"candidates={record['candidate_count']} "
                    f"path_score={record['path_score']} "
                    f"eta_sum={record['eta_sum']:.6f} "
                    f"switches={record['material_switch_count']} "
                    f"path_search_time={record['path_search_time_seconds']:.6f}s"
                )

    print("")
    print("Saved:")
    print(f"  {OUTPUT_JSON}")
    print(f"  {OUTPUT_CSV}")
    print(f"  {OUTPUT_MD}")


if __name__ == "__main__":
    main()
