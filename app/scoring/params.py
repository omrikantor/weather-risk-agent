"""Load the versioned method parameters (app/scoring/params.yaml)."""

from functools import lru_cache
from pathlib import Path

import yaml

PARAMS_FILE = Path(__file__).with_name("params.yaml")


@lru_cache
def get_params() -> dict:
    return yaml.safe_load(PARAMS_FILE.read_text(encoding="utf-8"))
