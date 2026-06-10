#!/usr/bin/env bash
# install.sh — idempotent, NO-SUDO install of the MLST GUI.
#
# Mirrors the vSNP GUI's setup-sandbox approach. Everything lives under the
# tool root (shared) or a personal conda env; nothing is written system-wide.
#
# What it does (each step is safe to re-run):
#   1. Ensure a conda (miniforge/mamba) is on PATH.
#   2. Create the `mlst` conda env at <repo>/env (or a personal env via --env-name).
#   3. pip-install the FastAPI backend requirements into that env.
#   4. Verify `mlst --version` and that its bundled PubMLST DB is present
#      (`mlst --list` non-empty).
#   5. Build the React frontend (frontend/dist/).
#
# It does NOT download or refresh the PubMLST database — `mlst` ships a bundled
# snapshot that works out of the box. See INSTALL.md for refreshing the DB and
# for registering the OOD app card on a new system.
#
# Usage:
#   deploy/install.sh [--env-prefix <dir>] [--env-name <name>]
#                     [--conda-base <dir>] [--skip-env] [--skip-frontend]
#                     [--dry-run]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---- defaults ----
ENV_PREFIX="${REPO_DIR}/env"        # shared env at the tool root
ENV_NAME=""                         # set via --env-name for a personal install
CONDA_BASE="${CONDA_BASE:-${HOME}/miniforge3}"
DO_ENV=1
DO_FRONTEND=1
DRY_RUN=0

c_b=$'\e[1m'; c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_0=$'\e[0m'
log()  { printf '%s==>%s %s\n' "$c_b" "$c_0" "$*"; }
ok()   { printf '  %sok%s %s\n' "$c_g" "$c_0" "$*"; }
warn() { printf '  %s!!%s %s\n' "$c_y" "$c_0" "$*" >&2; }
die()  { printf '%sERROR%s %s\n' "$c_r" "$c_0" "$*" >&2; exit 1; }
run()  { if [[ $DRY_RUN -eq 1 ]]; then echo "  [dry-run] $*"; else "$@"; fi; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-prefix)   ENV_PREFIX="$2"; ENV_NAME=""; shift 2;;
    --env-name)     ENV_NAME="$2"; ENV_PREFIX=""; shift 2;;
    --conda-base)   CONDA_BASE="$2"; shift 2;;
    --skip-env)     DO_ENV=0; shift;;
    --skip-frontend) DO_FRONTEND=0; shift;;
    --dry-run)      DRY_RUN=1; shift;;
    -h|--help)      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown arg: $1";;
  esac
done

log "MLST GUI install"
echo "  repo:   ${REPO_DIR}"
echo "  conda:  ${CONDA_BASE}"
if [[ -n "$ENV_NAME" ]]; then echo "  env:    name=${ENV_NAME}"; else echo "  env:    prefix=${ENV_PREFIX}"; fi

# ---- 1. conda ----
CONDA_SH="${CONDA_BASE}/etc/profile.d/conda.sh"
if [[ ! -f "$CONDA_SH" ]]; then
  # Fall back to whatever conda is on PATH.
  if command -v conda >/dev/null 2>&1; then
    CONDA_SH="$(conda info --base)/etc/profile.d/conda.sh"
  fi
fi
[[ -f "$CONDA_SH" ]] || die "conda not found. Install miniforge into ${CONDA_BASE} or pass --conda-base."
# shellcheck disable=SC1090
source "$CONDA_SH"
ok "conda: $(conda --version 2>/dev/null || echo unknown)"

# Prefer mamba if available (faster solves).
SOLVER="conda"
command -v mamba >/dev/null 2>&1 && SOLVER="mamba"

ENV_ARGS=()
if [[ -n "$ENV_NAME" ]]; then ENV_ARGS=(-n "$ENV_NAME"); else ENV_ARGS=(-p "$ENV_PREFIX"); fi

# ---- 2. conda env ----
if [[ $DO_ENV -eq 1 ]]; then
  log "Creating/updating conda env"
  if conda env list | grep -qE "(^|[[:space:]])${ENV_PREFIX}([[:space:]]|$)" \
     || { [[ -n "$ENV_NAME" ]] && conda env list | grep -qE "^${ENV_NAME}[[:space:]]"; }; then
    warn "env already exists — updating from environment.yml"
    run "$SOLVER" env update "${ENV_ARGS[@]}" -f "${REPO_DIR}/conda_setup/environment.yml"
  else
    run "$SOLVER" env create "${ENV_ARGS[@]}" -f "${REPO_DIR}/conda_setup/environment.yml"
  fi
  ok "env ready"
else
  warn "skipping conda env (--skip-env)"
fi

# Resolve the env's bin dir for verification + pip.
if [[ -n "$ENV_NAME" ]]; then
  ENV_DIR="${CONDA_BASE}/envs/${ENV_NAME}"
else
  ENV_DIR="${ENV_PREFIX}"
fi
ENV_BIN="${ENV_DIR}/bin"

# ---- 3. pip backend reqs (belt-and-suspenders; env.yml already has them) ----
if [[ -x "${ENV_BIN}/pip" ]]; then
  log "Installing backend requirements"
  run "${ENV_BIN}/pip" install -r "${REPO_DIR}/backend/requirements.txt"
  ok "backend requirements installed"
else
  warn "no pip at ${ENV_BIN}/pip — skipping backend pip step"
fi

# ---- 4. verify mlst + bundled PubMLST DB ----
log "Verifying mlst"
if [[ -x "${ENV_BIN}/mlst" ]]; then
  MLST="${ENV_BIN}/mlst"
elif command -v mlst >/dev/null 2>&1; then
  MLST="$(command -v mlst)"
else
  MLST=""
fi
if [[ -n "$MLST" && $DRY_RUN -eq 0 ]]; then
  # mlst is a Perl script with `#!/usr/bin/env perl`; it must run with the env's
  # bin on PATH so it uses the env Perl (which carries List::MoreUtils etc.),
  # not the system Perl. The OOD session does this too (PATH=$ENV/bin:$PATH).
  export PATH="${ENV_BIN}:${PATH}"
  "$MLST" --version || warn "mlst --version failed"
  scheme_count="$("$MLST" --list 2>/dev/null | wc -w | tr -d ' ')"
  if [[ "${scheme_count:-0}" -gt 0 ]]; then
    ok "PubMLST DB present (${scheme_count} schemes)"
  else
    warn "mlst --list returned no schemes — the bundled PubMLST DB may be missing. See INSTALL.md."
  fi
else
  warn "mlst not found in env — install it (conda install -c bioconda mlst) or check environment.yml."
fi

# ---- 5. frontend ----
if [[ $DO_FRONTEND -eq 1 ]]; then
  log "Building frontend"
  ( cd "${REPO_DIR}/frontend" \
      && { run npm ci || run npm install; } \
      && run npm run build )
  ok "frontend built -> frontend/dist/"
else
  warn "skipping frontend build (--skip-frontend)"
fi

log "Done."
echo "  Start (outside OOD):  cd ${REPO_DIR}/backend && ${ENV_BIN}/python -m uvicorn app.main:app --port 8000"
echo "  OOD:                  see deploy/INSTALL.md to register the app card."
