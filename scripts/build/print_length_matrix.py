from pathlib import Path

import numpy as np


LENGTH_MATRIX_PATH = Path("test_sample/derived/matrices/length_matrix.npy")


def load_length_matrix(path: Path) -> np.ndarray:
    if not path.exists():
        raise FileNotFoundError(f"Length matrix file not found: {path}")
    return np.load(path, allow_pickle=False)


def main() -> None:
    length_matrix = load_length_matrix(LENGTH_MATRIX_PATH)

    print("length_matrix:")
    print(length_matrix)
    print("length_matrix values:")
    for index, value in enumerate(length_matrix, start=1):
        print(f"{index}: {value}")


if __name__ == "__main__":
    main()
