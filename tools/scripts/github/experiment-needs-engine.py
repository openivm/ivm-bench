#!/usr/bin/env python3
"""Determine whether any resolved experiment uses an engine."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src/containers/benchmark-server"))

from models.experiments import parse_experiments_json


if __name__ == "__main__":
    experiments = parse_experiments_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(str(any(sys.argv[2] in experiment.engines for experiment in experiments)).lower())
