from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import random
import re
import time

from scripts.simulation.simulate_matrix_deposition import build_payload
from scripts.utils.property_program_utils import resolve_assignment_material_pair, resolve_property_program_path


SOURCE_TEXT_PATH = Path("test_sample/derived/matrices/assignment_candidate_matrix.txt")
PROPERTY_PROGRAM_PATH = resolve_property_program_path()
MATERIAL_DICTIONARY_PATH = Path(os.environ.get("B_FDM_MATERIAL_DICTIONARY_PATH", "input/config/material_dictionary.json"))
OUTPUT_JSON_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency.json")
OUTPUT_TXT_PATH = Path("test_sample/derived/adjacency/beam_step_adjacency.txt")

GA_POPULATION_SIZE_ENV_KEY = "B_FDM_GA_POPULATION_SIZE"
GA_GENERATIONS_ENV_KEY = "B_FDM_GA_GENERATIONS"
GA_ELITE_COUNT_ENV_KEY = "B_FDM_GA_ELITE_COUNT"
GA_MUTATION_RATE_ENV_KEY = "B_FDM_GA_MUTATION_RATE"
GA_TOURNAMENT_SIZE_ENV_KEY = "B_FDM_GA_TOURNAMENT_SIZE"
GA_RANDOM_SEED_ENV_KEY = "B_FDM_GA_RANDOM_SEED"
GA_MAX_BEST_CANDIDATES_ENV_KEY = "B_FDM_GA_MAX_BEST_CANDIDATES"
ETA_SUM_FITNESS_WEIGHT_ENV_KEY = "B_FDM_ETA_SUM_FITNESS_WEIGHT"

DEFAULT_GA_POPULATION_SIZE = 240
DEFAULT_GA_GENERATIONS = 350
DEFAULT_GA_ELITE_COUNT = 12
DEFAULT_GA_MUTATION_RATE = 0.06
DEFAULT_GA_TOURNAMENT_SIZE = 4
DEFAULT_GA_RANDOM_SEED = 42
DEFAULT_GA_MAX_BEST_CANDIDATES = 200
DEFAULT_ETA_SUM_FITNESS_WEIGHT = 10.0


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


def parse_non_negative_int_env(env_key: str, default: int) -> int:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return default
    value = int(raw_value)
    if value < 0:
        raise ValueError(f"{env_key} must be >= 0, got {value}")
    return value


def parse_positive_int_env(env_key: str, default: int) -> int:
    value = parse_non_negative_int_env(env_key, default)
    if value <= 0:
        raise ValueError(f"{env_key} must be > 0, got {value}")
    return value


def parse_probability_env(env_key: str, default: float) -> float:
    raw_value = os.environ.get(env_key)
    if raw_value is None or not raw_value.strip():
        return default
    value = float(raw_value)
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{env_key} must be in [0, 1], got {value}")
    return value


GA_POPULATION_SIZE = parse_positive_int_env(GA_POPULATION_SIZE_ENV_KEY, DEFAULT_GA_POPULATION_SIZE)
GA_GENERATIONS = parse_positive_int_env(GA_GENERATIONS_ENV_KEY, DEFAULT_GA_GENERATIONS)
GA_ELITE_COUNT = parse_non_negative_int_env(GA_ELITE_COUNT_ENV_KEY, DEFAULT_GA_ELITE_COUNT)
GA_MUTATION_RATE = parse_probability_env(GA_MUTATION_RATE_ENV_KEY, DEFAULT_GA_MUTATION_RATE)
GA_TOURNAMENT_SIZE = parse_positive_int_env(GA_TOURNAMENT_SIZE_ENV_KEY, DEFAULT_GA_TOURNAMENT_SIZE)
GA_RANDOM_SEED = int(os.environ.get(GA_RANDOM_SEED_ENV_KEY, DEFAULT_GA_RANDOM_SEED))
GA_MAX_BEST_CANDIDATES = parse_positive_int_env(
    GA_MAX_BEST_CANDIDATES_ENV_KEY,
    DEFAULT_GA_MAX_BEST_CANDIDATES,
)
ETA_SUM_FITNESS_WEIGHT = float(os.environ.get(ETA_SUM_FITNESS_WEIGHT_ENV_KEY, DEFAULT_ETA_SUM_FITNESS_WEIGHT))
if GA_ELITE_COUNT >= GA_POPULATION_SIZE:
    raise ValueError(f"{GA_ELITE_COUNT_ENV_KEY} must be smaller than {GA_POPULATION_SIZE_ENV_KEY}")


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


def row_contiguity_score(rows: list[str]) -> int:
    if len(rows) <= 1:
        return 0
    return sum(1 for previous, current in zip(rows, rows[1:]) if previous == current)


def max_step_score_for_row_count(row_count: int) -> int:
    if row_count <= 0:
        return 0
    return (row_count * 2) + (row_count - 1)


def combined_fitness(total_score: int, eta_sum: float) -> float:
    return float(total_score) + (ETA_SUM_FITNESS_WEIGHT * float(eta_sum))


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


@dataclass(frozen=True)
class CandidateState:
    selected_case_keys: list[str]
    selected_rows_per_step: list[list[str]]
    step_scores: list[int]
    total_score: int
    eta_sum: float
    material_switch_count: int | None = None
    gradient_eta_hit_assignments: tuple[int, ...] = ()
    all_gradient_eta_targets_hit: bool = True


@dataclass(frozen=True)
class GenerationInfo:
    generation: int
    best_material_switch_count: int
    best_score: int
    best_eta_sum: float
    average_material_switch_count: float
    unique_genome_count: int


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
            pending.candidate_keys = [item.strip() for item in raw.split(",") if item.strip()] if raw else []

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
    prev_rows = selected_rows_per_step[-1]
    adjacent_score = same_row_score(prev_rows, candidate_rows)

    skip_score = 0
    if len(selected_rows_per_step) >= 2:
        two_steps_back_rows = selected_rows_per_step[-2]
        skip_score = same_row_score(two_steps_back_rows, candidate_rows)

    internal_row_score = row_contiguity_score(candidate_rows)
    return adjacent_score + skip_score + internal_row_score


def genome_signature(genome: list[int]) -> tuple[int, ...]:
    return tuple(genome)


def evaluate_genome(
    genome: list[int],
    step_candidates: list[list[StepCandidate]],
    required_gradient_assignments: tuple[int, ...],
) -> CandidateState:
    first_candidate = step_candidates[0][genome[0]]
    selected_case_keys = [first_candidate.case_key]
    selected_rows_per_step = [first_candidate.rows]
    step_scores: list[int] = []
    total_score = 0
    eta_sum = first_candidate.eta
    hit_assignments: set[int] = set()
    if first_candidate.hits_max_eta:
        hit_assignments.add(first_candidate.assignment_index)

    for step_index in range(1, len(step_candidates)):
        candidate = step_candidates[step_index][genome[step_index]]
        step_score = compute_step_score_with_skip_connection(selected_rows_per_step, candidate.rows)
        selected_case_keys.append(candidate.case_key)
        selected_rows_per_step.append(candidate.rows)
        step_scores.append(step_score)
        total_score += step_score
        eta_sum += candidate.eta
        if candidate.hits_max_eta:
            hit_assignments.add(candidate.assignment_index)

    all_hit = hit_assignments.issuperset(set(required_gradient_assignments))
    return CandidateState(
        selected_case_keys=selected_case_keys,
        selected_rows_per_step=selected_rows_per_step,
        step_scores=step_scores,
        total_score=total_score,
        eta_sum=eta_sum,
        gradient_eta_hit_assignments=tuple(sorted(hit_assignments)),
        all_gradient_eta_targets_hit=all_hit,
    )


def state_sort_key(item: tuple[list[int], CandidateState]) -> tuple[int, int, int, float, list[str]]:
    _genome, state = item
    switch_count = state.material_switch_count if state.material_switch_count is not None else 10**9
    return (
        0 if state.all_gradient_eta_targets_hit else 1,
        switch_count,
        -combined_fitness(state.total_score, state.eta_sum),
        -state.eta_sum,
        -state.total_score,
        state.selected_case_keys,
    )


def best_state_sort_key(item: tuple[list[int], CandidateState]) -> tuple[int, int, int, float, list[str]]:
    _genome, state = item
    switch_count = state.material_switch_count if state.material_switch_count is not None else 10**9
    return (
        0 if state.all_gradient_eta_targets_hit else 1,
        switch_count,
        -combined_fitness(state.total_score, state.eta_sum),
        -state.eta_sum,
        -state.total_score,
        state.selected_case_keys,
    )


def build_material_name_matrix_from_selected_rows(selected_rows_per_step: list[list[str]]) -> list[list[str]]:
    if not selected_rows_per_step:
        return []
    row_count = len(selected_rows_per_step[0])
    return [
        [selected_rows_per_step[step_index][row_index] for step_index in range(len(selected_rows_per_step))]
        for row_index in range(row_count)
    ]


def simulate_material_switch_count(selected_rows_per_step: list[list[str]]) -> int:
    material_name_matrix = build_material_name_matrix_from_selected_rows(selected_rows_per_step)
    binary_placeholder = [[0 for _ in row] for row in material_name_matrix]
    try:
        payload = build_payload(binary_placeholder, material_name_matrix)
    except RuntimeError as exc:
        if "safety loop" not in str(exc):
            raise
        # Treat non-simulatable candidates as worst switch-count candidates
        # so GA can continue and naturally drop them during tie-breaking.
        return 10**9
    return int(payload["material_switch_count"])


def attach_material_switch_count(
    genome: list[int],
    state: CandidateState,
    switch_cache: dict[tuple[int, ...], int],
) -> CandidateState:
    signature = genome_signature(genome)
    switch_count = switch_cache.get(signature)
    if switch_count is None:
        switch_count = simulate_material_switch_count(state.selected_rows_per_step)
        switch_cache[signature] = switch_count
    return CandidateState(
        selected_case_keys=state.selected_case_keys,
        selected_rows_per_step=state.selected_rows_per_step,
        step_scores=state.step_scores,
        total_score=state.total_score,
        eta_sum=state.eta_sum,
        material_switch_count=switch_count,
        gradient_eta_hit_assignments=state.gradient_eta_hit_assignments,
        all_gradient_eta_targets_hit=state.all_gradient_eta_targets_hit,
    )


def random_genome(step_candidates: list[list[StepCandidate]], rng: random.Random) -> list[int]:
    return [rng.randrange(len(candidates)) for candidates in step_candidates]


def eta_greedy_genome(step_candidates: list[list[StepCandidate]]) -> list[int]:
    return [
        max(range(len(candidates)), key=lambda index: (candidates[index].eta, candidates[index].case_key))
        for candidates in step_candidates
    ]


def local_adjacency_greedy_genome(step_candidates: list[list[StepCandidate]]) -> list[int]:
    genome = [0]
    selected_rows_per_step = [step_candidates[0][0].rows]

    for step_index in range(1, len(step_candidates)):
        candidates = step_candidates[step_index]
        best_index = max(
            range(len(candidates)),
            key=lambda index: (
                combined_fitness(
                    compute_step_score_with_skip_connection(selected_rows_per_step, candidates[index].rows),
                    candidates[index].eta,
                ),
                candidates[index].case_key,
            ),
        )
        genome.append(best_index)
        selected_rows_per_step.append(candidates[best_index].rows)

    return genome


def initialize_population(
    step_candidates: list[list[StepCandidate]],
    rng: random.Random,
) -> list[list[int]]:
    population = [eta_greedy_genome(step_candidates), local_adjacency_greedy_genome(step_candidates)]
    while len(population) < GA_POPULATION_SIZE:
        population.append(random_genome(step_candidates, rng))
    return population[:GA_POPULATION_SIZE]


def tournament_select(
    evaluated_population: list[tuple[list[int], CandidateState]],
    rng: random.Random,
) -> list[int]:
    sample_size = min(GA_TOURNAMENT_SIZE, len(evaluated_population))
    contenders = rng.sample(evaluated_population, sample_size)
    winner_genome, _winner_state = min(contenders, key=state_sort_key)
    return list(winner_genome)


def crossover(parent_a: list[int], parent_b: list[int], rng: random.Random) -> list[int]:
    if len(parent_a) < 2:
        return list(parent_a)
    cut = rng.randrange(1, len(parent_a))
    return parent_a[:cut] + parent_b[cut:]


def mutate(
    genome: list[int],
    step_candidates: list[list[StepCandidate]],
    rng: random.Random,
) -> list[int]:
    mutated = list(genome)
    for step_index, candidates in enumerate(step_candidates):
        if len(candidates) > 1 and rng.random() < GA_MUTATION_RATE:
            current = mutated[step_index]
            next_index = rng.randrange(len(candidates) - 1)
            if next_index >= current:
                next_index += 1
            mutated[step_index] = next_index
    return mutated


def run_genetic_algorithm(
    step_candidates: list[list[StepCandidate]],
    required_gradient_assignments: tuple[int, ...],
) -> tuple[list[CandidateState], list[GenerationInfo]]:
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

    rng = random.Random(GA_RANDOM_SEED)
    population = initialize_population(step_candidates, rng)
    archive: dict[tuple[int, ...], CandidateState] = {}
    switch_cache: dict[tuple[int, ...], int] = {}
    generation_history: list[GenerationInfo] = []

    for generation in range(GA_GENERATIONS + 1):
        evaluated_population = [
            (
                genome,
                attach_material_switch_count(
                    genome,
                    evaluate_genome(genome, step_candidates, required_gradient_assignments),
                    switch_cache,
                ),
            )
            for genome in population
        ]
        evaluated_population.sort(key=state_sort_key)

        for genome, state in evaluated_population:
            archive[genome_signature(genome)] = state

        switch_counts = [
            state.material_switch_count if state.material_switch_count is not None else 10**9
            for _genome, state in evaluated_population
        ]
        best_state = evaluated_population[0][1]
        generation_history.append(
            GenerationInfo(
                generation=generation,
                best_material_switch_count=(
                    best_state.material_switch_count if best_state.material_switch_count is not None else 10**9
                ),
                best_score=best_state.total_score,
                best_eta_sum=best_state.eta_sum,
                average_material_switch_count=sum(switch_counts) / len(switch_counts),
                unique_genome_count=len({genome_signature(genome) for genome, _state in evaluated_population}),
            )
        )

        if generation == GA_GENERATIONS:
            break

        next_population = [list(genome) for genome, _state in evaluated_population[:GA_ELITE_COUNT]]
        while len(next_population) < GA_POPULATION_SIZE:
            parent_a = tournament_select(evaluated_population, rng)
            parent_b = tournament_select(evaluated_population, rng)
            child = crossover(parent_a, parent_b, rng)
            next_population.append(mutate(child, step_candidates, rng))
        population = next_population

    valid_states = [state for state in archive.values() if state.all_gradient_eta_targets_hit]
    archive_pool = valid_states if valid_states else list(archive.values())
    min_switch_count = min(
        state.material_switch_count if state.material_switch_count is not None else 10**9
        for state in archive_pool
    )
    best_items = [
        (list(signature), state)
        for signature, state in archive.items()
        if state in archive_pool
        and (state.material_switch_count if state.material_switch_count is not None else 10**9) == min_switch_count
    ]
    best_items.sort(key=best_state_sort_key)
    return [state for _genome, state in best_items[:GA_MAX_BEST_CANDIDATES]], generation_history


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
    search_started_at = time.perf_counter()
    best_states, generation_history = run_genetic_algorithm(step_candidates, required_gradient_assignments)
    search_time_seconds = time.perf_counter() - search_started_at

    report = {
        "source_text_path": str(SOURCE_TEXT_PATH),
        "search_algorithm": "genetic_algorithm_material_switch",
        "total_step_count": len(steps),
        "score_rule": "step_score = S(t-1,t) + S(t-2,t) + R(t), where R(t) is row contiguity within the current step",
        "gradient_max_eta_constraint": "Each Gradient assignment must hit its max eta at least once in the selected path.",
        "fitness_rule": "minimize material_switch_count, then maximize (score + eta_weight * eta_sum)",
        "search_time_seconds": search_time_seconds,
        "eta_sum_fitness_weight": ETA_SUM_FITNESS_WEIGHT,
        "ga_parameters": {
            "population_size": GA_POPULATION_SIZE,
            "generations": GA_GENERATIONS,
            "elite_count": GA_ELITE_COUNT,
            "mutation_rate": GA_MUTATION_RATE,
            "tournament_size": GA_TOURNAMENT_SIZE,
            "random_seed": GA_RANDOM_SEED,
            "max_best_candidates": GA_MAX_BEST_CANDIDATES,
        },
        "best_material_switch_count": (
            best_states[0].material_switch_count if best_states and best_states[0].material_switch_count is not None else None
        ),
        "best_score": best_states[0].total_score if best_states else 0,
        "best_tie_count": len(best_states),
        "generation_history": [
            {
                "generation": item.generation,
                "best_material_switch_count": item.best_material_switch_count,
                "best_score": item.best_score,
                "best_eta_sum": item.best_eta_sum,
                "average_material_switch_count": item.average_material_switch_count,
                "unique_genome_count": item.unique_genome_count,
            }
            for item in generation_history
        ],
        "best_candidates": [
            {
                "total_score": state.total_score,
                "step_scores": state.step_scores,
                "eta_sum": state.eta_sum,
                "material_switch_count": state.material_switch_count,
                "all_gradient_eta_targets_hit": state.all_gradient_eta_targets_hit,
                "selected_case_keys": state.selected_case_keys,
            }
            for state in best_states
        ],
    }

    OUTPUT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    text_lines: list[str] = []
    text_lines.append(f"source_text_path: {report['source_text_path']}")
    text_lines.append("search_algorithm: genetic_algorithm_material_switch")
    text_lines.append(f"total_step_count: {report['total_step_count']}")
    text_lines.append(f"score_rule: {report['score_rule']}")
    text_lines.append(f"fitness_rule: {report['fitness_rule']}")
    text_lines.append(f"eta_sum_fitness_weight: {report['eta_sum_fitness_weight']}")
    text_lines.append(f"search_time_seconds: {report['search_time_seconds']:.6f}")
    text_lines.append(
        "ga_parameters: "
        f"population_size={GA_POPULATION_SIZE}, generations={GA_GENERATIONS}, "
        f"elite_count={GA_ELITE_COUNT}, mutation_rate={GA_MUTATION_RATE}, "
        f"tournament_size={GA_TOURNAMENT_SIZE}, random_seed={GA_RANDOM_SEED}, "
        f"max_best_candidates={GA_MAX_BEST_CANDIDATES}"
    )
    text_lines.append(f"best_material_switch_count: {report['best_material_switch_count']}")
    text_lines.append(f"best_score: {report['best_score']}")
    text_lines.append(f"best_tie_count: {report['best_tie_count']}")
    text_lines.append("")
    text_lines.append("generation_history:")
    for item in report["generation_history"]:
        if item["generation"] == 0 or item["generation"] == GA_GENERATIONS or item["generation"] % 25 == 0:
            text_lines.append(
                f"  generation {item['generation']:04d} | best_material_switch_count {item['best_material_switch_count']} | "
                f"best_score {item['best_score']} | best_eta_sum {item['best_eta_sum']:.6f} | "
                f"average_material_switch_count {item['average_material_switch_count']:.3f} | "
                f"unique {item['unique_genome_count']}"
            )
    text_lines.append("")
    text_lines.append("parsed_steps:")
    text_lines.extend(format_step_summary(steps))
    text_lines.append("")
    text_lines.append("best_candidates:")
    for rank, state in enumerate(best_states, start=1):
        text_lines.append(
            f"{rank:02d}. score {state.total_score} | eta_sum {state.eta_sum:.6f} | "
            f"material_switch_count {state.material_switch_count} | step_scores {state.step_scores} | "
            f"selected_case_keys {', '.join(state.selected_case_keys)}"
        )
    text_lines.append("")
    text_lines.append("best_candidate_switch_eta:")
    for rank, state in enumerate(best_states, start=1):
        text_lines.append(
            f"{rank:02d}. material_switch_count {state.material_switch_count} | eta_sum {state.eta_sum:.6f}"
        )

    OUTPUT_TXT_PATH.write_text("\n".join(text_lines), encoding="utf-8")

    print(f"Saved JSON to: {OUTPUT_JSON_PATH}")
    print(f"Saved TXT to: {OUTPUT_TXT_PATH}")
    print(f"Best material switch count: {report['best_material_switch_count']}")
    print(f"Best score: {report['best_score']}")
    print(f"Best tie count: {report['best_tie_count']}")
    print(f"Search time (seconds): {report['search_time_seconds']:.6f}")
    print(
        "GA parameters: "
        f"population_size={GA_POPULATION_SIZE}, generations={GA_GENERATIONS}, "
        f"mutation_rate={GA_MUTATION_RATE}, random_seed={GA_RANDOM_SEED}"
    )


if __name__ == "__main__":
    main()
