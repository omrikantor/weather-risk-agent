"""Load the versioned method parameters (app/scoring/params.yaml)."""

from functools import lru_cache
from pathlib import Path

import yaml

PARAMS_FILE = Path(__file__).with_name("params.yaml")


@lru_cache   # read the file once per process; every module sees the same parameters
def get_params() -> dict:
    """All method parameters as a dict. Every value in the file has a written reason next to it."""
    return yaml.safe_load(PARAMS_FILE.read_text(encoding="utf-8"))
