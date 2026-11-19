"""Compile pyreaction's core with mypyc, in place, for 1.5-1.7x faster rendering.

    uv run python scripts/compile.py          # build src/reaction/core/*.so
    uv run python scripts/compile.py --clean  # back to plain Python

Compiled modules take precedence over the .py files next to them, so run this
again after editing one (the package warns when a compiled module is older than
its source). Needs a C compiler. The tests pass either way.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
MODULES = ["reaction/core/vdom.py", "reaction/core/element.py", "reaction/core/ssr.py", "reaction/core/hooks.py"]


def clean() -> None:
    for built in [*SRC.glob("reaction/core/*.so"), *SRC.glob("*__mypyc*.so"), *SRC.glob("reaction/core/*.pyd")]:
        built.unlink()
    for folder in (SRC / "build", SRC / ".mypy_cache"):
        if folder.exists():
            subprocess.run(["rm", "-rf", str(folder)], check=True)


def main() -> int:
    clean()
    if "--clean" in sys.argv:
        print("Removed the compiled modules: pyreaction runs as plain Python.")
        return 0
    result = subprocess.run([sys.executable, "-m", "mypyc", *MODULES], cwd=SRC)
    if result.returncode != 0:
        print("mypyc failed; the plain Python modules are still in use.", file=sys.stderr)
        clean()
        return result.returncode
    subprocess.run(["rm", "-rf", str(SRC / "build")], check=True)
    print("Compiled:", ", ".join(Path(m).stem for m in MODULES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
