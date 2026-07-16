import json
import os
from pathlib import Path
from typing import Any, Dict


def _user_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "mlst_gui"
    return Path.home() / ".config" / "mlst_gui"


DATA_DIR = _user_config_dir()
CONFIG_PATH = DATA_DIR / "config.json"

_SHARED_PROJECTS_ROOT = Path(os.environ.get("MLST_SHARED_PROJECTS", "/srv/kapurlab/projects"))
_DEFAULT_SHARED_PROJECTS_ROOT = (
    str(_SHARED_PROJECTS_ROOT) if _SHARED_PROJECTS_ROOT.is_dir() else ""
)

# `mlst` ships its own bundled PubMLST database; a path is only needed if the
# DB was relocated (e.g. refreshed via MDU-PHL mlstdb). Empty => let `mlst`
# autodetect its bundled db. Override via the MLST_DB env var if your site
# keeps a refreshed copy elsewhere.
_DEFAULT_MLST_DB = os.environ.get("MLST_DB", "")

DEFAULTS: Dict[str, Any] = {
    "projects_root": str(Path.home() / "projects"),
    "shared_projects_root": _DEFAULT_SHARED_PROJECTS_ROOT,
    "saved_project_roots": [],
    # Path to a relocated mlst PubMLST blast db dir (optional; "" => bundled).
    "mlst_db": _DEFAULT_MLST_DB,
    # Default assembly thread count for shovill/spades.
    "threads": int(os.environ.get("MLST_THREADS", "8") or 8),
}


def load_config() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULTS)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
