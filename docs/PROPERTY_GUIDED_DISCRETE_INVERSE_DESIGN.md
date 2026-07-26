## Property-Guided Discrete Inverse Design

This workflow adds a property-guided mode on top of the existing manual DM filament design flow.

### What It Means

The user-defined target properties are used as queries for a discrete property library constructed from reported experimental data. Rather than predicting arbitrary continuous material properties, the system selects the closest manufacturable material-design class, defined by base material pair, material ratio, and homogeneity parameter eta. The selected class, reported property value, and target mismatch are returned to the user and subsequently used for DM filament pattern generation and sequence optimization.

### Data Source

Primary workbook:

- `Property Data/source date.xlsx`

Current parsers use the reported data from:

- `Supplementary Figure 2` for color
- `Supplementary Figure 5` for `R0`
- `Supplementary Figure 6` for `Eb` and `GF`
- `Figure 2` for elongation at break

### Supported Properties

- `Color`
- `Eb` (flexural modulus)
- `Elongation at break`
- `R0` (initial resistance)
- `GF` (gauge factor)

### Workflow

1. User sets region assignments in either:
   - `manual` mode
   - `property_guided` mode
2. In `property_guided` mode, the system loads the Excel-based discrete property library.
3. The system selects the closest reported candidate class.
4. The selected candidate is converted into the current workflow parameters:
   - `material_start`
   - `material_end`
   - `material_start_ratio`
   - `material_end_ratio`
   - `eta`
5. Existing candidate generation, adjacency scoring, and sequence optimization then run as before.

### Gradient Handling

For property-guided gradients, the system builds a discrete sequence of reported classes and stores them as resolved step targets before candidate generation. The candidate generator then follows the resolved ratio and eta targets step by step.

### Important Limitations

Assigned properties represent nominal local effective properties of reported printed material-design classes. They do not guarantee exact final structural responses for arbitrary geometries. Mechanical and electrical behavior may vary depending on region geometry, infill density, raster orientation, layer bonding, conductive path length, electrode configuration, and printing conditions.

Also:

- `Eb` is flexural modulus, not flexural stress.
- `R0` is geometry and electrode-configuration dependent.
- `GF` depends on deformation mode, strain range, and conductive network stability.
- `eta` is a design variable, not a user target property.
- The system performs discrete property-class selection, not exact continuous material-property prediction.

### Files Added

- `scripts/property_guided/property_library.py`
- `scripts/property_guided/resolve_property_guided_program.py`
- `scripts/property_guided/property_guided_smoke_test.py`

