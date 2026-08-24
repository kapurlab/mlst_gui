import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def _user_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "mlst_gui"
    return Path.home() / ".config" / "mlst_gui"


DATA_DIR = _user_config_dir()
CONFIG_PATH = DATA_DIR / "config.json"

# The multi-user "shared projects" root, if this deployment has one. A laptop
# normally does not — only a lab server or an OOD site. Never assume a path:
# probe in order of authority and fall back to None (no shared root) rather than
# a fictional one, so macOS/WSL users aren't shown a directory that cannot exist.
#   1. BDTOOLS_SHARED_PROJECTS_ROOT — exported by the launcher, which resolved it
#      from the machine's recorded site config. An explicitly empty value is
#      authoritative: it DISABLES the shared root.
#   2. the user's own `shared_projects_root` setting — see shared_projects_root()
# There is no step 3. A site supplies its own value (bdtools records it in
# <BDTOOLS_HOME>/site.conf); this file contains no path of its own, so the same
# release is correct on macOS, WSL, Linux and OOD without editing.
#
# This used to be one lab server's projects path, guarded by is_dir(). The guard
# kept the value out of config.json off that server, but the literal still
# decided what "shared" MEANT: any other site with its own shared root got no
# shared projects at all, silently, because the only path this file would accept
# was one it could never have.
# MLST_SHARED_PROJECTS is this tool's own older name for the same thing, kept
# because a deployment may already export it. It is consulted only when the
# suite-wide variable is unset, so one site cannot have the two disagree without
# saying which it means.
_ENV_SHARED_PROJECTS_ROOT = "BDTOOLS_SHARED_PROJECTS_ROOT"
_ENV_SHARED_PROJECTS_ROOT_LEGACY = "MLST_SHARED_PROJECTS"


def _shared_projects_env():
    """The shared-root env value, or None when neither variable is set. An
    explicitly empty value is authoritative: it DISABLES the shared root."""
    for name in (_ENV_SHARED_PROJECTS_ROOT, _ENV_SHARED_PROJECTS_ROOT_LEGACY):
        env = os.environ.get(name)
        if env is not None:
            return env.strip()
    return None


def _default_shared_projects_root() -> str:
    env = _shared_projects_env()
    return env if env is not None else ""


_DEFAULT_SHARED_PROJECTS_ROOT = _default_shared_projects_root()


def shared_projects_root() -> Optional[Path]:
    """The resolved shared-projects root, or None when this deployment has none.

    Read through this rather than a module constant, so the Settings value is
    honoured: main.py used to carry its own hard-coded literal, which meant
    setting `shared_projects_root` in the GUI changed what Settings displayed and
    nothing about where projects were discovered.

    Returns None — never Path("") — because Path("") is Path("."), the current
    working directory. An "unset" sentinel that silently means "look in ." would
    turn a missing shared root into project lookups against wherever uvicorn
    happens to have been started."""
    env = _shared_projects_env()
    if env is not None:
        return Path(env) if env else None
    try:
        configured = str(load_config().get("shared_projects_root", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return Path(configured)
    return Path(_DEFAULT_SHARED_PROJECTS_ROOT) if _DEFAULT_SHARED_PROJECTS_ROOT else None

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
