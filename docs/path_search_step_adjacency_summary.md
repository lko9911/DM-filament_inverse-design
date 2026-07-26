# Path Search Summary

This note summarizes how `scripts/build/path_search_step_adjacency_from_text.py` works and what kind of algorithm it really is.

## What It Is

`path_search_step_adjacency_from_text.py` is a **custom path-search algorithm** for this project.

It is inspired by:

- `A*`
- `Dijkstra`
- `BFS`
- `DFS`

But it is **not** a pure textbook implementation of those algorithms.

The code searches over **candidate sequences across steps** and tries to find combinations with high continuity score.

## Main Goal

The search tries to choose one candidate for each step so that the full sequence has:

- high adjacency continuity
- high skip continuity
- high total score
- high `eta_sum` as a tie-break

## How The Custom Path Algorithm Operates

The custom search works as a **step-by-step sequence expansion**.

### Basic Idea

Each step has several candidate cases.

The algorithm builds full paths by:

1. choosing one candidate from step 1
2. expanding that partial path to step 2
3. scoring the transition
4. expanding again to step 3
5. continuing until the last step

Each full path is therefore:

```text
one selected candidate per step
```

### Internal Flow

The operation is:

1. Read all step blocks and candidate lists from `assignment_candidate_matrix.txt`.
2. Convert each candidate into a row pattern plus `eta`.
3. Create initial partial paths from every candidate in the first step.
4. Expand each partial path by trying every candidate of the next step.
5. Compute the new step score using continuity with recent history.
6. Update:
   - `selected_case_keys`
   - `selected_rows_per_step`
   - `step_scores`
   - `total_score`
   - `eta_sum`
   - `next_step_index`
   - `path_cost`
7. Rank the newly expanded partial paths.
8. Prune weak or excessive branches using the configured limits.
9. Continue until full terminal paths are reached.
10. Rank terminal paths by total score, then by `eta_sum`.

### What One Partial Path Stores

Each partial path remembers:

- which case keys were selected so far
- what row pattern each selected step has
- the score collected so far
- the sum of `eta`
- which step should be expanded next
- the accumulated path cost

This is why the algorithm can score using both the previous step and the step before that.

### Why History Is Important

This search is not just choosing the best candidate for one step independently.

It must remember recent path history because the score depends on:

- `t-1`
- `t-2`

So the quality of the current choice depends on earlier choices.

### Short Mental Model

You can think of the algorithm like this:

```text
step 1 candidates
-> build partial paths
-> expand to next step
-> score continuity
-> keep strong paths
-> expand again
-> repeat until the final step
-> rank final full paths
```

## How Scoring Works

For a new candidate at step `t`, the step score is:

```text
step_score = S(t-1, t) + S(t-2, t)
```

If `t-2` does not exist, then only:

```text
step_score = S(t-1, t)
```

Here, `S(a, b)` means how many rows match between two step patterns.

Because the score depends on both:

- previous step `t-1`
- two steps back `t-2`

the problem depends on **history**, not only the current node.

## Is It Scanning All Paths?

Usually, **no**.

This script does **not** fully enumerate every possible path in the default setting.

It prunes the search with:

- `BRANCH_LIMIT`
- `MAX_EXPANSIONS`
- `MAX_RESULTS`

That means it is a **pruned heuristic search**, not a full exhaustive search.

## Why It Is Custom

The implementation is custom because:

- the search state is a partial sequence of selected cases
- the score is a project-specific continuity score
- the objective is to maximize score, not minimize ordinary distance
- `eta_sum` is used as a tie-break
- branch pruning is built in
- global stopping limits are built in
- the priority formulas are custom

So the names `astar`, `dijkstra`, `bfs`, and `dfs` are best understood as **search styles**, not exact classical algorithms.

## Search Modes in This File

### `astar`

Uses a priority queue and prefers paths with:

- high current score
- optimistic future score
- slight preference for higher `eta_sum`

This behaves like a heuristic best-first search.

It is **not guaranteed** to be pure optimal A* because pruning and custom priority are used.

### `dijkstra`

Uses a priority queue and accumulated path cost.

But the priority is still modified by:

- depth bonus
- `eta_sum` bonus

So it is **not pure Dijkstra**.

### `bfs`

Explores paths level by level.

But it still prunes with `BRANCH_LIMIT`, so it is not a full textbook BFS over the whole search tree.

### `dfs`

Explores depth-first with a stack-like frontier.

But it still uses the same pruned child expansion logic.

## If Pure Path Algorithms Were Applied

If you replace this with pure graph/path algorithms, these are the main effects:

### Pure BFS

- explores all paths level by level
- complete, but memory can become very large

### Pure DFS

- can eventually scan all paths
- lower memory than BFS
- may spend a long time in poor branches first

### Pure Dijkstra

- can find a true global optimum if the problem is written as additive path cost
- requires the state to include enough history

### Pure A*

- can also find a true optimum if the heuristic is valid
- requires a correct admissible heuristic

## Important Modeling Detail

Because the score uses both `t-1` and `t-2`, a mathematically correct pure graph formulation should define the search state with enough history, for example:

- current step index
- previous selected candidate
- previous-previous selected candidate

If the state only stores the current step and current candidate, the search model is incomplete for this scoring rule.

## Practical Tradeoff

### Current custom search

- faster
- more practical for larger search spaces
- easier to control with pruning
- may miss the true global optimum

### Pure exact search

- cleaner mathematically
- can guarantee the true optimum
- usually much more expensive in time and memory

## Good Interpretation

The best short description of this script is:

> A custom, pruned, heuristic path search over step candidate sequences.

## Related Files

- `scripts/build/path_search_step_adjacency_from_text.py`
- `scripts/build/beam_search_step_adjacency_from_text.py`
- `scripts/build/genetic_algorithm_step_adjacency_from_text.py`
- `scripts/build/build_assignment_candidate_matrix.py`
