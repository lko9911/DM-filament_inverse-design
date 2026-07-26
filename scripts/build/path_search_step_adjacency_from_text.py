from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from heapq import heappop, heappush
import json
import os
from pathlib import Path
import time
from typing import Iterable

from scripts.build.genetic_algorithm_step_adjacency_from_text import (
    MATERIAL_DICTIONARY_PATH,
    OUTPUT_JSON_PATH,
    OUTPUT_TXT_PATH,
    PROPERTY_PROGRAM_PATH,
    SOURCE_TEXT_PATH,
    CandidateState,
    GRADIENT_MAX_ETA_HIT_WEIGHT,
    StepCandidate,
    StepInfo,
    ETA_SUM_FITNESS_WEIGHT,
    build_assignment_materials,
    build_case_eta_lookup,
    build_case_lookup,
    build_gradient_assignment_eta_targets,
    build_step_candidates,
    combined_fitness,
    compute_step_score_with_skip_connection,
    format_step_summary,
    load_json,
    load_text_lines,
    max_step_score_for_row_count,
    parse_steps_from_text,
    fitness_with_gradient_hits,
    reduce_to_local_gradient_best,
)


SEARCH_ALGORITHM_ENV_KEY = "B_FDM_ADJACENCY_SEARCH_ALGORITHM"
MAX_EXPANSIONS_ENV_KEY = "B_FDM_PATH_SEARCH_MAX_EXPANSIONS"
MAX_RESULTS_ENV_KEY = "B_FDM_PATH_SEARCH_MAX_RESULTS"
BRANCH_LIMIT_ENV_KEY = "B_FDM_PATH_SEARCH_BRANCH_LIMIT"
ETA_PRIORITY_WEIGHT_ENV_KEY = "B_FDM_PATH_SEARCH_ETA_PRIORITY_WEIGHT"

DEFAULT_SEARCH_ALGORITHM = "astar"
DEFAULT_MAX_EXPANSIONS = 200_000
DEFAULT_MAX_RESULTS = 200
DEFAULT_BRANCH_LIMIT = 64
DEFAULT_ETA_PRIORITY_WEIGHT = 1e-6

DEFAULT_MAX_STEP_SCORE = 28


def parse_positive_int_env(env_key: str, default: int) -> int:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return default
    value = int(raw_value)
    if value <= 0:
        raise ValueError(f"{env_key} must be > 0, got {value}")
    return value


def parse_float_env(env_key: str, default: float) -> float:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return default
    return float(raw_value)


def normalize_search_algorithm(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "a*": "astar",
        "a_star": "astar",
        "diax": "dijkstra",
        "dij": "dijkstra",
        "dijkstra_search": "dijkstra",
        "breadth_first": "bfs",
        "depth_first": "dfs",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"astar", "dijkstra", "bfs", "dfs"}:
        raise ValueError("Path search algorithm must be one of: astar, dijkstra, bfs, dfs")
    return normalized


PATH_SEARCH_ALGORITHM = normalize_search_algorithm(
    os.environ.get(SEARCH_ALGORITHM_ENV_KEY, DEFAULT_SEARCH_ALGORITHM)
)
MAX_EXPANSIONS = parse_positive_int_env(MAX_EXPANSIONS_ENV_KEY, DEFAULT_MAX_EXPANSIONS)
MAX_RESULTS = parse_positive_int_env(MAX_RESULTS_ENV_KEY, DEFAULT_MAX_RESULTS)
BRANCH_LIMIT = int(os.environ.get(BRANCH_LIMIT_ENV_KEY, DEFAULT_BRANCH_LIMIT))
if BRANCH_LIMIT < 0:
    raise ValueError(f"{BRANCH_LIMIT_ENV_KEY} must be >= 0, got {BRANCH_LIMIT}")
ETA_PRIORITY_WEIGHT = parse_float_env(ETA_PRIORITY_WEIGHT_ENV_KEY, DEFAULT_ETA_PRIORITY_WEIGHT)


@dataclass(frozen=True)
class PartialPath:
    selected_case_keys: tuple[str, ...]
    selected_rows_per_step: tuple[tuple[str, ...], ...]
    step_scores: tuple[int, ...]
    total_score: int
    eta_sum: float
    gradient_eta_hit_assignments: tuple[int, ...]
    next_step_index: int
    path_cost: int


@dataclass(frozen=True)
class SearchStats:
    algorithm: str
    expanded_state_count: int
    terminal_state_count: int
    max_frontier_size: int
    stopped_by_expansion_limit: bool


def validate_step_candidates(step_candidates: list[list[StepCandidate]]) -> None:
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


def initial_paths(
    step_candidates: list[list[StepCandidate]],
) -> list[PartialPath]:
    paths: list[PartialPath] = []
    for candidate in step_candidates[0]:
        hit_assignments = (candidate.assignment_index,) if candidate.hits_max_eta else ()
        paths.append(
            PartialPath(
                selected_case_keys=(candidate.case_key,),
                selected_rows_per_step=(tuple(candidate.rows),),
                step_scores=(),
                total_score=0,
                eta_sum=candidate.eta,
                gradient_eta_hit_assignments=hit_assignments,
                next_step_index=1,
                path_cost=0,
            )
        )
    return paths


def expand_path(
    path: PartialPath,
    step_candidates: list[list[StepCandidate]],
) -> Iterable[PartialPath]:
    if path.next_step_index >= len(step_candidates):
        return []

    next_paths: list[PartialPath] = []
    candidates = step_candidates[path.next_step_index]
    selected_rows = [list(rows) for rows in path.selected_rows_per_step]
    for candidate in candidates:
        step_score = compute_step_score_with_skip_connection(selected_rows, candidate.rows)
        step_cost = max_step_score_for_row_count(len(candidate.rows)) - (
            combined_fitness(step_score, candidate.eta)
            + (GRADIENT_MAX_ETA_HIT_WEIGHT if candidate.hits_max_eta else 0.0)
        )
        next_hit_assignments = set(path.gradient_eta_hit_assignments)
        if candidate.hits_max_eta:
            next_hit_assignments.add(candidate.assignment_index)
        next_hit_tuple = tuple(sorted(next_hit_assignments))
        next_paths.append(
            PartialPath(
                selected_case_keys=path.selected_case_keys + (candidate.case_key,),
                selected_rows_per_step=path.selected_rows_per_step + (tuple(candidate.rows),),
                step_scores=path.step_scores + (step_score,),
                total_score=path.total_score + step_score,
                eta_sum=path.eta_sum + candidate.eta,
                gradient_eta_hit_assignments=next_hit_tuple,
                next_step_index=path.next_step_index + 1,
                path_cost=path.path_cost + step_cost,
            )
        )
    next_paths.sort(key=terminal_sort_key)
    return next_paths[:BRANCH_LIMIT] if BRANCH_LIMIT > 0 else next_paths


def path_to_candidate_state(
    path: PartialPath,
    required_gradient_assignments: tuple[int, ...],
) -> CandidateState:
    required_set = set(required_gradient_assignments)
    hit_set = set(path.gradient_eta_hit_assignments)
    return CandidateState(
        selected_case_keys=list(path.selected_case_keys),
        selected_rows_per_step=[list(rows) for rows in path.selected_rows_per_step],
        step_scores=list(path.step_scores),
        total_score=path.total_score,
        eta_sum=path.eta_sum,
        gradient_eta_hit_assignments=path.gradient_eta_hit_assignments,
        all_gradient_eta_targets_hit=hit_set.issuperset(required_set),
    )


def final_state_sort_key(state: CandidateState) -> tuple[int, float, list[str]]:
    return (
        -fitness_with_gradient_hits(state.total_score, state.eta_sum, len(state.gradient_eta_hit_assignments)),
        -state.total_score,
        -state.eta_sum,
        state.selected_case_keys,
    )


def terminal_sort_key(path: PartialPath) -> tuple[int, float, tuple[str, ...]]:
    return (
        -fitness_with_gradient_hits(path.total_score, path.eta_sum, len(path.gradient_eta_hit_assignments)),
        -path.total_score,
        -path.eta_sum,
        path.selected_case_keys,
    )


def push_priority(
    heap: list[tuple[float, int, PartialPath]],
    path: PartialPath,
    counter: int,
    algorithm: str,
    total_step_count: int,
) -> int:
    remaining_steps = max(0, total_step_count - path.next_step_index)
    current_row_count = len(path.selected_rows_per_step[0]) if path.selected_rows_per_step else 0
    max_step_score = max_step_score_for_row_count(current_row_count) if current_row_count > 0 else DEFAULT_MAX_STEP_SCORE
    if algorithm == "astar":
        optimistic_remaining_score = remaining_steps * max_step_score
        priority = -(
            fitness_with_gradient_hits(path.total_score, path.eta_sum, len(path.gradient_eta_hit_assignments))
            + optimistic_remaining_score
            + path.next_step_index * max_step_score * 2
        )
    else:
        priority = path.path_cost - (path.next_step_index * max_step_score * 2)
    heappush(heap, (priority, counter, path))
    return counter + 1


def run_priority_path_search(
    step_candidates: list[list[StepCandidate]],
    algorithm: str,
    required_gradient_assignments: tuple[int, ...],
) -> tuple[list[CandidateState], SearchStats]:
    validate_step_candidates(step_candidates)
    total_step_count = len(step_candidates)
    heap: list[tuple[float, int, PartialPath]] = []
    counter = 0
    for path in initial_paths(step_candidates):
        counter = push_priority(heap, path, counter, algorithm, total_step_count)

    terminal_paths: list[PartialPath] = []
    expanded_count = 0
    max_frontier_size = len(heap)

    while heap and expanded_count < MAX_EXPANSIONS and len(terminal_paths) < MAX_RESULTS:
        _priority, _counter, path = heappop(heap)
        expanded_count += 1
        if path.next_step_index >= total_step_count:
            terminal_paths.append(path)
            continue
        for next_path in expand_path(path, step_candidates):
            counter = push_priority(heap, next_path, counter, algorithm, total_step_count)
        max_frontier_size = max(max_frontier_size, len(heap))

    terminal_paths.sort(key=terminal_sort_key)
    best_states = [path_to_candidate_state(path, required_gradient_assignments) for path in terminal_paths[:MAX_RESULTS]]
    best_states.sort(key=final_state_sort_key)
    return best_states, SearchStats(
        algorithm=algorithm,
        expanded_state_count=expanded_count,
        terminal_state_count=len(terminal_paths),
        max_frontier_size=max_frontier_size,
        stopped_by_expansion_limit=bool(heap and expanded_count >= MAX_EXPANSIONS),
    )


def run_queue_path_search(
    step_candidates: list[list[StepCandidate]],
    algorithm: str,
    required_gradient_assignments: tuple[int, ...],
) -> tuple[list[CandidateState], SearchStats]:
    validate_step_candidates(step_candidates)
    total_step_count = len(step_candidates)
    if algorithm == "bfs":
        frontier = initial_paths(step_candidates)
        terminal_paths: list[PartialPath] = []
        expanded_count = 0
        max_frontier_size = len(frontier)

        while frontier and expanded_count < MAX_EXPANSIONS:
            next_frontier: list[PartialPath] = []
            for path in frontier:
                if expanded_count >= MAX_EXPANSIONS:
                    break
                expanded_count += 1
                if path.next_step_index >= total_step_count:
                    terminal_paths.append(path)
                    continue
                next_frontier.extend(expand_path(path, step_candidates))
            if terminal_paths:
                break
            next_frontier.sort(key=terminal_sort_key)
            frontier = next_frontier[:BRANCH_LIMIT] if BRANCH_LIMIT > 0 else next_frontier
            max_frontier_size = max(max_frontier_size, len(frontier))

        terminal_paths.sort(key=terminal_sort_key)
        best_states = [path_to_candidate_state(path, required_gradient_assignments) for path in terminal_paths[:MAX_RESULTS]]
        best_states.sort(key=final_state_sort_key)
        return best_states, SearchStats(
            algorithm=algorithm,
            expanded_state_count=expanded_count,
            terminal_state_count=len(terminal_paths),
            max_frontier_size=max_frontier_size,
            stopped_by_expansion_limit=bool(frontier and expanded_count >= MAX_EXPANSIONS),
        )

    frontier = deque(initial_paths(step_candidates))
    terminal_paths: list[PartialPath] = []
    expanded_count = 0
    max_frontier_size = len(frontier)

    while frontier and expanded_count < MAX_EXPANSIONS:
        path = frontier.pop() if algorithm == "dfs" else frontier.popleft()
        expanded_count += 1
        if path.next_step_index >= total_step_count:
            terminal_paths.append(path)
            terminal_paths.sort(key=terminal_sort_key)
            terminal_paths = terminal_paths[:MAX_RESULTS]
            continue

        next_paths = list(expand_path(path, step_candidates))
        next_paths.sort(key=terminal_sort_key)
        if algorithm == "dfs":
            frontier.extend(next_paths)
        else:
            frontier.extend(next_paths)
        max_frontier_size = max(max_frontier_size, len(frontier))

    terminal_paths.sort(key=terminal_sort_key)
    best_states = [path_to_candidate_state(path, required_gradient_assignments) for path in terminal_paths[:MAX_RESULTS]]
    best_states.sort(key=final_state_sort_key)
    return best_states, SearchStats(
        algorithm=algorithm,
        expanded_state_count=expanded_count,
        terminal_state_count=len(terminal_paths),
        max_frontier_size=max_frontier_size,
        stopped_by_expansion_limit=bool(frontier and expanded_count >= MAX_EXPANSIONS),
    )


def run_path_search(
    step_candidates: list[list[StepCandidate]],
    required_gradient_assignments: tuple[int, ...],
) -> tuple[list[CandidateState], SearchStats]:
    if not step_candidates:
        return [], SearchStats(PATH_SEARCH_ALGORITHM, 0, 0, 0, False)
    if PATH_SEARCH_ALGORITHM in {"astar", "dijkstra"}:
        return run_priority_path_search(
            step_candidates,
            PATH_SEARCH_ALGORITHM,
            required_gradient_assignments,
        )
    return run_queue_path_search(
        step_candidates,
        PATH_SEARCH_ALGORITHM,
        required_gradient_assignments,
    )


def write_outputs(
    steps: list[StepInfo],
    best_states: list[CandidateState],
    stats: SearchStats,
    search_time_seconds: float,
    step_candidates: list[list[StepCandidate]],
    local_gradient_selection: list[dict[str, object]],
) -> None:
    report = {
        "source_text_path": str(SOURCE_TEXT_PATH),
        "search_algorithm": stats.algorithm,
        "total_step_count": len(steps),
        "score_rule": "step_score = S(t-1,t) + S(t-2,t)",
        "local_gradient_preselection": local_gradient_selection,
        "gradient_max_eta_constraint": "Gradient max-eta hits contribute a fitness bonus but do not filter out the candidate pool.",
        "search_time_seconds": search_time_seconds,
        "eta_sum_fitness_weight": ETA_SUM_FITNESS_WEIGHT,
        "gradient_max_eta_hit_weight": GRADIENT_MAX_ETA_HIT_WEIGHT,
        "path_search_parameters": {
            "max_expansions": MAX_EXPANSIONS,
            "max_results": MAX_RESULTS,
            "branch_limit": BRANCH_LIMIT,
            "eta_priority_weight": ETA_PRIORITY_WEIGHT,
            "max_step_score": max_step_score_for_row_count(len(step_candidates[0][0].rows)) if step_candidates and step_candidates[0] else DEFAULT_MAX_STEP_SCORE,
        },
        "search_stats": {
            "expanded_state_count": stats.expanded_state_count,
            "terminal_state_count": stats.terminal_state_count,
            "max_frontier_size": stats.max_frontier_size,
            "stopped_by_expansion_limit": stats.stopped_by_expansion_limit,
        },
        "best_score": best_states[0].total_score if best_states else 0,
        "best_tie_count": len(best_states),
        "best_candidates": [
            {
                "total_score": state.total_score,
                "step_scores": state.step_scores,
                "eta_sum": state.eta_sum,
                "selected_case_keys": state.selected_case_keys,
            }
            for state in best_states
        ],
    }
    OUTPUT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    text_lines: list[str] = []
    text_lines.append(f"source_text_path: {report['source_text_path']}")
    text_lines.append(f"search_algorithm: {stats.algorithm}")
    text_lines.append(f"total_step_count: {report['total_step_count']}")
    text_lines.append(f"score_rule: {report['score_rule']}")
    text_lines.append("local_gradient_stage: per-Gradient local score maximization, then local material-switch minimization, then global search")
    text_lines.append(f"eta_sum_fitness_weight: {report['eta_sum_fitness_weight']}")
    text_lines.append(f"search_time_seconds: {report['search_time_seconds']:.6f}")
    text_lines.append(
        "path_search_parameters: "
        f"max_expansions={MAX_EXPANSIONS}, max_results={MAX_RESULTS}, "
        f"branch_limit={BRANCH_LIMIT}, eta_priority_weight={ETA_PRIORITY_WEIGHT}, "
        f"max_step_score={report['path_search_parameters']['max_step_score']}"
    )
    text_lines.append(
        "search_stats: "
        f"expanded={stats.expanded_state_count}, terminals={stats.terminal_state_count}, "
        f"max_frontier={stats.max_frontier_size}, stopped_by_limit={stats.stopped_by_expansion_limit}"
    )
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
    text_lines.append("parsed_steps:")
    text_lines.extend(format_step_summary(steps))
    text_lines.append("")
    text_lines.append("best_candidates:")
    for rank, state in enumerate(best_states, start=1):
        text_lines.append(
            f"{rank:02d}. score {state.total_score} | step_scores {state.step_scores} | "
            f"selected_case_keys {', '.join(state.selected_case_keys)}"
        )
    text_lines.append("")
    text_lines.append("best_candidate_eta_sums:")
    for rank, state in enumerate(best_states, start=1):
        text_lines.append(f"{rank:02d}. eta_sum {state.eta_sum:.6f}")

    OUTPUT_TXT_PATH.write_text("\n".join(text_lines), encoding="utf-8")


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
        lambda local_candidates: run_path_search(local_candidates, required_gradient_assignments)[0],
    )
    search_started_at = time.perf_counter()
    best_states, stats = run_path_search(
        step_candidates,
        required_gradient_assignments,
    )
    search_time_seconds = time.perf_counter() - search_started_at
    write_outputs(steps, best_states, stats, search_time_seconds, step_candidates, local_gradient_selection)

    print(f"Saved JSON to: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT to: {OUTPUT_TXT_PATH}")
    print(f"Path search algorithm: {stats.algorithm}")
    print(f"Best score: {best_states[0].total_score if best_states else 0}")
    print(f"Best candidate count: {len(best_states)}")
    print(f"Search time (seconds): {search_time_seconds:.6f}")
    print(
        "Path search stats: "
        f"expanded={stats.expanded_state_count}, terminals={stats.terminal_state_count}, "
        f"max_frontier={stats.max_frontier_size}, stopped_by_limit={stats.stopped_by_expansion_limit}"
    )


if __name__ == "__main__":
    main()
