# Usage Log

This file is the living runbook for commands and usage code in `DM_filament_model ver4`.

Update this file whenever a command changes, a new workflow is added, or a prior command becomes outdated.

## Current Commands

### 1. Open the assignment editor

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\Gcode_Property_Program_Model_Designer.py" `
  ".\DM_filament_model ver4\vase.gcode"
```

### 2. Save assignment output to a custom folder

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\Gcode_Property_Program_Model_Designer.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --output-dir ".\my_outputs"
```

### 3. Calculate total filament amount from G-code

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode"
```

### 4. Save filament summary JSON

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --output-json ".\vase_filament_summary.json"
```

### 5. Save voxel summary JSON

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --output-voxels-json ".\vase_voxel_summary.json"
```

### 6. Save assignment summary JSON from a property program

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\DM_filament_model ver4\vase_property_program.json" `
  --output-assignments-json ".\vase_assignment_summary.json"
```

### 7. Save split assignment summary

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\DM_filament_model ver4\vase_property_program.json" `
  --split-assignment-half
```

### 8. Save test assignment summary from voxel chunks

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --test-assignment-chunk-size 100
```

### 9. Show the filament rectangle preview with `plt.show()`

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\DM_filament_model ver4\vase_model_designer_outputs\vase_property_program.json" `
  --show
```

### 10. Show assignment boxes more clearly in the filament preview

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_property_program.json" `
  --show
```

### 11. Save raw material-name matrices for copy-paste

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_property_program.json" `
  --output-step-material-raw ".\my_outputs\vase_step_material_raw.txt"
```

- The raw matrix uses 14 rows and `gradient_steps` columns per assignment.
- Row fill is driven by the representative 14-row weight pattern `2,3,3,4,4,4,4,4,4,4,4,3,3,2`.
- Each step column is generated independently from a stepwise transition profile that starts mat1-heavy, ends mat2-heavy, and preserves the assignment-level ratio on the weighted average.
- For each step, all row-combination candidates are generated from that step's own target ratio, then `eta` is computed for every candidate, and the chosen candidate keeps the ratio match while preferring the largest eta that does not exceed the assignment eta cap.
- If you see 5 columns, the property JSON you ran still has `"gradient_steps": 5`.
- If `gradient_steps` is set to 1, the output becomes `14 x 1` for that assignment.

### 11b. Generate a stepwise matrix with eta-guided candidate selection

```powershell
python ".\DM_filament_model ver4\gcode_filament_amount.py" ".\DM_filament_model ver4\vase.gcode" --property-json ".\my_outputs\vase_property_program.json" --output-step-material-raw ".\my_outputs\vase_step_material_raw.txt" --output-step-material-candidate-analysis ".\my_outputs\vase_step_material_candidate_analysis.txt" --output-step-material-candidate-raw ".\my_outputs\vase_step_material_candidate_raw.txt"
```

- This is the main command for the new stepwise pattern logic.
- It keeps the assignment-level ratio as the overall target, while each step transitions from mat1-dominant to mat2-dominant.
- The candidate analysis and raw candidate outputs let you inspect every step-level combination that was considered before the chosen pattern was written.
- If you are already in the activated `(torch)` prompt, `python` will use that environment directly.
- Use the PowerShell-style command only inside PowerShell, not inside `cmd.exe`.

### 12. Save the 48-slot material ratio analysis table

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --output-step-material-analysis ".\my_outputs\vase_step_material_analysis.txt"
```

- The analysis table compares target ratio vs actual 48-slot ratio for each assignment.
- The actual ratio is computed from the representative row weights `2,3,3,4,4,4,4,4,4,4,4,3,3,2`.
- This table is independent from the raw matrix text file.

### 13. Save all explored row-pattern candidates

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --output-step-material-candidates ".\my_outputs\vase_step_material_candidates.txt"
```

- The candidate table is now step-aware: each assignment contributes one candidate pool per `gradient_steps` column.
- Each step pool uses that step's own transition ratio, so the first step is mat1-heavy, the middle step follows the assignment average, and the last step is mat2-heavy.
- The assignment-level material ratio is still preserved by the stepwise transition profile; candidates are no longer generated from one flat assignment-wide ratio.
- This is the easiest place to compare plausible fill choices for each step before the eta-guided selection picks one.

### 14. Save all candidate matrices in raw matrix form

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --output-step-material-candidate-raw ".\my_outputs\vase_step_material_candidate_raw.txt"
```

- This writes every step-level candidate as a `material_name_matrix_raw`-style matrix.
- Each block is labeled by assignment and step, and only the candidate's own step column is filled so you can inspect the step-specific ratio target directly.
- Use this when you want to inspect the raw step candidate space before the final full assignment matrix is assembled.

### 14b. Save every candidate as its own PNG image

```bat
python ".\DM_filament_model ver4\gcode_filament_amount.py" ".\DM_filament_model ver4\vase.gcode" --property-json ".\my_outputs\vase_property_program.json" --output-step-material-candidate-gallery-dir ".\my_outputs\vase_candidate_gallery"
```

- This writes one PNG per candidate combination into a folder.
- Each PNG uses the same full filament rectangle layout as `vase_filament_rectangle_material.png`.
- Files are grouped by candidate index, so the output stays manageable even when the candidate count is large.
- A `candidate_gallery_index.json` file is also written so you can map each image back to its assignment and candidate index.
- Use this when you want every candidate visible as an image, not just as text.

### 15. Save candidate combination analysis

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --output-step-material-candidate-analysis ".\my_outputs\vase_step_material_candidate_analysis.txt"
```

- This table lists every step-level candidate combination with its actual slot count, step target ratio, ratio error, and eta proxy.
- Use it when you want to compare the candidate space numerically instead of visually.
- It is the analysis companion to `--output-step-material-candidate-raw`.

### 16. Filter candidates by eta proxy

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --candidate-eta-min 0.08 `
  --candidate-eta-max 0.15 `
  --output-step-material-candidate-analysis ".\my_outputs\vase_step_material_candidate_analysis_filtered.txt" `
  --output-step-material-candidate-raw ".\my_outputs\vase_step_material_candidate_raw_filtered.txt"
```

- `eta_proxy` is the discrete candidate score derived from the interface boundaries in the 14-layer pattern.
- In this project, `eta` is computed as the sum of `ceil(boundary_width / 4.0)` over every material-change boundary, where each boundary width comes from the adjacent layer weights in `2,3,3,4,4,4,4,4,4,4,4,3,3,2`.
- The candidate pool is generated separately for every step target ratio instead of from one assignment-wide ratio.
- One interface zone contributes `1`, and a wider or double-sided interface region can contribute `2` when the boundary spans enough 4-layer units.
- The assignment's own `eta` value is treated as the maximum eta cap during automatic step selection; among ratio-valid candidates under that cap, the selector prefers the largest eta.
- Use `--candidate-eta-tolerance` to keep only candidates whose `eta_proxy` is within that absolute distance from the assignment `eta`.
- `--candidate-eta-min` and `--candidate-eta-max` still exist as a direct proxy-range filter, but the assignment-targeted tolerance is usually the more meaningful choice.
- The step material analysis table now includes both `step_eta` and `step_ratio_error` lists so you can inspect each step's selected eta and ratio mismatch directly.

### 17. Save eta filter counts per assignment

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --candidate-eta-tolerance 0.05 `
  --output-step-material-candidate-eta-summary ".\my_outputs\vase_step_material_candidate_eta_summary.txt"
```

- This file shows how many candidates survived eta filtering for each assignment.
- It reports `before_count`, `after_count`, `removed_count`, and `keep_ratio`.
- Use it together with the candidate analysis table when you want both the surviving candidates and the survival count.

### 18. Preview one filtered candidate as a PNG

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --candidate-eta-tolerance 0.05 `
  --output-step-material-candidate-png ".\my_outputs\vase_candidate_preview.png"
```

- This writes one candidate matrix as a color-coded PNG.
- By default it previews assignment 1, candidate 1 from the filtered candidate pool.
- Use `--candidate-preview-assignment-index` and `--candidate-preview-candidate-index` if you want a different candidate.
- Add `--show` if you also want to open the preview window.

### 19. Preview the full filament with assignment regions filled by candidate matrices

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --candidate-eta-tolerance 0.05 `
  --output-step-material-assignment-candidate-png ".\my_outputs\vase_assignment_candidate_coverage.png"
```

- This draws the whole filament bar and fills each assignment region with a candidate matrix.
- The preview uses the same long rectangle proportion as `vase_filament_rectangle.png`.
- The step columns follow each assignment's saved `step_segments` start/end fractions directly, the same way the rectangle preview marks step boundaries.
- The step boundaries are drawn thicker and labeled so they are visible even after the material fill is applied.
- The preview uses the filtered candidate pool already produced by ratio and eta filtering.
- By default it takes candidate index 1 from each assignment's candidate list.
- Add `--candidate-preview-candidate-index` if you want the same candidate slot across all assignments.

### 20. Save the exact rectangle preview for 10 candidate combinations

```bat
python ".\DM_filament_model ver4\gcode_filament_amount.py" ".\DM_filament_model ver4\vase.gcode" --property-json ".\my_outputs\vase_property_program.json" --output-rectangle-material-gallery-dir ".\my_outputs\vase_filament_rectangle_material_gallery" --rectangle-material-gallery-count 10
```

- This uses the exact same rectangle drawing logic as `vase_filament_rectangle_material.png`.
- Only the material fill comes from the candidate pool.
- The command writes 10 rectangle PNGs by default, one for each candidate index.
- The output folder also gets a `vase_filament_rectangle_material_gallery_index.json` file.
- `gradient_steps` controls how many step columns appear inside each assignment region.
- Different `gradient_steps` values change the actual fill pattern, not just the label count.
- Add `--show` if you also want to open the preview window.

### 20. Preview each assignment's internal shape as a gallery

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --candidate-eta-tolerance 0.05 `
  --output-step-material-assignment-shape-png ".\my_outputs\vase_assignment_shape_gallery.png"
```

- This shows each assignment in its own panel, like a set of DM-filament cross-section shapes.
- It is the closest preview to the eta explanation figure you referenced.
- By default it uses candidate index 1 for each assignment after filtering.
- Use `--candidate-preview-candidate-index` to switch to a different candidate slot.
- Add `--show` if you also want to open the preview window.

### 21. Preview each assignment as a radial cross-section

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --candidate-eta-tolerance 0.05 `
  --output-step-material-assignment-radial-png ".\my_outputs\vase_assignment_radial_preview.png"
```

- This shows the assignment cross-section as same-size blocks stacked by row, without clipping the outer boundary.
- The row block counts use the 14-layer weight pattern `2,3,3,4,4,4,4,4,4,4,4,3,3,2`, every block uses the same width, and the preview is sized to a wide rectangle of about `440 x 125`.
- By default it uses candidate index 1 for each assignment after filtering.
- Add `--candidate-preview-candidate-index` if you want a different candidate slot.
- Add `--show` if you also want to open the preview window.

### 22. Fill the rectangle preview with candidate materials

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --output-rectangle-material-png ".\my_outputs\vase_filament_rectangle_material.png"
```

- This keeps the exact rectangle layout from `vase_filament_rectangle.png`.
- The assignment lengths and step boundaries follow the saved `step_segments` fractions.
- Each assignment is filled using the stepwise ratio curve for its `gradient_steps` value, then the candidate rank is used to pick among eta-valid row combinations.
- Do not add candidate analysis flags for normal preview generation; the selector already uses the assignment eta cap and avoids writing the full candidate pool.
- Use `--candidate-preview-candidate-index` if you want a different candidate rank.
- Add `--show` if you also want to open the preview window.

### 23. Save 10 rectangle variants from the stepwise candidate ranks

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_assignment_summary.json" `
  --output-rectangle-material-gallery-dir ".\my_outputs\vase_filament_rectangle_material_gallery" `
  --rectangle-material-gallery-count 10
```

- This writes `vase_filament_rectangle_material_candidate_01.png` through `..._10.png`.
- Each image uses the same rectangle drawing logic as `vase_filament_rectangle.png`, but the fill matrix comes from a different candidate rank.
- The stepwise ratio profile still follows `gradient_steps`, so changing the step count changes the actual fill pattern.
- The gallery count is controlled by `--rectangle-material-gallery-count`, not by the number of precomputed raw candidate matrices.
- Candidate text/analysis outputs are exhaustive and can be slow on large assignment sets; use them only when you explicitly need the full candidate table.

### 23b. Save full-filament candidate matrices and 100 images

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\DM_filament_model ver4\vase_assignment_summary.json" `
  --assignment-candidate-count 0 `
  --output-assignment-candidate-raw ".\DM_filament_model ver4\vase_assignment_candidate_raw.txt" `
  --output-full-filament-candidate-raw ".\DM_filament_model ver4\vase_full_filament_candidate_raw.txt" `
  --full-filament-candidate-count 0 `
  --output-rectangle-material-gallery-dir ".\DM_filament_model ver4\vase_filament_rectangle_material_gallery" `
  --rectangle-material-gallery-count 100
```

- `vase_full_filament_candidate_raw.txt` stores each assignment-combination candidate as one full `14 x total_assignment_steps` matrix.
- `vase_assignment_candidate_raw.txt` stores the assignment-level candidate matrices first, grouped as `# assignment 1 | candidate_count=N`.
- Columns are labeled in the comment above each matrix, for example `A1s1 ... A1s11, A2s1 ... A2s5`.
- This is the recommended text file for the next optimization stage because every saved candidate is already represented as the whole filament pattern.
- The total candidate count is computed as `assignment 1 candidate count x assignment 2 candidate count x ...`.
- `--assignment-candidate-count 0` asks the script to keep all ranked candidate matrices per assignment instead of the default 10.
- Set `--full-filament-candidate-count 0` to save every assignment-combination candidate matrix instead of capping the text file at 100.
- The text header records `assignment_candidate_counts`, `assignment_combination_candidate_count`, and `matrices_written`.
- The image gallery now follows the same assignment-combination order as the text file and records `candidate_indices_by_assignment` in the gallery manifest.
- The image gallery is capped by `--rectangle-material-gallery-count 100`.

### 24. Save every raw candidate combination as its own PNG

```powershell
& C:\Users\user\anaconda3\envs\torch_gpu\python.exe `
  ".\DM_filament_model ver4\gcode_filament_amount.py" `
  ".\DM_filament_model ver4\vase.gcode" `
  --property-json ".\my_outputs\vase_property_program.json" `
  --candidate-eta-tolerance 0.05 `
  --output-step-material-candidate-raw-gallery-dir ".\my_outputs\vase_candidate_raw_gallery"
```

- This writes one PNG for every raw candidate combination that survives the stepwise candidate filters.
- The raw combinations now come from the stepwise candidate matrix pool, so each image reflects the step-by-step ratio profile instead of a flat assignment-wide row pattern.
- The candidate pool keeps the full ratio-valid search space first, then applies the step-start material rule and eta filter, so fragmented row subsets are still available when they satisfy the requested constraints.
- The console now prints the per-assignment candidate counts and their product before the gallery starts rendering, so the image count and the final candidate-space size are easy to compare.
- Each PNG is a full rectangle preview, using the same overall filament layout as `vase_filament_rectangle_material.png`.
- The images are written directly into the target folder as `candidate_combo_######.png`, and each image shows the whole filament structure for one Cartesian-product combination across all assignments.
- The raw candidate generation still keeps the stepwise candidate ranks, so the printed count reflects the full Cartesian-product sweep after the step-ratio and eta checks.
- The step-by-step candidate details are still stored in the text outputs, so the image stays at the assignment-level overview.
- The rectangle preview legend now also prints each step's target ratio, selected ratio, and eta so the stepwise ratio change is visible in the image itself.
- The gallery currently writes 10 candidate ranks per assignment by default, so increasing the gallery size expands the Cartesian-product coverage.
- If you want the unfiltered raw combinations, remove `--candidate-eta-tolerance`.

## Notes

- `Save Assignment` writes the property program next to the G-code in `*_model_designer_outputs`.
- `Add` previews the current range as a pending assignment.
- `Result` finalizes the full assignment list, renumbers it, and saves the property program.
- `Remove` deletes the current range assignment from the preview list.
- The assignment designer no longer includes a `Property on/off` toggle.
- Use `plt.show()` from the editor to inspect the interactive 3D assignment view.
- Use `--show` on `gcode_filament_amount.py` when you want the rectangle preview window.
- The rectangle PNG is only written when you pass `--output-rectangle-png` explicitly.
- Assignment boxes are now drawn with stronger borders so the full filament bar and each assignment region are easier to distinguish.
- Layer-based step splitting in `gcode_filament_amount.py` uses the saved `voxel_layer_table` from each assignment.
- If layer-based step boxes look off, re-save the assignment so the table and the preview use the same layer mapping.
- Step regions inside a single assignment are drawn without extra fill color changes; only the borders separate them.
