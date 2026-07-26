# Project Structure

## `scripts/`

- `scripts/build/`
  - Build intermediate data from raw inputs.
  - Examples: material dictionary, assignment matrices, length matrix, beam adjacency text.

- `scripts/analysis/`
  - Evaluate and rank generated candidates.
  - Examples: continuity scoring, adjacency cluster evaluation, representative patterns, material switch counting.

- `scripts/simulation/`
  - Run deposition and material-switch simulation from matrix inputs.

- `scripts/ui/`
  - Interactive selection and visualization tools.

- `scripts/utils/`
  - Small helper scripts.

## `test_sample/`

- `test_sample/inputs/gcode/`
  - Raw G-code files.

- `test_sample/inputs/config/`
  - Input JSON/config files.
  - Examples: `Property_sample.json`, `sample_info.json`, `material_dictionary.json`.

- `test_sample/derived/matrices/`
  - Matrix-style intermediate outputs.
  - Examples: assignment candidate matrices, candidate matrix text, length matrix.

- `test_sample/derived/adjacency/`
  - Beam step adjacency and cluster outputs.
  - Examples: `beam_step_adjacency*.txt/json/png`.

- `test_sample/derived/continuity/`
  - Continuity-based analysis outputs.

- `test_sample/derived/simulation/`
  - Simulation outputs and material-switch reports.
  - Examples: GIF/PNG/JSON/TXT simulation files and candidate switch summaries.

## Notes

- Hard-coded paths in the moved Python scripts were updated to match this structure.
- Run scripts from the project root directory.
