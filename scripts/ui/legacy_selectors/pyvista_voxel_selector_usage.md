# PyVista Voxel Selector Usage

This guide explains how to use `interactive_voxel_selector_pyvista.py` to select rectangular regions or path voxels from a G-code preview and save them as assignment data.

## Run

From the project root:

```powershell
& C:\Users\user\anaconda3\python.exe scripts/ui/interactive_voxel_selector_pyvista.py
```

The script currently loads:

```text
input/gcode/vase.gcode
```

The output folder is:

```text
out/ui/pyvista_voxel_selector
```

The saved assignment file is:

```text
out/ui/pyvista_voxel_selector/pyvista_rectangular_region_property_program.json
```

## Views

The UI has two linked views.

- Left view: rectangular region cells covering the printed shape.
- Right view: original path voxel/printing sequence view.

Camera movement is linked between both views.

## Basic Region Multi-Select

Use mouse clicks in the left view.

- Click a region once to add it to the current selection.
- Click the same selected region again to remove it.
- Multiple clicked regions are combined into one current selection.
- Press `A` to add the current selection as an assignment.
- Press `W` to save all assignments.

This is the recommended workflow for precise region selection.

## Box Drag Region Selection

Press `B` to enable box-region drag mode.

Workflow:

```text
B -> drag over the left view -> release
```

Notes:

- The drag box is drawn in white.
- `B` is used instead of `R` because `R` is commonly used by PyVista/VTK as a camera reset shortcut.
- The selector tries to avoid selecting hidden back-side regions using a visible-surface depth check.

## Path Voxel Selection

Use the right view to select path voxels directly.

- Press `V` to enable brush-add mode.
- Drag over the right path voxel view to add voxels.
- Press `X` to enable brush-remove mode.
- Drag over selected voxels to remove them.

After selecting path voxels:

```text
A -> add assignment
W -> save assignments
```

## Keyboard Shortcuts

| Key | Action |
| --- | --- |
| `B` | Enable box-region drag selection in the left view |
| `V` | Enable path voxel brush-add selection in the right view |
| `X` | Enable path voxel brush-remove selection in the right view |
| `A` | Add the current selection as an assignment |
| `W` | Save all assignments to JSON |
| `C` | Clear the current selection |

Avoid using `R` for region selection because it may reset the camera.

## Current Selection Behavior

When a selection is active, the UI highlights:

- Selected region cells in the left view.
- Corresponding path voxels in the right view.

When `A` is pressed, the assignment stores:

- Region IDs
- Region bounds
- Selected voxel IDs
- Voxel count
- Filament extrusion amount
- Cumulative extrusion range
- Layer range
- Default `gradient_steps`
- Default `eta`

## Changing Input G-code

At the bottom of `interactive_voxel_selector_pyvista.py`, update:

```python
gcode_file = project_root / "input" / "gcode" / "vase.gcode"
```

For example, to use Benchy:

```python
gcode_file = project_root / "input" / "gcode" / "3DBenchy.gcode"
```

## Selection Tuning

The script uses automatic rectangular-region size estimation when `rectangular_region_size_mm` is not provided.

Current run setup:

```python
selector = PyVistaVoxelRegionSelector(
    gcode_path=str(gcode_file),
    voxel_threshold_e=0.1,
    output_dir=str(output_dir),
)
```

If you need manually smaller rectangular cells, pass:

```python
rectangular_region_size_mm=(0.25, 0.25, 0.15)
```

If selection feels too narrow or too broad, tune these class values:

```python
self.drag_selection_padding_px
self.visible_pick_zbuffer_tolerance
```

General guidance:

- Increase `drag_selection_padding_px` if the drag area feels too narrow.
- Decrease `visible_pick_zbuffer_tolerance` if hidden back-side regions are selected.
- Increase `visible_pick_zbuffer_tolerance` if visible front-side regions are missed.

## Troubleshooting

If the camera jumps back to the home view:

- Do not use `R`.
- Use `B` for box-region drag selection.

If G-code is not found:

- Confirm the target file exists under `input/gcode`.
- Confirm `gcode_file` uses `project_root / "input" / "gcode" / "...gcode"`.

If drag selection is too hard to control:

- Prefer click-based multi-select.
- Click regions one by one, then press `A`.

