# MLST GUI

A web GUI for **MLST** (multi-locus sequence typing) built on Torsten
Seemann's [`mlst`](https://github.com/tseemann/mlst). It autodetects the
best-matching PubMLST scheme for an assembly and reports the Sequence Type (ST)
plus allele numbers — no species input required.

It is a sibling of `vsnp_gui` and `kraken_id_parse_gui`, sharing their look,
feel, project layout, and OOD deployment model. It runs both as a standalone
dashboard and as a service `amr_plus_gui` shells out to for species/ST
corroboration (much like Kraken is used alongside vSNP).

## What it does

1. **Input**: a sample's reads (R1[/R2]) **or** a provided assembly FASTA
   (GenBank / EMBL accepted, optionally gzipped).
2. **Assemble if reads**: `shovill` (preferred) with a `spades.py --isolate`
   fallback → `assembly.fasta`. If an assembly is provided, this is skipped.
3. **Type**: `mlst --quiet --json mlst.json --label <sample> assembly` (also
   captures the default TSV). The detected PubMLST scheme is mapped to an
   AMRFinderPlus `--organism` token via `config/scheme_organism_map.yaml`.
4. **Report**: writes `mlst_result.json` with `scheme`, `st`, `alleles`,
   `novel`/`partial` flags, `organism_token`, `species_guess`, and provenance.

## Layout

```
backend/app/main.py     FastAPI app — projects/inputs/run/jobs/schemes/results
backend/app/config.py   per-user config (~/.config/mlst_gui/config.json) + DEFAULTS
backend/app/jobs.py     background job manager (shared with siblings)
backend/app/sra.py      SRA accession download helper (shared with siblings)
bin/mlst_pipeline.py    orchestrator + stable CLI (assemble → mlst → result json)
config/scheme_organism_map.yaml   PubMLST scheme -> AMRFinderPlus organism token
frontend/src/App.jsx    React SPA (built into frontend/dist/, served by FastAPI)
conda_setup/            conda environment.yml (name: mlst)
deploy/install.sh       idempotent no-sudo installer
deploy/INSTALL.md       porting guide for a new OOD system
ood/apps/mlst_gui/      production OOD batch_connect app card
ood/apps/mlst_gui_dev/  dev OOD card (per-session git worktree from a branch)
```

Projects share the lab layout: `download/`, `step1/`, `step2/vcf_source/`,
`<name>_VCFs/`, plus an `mlst/<sample>/` subdir for typing outputs. They live
under `/srv/kapurlab/projects` (shared) and `~/projects` (personal), so a
project created in any sibling GUI is visible here and vice versa.

## Stable CLI (callable by amr_plus_gui)

```bash
python bin/mlst_pipeline.py --assembly X.fasta --outdir DIR [--label NAME]
# or from reads:
python bin/mlst_pipeline.py --r1 R1.fastq.gz --r2 R2.fastq.gz --outdir DIR --label NAME
```

This writes `DIR/mlst_result.json` containing at least `scheme`, `st`, and
`organism_token`. `amr_plus_gui` reads `organism_token` to corroborate
Kraken's organism call and to pick the AMRFinderPlus `--organism`.

## Install / run

See [`deploy/INSTALL.md`](deploy/INSTALL.md). Quick start:

```bash
deploy/install.sh            # build conda env + frontend (no sudo)
cd backend && ../env/bin/python -m uvicorn app.main:app --port 8000
```

Under Open OnDemand the session script starts uvicorn on an allocated port and
the React SPA is served from `frontend/dist/`. All frontend URLs are relative
(`./api/...`) so the app works behind the OOD `/rnode/<host>/<port>/` proxy.

## API

| Route | Purpose |
|---|---|
| `GET  /api/projects` | shared + personal projects |
| `GET  /api/projects/{n}/samples` | FASTQ pairs in `download/` |
| `GET  /api/schemes` | PubMLST schemes (`mlst --longlist`), cached |
| `POST /api/run` | `{project, r1, r2?, assembly?, scheme?, threads?}` |
| `GET  /api/projects/{n}/samples/{s}/mlst-results` | per-sample output files |
| `GET  /api/projects/{n}/samples/{s}/mlst-table` | parsed scheme/ST/alleles/token |
| `GET  /api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/log` | jobs + SSE log |
