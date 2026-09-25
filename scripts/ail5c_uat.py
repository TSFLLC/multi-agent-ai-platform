"""AIL.5C deterministic local UAT runner.

    python -m scripts.ail5c_uat

Runs the eight acceptance scenarios in ``tests/test_ail5c_uat.py`` against a
disposable temp database with a scripted fake Grader provider — no network, no
real model, never the real database — and prints one PASS/FAIL line per
scenario. Exits non-zero if any scenario fails.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_ail5c_uat.py",
        "-v",
        "-p",
        "no:cacheprovider",
        "-W",
        "ignore",
    ]
    print("AIL.5C deterministic local UAT (disposable DB, scripted provider)\n")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    scenarios = [line for line in result.stdout.splitlines() if "::test_uat" in line]
    for line in scenarios:
        name = line.split("::")[1].split(" ")[0]
        print(f"  {'PASS' if ' PASSED' in line else 'FAIL'}  {name}")
    print()
    print(result.stdout.splitlines()[-1] if result.stdout.strip() else result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
