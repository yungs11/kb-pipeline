#!/usr/bin/env python3
"""Run EXP-C1 in three bounded concurrent batches (GW<=3, VL<=3)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]
EXP = REPO / "research/EXP-C1-20260811"
BATCHES = (
    "3,10,14,17,18,19",
    "20,24,26,35,37",
    "41,42,46,49,56",
)


def main() -> None:
    procs = []
    for number, selected in enumerate(BATCHES, 1):
        env = dict(os.environ)
        env["KBP_EXP_SELECTED"] = selected
        env["KBP_EXP_DIR"] = str(EXP / f"batch-{number}")
        procs.append(subprocess.Popen(
            [sys.executable, str(REPO / "scripts/research_exp_c1.py")],
            cwd=REPO, env=env,
        ))
    codes = [proc.wait() for proc in procs]
    if any(codes):
        raise SystemExit(f"batch failures: {codes}")


if __name__ == "__main__":
    main()
