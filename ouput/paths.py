"""Repository-local destinations for generated files."""

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_PATH = PROJECT_ROOT / ".cache" / "finacial" / "kline.sqlite3"


def output_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    if not path.is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError(f"Output path must be inside the repository: {path}")
    return path


def configure_plotting() -> None:
    # Riskfolio imports pyplot even when only its optimizer is used.
    cache = output_path(".cache/matplotlib")
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(cache)
