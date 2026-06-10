#!/usr/bin/env python3
"""mlst_pipeline.py — assemble-if-reads, then run Torsten Seemann's `mlst`.

This is the orchestrator the MLST GUI shells out to, and the *stable CLI*
amr_plus_gui calls to corroborate a sample's organism call:

    python bin/mlst_pipeline.py --assembly X.fasta --outdir DIR [--label NAME]

It always writes ``DIR/mlst_result.json`` with at least ``scheme``, ``st`` and
``organism_token`` (the AMRFinderPlus ``--organism`` token mapped from the
detected PubMLST scheme via config/scheme_organism_map.yaml).

Workflow
--------
1. Inputs: a sample's reads (``--r1`` [``--r2``]) OR a provided assembly
   (``--assembly``). If reads are given, assemble first with ``shovill``
   (fallback ``spades.py --isolate``) into ``DIR/assembly.fasta``. If an
   assembly FASTA/GBK/EMBL is given, use it directly (skip assembly).
2. Run ``mlst --quiet --json mlst.json --label <label> assembly`` and also
   capture the default TSV to ``mlst.tsv``. ``--scheme <s>`` is added only when
   the user forces a scheme.
3. Map the detected scheme -> AMRFinderPlus organism token.
4. Write ``mlst_result.json`` with the parsed result + provenance.

`mlst` autodetects the best PubMLST scheme; no species input is needed. FASTA /
GenBank / EMBL inputs are accepted (optionally gzipped).
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_SCHEME_MAP_PATH = _REPO_ROOT / "config" / "scheme_organism_map.yaml"

# Assembly file extensions `mlst` (via any2fasta) accepts directly.
_ASSEMBLY_EXTS = (
    ".fasta", ".fa", ".fna", ".fasta.gz", ".fa.gz", ".fna.gz",
    ".gbk", ".gbff", ".gb", ".genbank", ".gbk.gz",
    ".embl", ".embl.gz",
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, **kw)


# ---------------------------------------------------------------------------
# scheme -> organism token map (dependency-free YAML reader)
# ---------------------------------------------------------------------------
def load_scheme_map(path: Path = _SCHEME_MAP_PATH) -> Dict[str, str]:
    """Read config/scheme_organism_map.yaml.

    Flat ``key: value`` mapping. Parsed without PyYAML so the stable CLI works
    in a minimal env. Comments (``#``) and blank lines are ignored.
    """
    mapping: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return mapping
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().strip("\"'")
        # strip an inline comment from the value
        val = val.split(" #", 1)[0].strip().strip("\"'")
        if key:
            mapping[key.lower()] = val
    return mapping


def scheme_to_organism(scheme: Optional[str], mapping: Dict[str, str]) -> str:
    """Map a PubMLST scheme name to an AMRFinderPlus --organism token.

    Tries the exact (lowercased) key first, then strips a trailing ``_<n>``
    numeric variant suffix (``abaumannii_2`` -> ``abaumannii``). Returns "" if
    no mapping is found (amr_plus_gui then runs AMRFinderPlus organism-agnostic).
    """
    if not scheme:
        return ""
    key = scheme.strip().lower()
    if key in mapping:
        return mapping[key]
    stripped = re.sub(r"_\d+$", "", key)
    return mapping.get(stripped, "")


def organism_to_species_guess(token: str) -> str:
    """Human-readable species label from an AMRFinderPlus organism token."""
    if not token:
        return ""
    return token.replace("_", " ")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def _looks_like_assembly(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(ext) for ext in _ASSEMBLY_EXTS)


def assemble_reads(r1: Path, r2: Optional[Path], outdir: Path,
                   threads: int) -> Tuple[Path, str]:
    """Assemble reads into outdir/assembly.fasta.

    Prefers ``shovill`` (SPAdes wrapper tuned for isolates); falls back to
    ``spades.py --isolate`` if shovill is unavailable or fails. Returns
    (assembly_path, assembler_label).
    """
    target = outdir / "assembly.fasta"

    if shutil.which("shovill"):
        shov_out = outdir / "shovill"
        # shovill refuses to write into a non-empty dir; clear a prior attempt.
        if shov_out.exists():
            shutil.rmtree(shov_out, ignore_errors=True)
        cmd = [
            "shovill", "--outdir", str(shov_out),
            "--R1", str(r1), "--cpus", str(threads), "--force",
        ]
        if r2:
            cmd.extend(["--R2", str(r2)])
        else:
            # shovill is paired-end only; single-end falls through to SPAdes.
            log("shovill requires paired reads; falling back to SPAdes for single-end input.")
            cmd = []
        if cmd:
            proc = _run(cmd)
            contigs = shov_out / "contigs.fa"
            if proc.returncode == 0 and contigs.is_file():
                shutil.copyfile(contigs, target)
                return target, _assembler_label("shovill")
            log("shovill failed; falling back to spades.py --isolate.")

    if shutil.which("spades.py"):
        spades_out = outdir / "spades"
        if spades_out.exists():
            shutil.rmtree(spades_out, ignore_errors=True)
        cmd = ["spades.py", "--isolate", "-o", str(spades_out), "-t", str(threads)]
        if r2:
            cmd.extend(["-1", str(r1), "-2", str(r2)])
        else:
            cmd.extend(["-s", str(r1)])
        proc = _run(cmd)
        contigs = spades_out / "contigs.fasta"
        if proc.returncode == 0 and contigs.is_file():
            shutil.copyfile(contigs, target)
            return target, _assembler_label("spades")
        raise RuntimeError("SPAdes assembly failed (see log above).")

    raise RuntimeError(
        "No assembler found on PATH. Install shovill (preferred) or spades, "
        "or pass an existing assembly with --assembly."
    )


def _assembler_label(tool: str) -> str:
    ver = ""
    try:
        flag = "--version" if tool != "shovill" else "--version"
        out = subprocess.run([tool if tool != "spades" else "spades.py", flag],
                             capture_output=True, text=True, timeout=30)
        ver = (out.stdout or out.stderr or "").strip().splitlines()[0] if (out.stdout or out.stderr) else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        ver = ""
    return f"{tool} ({ver})" if ver else tool


# ---------------------------------------------------------------------------
# mlst
# ---------------------------------------------------------------------------
def mlst_version() -> str:
    try:
        out = subprocess.run(["mlst", "--version"], capture_output=True,
                             text=True, timeout=30)
        return (out.stdout or out.stderr or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def mlst_db_snapshot(limit: int = 0) -> List[str]:
    """List the available schemes (PubMLST DB snapshot) via `mlst --list`."""
    try:
        out = subprocess.run(["mlst", "--list"], capture_output=True,
                             text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    schemes = (out.stdout or "").split()
    return schemes[:limit] if limit else schemes


def run_mlst(assembly: Path, outdir: Path, label: str,
             scheme: Optional[str], mlst_db: Optional[str]) -> Tuple[Path, Path, dict]:
    """Run mlst, capturing both --json and the default TSV.

    Returns (tsv_path, json_path, parsed_json_obj).
    """
    json_path = outdir / "mlst.json"
    tsv_path = outdir / "mlst.tsv"

    cmd = ["mlst", "--quiet", "--json", str(json_path), "--label", label]
    if scheme:
        cmd.extend(["--scheme", scheme])
    if mlst_db:
        # `mlst` honours --blastdb / --datadir for a relocated PubMLST DB.
        blastdb = Path(mlst_db) / "blast" / "mlst.fa"
        datadir = Path(mlst_db) / "pubmlst"
        if blastdb.is_file():
            cmd.extend(["--blastdb", str(blastdb)])
        if datadir.is_dir():
            cmd.extend(["--datadir", str(datadir)])
    cmd.append(str(assembly))

    with open(tsv_path, "w", encoding="utf-8") as tsv_out:
        proc = _run(cmd, stdout=tsv_out, stderr=subprocess.PIPE, text=True)
    if proc.stderr:
        log(proc.stderr.rstrip())
    if proc.returncode != 0:
        raise RuntimeError(f"mlst exited with status {proc.returncode}")

    parsed: dict = {}
    if json_path.is_file():
        try:
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = {}
    return tsv_path, json_path, parsed


# ---------------------------------------------------------------------------
# Parse mlst output into our normalized result.
# ---------------------------------------------------------------------------
def _parse_tsv_row(tsv_path: Path) -> Tuple[str, str, Dict[str, str]]:
    """Parse the default mlst TSV row: FILE  SCHEME  ST  allele1(n) ...

    Returns (scheme, st, {locus: allele}). Allele cells look like ``adk(12)``,
    novel ``adk(~12)``, partial ``adk(12?)`` or missing ``adk(-)``.
    """
    try:
        text = tsv_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "", "", {}
    if not text:
        return "", "", {}
    row = text.splitlines()[0].split("\t")
    if len(row) < 3:
        return "", "", {}
    scheme = row[1].strip()
    st = row[2].strip()
    alleles: Dict[str, str] = {}
    cell_re = re.compile(r"^([^()]+)\(([^()]*)\)$")
    for cell in row[3:]:
        cell = cell.strip()
        if not cell:
            continue
        m = cell_re.match(cell)
        if m:
            alleles[m.group(1)] = m.group(2)
        else:
            alleles[cell] = ""
    return scheme, st, alleles


def normalize_result(parsed_json: dict, tsv_path: Path) -> Tuple[str, str, Dict[str, str], bool, bool]:
    """Combine --json output and the TSV row into (scheme, st, alleles, novel, partial).

    mlst's JSON is a list with one object per input file:
      {filename, scheme, sequence_type, alleles: {locus: "12"|"~12"|"12?"|"-"}}
    Field names vary slightly between mlst versions, so fall back to the TSV.
    """
    scheme = st = ""
    alleles: Dict[str, str] = {}

    obj = None
    if isinstance(parsed_json, list) and parsed_json:
        obj = parsed_json[0]
    elif isinstance(parsed_json, dict):
        obj = parsed_json
    if isinstance(obj, dict):
        scheme = str(obj.get("scheme") or obj.get("Scheme") or "")
        st = str(obj.get("sequence_type") or obj.get("ST") or obj.get("st") or "")
        raw_alleles = obj.get("alleles") or {}
        if isinstance(raw_alleles, dict):
            alleles = {str(k): str(v) for k, v in raw_alleles.items()}

    # Fall back to / supplement from the TSV (always present).
    tsv_scheme, tsv_st, tsv_alleles = _parse_tsv_row(tsv_path)
    if not scheme:
        scheme = tsv_scheme
    if not st:
        st = tsv_st
    if not alleles:
        alleles = tsv_alleles

    # Novel alleles are flagged "~", partial/uncertain "?", missing "-".
    blob = " ".join(f"{v}" for v in alleles.values())
    novel = "~" in blob or st == "~" or st.startswith("~")
    partial = "?" in blob or "-" in [v.strip() for v in alleles.values()]

    if scheme == "-":
        scheme = ""
    if st == "-":
        st = ""
    return scheme, st, alleles, novel, partial


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_pipeline(outdir: Path, label: str, threads: int,
                 assembly: Optional[Path] = None,
                 r1: Optional[Path] = None, r2: Optional[Path] = None,
                 scheme: Optional[str] = None,
                 mlst_db: Optional[str] = None) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)

    assembler = "provided"
    if assembly is not None:
        if not assembly.is_file():
            raise RuntimeError(f"Assembly not found: {assembly}")
        if not _looks_like_assembly(assembly):
            log(f"WARNING: {assembly.name} does not look like a FASTA/GenBank/EMBL; passing to mlst anyway.")
        asm_path = assembly
        log(f"Using provided assembly: {asm_path}")
    else:
        if r1 is None:
            raise RuntimeError("Provide --assembly OR --r1 [--r2].")
        log(f"No assembly provided — assembling reads (threads={threads})…")
        asm_path, assembler = assemble_reads(r1, r2, outdir, threads)
        log(f"Assembly complete: {asm_path} (assembler: {assembler})")

    log("Running mlst…")
    tsv_path, json_path, parsed = run_mlst(asm_path, outdir, label, scheme, mlst_db)
    det_scheme, st, alleles, novel, partial = normalize_result(parsed, tsv_path)

    mapping = load_scheme_map()
    organism_token = scheme_to_organism(det_scheme, mapping)
    species_guess = organism_to_species_guess(organism_token)

    result = {
        "label": label,
        "scheme": det_scheme,
        "st": st,
        "alleles": alleles,
        "novel": novel,
        "partial": partial,
        "organism_token": organism_token,
        "species_guess": species_guess,
        "assembly": str(asm_path),
        "provenance": {
            "mlst_version": mlst_version(),
            "db_schemes": len(mlst_db_snapshot()),
            "assembler": assembler,
            "scheme_forced": bool(scheme),
            "mlst_db": mlst_db or "(bundled)",
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        },
    }

    result_path = outdir / "mlst_result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    log(f"Wrote {result_path}")
    log(f"Scheme: {det_scheme or '(none)'}  ST: {st or '-'}  organism_token: {organism_token or '(unmapped)'}")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Assemble-if-reads then run mlst; write mlst_result.json."
    )
    ap.add_argument("--assembly", help="Assembly FASTA/GenBank/EMBL (skip assembly).")
    ap.add_argument("--r1", help="R1 FASTQ (assemble first).")
    ap.add_argument("--r2", help="R2 FASTQ (paired reads).")
    ap.add_argument("--outdir", required=True, help="Output directory.")
    ap.add_argument("--label", default=None, help="Sample label (default: derived).")
    ap.add_argument("--scheme", default=None, help="Force a PubMLST scheme (skip autodetect).")
    ap.add_argument("--threads", type=int, default=int(os.environ.get("MLST_THREADS", "8") or 8),
                    help="Assembly thread count.")
    ap.add_argument("--mlst-db", default=os.environ.get("MLST_DB", "") or None,
                    help="Relocated PubMLST db root (optional; default uses bundled db).")
    args = ap.parse_args(argv)

    outdir = Path(args.outdir).resolve()
    assembly = Path(args.assembly).resolve() if args.assembly else None
    r1 = Path(args.r1).resolve() if args.r1 else None
    r2 = Path(args.r2).resolve() if args.r2 else None

    # Derive a label if not given.
    label = args.label
    if not label:
        if assembly:
            label = re.sub(r"\.(fa|fasta|fna|gbk|gbff|gb|embl)(\.gz)?$", "", assembly.name, flags=re.I)
        elif r1:
            label = re.sub(r"(_R?1(_\d+)?)?\.f(ast)?q(\.gz)?$", "", r1.name, flags=re.I)
        else:
            label = "sample"

    try:
        run_pipeline(outdir, label, args.threads, assembly=assembly,
                     r1=r1, r2=r2, scheme=args.scheme, mlst_db=args.mlst_db)
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
