# MLST GUI — Install & Porting Guide

This guide covers installing the MLST GUI on a new Open OnDemand (OOD) system.
It is a sibling of `vsnp_gui` and `kraken_id_parse_gui` and follows the same
deployment model: a FastAPI backend that serves a built React SPA on a single
uvicorn port per OOD session.

## 1. Prerequisites

- A Linux host with Open OnDemand installed and a configured cluster
  (the sibling tools use a cluster named `wgs3` — adjust `cluster:` in the OOD
  `form.yml`/`submit.yml.erb` for your site).
- Miniforge / conda available to the service account (default base:
  `~/miniforge3`).
- Node.js + npm on PATH (system `npm` is fine) for the frontend build.

## 2. Quick install (no sudo)

```bash
cd /srv/kapurlab/tools/mlst_gui          # the tool root on your site
deploy/install.sh                        # creates env/ , builds frontend/dist/
# personal env instead of the shared one:
deploy/install.sh --env-name mlst
# preview without changing anything:
deploy/install.sh --dry-run
```

`install.sh` is idempotent. It:
1. creates/updates the `mlst` conda env (at `<repo>/env`, or `--env-name`),
2. pip-installs `backend/requirements.txt`,
3. verifies `mlst --version` and that `mlst --list` returns schemes,
4. builds `frontend/dist/`.

It does **not** download databases (see §4) and does **not** touch system files.

## 3. Paths & configuration (no home-dir hardcoding)

All site-specific paths live in `backend/app/config.py` DEFAULTS or env vars:

| Env var | Meaning | Default |
|---|---|---|
| `MLST_SHARED_PROJECTS` | Shared projects root | `/srv/kapurlab/projects` |
| `MLST_DB` | Relocated PubMLST db root (optional) | "" (use bundled) |
| `MLST_THREADS` | Default assembly threads | `8` |
| `XDG_CONFIG_HOME` | Where per-user `config.json` is stored | `~/.config` |

Per-user runtime config is written to `~/.config/mlst_gui/config.json`.
The "Personal projects root" and "PubMLST database path" can also be set from
the GUI's **Settings** panel.

## 4. PubMLST database

`mlst` ships a **bundled** PubMLST snapshot — no download is required to start.
Verify it:

```bash
mlst --version
mlst --list        # should print many scheme names
mlst --longlist    # scheme + locus listing (the GUI's /api/schemes uses this)
```

Refreshing the DB:
- The legacy `mlst-download_pub_mlst` script is **deprecated** and no longer
  works against the current PubMLST API.
- Current practice is the MDU-PHL [`mlstdb`](https://github.com/MDU-PHL/mlstdb)
  tooling together with a PubMLST API key. After refreshing into a new
  directory, point the GUI at it via the **Settings → PubMLST database path**
  field or the `MLST_DB` env var. The pipeline expects `blast/mlst.fa` and a
  `pubmlst/` data dir under that root.
- A DB refresh is **optional** — document it for your site, but it is not
  required for the tool to function.

## 5. Register the OOD app

OOD app cards live under `ood/apps/mlst_gui/` (production) and
`ood/apps/mlst_gui_dev/` (branch-picker dev variant). To register them:

```bash
# Production card:
sudo cp -r /srv/kapurlab/tools/mlst_gui/ood/apps/mlst_gui \
           /var/www/ood/apps/sys/mlst_gui
# Dev card (per-session git worktree from a chosen branch):
sudo cp -r /srv/kapurlab/tools/mlst_gui/ood/apps/mlst_gui_dev \
           /var/www/ood/apps/sys/mlst_gui_dev
```

Edit these tokens for your site if it is not the Kapur Lab box:
- `cluster:` in `form.yml` / `submit.yml.erb`
- the tool root `/srv/kapurlab/tools/mlst_gui` in `template/script.sh.erb`
- the shared projects root (`MLST_SHARED_PROJECTS`) if different.

The session script (`template/script.sh.erb`) picks the conda env in this
order: shared `<repo>/env`, then personal `~/miniforge3/envs/mlst`, then the
base python. It then starts uvicorn on the OOD-allocated `$port`.

## 6. Smoke test

```bash
# Activate the env and start the backend directly (outside OOD):
cd /srv/kapurlab/tools/mlst_gui/backend
/srv/kapurlab/tools/mlst_gui/env/bin/python -m uvicorn app.main:app --port 8000
# then open http://localhost:8000/ and check /api/schemes returns schemes.

# Stable CLI (what amr_plus_gui calls):
python /srv/kapurlab/tools/mlst_gui/bin/mlst_pipeline.py \
    --assembly /path/to/assembly.fasta --outdir /tmp/mlst_test --label test
cat /tmp/mlst_test/mlst_result.json   # has scheme, st, organism_token
```
