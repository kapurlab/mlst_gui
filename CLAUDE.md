# MLST GUI — Claude Code Context

> Read this before touching any code. Deployment-critical constraints that will
> cause silent breakage under the OOD proxy if ignored.

## What this is

A web GUI for **MLST** (multi-locus sequence typing) wrapping Torsten Seemann's
`mlst`. FastAPI backend + React (Vite) SPA, deployed as an Open OnDemand (OOD)
batch_connect interactive app. Sibling of `vsnp_gui` and `kraken_id_parse_gui`
— same look, project layout, and deployment model. It is also callable by
`amr_plus_gui` (via the stable `bin/mlst_pipeline.py` CLI) for species/ST
corroboration.

Authoritative build spec: `SPEC_BUILD.md` in this repo.

## Repository layout

```
backend/app/main.py     FastAPI routes (clone of Kraken GUI, renamed kraken->mlst)
backend/app/config.py   per-user config + DEFAULTS (env-var overridable)
backend/app/jobs.py     JobManager (shared, marker "mlst")
backend/app/sra.py      SRA download helper (shared)
bin/mlst_pipeline.py    orchestrator + STABLE CLI (amr_plus_gui depends on this)
config/scheme_organism_map.yaml   scheme -> AMRFinderPlus organism token
frontend/src/App.jsx    React SPA; build to frontend/dist/ (gitignored)
frontend/src/App.css    shared theme — DO NOT restyle (identical to siblings)
deploy/install.sh       idempotent no-sudo installer
deploy/INSTALL.md       porting guide
ood/apps/mlst_gui/      production OOD app card
ood/apps/mlst_gui_dev/  dev OOD app card (per-session worktree from a branch)
```

## CRITICAL constraints

### 1. All frontend URLs must be relative — no exceptions
OOD proxies the app at `/rnode/<host>/<port>/<path>`. Apache strips the prefix
and forwards `/<path>` to uvicorn. The browser origin is the OOD server, not
the app server. Use `fetch('./api/...')` and relative `EventSource('./api/...')`.
`vite.config.js` has `base: "./"` — **never change this**. Hardcoding a host,
port, or absolute URL 404s under the proxy.

### 2. FastAPI serves the React frontend
`main.py` mounts `frontend/dist/` as StaticFiles and serves `index.html` at `/`.
Do not add a separate static server — it breaks the single-port OOD model.

### 3. Rebuild the frontend after any frontend edit
```bash
cd frontend && npm run build      # writes frontend/dist/
```
Uvicorn serves the built `dist/`. The dev server (`npm run dev`) does not work
through OOD. Start a new OOD session to serve the new dist/.

### 4. The stable CLI is a contract
`amr_plus_gui` shells out to:
```
python bin/mlst_pipeline.py --assembly X.fasta --outdir DIR [--label NAME]
```
It must keep writing `DIR/mlst_result.json` with at least `scheme`, `st`, and
`organism_token`. Do not break these flags or that output schema.

### 5. mlst autodetects — no species input needed
`mlst` picks the best PubMLST scheme from the assembly. `--scheme` is only
passed when the user forces one. The scheme->organism map
(`config/scheme_organism_map.yaml`) is read dependency-free (no PyYAML
required) so the CLI works in a minimal env.

### 6. Shared project layout
Projects live in `/srv/kapurlab/projects` (shared) + `~/projects` (personal),
the same dirs the sibling GUIs use, so a project is visible across all three.
This tool adds an `mlst/<sample>/` subdir. Never hardcode home-dir paths —
site paths come from `config.py` DEFAULTS / env vars (`MLST_SHARED_PROJECTS`,
`MLST_DB`, `MLST_THREADS`).

### 7. Theme CSS is shared and fixed
`frontend/src/App.css` is copied verbatim from the Kraken GUI. Reuse the class
names (`row-header`, `row-grid-split`, `panel`, `status-strip`, `log`, …). Do
NOT restyle — the three GUIs must look identical.

## Pipeline (bin/mlst_pipeline.py)

1. reads in → assemble (`shovill`, fallback `spades.py --isolate`) →
   `assembly.fasta`; assembly FASTA in → use directly.
2. `mlst --quiet --json mlst.json --label <label> assembly` + capture TSV.
3. map scheme → AMRFinderPlus organism token.
4. write `mlst_result.json` (scheme, st, alleles, novel, partial,
   organism_token, species_guess, provenance).

## Dev workflow

- Backend-only change: restart uvicorn (start a new OOD session — ~10s).
- Frontend change: `cd frontend && npm run build`, then new OOD session.
- OOD card change: edit under `ood/apps/`, copy to `/var/www/ood/apps/sys/...`
  with sudo, start a new session.
- Verify Python parses: `python -m py_compile backend/app/main.py bin/mlst_pipeline.py`.

## Do NOT

- Run conda installs or PubMLST DB downloads as part of routine edits — that is
  the operator's job via `deploy/install.sh` / `INSTALL.md`.
- Change `vite.config.js` `base`.
- Restyle App.css.
- Break the `bin/mlst_pipeline.py` CLI flags or `mlst_result.json` schema.
