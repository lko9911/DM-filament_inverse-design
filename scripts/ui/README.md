# UI Tools

## All-in-One Pipeline UI

Use this launcher when you want one UI to control:

- G-code property design
- optimization pattern + length generation
- candidate selection
- DM filament output generation
- optional Prusa XL tool-change G-code conversion

```powershell
python scripts\ui\all_in_one_pipeline_ui.py
```

Notes:

- The selected G-code is used by the property designer step.
- The selected `sample_info.json` is used by the optimization/length stage.
- The selected property JSON determines the output folder under `out/<PropertyName>`.
- `Generate Prusa XL G-code` runs after MATLAB creates the DM `*_mod.txt` file.
- Configure `XL tool map` to match the material codes in the selected candidate
  with the five physically loaded Prusa XL tools (`T0` through `T4`).
- The XL result is saved beside the MATLAB output as
  `*_mod_PrusaXL.gcode`. The original `*_mod.txt` file is kept.
- `Prepare only` cannot be combined with XL conversion because it does not run
  MATLAB and therefore does not create a DM G-code input for the converter.

## Layer x Region Resolution

The property designer now treats the effective DM program as a chronological
sequence of deposition occurrences:

```text
Layer -> Region -> repeated occurrence of that Region
```

Use `Preview Layer x Region` in the property designer to inspect the exact XY
deposition paths for one layer. The layer slider changes Z, and the table shows
the Region name, occurrence number, positive extrusion E, path-segment count,
and the assigned Property/Gradient. `Export Current Layer PNG` saves the
currently displayed evidence image.

Saving the property design also writes these files under
`out/<PropertyName>/input`:

- `*_layer_region_analysis.json`: source-line ranges, XY bounds, features,
  positive-E segments, and per-layer/per-Region totals.
- `*_layer_region_execution_plan.json`: chronological Region events mapped to
  property assignments.

When `main.py` sees this execution plan, it creates
`test_sample/derived/layer_region/expanded_property_program.json`. Each
Layer x Region occurrence becomes one effective optimization/material-switch
step whose length is its measured positive extrusion E. A printing-direction
gradient is sampled by cumulative deposition position; a layer-direction
gradient is sampled by layer position.

Only labeled model deposition can be mapped automatically. Extrusion outside
`;MESH`, `M486`, `; printing object`, or explicit Region markers is reported as
`non_region_deposition_e_mm` and shown as a coverage warning. Prime, skirt,
support, wipe, and purge behavior must be assigned deliberately before relying
on exact material-boundary synchronization.

## Current Component Property Designer

Use this PyQt5/VTK designer for the current workflow:

```powershell
& C:\Users\user\anaconda3\python.exe scripts\ui\qt_full_gcode_object_property_designer.py
```

It reads the default `INPUT_GCODE` value inside
`qt_full_gcode_object_property_designer.py`, detects object sections from
Prusa/Orca-style `; printing object ... id:N copy M` comments or mesh sections
from `;MESH:part_name.STL` comments, previews selected objects in 3D, edits
object property settings, and saves. The component list is ordered by each
component's first real positive-E extrusion, not by the earlier M486 declaration
order, so purge/custom sections appear where they actually print.

```text
input\config\Property_sample.json
```

If you prefer a small launcher with only input/output variables, edit and run:

```powershell
& C:\Users\user\anaconda3\python.exe scripts\ui\run_qt_full_gcode_object_designer_example.py
```

## Support Modules

- `full_gcode_object_property_designer.py`: object-comment G-code parser and older optional preview helpers.
- `component_property_designer.py`: shared property JSON builder and older Matplotlib designer.

## Legacy Selectors

Older voxel and mesh selector tools are stored in:

```text
scripts\ui\legacy_selectors
```
