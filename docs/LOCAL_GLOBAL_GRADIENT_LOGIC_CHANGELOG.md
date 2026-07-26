# Local / Global Gradient Logic Changelog

## Purpose

This document summarizes the recent logic changes for Gradient handling in the path-search pipeline.

The goal is to make the behavior explicit for:

1. local Gradient optimization
2. global path search
3. final material-switch evaluation


## Current Intended Logic

### 1. Candidate generation

- Gradient assignments still use the original step count.
- Ratio calculation for each step is preserved.
- For the middle Gradient step:
  - only candidates at the maximum allowed eta are kept
  - ratio matching logic is otherwise unchanged


### 2. Local Gradient optimization

Each Gradient assignment is optimized first as a local subproblem.

- Local optimization is not a single fixed-pattern selection anymore.
- The local stage uses the same search-family logic as the selected global algorithm:
  - `ga` uses local GA score search
  - `beam` uses local beam score search
  - `astar / bfs / dfs / dijkstra` use local path-search score search

### Local score inputs

For a single Gradient assignment, the local score includes:

- adjacency between Gradient steps
- adjacency between the left Property step and the first Gradient step
- adjacency between the last Gradient step and the right Property step

This means the Gradient is optimized locally, but still with boundary awareness to neighboring Property steps.


### 3. Local candidate retention rule

For each Gradient assignment:

1. compute all local candidates considered by the selected search algorithm
2. keep the full local **best-score tie**
3. among that tie, compute local material switch count
4. keep **all** patterns with the minimum local material switch count

Important:

- local optimization must **not** collapse to a single representative unless the minimum-switch set truly has size 1
- `eta_sum` is not used to break the final local minimum-switch tie


## Previous Bug

The previous implementation was too aggressive.

It incorrectly reduced the local result to one pattern because the post-score filtering effectively used:

- local switch count
- eta-related values
- case-key identity

This made the local minimum-switch set collapse to one representative even when multiple local-optimal patterns should have survived.

That behavior was logically wrong for this workflow.


## Fix Applied

The local post-score filtering now keeps:

- all local best-score candidates
- then all candidates with the minimum local switch count

The retained local patterns are now represented as:

- `selected_case_key_sets`

instead of a single:

- `selected_case_keys`


## Local Output Format Change

The local summary was updated.

### Before

Local logs were effectively shaped like:

- `best_local_score`
- `best_score_tie_count`
- `best_local_switch`
- `best_pattern_count_after_switch`
- `best_local_eta_sum`
- one `pattern_01`

### Now

Local logs use:

- `best_local_score`
- `best_score_tie_count`
- `best_local_switch`
- `best_pattern_count_after_switch`
- `best_local_eta_sum_min`
- `best_local_eta_sum_max`
- `pattern_01`, `pattern_02`, ...

This reflects that multiple local patterns may survive the minimum-switch filter.


## Global Search Logic

After local filtering:

- the surviving local-optimal Gradient patterns are passed forward to the global stage
- global search is then run on the reduced candidate space

Global search should therefore operate on:

- all locally optimal Gradient candidates
- not one fixed representative per Gradient


## Final Material Switch Evaluation

The final material switch count is still applied after the search candidate pool is formed.

The intended sequence is:

1. search by score
2. keep the relevant best-score candidate set
3. compute material-switch count on that candidate set
4. choose the smallest switch count from that final set


## Files Touched

- [scripts/build/build_assignment_candidate_matrix.py](C:\Users\user\Desktop\AML_Research\b-FDM_main2\scripts\build\build_assignment_candidate_matrix.py)
- [scripts/build/genetic_algorithm_step_adjacency_from_text.py](C:\Users\user\Desktop\AML_Research\b-FDM_main2\scripts\build\genetic_algorithm_step_adjacency_from_text.py)
- [scripts/build/beam_search_step_adjacency_from_text.py](C:\Users\user\Desktop\AML_Research\b-FDM_main2\scripts\build\beam_search_step_adjacency_from_text.py)
- [scripts/build/path_search_step_adjacency_from_text.py](C:\Users\user\Desktop\AML_Research\b-FDM_main2\scripts\build\path_search_step_adjacency_from_text.py)
- [main.py](C:\Users\user\Desktop\AML_Research\b-FDM_main2\main.py)


## Notes

- If local output still shows only one surviving pattern, that is now interpreted as:
  - either the minimum-switch local set truly has size 1
  - or the output came from an older run before the fix
- Re-running the pipeline is necessary to regenerate adjacency logs using the updated logic.
