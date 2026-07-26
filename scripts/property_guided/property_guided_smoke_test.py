from __future__ import annotations

from pprint import pprint
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.property_guided.property_library import (
    PropertyRequirement,
    load_property_library,
    property_library_summary,
    select_gradient_sequence,
    select_property_candidate,
)


def main() -> None:
    library = load_property_library()
    print("Library summary:")
    pprint(property_library_summary(library))

    print("\nTest 1: target_Eb_MPa=600")
    eb_result = select_property_candidate(PropertyRequirement(target_Eb_MPa=600), library)
    pprint(eb_result.to_dict())

    print("\nTest 2: min_GF=2.5")
    gf_result = select_property_candidate(PropertyRequirement(min_GF=2.5), library)
    pprint(gf_result.to_dict())

    print("\nTest 3: max_R0_ohm=5000")
    r0_result = select_property_candidate(PropertyRequirement(max_R0_ohm=5000), library)
    pprint(r0_result.to_dict())

    print("\nTest 4: out-of-range target_Eb_MPa=30")
    out_of_range = select_property_candidate(PropertyRequirement(target_Eb_MPa=30), library)
    pprint(out_of_range.to_dict())

    print("\nTest 5: gradient Eb 400 -> 1400")
    gradient_result = select_gradient_sequence(
        PropertyRequirement(
            gradient_enabled=True,
            gradient_property="Eb",
            gradient_start_value=400,
            gradient_end_value=1400,
        ),
        library,
    )
    pprint(gradient_result)


if __name__ == "__main__":
    main()
