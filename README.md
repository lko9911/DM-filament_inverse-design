# b-FDM_main2
b-FDM_최적화 방법론 

1. UI 구현


2. 재료의 교체 최적화 이론만 세우면됨
- FDM 최적 시뮬레이션 이론으로 진행 (순차 진행해야 안햇갈림)

3. 최종 매트랩 연결

## Optimization Problem Summary

This project solves a material-pattern selection problem for b-FDM gradient
printing. For every printing step, the workflow chooses one feasible material
case from the material dictionary.

### Decision variable

```text
x = (x_1, x_2, ..., x_N)
x_t in C_t,  t = 1,...,N
```

- `N`: number of printing or gradient steps.
- `x_t`: selected material case at step `t`.
- `C_t`: feasible candidate set for step `t`.

For the current `input/config/Property_sample.json`, `N = 13`.

### Feasible candidate set

```text
C_t = { c in D :
        |phi_start(c) - phi_target,t| <= epsilon,
        eta_min <= eta(c) <= eta_limit,t }

epsilon = 1 / 48
```

- `D`: full material dictionary.
- `phi_start(c)`: start-material ratio of candidate `c`.
- `phi_target,t`: target start-material ratio at step `t`.
- `eta(c)`: eta value of candidate `c`.
- `eta_limit,t`: assignment eta limit.

For the current gradient assignment, `eta_limit,t = 2.0`.

### Adjacency score

Let `M_r(x_t)` be the material at row `r` when case `x_t` is selected.

```text
S(x_i, x_j) = sum_r 1[ M_r(x_i) = M_r(x_j) ]
```

The adjacency search score is:

```text
Q_adj(x) =
sum_{t=2..N} S(x_{t-1}, x_t)
+ sum_{t=3..N} S(x_{t-2}, x_t)
```

This rewards material continuity between neighboring steps and skip-neighbor
steps.

### Final optimization

The final ranking is a lexicographic minimization:

```text
x* = argmin_x ( K(x), -E(x), -Q(x), Rank(x) )

subject to:
    x_t in C_t,  t = 1,...,N

where:
    K(x) = material switch count
    E(x) = sum_t eta(x_t)
    Q(x) = adjacency / compactness score
```

Priority order:

```text
1. minimize material switch count K(x)
2. maximize eta sum E(x)
3. maximize adjacency/cluster score Q(x)
4. minimize original rank
```

Current optimal output:

```text
K(x*) = 2
E(x*) = 19.0
Score = 28
```

## Adjacency Search Algorithms

The default adjacency search is still GA. Additional path-search algorithms are
available without changing the GA implementation:

```powershell
$env:B_FDM_ADJACENCY_SEARCH_ALGORITHM="astar"      # or dijkstra, bfs, dfs
$env:B_FDM_PATH_SEARCH_MAX_EXPANSIONS="200000"
$env:B_FDM_PATH_SEARCH_MAX_RESULTS="200"
$env:B_FDM_PATH_SEARCH_BRANCH_LIMIT="64"
uv run python main.py
```

Supported values:

```text
ga, beam, astar, dijkstra, bfs, dfs
```

`diax` is accepted as an alias for `dijkstra`.

## Component G-code Property UI

Create `input/config/Property_sample.json` from 1 to 5 component G-code files:

```powershell
$env:UV_CACHE_DIR='.uv-cache'
uv run python scripts\ui\component_property_designer.py `
  input\gcode\component_1.gcode `
  input\gcode\component_2.gcode `
  --output input\config\Property_sample.json
```

The UI previews each component path, calculates total extrusion `E`, lets you
show multiple components, choose component print order, select property settings,
and saves the compact property JSON format used by `main.py`.

If all components are inside one full G-code file, use the object-comment parser:

```powershell
$env:UV_CACHE_DIR='.uv-cache'
uv run python scripts\ui\full_gcode_object_property_designer.py `
  input\gcode\full_model.gcode `
  --output input\config\Property_sample.json
```

This version detects component sections from comments like
`; printing object ... id:2 copy 0` and `; stop printing object ... id:2 copy 0`.
Use the component checkboxes to choose which detected components are written to
the output JSON.

The designer preview now shows only the active selected component. If your Python
environment has PyVista/VTK installed, add `--pyvista-preview` to replace the
2D component preview with a live PyVista 3D preview window; selecting `C1` to
`C5` in the property designer updates that PyVista window to show only the
selected object.

For one integrated window, use the Qt/VTK designer. It embeds the 3D G-code
component preview directly beside the property controls:

```powershell
& C:\Users\user\anaconda3\python.exe scripts\ui\qt_full_gcode_object_property_designer.py `
  "Sample_compenent\origami_gripper - base^origami_gripper-2_0.15mm_ABS_MK3S_55m.gcode" `
  --output input\config\Property_sample.json
```

The integrated designer also provides `Preview Layer x Region`. It displays
the actual positive-E XY toolpath for every labeled Region occurrence in the
selected layer. Saving the design records an analysis JSON and chronological
execution-plan JSON; `main.py` then expands that plan so optimization and
material-switch lengths operate at Layer x Region occurrence resolution rather
than only at whole-component resolution. See `scripts/ui/README.md` for the
output contract and the non-Region/purge synchronization limitation.

Or edit the input path in:

```text
scripts\ui\run_qt_full_gcode_object_designer_example.py
```

and run:

```powershell
& C:\Users\user\anaconda3\python.exe scripts\ui\run_qt_full_gcode_object_designer_example.py
```
