"""Remove generated build outputs only; preserve local history and environments."""

import shutil
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    for name in ("build", "dist", "zontcontrol.egg-info"):
        path = root / name
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    print("Generated package outputs removed.")


if __name__ == "__main__":
    main()
