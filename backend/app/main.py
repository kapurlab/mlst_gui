"""
MLST GUI — FastAPI backend.

Serves the React SPA from frontend/dist/ and provides:
  /api/projects        — list shared + personal projects (FASTQ browser)
  /api/projects/{n}/samples — list FASTQ pairs in project/download/
  /api/config          — get/set user config (mlst db path, threads)
  /api/schemes         — list PubMLST schemes (mlst --longlist), cached
  /api/run             — start an mlst_pipeline.py run
  /api/jobs            — list running/completed jobs
  /api/jobs/{id}       — job detail
  /api/jobs/{id}/log   — SSE stream of the job log
  /api/projects/{n}/samples/{s}/mlst-results — per-sample files on disk
  /api/projects/{n}/samples/{s}/mlst-table   — parsed scheme/ST/alleles/token

This is a sibling of the vSNP and Kraken ID Parse GUIs and shares their project
layout (/srv/kapurlab/projects + ~/projects), adding an mlst/<sample>/ subdir.

All URLs served from / (uvicorn is behind OOD rnode proxy — relative paths only).
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_config, save_config
from .jobs import JobManager
from .sra import (
    SRAExpansionError,
    build_download_script,
    expand_accessions_with_mapping,
    write_crosswalk_tsv,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent          # /srv/kapurlab/tools/mlst_gui
_BIN_DIR = _REPO_ROOT / "bin"
_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"

# Shared project root
_SHARED_PROJECTS = Path(os.environ.get("MLST_SHARED_PROJECTS", "/srv/kapurlab/projects"))

# Jobs log directory (inside repo so it survives across sessions)
_JOBS_DIR = _REPO_ROOT / "backend" / "jobs"

# ---------------------------------------------------------------------------
# App & job manager
# ---------------------------------------------------------------------------
app = FastAPI(title="MLST GUI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

job_manager = JobManager(_JOBS_DIR)


# ---------------------------------------------------------------------------
# Helpers — project listing
# ---------------------------------------------------------------------------
_SCOPE_SHARED = "shared"
_SCOPE_PERSONAL = "personal"


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime if p.is_dir() else 0
    except PermissionError:
        return 0


def _count_project_reads(download_dir: Path, step1_dir: Path) -> int:
    """Count input read files (*.fastq.gz) across download/ and step1/.

    Native projects keep reads in download/; vSNP/Roar-imported projects keep
    them in step1/<sample>/ (and may symlink them into download/). Count the
    union, deduped by resolved path, skipping *_unmapped_* (the unmapped-read
    subset vSNP3 emits — not an input read set)."""
    seen: set = set()
    candidates = []
    if download_dir.is_dir():
        candidates += download_dir.rglob("*.fastq.gz")
    if step1_dir.is_dir():
        candidates += step1_dir.glob("*/*.fastq.gz")
    for f in candidates:
        if "_unmapped_" in f.name:
            continue
        try:
            key = f.resolve()
        except OSError:
            key = f
        seen.add(key)
    return len(seen)


def _list_projects_from_root(root: Path, scope: str) -> List[Dict]:
    if not root.is_dir():
        return []
    projects = []
    try:
        entries = sorted(root.iterdir(), key=_safe_mtime, reverse=True)
    except PermissionError:
        return []
    for p in entries:
        try:
            if not p.is_dir() or p.name.startswith("."):
                continue
        except PermissionError:
            continue
        download_dir = p / "download"
        try:
            fastq_count = _count_project_reads(download_dir, p / "step1")
        except PermissionError:
            fastq_count = -1  # signals "no access" to frontend
        mlst_runs = []
        mlst_dir = p / "mlst"
        try:
            if mlst_dir.is_dir():
                mlst_runs = [d.name for d in sorted(mlst_dir.iterdir()) if d.is_dir()]
        except PermissionError:
            pass
        projects.append({
            "name": p.name,
            "path": str(p),
            "scope": scope,
            "fastq_count": fastq_count,
            "mlst_runs": mlst_runs,
        })
    return projects


def _get_project_dir(name: str) -> Optional[Path]:
    """Find a project dir in shared then personal roots."""
    if "/" in name or name.startswith("."):
        return None
    cfg = load_config()
    for root in [_SHARED_PROJECTS, Path(cfg.get("projects_root", ""))]:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Project creation — same on-disk skeleton vSNP/Kraken GUIs create, so a
# project made in MLST is immediately usable in the siblings (and vice versa).
# We add the mlst/ subdir up front so the sample browser and results endpoints
# have a stable layout.
# ---------------------------------------------------------------------------
_PROJECT_NAME_OK_CHARSET = re.compile(r"^[A-Za-z0-9._-]+$")


def _normalize_project_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("Project name must be a string")
    cleaned = re.sub(r"\s+", "_", name.strip())
    if not cleaned:
        raise ValueError("Project name is empty")
    if cleaned.startswith("."):
        raise ValueError("Project name cannot start with '.'")
    if len(cleaned) > 100:
        raise ValueError("Project name too long (max 100 characters)")
    if not _PROJECT_NAME_OK_CHARSET.match(cleaned):
        bad = sorted(set(ch for ch in cleaned if not re.match(r"[A-Za-z0-9._-]", ch)))
        raise ValueError(
            f"Project name contains unsupported characters: {''.join(bad)!r}. "
            "Only letters, digits, _ - . are allowed (spaces become underscores)."
        )
    return cleaned


def _ensure_project_dirs(project_dir: Path) -> None:
    (project_dir / "download").mkdir(parents=True, exist_ok=True)
    (project_dir / "mlst").mkdir(parents=True, exist_ok=True)
    # vSNP-compatible layout so the project is shared cleanly between tools.
    (project_dir / "step1").mkdir(parents=True, exist_ok=True)
    (project_dir / "step2" / "vcf_source").mkdir(parents=True, exist_ok=True)
    (project_dir / f"{project_dir.name}_VCFs").mkdir(parents=True, exist_ok=True)


def _create_project(name: str, scope: str) -> Path:
    name = _normalize_project_name(name)
    cfg = load_config()
    if scope == _SCOPE_SHARED:
        root = _SHARED_PROJECTS
    else:
        root = Path(cfg.get("projects_root", "") or (Path.home() / "projects"))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Cannot create projects root {root}: {exc}")
    project_dir = root / name
    if project_dir.exists():
        raise ValueError(f"Project already exists: {name}")
    try:
        _ensure_project_dirs(project_dir)
    except PermissionError:
        raise ValueError(
            f"No permission to create a project under {root}. "
            "Shared projects require lab write access; create it as a personal "
            "project instead."
        )
    meta = {"name": name, "created_at": _now_iso(), "status": "created"}
    try:
        with open(project_dir / "project.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, sort_keys=True)
    except OSError:
        pass
    return project_dir


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# Matches _R1/_R2 (with optional _001 etc.) or _1/_2 immediately before .fastq.gz
_READ_TAG_RE = re.compile(r'(?:_R([12])(?:_\d+)?|_([12]))\.fastq\.gz$', re.IGNORECASE)


def _strip_read_tag(filename: str):
    """Return (base, read_num) where read_num is '1', '2', or None."""
    m = _READ_TAG_RE.search(filename)
    if m:
        tag = m.group(1) or m.group(2)
        return filename[:m.start()], tag
    return filename[:-len(".fastq.gz")], None


def _list_fastq_pairs(download_dir: Path) -> List[Dict]:
    """Return samples as {sample, paired, r1, r1_name, r2, r2_name} dicts.

    Handles both Illumina (_R1/_R2) and SRA (_1/_2) naming conventions.
    Files with no read suffix are treated as single-end.
    """
    try:
        all_fq = sorted(download_dir.glob("*.fastq.gz"))
    except PermissionError:
        return []

    groups: Dict[str, Dict] = {}
    for fq in all_fq:
        base, tag = _strip_read_tag(fq.name)
        if base not in groups:
            groups[base] = {"r1": None, "r2": None, "extras": []}
        g = groups[base]
        if tag == "1":
            g["r1"] = fq
        elif tag == "2":
            g["r2"] = fq
        else:
            g["extras"].append(fq)

    pairs = []
    for base, g in groups.items():
        r1, r2 = g["r1"], g["r2"]
        if r1 or r2:
            eff_r1 = r1 or r2
            eff_r2 = r2 if r1 else None
            pairs.append({
                "sample": base,
                "paired": bool(r1 and r2),
                "r1": str(eff_r1), "r1_name": eff_r1.name,
                "r1_size": eff_r1.stat().st_size,
                "r2": str(eff_r2) if eff_r2 else None,
                "r2_name": eff_r2.name if eff_r2 else None,
                "r2_size": eff_r2.stat().st_size if eff_r2 else None,
            })
        for fq in g["extras"]:
            pairs.append({
                "sample": fq.name[:-len(".fastq.gz")],
                "paired": False,
                "r1": str(fq), "r1_name": fq.name,
                "r1_size": fq.stat().st_size,
                "r2": None, "r2_name": None,
                "r2_size": None,
            })

    return pairs


# ---------------------------------------------------------------------------
# API routes — projects / inputs
# ---------------------------------------------------------------------------

@app.get("/api/projects")
def api_list_projects():
    cfg = load_config()
    projects = _list_projects_from_root(_SHARED_PROJECTS, _SCOPE_SHARED)
    personal_root = Path(cfg.get("projects_root", ""))
    if personal_root != _SHARED_PROJECTS:
        personal = _list_projects_from_root(personal_root, _SCOPE_PERSONAL)
        seen = {p["name"] for p in projects}
        projects += [p for p in personal if p["name"] not in seen]
    return JSONResponse(projects)


class ProjectCreate(BaseModel):
    name: str
    scope: Optional[str] = None   # "personal" (default) | "shared"


@app.post("/api/projects")
def api_create_project(payload: ProjectCreate):
    scope = (payload.scope or _SCOPE_PERSONAL).strip() or _SCOPE_PERSONAL
    if scope not in (_SCOPE_PERSONAL, _SCOPE_SHARED):
        raise HTTPException(400, f"Invalid scope: {scope!r}")
    try:
        project_dir = _create_project(payload.name, scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"name": project_dir.name, "path": str(project_dir), "scope": scope})


def _writable_project_dir(name: str) -> Path:
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    (project_dir / "download").mkdir(parents=True, exist_ok=True)
    return project_dir


@app.get("/api/projects/{name}/inputs")
def api_project_inputs(name: str):
    """List files currently in <project>/download/ (name + size + mtime)."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    download_dir = project_dir / "download"
    files: List[Dict] = []
    total = 0
    if download_dir.is_dir():
        for p in sorted(download_dir.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
            total += st.st_size
    return JSONResponse({"files": files, "total_bytes": total, "count": len(files)})


@app.delete("/api/projects/{name}/inputs/{filename}")
def api_project_input_delete(name: str, filename: str):
    if not filename or "/" in filename or "\\" in filename or filename.startswith(".") or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    target = project_dir / "download" / filename
    if not target.is_file() and not target.is_symlink():
        raise HTTPException(404, f"File not found: {filename}")
    target.unlink()
    return JSONResponse({"deleted": filename})


@app.post("/api/projects/{name}/upload")
async def api_project_upload(name: str, files: List[UploadFile] = File(...)):
    """Save drag-and-dropped / chosen FASTQ (or assembly FASTA) files into download/."""
    project_dir = _writable_project_dir(name)
    download_dir = project_dir / "download"
    saved = 0
    for f in files:
        if not f.filename:
            continue
        target = download_dir / Path(f.filename).name
        async with aiofiles.open(target, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                await out.write(chunk)
        saved += 1
    return JSONResponse({"uploaded": saved})


class LinkLocalRequest(BaseModel):
    path: str


@app.post("/api/projects/{name}/link-local")
def api_project_link_local(name: str, payload: LinkLocalRequest):
    """Symlink every *.fastq.gz (and *.fasta/*.fa) under a server-side directory
    into download/ — lets users import reads or assemblies that already live on
    the shared filesystem without copying gigabytes around."""
    project_dir = _writable_project_dir(name)
    src = Path((payload.path or "").strip()).expanduser()
    if not src.exists():
        raise HTTPException(400, f"Input path not found: {src}")
    download_dir = project_dir / "download"
    _link_exts = (".fastq.gz", ".fasta", ".fa", ".fna", ".fasta.gz", ".fa.gz")
    if src.is_file():
        candidates = [src]
    else:
        candidates = sorted(
            f for f in src.iterdir()
            if f.is_file() and any(f.name.lower().endswith(e) for e in _link_exts)
        )
    count = 0
    for f in candidates:
        if not any(f.name.lower().endswith(e) for e in _link_exts):
            continue
        target = download_dir / f.name
        if not target.exists():
            target.symlink_to(f.resolve())
            count += 1
    return JSONResponse({"linked": count})


class SraRequest(BaseModel):
    accessions: List[str]
    folder: Optional[str] = None


@app.post("/api/projects/{name}/sra/download")
def api_project_sra_download(name: str, payload: SraRequest):
    """Resolve SRA accessions and kick off a background download into download/."""
    project_dir = _writable_project_dir(name)
    try:
        expanded, mapping = expand_accessions_with_mapping(payload.accessions, strict=True)
    except SRAExpansionError as e:
        raise HTTPException(
            502,
            f"Could not resolve SRA accessions via NCBI eutils: {e}. "
            "This is usually NCBI rate-limiting; wait ~30 s and retry.",
        )
    download_root = project_dir / "download"
    if payload.folder:
        download_root = download_root / Path(payload.folder).name
    download_root.mkdir(parents=True, exist_ok=True)
    try:
        write_crosswalk_tsv(download_root, mapping)
    except OSError as e:
        logger.warning("Failed to write sra_crosswalk.tsv: %s", e)
    script = build_download_script(download_root, expanded, allow_insecure_https=False)
    script_path = download_root / "download_sra.sh"
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)
    env = {"PATH": os.environ.get("PATH", "")}
    job_id = job_manager.start_job(
        name=f"sra_download — {name}",
        command=["bash", str(script_path)],
        cwd=download_root,
        env=env,
    )
    return JSONResponse({"job_id": job_id})


@app.get("/api/projects/{name}/sra-crosswalk")
def api_project_sra_crosswalk(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    crosswalk = project_dir / "download" / "sra_crosswalk.tsv"
    if not crosswalk.is_file():
        raise HTTPException(404, "No SRA crosswalk for this project")
    return FileResponse(crosswalk, media_type="text/plain")


@app.get("/api/projects/{name}/samples")
def api_project_samples(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    download_dir = project_dir / "download"
    if not download_dir.is_dir():
        return JSONResponse([])
    return JSONResponse(_list_fastq_pairs(download_dir))


# ---------------------------------------------------------------------------
# Per-sample MLST results (decoupled from a single job).
#
# Results are read straight from <project>/mlst/<sample>/ on disk so any
# previously-run sample's outputs can be revisited — not just the last job.
# ---------------------------------------------------------------------------
def _sample_run_status(run_dir: Path) -> str:
    """'running' if a live job owns the dir, else 'done' if it holds output, else 'none'."""
    run_dir_str = str(run_dir)
    for job in job_manager.list_jobs():
        if job.get("cwd") == run_dir_str and job.get("status") == "running":
            return "running"
    try:
        if (run_dir / "mlst_result.json").is_file():
            return "done"
        if run_dir.is_dir() and any(p.is_file() for p in run_dir.rglob("*")):
            return "done"
    except PermissionError:
        pass
    return "none"


def _collect_mlst_files(run_dir: Path, include_all: bool) -> List[Dict]:
    """List result files under an mlst run dir, categorized + sorted."""
    files: List[Dict] = []
    if not run_dir.is_dir():
        return files
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file() or p.name.endswith(".log"):
            continue
        # skip large assembly intermediates unless include_all
        rel = str(p.relative_to(run_dir))
        category = _mlst_category(rel)
        if not include_all and category is None:
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        files.append({
            "name": rel,
            "path": str(p),
            "label": _mlst_label(rel, category),
            "size": stat.st_size,
            "openable": _can_open_inline(rel),
            "category": category,
        })

    def sort_key(f):
        category = f.get("category")
        if category in _CATEGORY_ORDER:
            return (_CATEGORY_ORDER[category], f["name"])
        return (50, f["name"])

    files.sort(key=sort_key)
    for f in files:
        if include_all and f.get("category") is None:
            f["label"] = f["name"]
    return files


@app.get("/api/projects/{name}/samples/{sample}/mlst-results")
def api_sample_mlst_results(name: str, sample: str, all: int = Query(0)):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    run_dir = project_dir / "mlst" / sample
    return JSONResponse({
        "project": name,
        "sample": sample,
        "present": run_dir.is_dir(),
        "status": _sample_run_status(run_dir),
        "run_dir": str(run_dir),
        "files": _collect_mlst_files(run_dir, bool(all)),
    })


@app.get("/api/projects/{name}/samples/{sample}/mlst-table")
def api_sample_mlst_table(name: str, sample: str):
    """Parsed mlst_result.json for the results display."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    result_path = project_dir / "mlst" / sample / "mlst_result.json"
    if not result_path.is_file():
        return JSONResponse({"present": False})
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Could not read mlst_result.json: {exc}")
    return JSONResponse({
        "present": True,
        "scheme": data.get("scheme", ""),
        "st": data.get("st", ""),
        "alleles": data.get("alleles", {}),
        "novel": data.get("novel", False),
        "partial": data.get("partial", False),
        "organism_token": data.get("organism_token", ""),
        "species_guess": data.get("species_guess", ""),
        "provenance": data.get("provenance", {}),
    })


# ---------------------------------------------------------------------------
# Cross-tool visibility — surface vSNP results for a sample (read-only).
# ---------------------------------------------------------------------------
def _resolve_vsnp_sample_dir(step1_dir: Path, sample: str) -> Optional[Path]:
    exact = step1_dir / sample
    if exact.is_dir():
        return exact
    try:
        candidates = sorted(
            d for d in step1_dir.iterdir()
            if d.is_dir() and d.name.startswith(f"{sample}_")
        )
    except (OSError, PermissionError):
        return None
    return candidates[0] if candidates else None


def _sample_in_step2_run(run_dir: Path, sample: str) -> bool:
    try:
        fastas = list(run_dir.rglob("*.fasta"))
    except (OSError, PermissionError):
        return False
    for fa in fastas:
        try:
            with fa.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if not line.startswith(">"):
                        continue
                    leaf = line[1:].strip()
                    for suf in ("_zc.vcf.gz", "_zc.vcf"):
                        if leaf.endswith(suf):
                            leaf = leaf[: -len(suf)]
                            break
                    if leaf == sample or leaf.startswith(f"{sample}_"):
                        return True
        except (OSError, PermissionError):
            continue
    return False


def _find_latest_step2_for_sample(project_dir: Path, sample: str) -> Dict:
    step2_dir = project_dir / "step2"
    if not step2_dir.is_dir():
        return {"present": False}
    runs_dir = step2_dir / "runs"
    candidates: List = []
    if runs_dir.is_dir():
        try:
            candidates = [(d, d.name) for d in sorted(runs_dir.iterdir(), reverse=True) if d.is_dir()]
        except (OSError, PermissionError):
            candidates = []
    else:
        candidates = [(step2_dir, "legacy")]
    for run_dir, run_id in candidates:
        if not _sample_in_step2_run(run_dir, sample):
            continue
        html = sorted(run_dir.glob("*.html"), key=_safe_mtime)
        report = html[-1] if html else None
        started_at = None
        meta = run_dir / "run_metadata.json"
        if meta.is_file():
            try:
                started_at = json.loads(meta.read_text(encoding="utf-8")).get("started_at")
            except (json.JSONDecodeError, OSError):
                pass
        try:
            groups = [
                g.name for g in sorted(run_dir.iterdir())
                if g.is_dir() and g.name not in ("vcf_source", "runs", "_provenance")
                and not g.name.startswith(".")
            ]
        except (OSError, PermissionError):
            groups = []
        return {
            "present": True,
            "run_id": run_id,
            "started_at": started_at,
            "report_name": report.name if report else None,
            "report_path": str(report) if report else None,
            "groups": groups,
        }
    return {"present": False}


@app.get("/api/projects/{name}/vsnp/samples/{sample}/files")
def api_vsnp_sample_files(name: str, sample: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    step1_dir = project_dir / "step1"
    sample_dir = _resolve_vsnp_sample_dir(step1_dir, sample) if step1_dir.is_dir() else None
    files: List[Dict] = []
    sample_dir_str = ""
    if sample_dir:
        base = sample_dir.resolve()
        sample_dir_str = str(base)
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.name.startswith(".~lock"):
                continue
            try:
                rel = p.relative_to(base).as_posix()
                st = p.stat()
            except (OSError, ValueError):
                continue
            files.append({
                "name": p.name,
                "relpath": rel,
                "path": str(p),
                "size": st.st_size,
                "openable": _can_open_inline(p.name),
                "type": p.suffix.lstrip(".").lower() or "file",
            })
    return JSONResponse({
        "project": name,
        "sample": sample,
        "step1_present": bool(sample_dir),
        "step1_dir": sample_dir_str,
        "files": files,
        "step2": _find_latest_step2_for_sample(project_dir, sample),
    })


@app.get("/api/projects/{name}/file")
def api_project_file(name: str, path: str = Query(...), inline: int = 0):
    """Serve a file from anywhere inside a project dir (cross-tool downloads)."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    root = project_dir.resolve()
    target = Path(path).resolve()
    if root != target and root not in target.parents:
        raise HTTPException(403, "Path outside project directory")
    if not target.is_file():
        raise HTTPException(404, f"File not found: {path}")
    media_type = _media_type_for(target.name)
    want_inline = bool(inline) and _can_open_inline(target.name)
    disposition = "inline" if want_inline else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{target.name}"'}
    return FileResponse(target, media_type=media_type, headers=headers)


# ---------------------------------------------------------------------------
# Schemes — list PubMLST schemes (mlst --longlist), cached.
# ---------------------------------------------------------------------------
_schemes_cache: Dict[str, Any] = {"at": 0.0, "schemes": None}
_SCHEMES_TTL = 3600.0  # seconds


def _parse_longlist(text: str) -> List[Dict[str, Any]]:
    """Parse `mlst --longlist` output.

    Each line is: <scheme> <locus1> <locus2> ...  (whitespace/tab separated).
    Returns [{scheme, loci:[...]}], sorted by scheme name.
    """
    schemes: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = re.split(r"\s+", line)
        scheme = parts[0]
        loci = parts[1:]
        schemes.append({"scheme": scheme, "loci": loci})
    schemes.sort(key=lambda s: s["scheme"].lower())
    return schemes


@app.get("/api/schemes")
def api_schemes(refresh: int = Query(0)):
    """List PubMLST schemes via `mlst --longlist`, cached for an hour."""
    now = time.time()
    if not refresh and _schemes_cache["schemes"] is not None and (now - _schemes_cache["at"]) < _SCHEMES_TTL:
        return JSONResponse({"schemes": _schemes_cache["schemes"], "cached": True})
    import subprocess
    try:
        out = subprocess.run(["mlst", "--longlist"], capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return JSONResponse({"schemes": [], "error": "mlst not found on PATH"}, status_code=200)
    except subprocess.SubprocessError as exc:
        return JSONResponse({"schemes": [], "error": str(exc)}, status_code=200)
    schemes = _parse_longlist(out.stdout or "")
    _schemes_cache["schemes"] = schemes
    _schemes_cache["at"] = now
    return JSONResponse({"schemes": schemes, "cached": False})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@app.get("/api/config")
def api_get_config():
    return JSONResponse(load_config())


class ConfigPayload(BaseModel):
    mlst_db: Optional[str] = None
    threads: Optional[int] = None
    projects_root: Optional[str] = None
    shared_projects_root: Optional[str] = None


@app.post("/api/config")
def api_save_config(payload: ConfigPayload):
    cfg = load_config()
    updates = payload.model_dump(exclude_none=True)
    cfg.update(updates)
    new_root = (updates.get("projects_root") or "").strip()
    if new_root:
        recent = [r for r in cfg.get("recent_projects_roots", []) if r != new_root]
        recent.insert(0, new_root)
        cfg["recent_projects_roots"] = recent[:10]
    save_config(cfg)
    return JSONResponse({"ok": True})


@app.get("/api/browse-dirs")
def api_browse_dirs(path: str = ""):
    """List sub-directories of `path` for the project-root folder picker."""
    try:
        p = (Path(path).expanduser() if path.strip() else Path.home()).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, "Invalid path")
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {p}")
    entries: List[Dict[str, str]] = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, f"Permission denied: {p}")
    parent = str(p.parent) if p.parent != p else None
    return JSONResponse({"path": str(p), "parent": parent, "entries": entries})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
class RunPayload(BaseModel):
    project: str
    r1: Optional[str] = None        # absolute path to R1 FASTQ (assemble first)
    r2: Optional[str] = None
    assembly: Optional[str] = None  # absolute path to an assembly FASTA (skip assembly)
    scheme: Optional[str] = None    # force a PubMLST scheme (skip autodetect)
    threads: Optional[int] = None


@app.post("/api/run")
def api_run(payload: RunPayload):
    cfg = load_config()
    threads = payload.threads or int(cfg.get("threads", 8) or 8)
    mlst_db = (cfg.get("mlst_db") or "").strip()

    if not payload.assembly and not payload.r1:
        raise HTTPException(400, "Provide an assembly FASTA or R1 reads.")

    project_dir = _get_project_dir(payload.project)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {payload.project}")

    # Derive sample name from the input.
    if payload.assembly:
        asm = Path(payload.assembly)
        if not asm.exists():
            raise HTTPException(400, f"Assembly file not found: {payload.assembly}")
        sample_name = re.sub(
            r"\.(fa|fasta|fna|gbk|gbff|gb|embl)(\.gz)?$", "", asm.name, flags=re.IGNORECASE
        )
    else:
        r1 = Path(payload.r1)
        if not r1.exists():
            raise HTTPException(400, f"R1 file not found: {payload.r1}")
        sample_name, _ = _strip_read_tag(r1.name)

    run_dir = project_dir / "mlst" / sample_name

    # Refuse a second run in the same output dir (avoids racing on temp/output).
    for existing in job_manager.list_jobs():
        if existing.get("status") == "running" and existing.get("cwd") == str(run_dir):
            raise HTTPException(
                409,
                f"A run is already in progress for {sample_name} "
                f"(job {existing['id'][:8]}). Wait for it to finish before re-running.",
            )

    run_dir.mkdir(parents=True, exist_ok=True)

    script = _BIN_DIR / "mlst_pipeline.py"
    command = [sys.executable, "-u", str(script), "--outdir", str(run_dir),
               "--label", sample_name, "--threads", str(threads)]
    if payload.assembly:
        command.extend(["--assembly", str(Path(payload.assembly))])
    else:
        command.extend(["--r1", str(Path(payload.r1))])
        if payload.r2:
            r2 = Path(payload.r2)
            if not r2.exists():
                raise HTTPException(400, f"R2 file not found: {payload.r2}")
            command.extend(["--r2", str(r2)])
    if payload.scheme:
        command.extend(["--scheme", payload.scheme])
    if mlst_db:
        command.extend(["--mlst-db", mlst_db])

    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "MLST_THREADS": str(threads),
    }
    if mlst_db:
        env["MLST_DB"] = mlst_db

    src_label = "assembly" if payload.assembly else "reads"
    job_name = f"{payload.project}/{sample_name} — mlst ({src_label})"
    job_id = job_manager.start_job(name=job_name, command=command, cwd=run_dir, env=env)
    return JSONResponse({"job_id": job_id, "run_dir": str(run_dir)})


@app.get("/api/jobs")
def api_list_jobs():
    return JSONResponse(job_manager.list_jobs())


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@app.get("/api/jobs/{job_id}/log")
async def api_job_log(job_id: str, request: Request):
    """SSE stream of the job's log file. Tails from beginning, closes when job finishes."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    log_path = Path(job["log_path"])
    _ansi_re = re.compile(r'\x1b\[[0-9;]*[mGKHFABCDJsur]')

    async def event_stream():
        position = 0
        while True:
            if await request.is_disconnected():
                break
            current_job = job_manager.get_job(job_id)
            if log_path.exists():
                async with aiofiles.open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    await f.seek(position)
                    chunk = await f.read(4096)
                    if chunk:
                        lines = chunk.splitlines(keepends=True)
                        for line in lines:
                            clean = _ansi_re.sub("", line.rstrip())
                            if clean:
                                yield f"data: {clean}\n\n"
                        position += len(chunk.encode("utf-8"))
            if current_job and current_job["status"] in ("succeeded", "failed"):
                yield "data: [DONE]\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Result file categorization
# ---------------------------------------------------------------------------
_INLINE_MEDIA = {
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".json": "application/json",
    ".tsv": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".csv": "text/plain",
}
_DOWNLOAD_MEDIA = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".fasta": "text/plain",
    ".fa": "text/plain",
    ".fna": "text/plain",
    ".gz": "application/gzip",
}


def _can_open_inline(name: str) -> bool:
    return Path(name).suffix.lower() in _INLINE_MEDIA


def _media_type_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    return _INLINE_MEDIA.get(ext) or _DOWNLOAD_MEDIA.get(ext) or "application/octet-stream"


def _mlst_category(rel: str) -> Optional[str]:
    """Primary-results category for a relative mlst run output path."""
    path = Path(rel)
    name = path.name
    parts = path.parts
    if any(part.startswith(".") for part in parts):
        return None
    # Hide assembler intermediate dirs (shovill/, spades/).
    if parts and parts[0] in ("shovill", "spades"):
        return None
    if name == "mlst_result.json":
        return "result_json"
    if name == "mlst.tsv":
        return "mlst_tsv"
    if name == "mlst.json":
        return "mlst_json"
    if name == "assembly.fasta":
        return "assembly"
    return None


_CATEGORY_ORDER = {
    "result_json": 0,
    "mlst_tsv": 1,
    "mlst_json": 2,
    "assembly": 3,
    "log": 99,
}


def _mlst_label(rel: str, category: Optional[str]) -> str:
    return {
        "result_json": "MLST result (normalized JSON)",
        "mlst_tsv": "mlst TSV",
        "mlst_json": "mlst JSON",
        "assembly": "Assembly FASTA",
        "log": "Pipeline log",
    }.get(category or "", rel)


@app.get("/api/jobs/{job_id}/results")
def api_job_results(job_id: str, all: int = Query(0)):
    """List output files in the job's run directory, plus the pipeline log."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    files = []
    cwd = job.get("cwd")
    if cwd and Path(cwd).is_dir():
        run_dir = Path(cwd)
        for p in sorted(run_dir.rglob("*")):
            if p.is_file() and not p.name.endswith(".log"):
                rel = str(p.relative_to(run_dir))
                category = _mlst_category(rel)
                if not all and category is None:
                    continue
                files.append({
                    "name": rel,
                    "label": _mlst_label(rel, category),
                    "size": p.stat().st_size,
                    "openable": _can_open_inline(rel),
                    "category": category,
                })

    log_path = Path(job.get("log_path", ""))
    if log_path.is_file():
        files.append({
            "name": "pipeline_log.txt",
            "label": "Pipeline log",
            "size": log_path.stat().st_size,
            "openable": True,
            "category": "log",
            "is_log": True,
        })

    def sort_key(f):
        if f.get("is_log"):
            return (_CATEGORY_ORDER["log"], f["name"])
        category = f.get("category")
        if category in _CATEGORY_ORDER:
            return (_CATEGORY_ORDER[category], f["name"])
        return (50, f["name"])

    files.sort(key=sort_key)
    for file in files:
        if all and file.get("category") is None:
            file["label"] = file["name"]
    return JSONResponse(files)


@app.get("/api/jobs/{job_id}/file")
def api_job_file(job_id: str, path: str = Query(...), inline: int = 0):
    """Serve a single result file. `inline=1` renders in the browser."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    if path == "pipeline_log.txt":
        target = Path(job.get("log_path", ""))
        display_name = f"{job_id[:8]}_pipeline_log.txt"
    else:
        cwd = job.get("cwd")
        if not cwd:
            raise HTTPException(404, "No run directory for job")
        run_dir = Path(cwd).resolve()
        target = (run_dir / path).resolve()
        if run_dir != target and run_dir not in target.parents:
            raise HTTPException(403, "Path outside run directory")
        display_name = target.name

    if not target.is_file():
        raise HTTPException(404, f"File not found: {path}")

    media_type = _media_type_for(target.name)
    want_inline = bool(inline) and _can_open_inline(target.name)
    disposition = "inline" if want_inline else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{display_name}"'}
    return FileResponse(target, media_type=media_type, headers=headers)


# ---------------------------------------------------------------------------
# Static frontend — must be last (catches everything not matched above)
# ---------------------------------------------------------------------------
if _FRONTEND_DIST.is_dir():
    _INDEX_HTML = _FRONTEND_DIST / "index.html"

    @app.get("/")
    def index():
        return FileResponse(
            _INDEX_HTML,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="static")
else:
    @app.get("/")
    def root():
        return JSONResponse(
            {"error": "Frontend not built. Run: cd frontend && npm run build"},
            status_code=503,
        )
