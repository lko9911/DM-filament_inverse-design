from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import time

from scripts.build.genetic_algorithm_step_adjacency_from_text import CandidateState, reduce_to_local_gradient_best
from scripts.utils.property_program_utils import resolve_assignment_material_pair, resolve_property_program_path


SOURCE_TEXT_PATH = Path("test_sample/derived/matrices/assignment_candidate_matrix.txt")
PROPERTY_PROGRAM_PATH = resolve_property_program_path()
MATERIAL_DICTIONARY_PATH = Path(os.environ.get("B_FDM_MATERIAL_DICTIONARY_PATH", "input/config/material_dictionary.json"))
OUTPUT_JSON_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency.txt")
BEAM_BEST_PER_STEP_ENV_KEY = "B_FDM_BEAM_BEST_PER_STEP"
ETA_SUM_FITNESS_WEIGHT_ENV_KEY = "B_FDM_ETA_SUM_FITNESS_WEIGHT"
GRADIENT_MAX_ETA_HIT_WEIGHT_ENV_KEY = "B_FDM_GRADIENT_MAX_ETA_HIT_WEIGHT"
DEFAULT_MAX_BEST_STATES_PER_STEP = 0
DEFAULT_ETA_SUM_FITNESS_WEIGHT = 10.0
DEFAULT_GRADIENT_MAX_ETA_HIT_WEIGHT = 100.0


def parse_non_negative_int_env(env_key: str, default: int) -> int:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return default
    value = int(raw_value)
    if value < 0:
        raise ValueError(f"{env_key} must be >= 0, got {value}")
    return value


# 0 keeps the old behavior: keep every state tied at the best score for each step.
# If set to N > 0, each step keeps at most N best-score states, preferring higher eta_sum.
MAX_BEST_STATES_PER_STEP = parse_non_negative_int_env(
    BEAM_BEST_PER_STEP_ENV_KEY,
    DEFAULT_MAX_BEST_STATES_PER_STEP,
)
ETA_SUM_FITNESS_WEIGHT = float(os.environ.get(ETA_SUM_FITNESS_WEIGHT_ENV_KEY, DEFAULT_ETA_SUM_FITNESS_WEIGHT))
GRADIENT_MAX_ETA_HIT_WEIGHT = float(
    os.environ.get(GRADIENT_MAX_ETA_HIT_WEIGHT_ENV_KEY, DEFAULT_GRADIENT_MAX_ETA_HIT_WEIGHT)
)


STEP_RE = re.compile(
    r"^step_(?P<step>\d+)\s+\|\s+assignment\s+(?P<assignment>\d+)\s+\|\s+local_step\s+(?P<local_step>\d+)"
    r"(?:\s+\|\s+materials\s+(?P<start>[A-Za-z0-9_]+)->(?P<end>[A-Za-z0-9_]+))?"
    r"\s+\|\s+target\s+(?P<start_count>\d+)/(?P<end_count>\d+)"
    r"\s+\((?P<start_ratio>[0-9.]+)/(?P<end_ratio>[0-9.]+)\)"
    r"(?:\s+\|\s+ratio_tol<=\s+(?P<ratio_tolerance>[0-9.]+))?"
    r"(?:\s+\|\s+eta>=\s+(?P<eta_min>[0-9.]+|None))?"
    r"\s+\|\s+eta<=\s+(?P<eta>[0-9.]+|None)"
    r"\s+\|.*candidate_count\s+(?P<count>\d+)\s*$"
)
CANDIDATES_RE = re.compile(r"^\s*candidates:\s*(?P<candidates>.*)\s*$")


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


def build_case_lookup(material_dictionary: dict[str, dict[str, object]]) -> dict[str, list[str]]:
    case_lookup: dict[str, list[str]] = {}
    for case_key, case_info in material_dictionary.items():
        case_lookup[case_key] = [str(row) for row in case_info["case_rows"]]
    return case_lookup


def build_case_eta_lookup(material_dictionary: dict[str, dict[str, object]]) -> dict[str, float]:
    return {
        case_key: float(case_info.get("eta", 0.0))
        for case_key, case_info in material_dictionary.items()
    }


def build_assignment_materials(property_program: dict) -> dict[int, tuple[str, str]]:
    assignment_materials: dict[int, tuple[str, str]] = {}
    for assignment in property_program.get("assignments", []):
        assignment_index = int(assignment["assignment_index"])
        assignment_materials[assignment_index] = resolve_assignment_material_pair(property_program, assignment)
    return assignment_materials


def build_gradient_assignment_eta_targets(property_program: dict) -> dict[int, float]:
    targets: dict[int, float] = {}
    for assignment in property_program.get("assignments", []):
        if str(assignment.get("Property_type", "Property")) != "Gradient":
            continue
        targets[int(assignment["assignment_index"])] = float(assignment.get("eta", 0.0))
    return targets


def same_row_score(prev_rows: list[str], curr_rows: list[str]) -> int:
    if len(prev_rows) != len(curr_rows):
        raise ValueError("Row length mismatch between steps")
    return sum(1 for prev, curr in zip(prev_rows, curr_rows) if prev == curr)


def combined_fitness(total_score: int, eta_sum: float) -> float:
    return float(total_score) + (ETA_SUM_FITNESS_WEIGHT * float(eta_sum))


def fitness_with_gradient_hits(total_score: int, eta_sum: float, gradient_hit_count: int) -> float:
    return combined_fitness(total_score, eta_sum) + (GRADIENT_MAX_ETA_HIT_WEIGHT * float(gradient_hit_count))


@dataclass(frozen=True)
class StepCandidate:
    case_key: str
    rows: list[str]
    eta: float
    assignment_index: int
    hits_max_eta: bool


@dataclass
class StepInfo:
    step_index: int
    assignment_index: int
    local_step_index: int
    eta_limit: float | None
    material_start: str
    material_end: str
    candidate_keys: list[str]


@dataclass
class BeamState:
    selected_case_keys: list[str]
    selected_rows_per_step: list[list[str]]
    step_scores: list[int]
    total_score: int
    eta_sum: float
    gradient_eta_hit_assignments: tuple[int, ...]


@dataclass(frozen=True)
class BeamPruneInfo:
    step_index: int
    generated_state_count: int
    best_score: int
    best_tie_count_before_limit: int
    kept_state_count: int
    max_best_states_per_step: int


def parse_steps_from_text(lines: list[str]) -> list[StepInfo]:
    steps: list[StepInfo] = []
    pending: StepInfo | None = None

    for line in lines:
        if not line.strip():
            continue

        step_match = STEP_RE.match(line)
        if step_match:
            start_material = step_match.group("start")
            end_material = step_match.group("end")
            pending = StepInfo(
                step_index=int(step_match.group("step")),
                assignment_index=int(step_match.group("assignment")),
                local_step_index=int(step_match.group("local_step")),
                eta_limit=(
                    None
                    if step_match.group("eta") == "None"
                    else float(step_match.group("eta"))
                ),
                material_start=str(start_material) if start_material is not None else "",
                material_end=str(end_material) if end_material is not None else "",
                candidate_keys=[],
            )
            steps.append(pending)
            continue

        if pending is None:
            continue

        candidates_match = CANDIDATES_RE.match(line)
        if candidates_match:
            raw = candidates_match.group("candidates").strip()
            if raw:
                pending.candidate_keys = [item.strip() for item in raw.split(",") if item.strip()]
            else:
                pending.candidate_keys = []

    if not steps:
        raise ValueError(f"No step blocks found in {SOURCE_TEXT_PATH}")

    return steps


def build_step_candidates(
    steps: list[StepInfo],
    case_lookup: dict[str, list[str]],
    case_eta_lookup: dict[str, float],
    assignment_materials: dict[int, tuple[str, str]],
    gradient_assignment_eta_targets: dict[int, float],
) -> list[list[StepCandidate]]:
    step_candidates: list[list[StepCandidate]] = []
    for step in steps:
        start_material, end_material = assignment_materials.get(step.assignment_index, ("", ""))
        options: list[StepCandidate] = []
        for case_key in step.candidate_keys:
            case_rows = case_lookup[case_key]
            eta = case_eta_lookup.get(case_key, 0.0)
            rows = (
                materialize_case_rows(case_rows, start_material, end_material)
                if start_material and end_material
                else case_rows
            )
            target_eta = gradient_assignment_eta_targets.get(step.assignment_index)
            hits_max_eta = target_eta is not None and abs(float(eta) - float(target_eta)) <= 1e-12
            options.append(
                StepCandidate(
                    case_key=case_key,
                    rows=rows,
                    eta=eta,
                    assignment_index=step.assignment_index,
                    hits_max_eta=hits_max_eta,
                )
            )
        step_candidates.append(options)
    return step_candidates


def compute_step_score_with_skip_connection(
    selected_rows_per_step: list[list[str]],
    candidate_rows: list[str],
) -> int:
    """
    기본 인접 score:
        S(t-1, t)

    추가 skip score:
        S(t-2, t)

    즉,
        step_score = S(t-1, t) + S(t-2, t)
    단, t-2가 존재할 때만 더함.
    """
    prev_rows = selected_rows_per_step[-1]
    adjacent_score = same_row_score(prev_rows, candidate_rows)

    skip_score = 0
    if len(selected_rows_per_step) >= 2:
        two_steps_back_rows = selected_rows_per_step[-2]
        skip_score = same_row_score(two_steps_back_rows, candidate_rows)

    return adjacent_score + skip_score


def sort_best_states(states: list[BeamState]) -> list[BeamState]:
    return sorted(
        states,
        key=lambda state: (
            -fitness_with_gradient_hits(state.total_score, state.eta_sum, len(state.gradient_eta_hit_assignments)),
            -state.total_score,
            -state.eta_sum,
            state.selected_case_keys,
        ),
    )


def keep_best_score_states(
    states: list[BeamState],
    step_index: int,
) -> tuple[list[BeamState], BeamPruneInfo]:
    if not states:
        raise ValueError(f"No states generated at step {step_index}")

    ranked_states = sort_best_states(states)
    best_score = ranked_states[0].total_score

    if MAX_BEST_STATES_PER_STEP > 0:
        kept_states = ranked_states[:MAX_BEST_STATES_PER_STEP]
    else:
        kept_states = ranked_states

    prune_info = BeamPruneInfo(
        step_index=step_index,
        generated_state_count=len(states),
        best_score=best_score,
        best_tie_count_before_limit=len(ranked_states),
        kept_state_count=len(kept_states),
        max_best_states_per_step=MAX_BEST_STATES_PER_STEP,
    )
    return kept_states, prune_info


def run_beam_search(
    step_candidates: list[list[StepCandidate]],
    required_gradient_assignments: tuple[int, ...],
) -> tuple[list[BeamState], list[BeamPruneInfo]]:
    if not step_candidates:
        return [], []
    empty_steps = [
        step_index
        for step_index, candidates in enumerate(step_candidates, start=1)
        if not candidates
    ]
    if empty_steps:
        raise ValueError(
            "No candidates available for step(s): "
            + ", ".join(str(step_index) for step_index in empty_steps)
            + ". Check ratio and eta filters in assignment_candidate_matrix.txt."
        )

    beam: list[BeamState] = []
    for candidate in step_candidates[0]:
        hit_assignments = (candidate.assignment_index,) if candidate.hits_max_eta else ()
        beam.append(
            BeamState(
                selected_case_keys=[candidate.case_key],
                selected_rows_per_step=[candidate.rows],
                step_scores=[],
                total_score=0,
                eta_sum=candidate.eta,
                gradient_eta_hit_assignments=hit_assignments,
            )
        )
    prune_history: list[BeamPruneInfo] = []
    beam, first_prune_info = keep_best_score_states(beam, step_index=1)
    prune_history.append(first_prune_info)

    for step_index in range(1, len(step_candidates)):
        next_states: list[BeamState] = []
        for state in beam:
            for candidate in step_candidates[step_index]:
                step_score = compute_step_score_with_skip_connection(
                    selected_rows_per_step=state.selected_rows_per_step,
                    candidate_rows=candidate.rows,
                )
                next_hit_assignments = set(state.gradient_eta_hit_assignments)
                if candidate.hits_max_eta:
                    next_hit_assignments.add(candidate.assignment_index)
                next_hit_tuple = tuple(sorted(next_hit_assignments))
                next_states.append(
                    BeamState(
                        selected_case_keys=state.selected_case_keys + [candidate.case_key],
                        selected_rows_per_step=state.selected_rows_per_step + [candidate.rows],
                        step_scores=state.step_scores + [step_score],
                        total_score=state.total_score + step_score,
                        eta_sum=state.eta_sum + candidate.eta,
                        gradient_eta_hit_assignments=next_hit_tuple,
                    )
                )

        beam, prune_info = keep_best_score_states(next_states, step_index=step_index + 1)
        prune_history.append(prune_info)

    beam.sort(
        key=lambda state: (
            -fitness_with_gradient_hits(state.total_score, state.eta_sum, len(state.gradient_eta_hit_assignments)),
            -state.total_score,
            -state.eta_sum,
            state.selected_case_keys,
        )
    )
    return beam, prune_history


def run_local_beam_best_score_tie(
    step_candidates: list[list[StepCandidate]],
    required_gradient_assignments: tuple[int, ...] = (),
) -> list[CandidateState]:
    beam, _prune_history = run_beam_search(step_candidates, required_gradient_assignments)
    if not beam:
        return []
    best_score = beam[0].total_score
    tied = [state for state in beam if state.total_score == best_score]
    return [
        CandidateState(
            selected_case_keys=state.selected_case_keys,
            selected_rows_per_step=state.selected_rows_per_step,
            step_scores=state.step_scores,
            total_score=state.total_score,
            eta_sum=state.eta_sum,
        )
        for state in tied
    ]


def format_step_summary(steps: list[StepInfo]) -> list[str]:
    lines: list[str] = []
    for step in steps:
        eta_limit_text = "None" if step.eta_limit is None else f"{step.eta_limit:g}"
        lines.append(
            f"step_{step.step_index:03d} | assignment {step.assignment_index} | local_step {step.local_step_index} | "
            f"materials {step.material_start}->{step.material_end} | eta<= {eta_limit_text} | "
            f"candidate_count {len(step.candidate_keys)}"
        )
        lines.append("  candidates: " + ", ".join(step.candidate_keys))
    return lines


def main() -> None:
    property_program = load_json(PROPERTY_PROGRAM_PATH)
    material_dictionary = load_json(MATERIAL_DICTIONARY_PATH)
    case_lookup = build_case_lookup(material_dictionary)
    case_eta_lookup = build_case_eta_lookup(material_dictionary)
    assignment_materials = build_assignment_materials(property_program)
    gradient_assignment_eta_targets = build_gradient_assignment_eta_targets(property_program)
    required_gradient_assignments = tuple(sorted(gradient_assignment_eta_targets))
    steps = parse_steps_from_text(load_text_lines(SOURCE_TEXT_PATH))
    step_candidates = build_step_candidates(
        steps,
        case_lookup,
        case_eta_lookup,
        assignment_materials,
        gradient_assignment_eta_targets,
    )
    step_candidates, local_gradient_selection = reduce_to_local_gradient_best(
        steps,
        step_candidates,
        set(required_gradient_assignments),
        lambda local_candidates: run_local_beam_best_score_tie(local_candidates),
    )
    search_started_at = time.perf_counter()
    beam, prune_history = run_beam_search(
        step_candidates,
        required_gradient_assignments,
    )
    search_time_seconds = time.perf_counter() - search_started_at

    report = {
        "source_text_path": str(SOURCE_TEXT_PATH),
        "total_step_count": len(steps),
        "score_rule": "step_score = S(t-1,t) + S(t-2,t)",
        "local_gradient_preselection": local_gradient_selection,
        "gradient_max_eta_constraint": "Gradient max-eta hits contribute a fitness bonus but do not filter out the candidate pool.",
        "eta_sum_fitness_weight": ETA_SUM_FITNESS_WEIGHT,
        "gradient_max_eta_hit_weight": GRADIENT_MAX_ETA_HIT_WEIGHT,
        "search_time_seconds": search_time_seconds,
        "beam_best_per_step_limit": MAX_BEST_STATES_PER_STEP,
        "beam_best_per_step_env_key": BEAM_BEST_PER_STEP_ENV_KEY,
        "best_score": beam[0].total_score if beam else 0,
        "best_tie_count": len(beam),
        "prune_history": [
            {
                "step_index": item.step_index,
                "generated_state_count": item.generated_state_count,
                "best_score": item.best_score,
                "best_tie_count_before_limit": item.best_tie_count_before_limit,
                "kept_state_count": item.kept_state_count,
                "max_best_states_per_step": item.max_best_states_per_step,
            }
            for item in prune_history
        ],
        "best_candidates": [
            {
                "total_score": state.total_score,
                "step_scores": state.step_scores,
                "eta_sum": state.eta_sum,
                "selected_case_keys": state.selected_case_keys,
            }
            for state in beam
        ],
    }

    OUTPUT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    text_lines: list[str] = []
    text_lines.append(f"source_text_path: {report['source_text_path']}")
    text_lines.append(f"total_step_count: {report['total_step_count']}")
    text_lines.append(f"score_rule: {report['score_rule']}")
    text_lines.append("local_gradient_stage: per-Gradient local score maximization, then local material-switch minimization, then global search")
    text_lines.append(f"eta_sum_fitness_weight: {report['eta_sum_fitness_weight']}")
    text_lines.append(f"search_time_seconds: {report['search_time_seconds']:.6f}")
    text_lines.append(f"beam_best_per_step_limit: {report['beam_best_per_step_limit']} (0 means unlimited)")
    text_lines.append(f"best_score: {report['best_score']}")
    text_lines.append(f"best_tie_count: {report['best_tie_count']}")
    text_lines.append("")
    text_lines.append("local_gradient_preselection:")
    for item in local_gradient_selection:
        text_lines.append(
            "  assignment_{assignment:02d} | local_steps {steps} | combinations {combos} | "
            "best_local_score {score} | best_score_tie_count {tie_count} | "
            "best_local_switch {switches} | best_pattern_count_after_switch {pattern_count} | "
            "best_local_eta_sum_min {eta_sum_min:.6f} | best_local_eta_sum_max {eta_sum_max:.6f}".format(
                assignment=int(item["assignment_index"]),
                steps=int(item["local_step_count"]),
                combos=int(item["combination_count"]),
                score=int(item["best_local_score"]),
                tie_count=int(item["best_local_best_score_tie_count"]),
                switches=int(item["best_local_material_switch_count"]),
                pattern_count=int(item["best_local_pattern_count_after_switch"]),
                eta_sum_min=float(item["best_local_eta_sum_min"]),
                eta_sum_max=float(item["best_local_eta_sum_max"]),
            )
        )
        for index, case_key_set in enumerate(item["selected_case_key_sets"], start=1):
            text_lines.append(f"    pattern_{index:02d}: {', '.join(case_key_set)}")
    text_lines.append("")
    text_lines.append("prune_history:")
    for item in report["prune_history"]:
        text_lines.append(
            f"  step_{item['step_index']:03d} | generated {item['generated_state_count']} | "
            f"best_score {item['best_score']} | best_ties_before_limit {item['best_tie_count_before_limit']} | "
            f"kept {item['kept_state_count']}"
        )
    text_lines.append("")
    text_lines.append("parsed_steps:")
    text_lines.extend(format_step_summary(steps))
    text_lines.append("")
    text_lines.append("best_candidates:")
    for rank, state in enumerate(beam, start=1):
        text_lines.append(
            f"{rank:02d}. score {state.total_score} | step_scores {state.step_scores} | "
            f"selected_case_keys {', '.join(state.selected_case_keys)}"
        )
    text_lines.append("")
    text_lines.append("best_candidate_eta_sums:")
    for rank, state in enumerate(beam, start=1):
        text_lines.append(f"{rank:02d}. eta_sum {state.eta_sum:.6f}")

    OUTPUT_TXT_PATH.write_text("\n".join(text_lines), encoding="utf-8")

    print(f"Saved JSON to: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT to: {OUTPUT_TXT_PATH}")
    print(f"Best score: {report['best_score']}")
    print(f"Best tie count: {report['best_tie_count']}")
    print(f"Search time (seconds): {report['search_time_seconds']:.6f}")
    print(f"Beam best per step limit: {MAX_BEST_STATES_PER_STEP} (0 means unlimited)")


if __name__ == "__main__":
    main()
