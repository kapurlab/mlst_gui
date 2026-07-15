import { useState, useEffect, useRef } from "react";
import "./App.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const APP_VERSION = "0.1.0";

function fileIcon(name) {
  if (name.endsWith(".json")) return "🧾";
  if (name.endsWith(".tsv") || name.endsWith(".csv")) return "📊";
  if (name.endsWith(".pdf")) return "📄";
  if (name.endsWith(".png")) return "🖼";
  if (name.endsWith(".fasta") || name.endsWith(".fa") || name.endsWith(".fna")) return "🧬";
  if (name.endsWith(".vcf")) return "🔬";
  if (name.endsWith(".txt")) return "📝";
  if (name.endsWith(".html")) return "🌐";
  return "📁";
}

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
export default function App() {
  const [projects, setProjects] = useState([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [activeProject, setActiveProject] = useState(""); // project the Inputs pane targets
  const [addPath, setAddPath] = useState({});       // proj -> import path string
  const [sraText, setSraText] = useState({});       // proj -> SRA accessions string
  const [fastaText, setFastaText] = useState({});   // proj -> genome-accession string
  const [fastaRename, setFastaRename] = useState(true);
  const [addStatus, setAddStatus] = useState({});   // proj -> status message
  const [inputsByProj, setInputsByProj] = useState({}); // proj -> {files,count,total_bytes}
  const uploadProjRef = useRef("");                 // which project the file dialog targets
  const uploadInputRef = useRef(null);
  const [expanded, setExpanded] = useState({});          // project name → bool
  const [samples, setSamples] = useState({});            // project name → [sample]
  const [checkedKeys, setCheckedKeys] = useState({});    // key → {project, ...sample}
  const [openResults, setOpenResults] = useState({});    // key → bool (inline results expanded)
  const [sampleResults, setSampleResults] = useState({}); // key → {loading, status, present, files}
  const [sampleTable, setSampleTable] = useState({});    // key → parsed mlst-table
  const [vsnpResults, setVsnpResults] = useState({});    // key → {loading, step1_present, files, step2}
  const [activeRun, setActiveRun] = useState(null);      // {project, sample} currently running
  const [queueInfo, setQueueInfo] = useState({ total: 0, done: 0 });
  const [schemes, setSchemes] = useState([]);            // from /api/schemes
  const [forceScheme, setForceScheme] = useState("");    // "" => autodetect
  const [threads, setThreads] = useState(8);
  const [running, setRunning] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState("idle"); // idle | running | succeeded | failed
  const [logLines, setLogLines] = useState([]);
  const [settingsDraft, setSettingsDraft] = useState({});
  const [folderBrowser, setFolderBrowser] = useState({ open: false, path: "", parent: null, entries: [], loading: false, error: "" });
  const [currentStep, setCurrentStep] = useState("");

  // Section visibility (collapsible flow)
  const [showSettings, setShowSettings] = useState(false);
  const [showProjects, setShowProjects] = useState(true);
  const [showRun, setShowRun] = useState(true);
  const [showLogs, setShowLogs] = useState(true);

  const logRef = useRef(null);
  const eventSourceRef = useRef(null);

  // Load config, schemes & projects on mount; reconnect to any running pipeline
  useEffect(() => {
    fetch("./api/config")
      .then((r) => r.json())
      .then((cfg) => {
        setThreads(cfg.threads || 8);
        setSettingsDraft(cfg);
      })
      .catch(() => {});
    fetch("./api/schemes")
      .then((r) => r.json())
      .then((d) => {
        if (Array.isArray(d.schemes)) setSchemes(d.schemes);
      })
      .catch(() => {});
    loadProjects();
    fetch("./api/jobs")
      .then((r) => r.json())
      .then((jobs) => {
        const live = jobs.find((j) => j.status === "running");
        if (live) {
          setJobId(live.id);
          setJobStatus("running");
          setRunning(true);
          let samp = null;
          const m = (live.name || "").match(/^(.*?)\/(.*?) — /);
          if (m) {
            samp = { project: m[1], sample: m[2] };
            setActiveRun(samp);
          }
          streamLogUntilDone(live.id, samp, () => {});
        }
      })
      .catch(() => {});
  }, []);

  function loadProjects() {
    setProjectsLoading(true);
    fetch("./api/projects")
      .then((r) => r.json())
      .then((data) => {
        setProjects(data);
        setProjectsLoading(false);
      })
      .catch(() => setProjectsLoading(false));
  }

  async function createProject() {
    const name = newProjectName.trim();
    if (!name || creatingProject) return;
    setCreatingProject(true);
    try {
      const res = await fetch("./api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        window.alert(`Could not create project: ${detail.detail || res.status}`);
        return;
      }
      const created = await res.json().catch(() => ({}));
      setNewProjectName("");
      loadProjects();
      if (created.name) {
        const n = created.name;
        setExpanded((e) => ({ ...e, [n]: true }));
        setActiveProject(n);
        await Promise.all([fetchSamples(n), loadInputs(n)]);
      }
    } finally {
      setCreatingProject(false);
    }
  }

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [logLines]);

  useEffect(() => {
    if (!projects.length) {
      if (activeProject) setActiveProject("");
      return;
    }
    if (!activeProject || !projects.find((p) => p.name === activeProject)) {
      const first = projects[0].name;
      setActiveProject(first);
      if (inputsByProj[first] === undefined) loadInputs(first);
    }
  }, [projects]);

  function fetchSamples(name) {
    return fetch(`./api/projects/${encodeURIComponent(name)}/samples`)
      .then((r) => r.json())
      .then((data) => setSamples((s) => ({ ...s, [name]: data })))
      .catch(() => setSamples((s) => ({ ...s, [name]: [] })));
  }

  function toggleProject(name) {
    const isExpanded = expanded[name];
    setExpanded((e) => ({ ...e, [name]: !isExpanded }));
    setActiveProject(name);
    if (!isExpanded) {
      if (!samples[name]) fetchSamples(name);
      loadInputs(name);
    }
  }

  function selectProject(name) {
    setActiveProject(name);
    if (inputsByProj[name] === undefined) loadInputs(name);
  }

  // ---- Sample loading (import / upload / SRA) -------------------------------
  function loadInputs(name) {
    return fetch(`./api/projects/${encodeURIComponent(name)}/inputs`)
      .then((r) => r.json())
      .then((data) => setInputsByProj((m) => ({ ...m, [name]: data })))
      .catch(() => setInputsByProj((m) => ({ ...m, [name]: { files: [], count: 0, total_bytes: 0 } })));
  }

  const setStat = (name, msg) => setAddStatus((m) => ({ ...m, [name]: msg }));

  async function refreshAfterLoad(name) {
    await Promise.all([fetchSamples(name), loadInputs(name)]);
    loadProjects();
  }

  async function linkLocal(name) {
    const path = (addPath[name] || "").trim();
    if (!path) return;
    setStat(name, "Linking…");
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/link-local`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { setStat(name, `Import failed: ${data.detail || res.status}`); return; }
      setStat(name, `Linked ${data.linked} file${data.linked === 1 ? "" : "s"}.`);
      setAddPath((m) => ({ ...m, [name]: "" }));
      await refreshAfterLoad(name);
    } catch (e) {
      setStat(name, `Import failed: ${e.message}`);
    }
  }

  function pickFiles(name) {
    uploadProjRef.current = name;
    uploadInputRef.current?.click();
  }

  async function uploadFiles(name, fileList) {
    const files = Array.from(fileList || []).filter(
      (f) => f.name.endsWith(".fastq.gz") || /\.(fa|fasta|fna)(\.gz)?$/i.test(f.name)
    );
    if (!name || !files.length) return;
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    setStat(name, `Uploading ${files.length} file${files.length === 1 ? "" : "s"}…`);
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/upload`, { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { setStat(name, `Upload failed: ${data.detail || res.status}`); return; }
      setStat(name, `Uploaded ${data.uploaded} file${data.uploaded === 1 ? "" : "s"}.`);
      await refreshAfterLoad(name);
    } catch (e) {
      setStat(name, `Upload failed: ${e.message}`);
    }
  }

  function parseAccessions(text) {
    return (text || "").split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  }

  async function sraDownload(name) {
    const accessions = parseAccessions(sraText[name]);
    if (!accessions.length) return;
    setStat(name, `Resolving ${accessions.length} accession${accessions.length === 1 ? "" : "s"}…`);
    setShowLogs(true);
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/sra/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accessions }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { setStat(name, `Download failed: ${data.detail || res.status}`); return; }
      setStat(name, "Downloading… progress shows in the Pipeline Log below.");
      setSraText((m) => ({ ...m, [name]: "" }));
      setJobId(data.job_id);
      setJobStatus("running");
      setLogLines([]);
      streamLogUntilDone(data.job_id, null, () => {
        setStat(name, "Download finished — see samples below.");
        refreshAfterLoad(name);
      });
    } catch (e) {
      setStat(name, `Download failed: ${e.message}`);
    }
  }

  async function fastaDownload(name) {
    const accessions = parseAccessions(fastaText[name]);
    if (!accessions.length) return;
    setStat(name, `Fetching ${accessions.length} genome${accessions.length === 1 ? "" : "s"}…`);
    setShowLogs(true);
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/fasta/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accessions, rename: fastaRename }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { setStat(name, `Download failed: ${data.detail || res.status}`); return; }
      setStat(name, "Downloading genomes… progress shows in the Pipeline Log below.");
      setFastaText((m) => ({ ...m, [name]: "" }));
      setJobId(data.job_id);
      setJobStatus("running");
      setLogLines([]);
      streamLogUntilDone(data.job_id, null, () => {
        setStat(name, "Genome download finished — see samples below.");
        refreshAfterLoad(name);
      });
    } catch (e) {
      setStat(name, `Download failed: ${e.message}`);
    }
  }

  async function deleteInput(name, filename) {
    if (!window.confirm(`Remove ${filename} from this project's download/ folder?`)) return;
    try {
      await fetch(`./api/projects/${encodeURIComponent(name)}/inputs/${encodeURIComponent(filename)}`, { method: "DELETE" });
      await refreshAfterLoad(name);
    } catch (e) {
      setStat(name, `Delete failed: ${e.message}`);
    }
  }

  // --- Sample selection / results --------------------------------------------
  const sampleKey = (project, s) => `${project}::${s.sample}`;
  const isActive = (project, s) =>
    activeRun && activeRun.project === project && activeRun.sample === s.sample;

  function toggleChecked(project, s) {
    const key = sampleKey(project, s);
    setCheckedKeys((m) => {
      const next = { ...m };
      if (next[key]) delete next[key];
      else next[key] = { project, ...s };
      return next;
    });
  }

  function loadSampleResults(project, s) {
    const key = sampleKey(project, s);
    setSampleResults((m) => ({ ...m, [key]: { ...(m[key] || {}), loading: true } }));
    fetch(`./api/projects/${encodeURIComponent(project)}/samples/${encodeURIComponent(s.sample)}/mlst-results`)
      .then((r) => r.json())
      .then((data) => setSampleResults((m) => ({ ...m, [key]: { loading: false, ...data } })))
      .catch(() => setSampleResults((m) => ({ ...m, [key]: { loading: false, present: false, status: "none", files: [] } })));
    fetch(`./api/projects/${encodeURIComponent(project)}/samples/${encodeURIComponent(s.sample)}/mlst-table`)
      .then((r) => r.json())
      .then((data) => setSampleTable((m) => ({ ...m, [key]: data })))
      .catch(() => setSampleTable((m) => ({ ...m, [key]: { present: false } })));
  }

  function loadVsnpResults(project, s) {
    const key = sampleKey(project, s);
    setVsnpResults((m) => ({ ...m, [key]: { ...(m[key] || {}), loading: true } }));
    fetch(`./api/projects/${encodeURIComponent(project)}/vsnp/samples/${encodeURIComponent(s.sample)}/files`)
      .then((r) => r.json())
      .then((data) => setVsnpResults((m) => ({ ...m, [key]: { loading: false, ...data } })))
      .catch(() => setVsnpResults((m) => ({ ...m, [key]: { loading: false, step1_present: false, files: [], step2: { present: false } } })));
  }

  function toggleResults(project, s) {
    const key = sampleKey(project, s);
    const willOpen = !openResults[key];
    setOpenResults((m) => ({ ...m, [key]: willOpen }));
    if (willOpen && !sampleResults[key]) loadSampleResults(project, s);
    if (willOpen && !vsnpResults[key]) loadVsnpResults(project, s);
  }

  async function runSamples(list) {
    if (running || !list.length) return;
    setShowLogs(true);
    setQueueInfo({ total: list.length, done: 0 });
    for (let i = 0; i < list.length; i++) {
      await runOne(list[i]);
      setQueueInfo({ total: list.length, done: i + 1 });
    }
    setActiveRun(null);
  }

  function runSelected() {
    runSamples(Object.values(checkedKeys));
  }

  function runOne(samp) {
    return new Promise((resolve) => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      setRunning(true);
      setActiveRun({ project: samp.project, sample: samp.sample });
      setJobStatus("running");
      setLogLines([]);
      setCurrentStep("");
      const key = sampleKey(samp.project, samp);
      setSampleResults((m) => ({ ...m, [key]: { ...(m[key] || {}), status: "running" } }));
      setOpenResults((m) => ({ ...m, [key]: true }));

      fetch("./api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project: samp.project,
          r1: samp.r1,
          r2: samp.r2 || null,
          scheme: forceScheme.trim() || null,
          threads: Number(threads) || null,
        }),
      })
        .then((r) => (r.ok ? r.json() : r.json().then((e) => { throw new Error(e.detail || "Run failed"); })))
        .then(({ job_id }) => {
          setJobId(job_id);
          streamLogUntilDone(job_id, samp, resolve);
        })
        .catch((err) => {
          setLogLines((prev) => [...prev, `ERROR: ${err.message}`]);
          setRunning(false);
          setJobStatus("failed");
          resolve();
        });
    });
  }

  function streamLogUntilDone(id, samp, done) {
    const es = new EventSource(`./api/jobs/${id}/log`);
    eventSourceRef.current = es;
    es.onmessage = (evt) => {
      const data = evt.data;
      if (data === "[DONE]") {
        es.close();
        setRunning(false);
        fetch(`./api/jobs/${id}`)
          .then((r) => r.json())
          .then((job) => {
            setJobStatus(job.status);
            setCurrentStep("");
            if (samp) loadSampleResults(samp.project, samp);
            loadProjects();
          })
          .catch(() => {})
          .finally(() => done());
      } else {
        setLogLines((prev) => [...prev, data]);
        if (/assembl/i.test(data) ||
            /Running mlst/i.test(data) ||
            /Scheme:/i.test(data) ||
            /Wrote /i.test(data)) {
          setCurrentStep(data.trim().replace(/^[$#]+\s*/, ""));
        }
      }
    };
    es.onerror = () => {
      es.close();
      setRunning(false);
      setJobStatus("failed");
      done();
    };
  }

  // --- Project-root folder browser ---------------------------------------
  function browseDirs(path) {
    setFolderBrowser((s) => ({ ...s, loading: true, error: "" }));
    fetch(`./api/browse-dirs?path=${encodeURIComponent(path || "")}`)
      .then((r) => (r.ok ? r.json() : r.json().then((e) => { throw new Error(e.detail || "Cannot open folder"); })))
      .then((d) => setFolderBrowser((s) => ({ ...s, path: d.path, parent: d.parent, entries: d.entries, loading: false })))
      .catch((err) => setFolderBrowser((s) => ({ ...s, loading: false, error: err.message })));
  }
  function openFolderBrowser() {
    setFolderBrowser({ open: true, path: "", parent: null, entries: [], loading: true, error: "" });
    browseDirs(settingsDraft.projects_root || "");
  }
  function chooseFolder() {
    setSettingsDraft((d) => ({ ...d, projects_root: folderBrowser.path }));
    setFolderBrowser((s) => ({ ...s, open: false }));
  }

  function saveSettings() {
    fetch("./api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mlst_db: settingsDraft.mlst_db,
        threads: Number(settingsDraft.threads) || undefined,
        projects_root: settingsDraft.projects_root,
      }),
    })
      .then((r) => r.json())
      .then(() => {
        setThreads(settingsDraft.threads || 8);
        loadProjects();
      })
      .catch(() => {});
  }

  function refreshSchemes() {
    fetch("./api/schemes?refresh=1")
      .then((r) => r.json())
      .then((d) => { if (Array.isArray(d.schemes)) setSchemes(d.schemes); })
      .catch(() => {});
  }

  const logLineClass = (line) => {
    if (line.startsWith("$ ")) return "log-line cmd";
    if (line.startsWith("ERROR") || line.startsWith("error")) return "log-line error";
    if (line === "[DONE]") return "log-line done";
    return "log-line";
  };

  const statusText = {
    idle: "idle",
    running: "running",
    succeeded: "succeeded",
    failed: "failed",
  }[jobStatus];

  // KPI: scheme/ST of the most recently opened sample with a table result.
  const lastTable = (() => {
    const keys = Object.keys(sampleTable);
    for (let i = keys.length - 1; i >= 0; i--) {
      const t = sampleTable[keys[i]];
      if (t && t.present) return t;
    }
    return null;
  })();

  return (
    <div className="app">
      <input
        ref={uploadInputRef}
        type="file"
        multiple
        accept=".fastq.gz,.fasta,.fa,.fna,application/gzip"
        style={{ display: "none" }}
        onChange={(e) => {
          const files = Array.from(e.target.files);
          e.target.value = "";
          if (uploadProjRef.current) uploadFiles(uploadProjRef.current, files);
        }}
      />
      {/* ── Header ─────────────────────────────────────────────── */}
      <header className="app-header">
        <div className="app-brand">
          <span className="app-logo" role="img" aria-label="DNA barcode" style={{ fontSize: 30 }}>🧬</span>
          <div>
            <h1>
              MLST <span className="version-tag">v{APP_VERSION}</span>
            </h1>
            <p>Multi-locus sequence typing — autodetect the PubMLST scheme and report the sequence type</p>
          </div>
        </div>
        <div className="status-pill">
          <span className="dot" data-state={jobStatus} />
          <span>{statusText}</span>
        </div>
      </header>

      <main className="layout">
        {/* ── Status strip ─────────────────────────────────────── */}
        <section className="status-strip">
          <div className="status-item">
            <span className="status-label">Selected</span>
            <span className="status-value">
              {Object.keys(checkedKeys).length
                ? `${Object.keys(checkedKeys).length} sample${Object.keys(checkedKeys).length > 1 ? "s" : ""}`
                : activeRun ? activeRun.sample : "—"}
            </span>
          </div>
          <div className="status-item">
            <span className="status-label">Scheme</span>
            <span className="status-value">{lastTable?.scheme || forceScheme || "auto"}</span>
          </div>
          <div className="status-item">
            <span className="status-label">ST</span>
            <span className="status-value">{lastTable?.st || "—"}</span>
          </div>
          <div className="status-item">
            <span className="status-label">Job</span>
            <span className="status-value cap">
              {jobStatus === "running" ? <><span className="pulse-dot" />running</> : statusText}
            </span>
          </div>
        </section>

        {/* ════ SECTION: Settings ════ */}
        <div className="row-header">
          <h2>Settings</h2>
          <button className="ghost" onClick={() => {
            if (!showSettings) {
              fetch("./api/config").then((r) => r.json()).then(setSettingsDraft).catch(() => {});
            }
            setShowSettings(!showSettings);
          }}>
            {showSettings ? "Hide" : "Show"}
          </button>
        </div>
        {showSettings && (
          <div className="row-grid row-grid-single">
            <section className="panel">
              <div className="form-section">
                <label className="form-label">PubMLST database path (optional)</label>
                <input
                  placeholder="(leave blank to use the bundled mlst database)"
                  value={settingsDraft.mlst_db || ""}
                  onChange={(e) => setSettingsDraft((d) => ({ ...d, mlst_db: e.target.value }))}
                />
                <div className="form-hint">Only set this if the mlst PubMLST DB was relocated/refreshed (expects blast/mlst.fa + pubmlst/ inside).</div>
              </div>
              <div className="form-section">
                <label className="form-label">Assembly threads</label>
                <input
                  type="number"
                  min={1}
                  max={64}
                  value={settingsDraft.threads || 8}
                  onChange={(e) => setSettingsDraft((d) => ({ ...d, threads: e.target.value }))}
                />
                <div className="form-hint">CPU threads for shovill / SPAdes when assembling reads.</div>
              </div>
              <div className="form-section">
                <label className="form-label">Personal projects root</label>
                <div style={{ display: "flex", gap: 6 }}>
                  <input
                    style={{ flex: 1 }}
                    value={settingsDraft.projects_root || ""}
                    onChange={(e) => setSettingsDraft((d) => ({ ...d, projects_root: e.target.value }))}
                  />
                  <button type="button" className="ghost" onClick={openFolderBrowser}>Browse…</button>
                </div>
                {Array.isArray(settingsDraft.recent_projects_roots) && settingsDraft.recent_projects_roots.length > 0 && (
                  <select
                    style={{ marginTop: 6, width: "100%" }}
                    value=""
                    onChange={(e) => { if (e.target.value) setSettingsDraft((d) => ({ ...d, projects_root: e.target.value })); }}
                  >
                    <option value="">↻ Recent roots…</option>
                    {settingsDraft.recent_projects_roots.map((r) => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                  </select>
                )}
                <div className="form-hint">New projects are created under this root. Shared projects at /srv/kapurlab/projects/ are always visible. Click Save to apply.</div>
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
                <button onClick={saveSettings}>Save</button>
              </div>
            </section>
          </div>
        )}

        {/* ════ SECTION: Projects & Samples ════ */}
        <div className="row-header">
          <h2>Projects &amp; Samples</h2>
          <button className="ghost" onClick={() => setShowProjects(!showProjects)}>
            {showProjects ? "Hide" : "Show"}
          </button>
        </div>
        {showProjects && (
          <div className="row-grid row-grid-split">
            {/* LEFT — project / sample browser */}
            <section className="panel">
              <div className="panel-header">
                <h2>Projects</h2>
                <div className="panel-actions">
                  <button className="ghost action" onClick={loadProjects}>↻ Refresh</button>
                </div>
              </div>
              <div className="row">
                <input
                  placeholder="New project name (e.g. Salmonella_2024)"
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value.replace(/\s+/g, "_"))}
                  onKeyDown={(e) => { if (e.key === "Enter") createProject(); }}
                  disabled={creatingProject}
                  title="Spaces become underscores. Letters, digits, _ - . are allowed. Created under your personal projects and visible in the sibling GUIs too."
                />
                <button onClick={createProject} disabled={creatingProject || !newProjectName.trim()}>
                  {creatingProject ? "Creating…" : "Create"}
                </button>
              </div>
              <div className="form-hint" style={{ marginTop: -4, marginBottom: 8 }}>
                Created under your personal projects root — also visible in the vSNP &amp; Kraken GUIs. Add FASTQs or assembly FASTAs to the project’s <code>download/</code> folder.
              </div>
              <div className="list project-list">
                {projectsLoading && <div className="loading-text">Loading projects…</div>}
                {!projectsLoading && projects.length === 0 && (
                  <div className="note">No projects found. Check Settings for the projects path.</div>
                )}
                {projects.map((proj) => (
                  <div
                    key={proj.name}
                    className={`list-item ${activeRun?.project === proj.name || activeProject === proj.name ? "active" : ""}`}
                  >
                    <div className="item-top" onClick={() => toggleProject(proj.name)}>
                      <span className="expand-icon">{expanded[proj.name] ? "▾" : "▸"}</span>
                      <div className="list-title" title={proj.name}>{proj.name}</div>
                      <span className={`scope-badge scope-${proj.scope}`}>{proj.scope}</span>
                    </div>
                    {proj.path && <div className="list-path" title={proj.path}>{proj.path}</div>}
                    <div className="list-meta">
                      {proj.fastq_count} FASTQ
                      {proj.mlst_runs?.length > 0 &&
                        ` · ${proj.mlst_runs.length} MLST run${proj.mlst_runs.length > 1 ? "s" : ""}`}
                    </div>
                    {expanded[proj.name] && (
                      <div className="sample-list">
                        {!samples[proj.name] && <div className="loading-text">Loading samples…</div>}
                        {samples[proj.name]?.length === 0 && (
                          <div className="empty-msg" style={{ paddingLeft: 4 }}>
                            No FASTQ files yet — add some from the <strong>Inputs</strong> pane on the right.
                          </div>
                        )}
                        {samples[proj.name]?.map((s) => {
                          const key = sampleKey(proj.name, s);
                          const res = sampleResults[key];
                          const tbl = sampleTable[key];
                          const vres = vsnpResults[key];
                          const hasRun = proj.mlst_runs?.includes(s.sample);
                          const status = res?.status || (hasRun ? "done" : "none");
                          const checked = !!checkedKeys[key];
                          const open = !!openResults[key];
                          const statusLabel =
                            status === "running" ? "● running" : status === "done" ? "✓ typed" : "not run";
                          return (
                          <div
                            key={s.r1}
                            className={`sample-item ${isActive(proj.name, s) ? "active" : ""}`}
                          >
                            <div className="sample-name-row" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleChecked(proj.name, s)}
                                title="Select for batch run"
                              />
                              <div
                                className="sample-name"
                                title={`${s.sample} — click to show results`}
                                style={{ flex: 1, cursor: "pointer" }}
                                onClick={() => toggleResults(proj.name, s)}
                              >
                                {s.sample}
                              </div>
                              <span className={`read-badge ${s.paired ? "badge-pe" : "badge-se"}`}>
                                {s.paired ? "PE" : "SE"}
                              </span>
                              <span
                                className={`run-status run-status-${status}`}
                                title={`Run status: ${status}`}
                                style={{ fontSize: 11, whiteSpace: "nowrap" }}
                              >
                                {statusLabel}
                              </span>
                              <button
                                className="ghost"
                                style={{ fontSize: 11 }}
                                onClick={() => toggleResults(proj.name, s)}
                                title="Show/hide results for this sample"
                              >
                                {open ? "▾" : "▸"}
                              </button>
                            </div>
                            <div className="sample-files">
                              {s.paired ? (
                                <>
                                  <div className="sample-file-row">
                                    <span className="file-label">R1</span>
                                    <span className="file-name" title={s.r1_name}>{s.r1_name}</span>
                                    <span className="file-size">{fmtSize(s.r1_size)}</span>
                                  </div>
                                  <div className="sample-file-row">
                                    <span className="file-label">R2</span>
                                    <span className="file-name" title={s.r2_name}>{s.r2_name}</span>
                                    <span className="file-size">{fmtSize(s.r2_size)}</span>
                                  </div>
                                </>
                              ) : (
                                <div className="sample-file-row">
                                  <span className="file-name" title={s.r1_name}>{s.r1_name}</span>
                                  <span className="file-size">{fmtSize(s.r1_size)}</span>
                                </div>
                              )}
                            </div>
                            {open && (
                              <div className="sample-results-inline" style={{ marginTop: 6, paddingLeft: 22 }}>
                                <div style={{ display: "flex", gap: 8, marginBottom: 4 }}>
                                  <button
                                    className="ghost action"
                                    disabled={running}
                                    onClick={() => runSamples([{ project: proj.name, ...s }])}
                                  >
                                    {status === "done" ? "↻ Re-type this sample" : "▶ Type this sample"}
                                  </button>
                                  <button className="ghost action" onClick={() => loadSampleResults(proj.name, s)}>
                                    ↻ Refresh
                                  </button>
                                </div>

                                {/* MLST typing result table */}
                                {tbl && tbl.present && (
                                  <div className="selection-box" style={{ marginBottom: 8 }}>
                                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                                      <span className="sel-title" style={{ margin: 0 }}>Scheme:</span>
                                      <strong>{tbl.scheme || "—"}</strong>
                                      <span className="read-badge badge-pe" style={{ fontSize: 13 }}>ST {tbl.st || "-"}</span>
                                      {tbl.novel && <span className="read-badge badge-se" title="One or more novel alleles (~)">novel</span>}
                                      {tbl.partial && <span className="read-badge badge-se" title="Partial / uncertain alleles (? or -)">partial</span>}
                                    </div>
                                    {tbl.organism_token && (
                                      <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
                                        AMRFinderPlus organism token: <code>{tbl.organism_token}</code>
                                        {tbl.species_guess ? ` (${tbl.species_guess})` : ""}
                                      </div>
                                    )}
                                    {tbl.alleles && Object.keys(tbl.alleles).length > 0 && (
                                      <div className="sample-files" style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 6 }}>
                                        {Object.entries(tbl.alleles).map(([locus, allele]) => (
                                          <span key={locus} className="read-badge" style={{ background: "var(--panel-2, #f0f0f0)", color: "inherit" }}>
                                            {locus}: <strong>{allele || "-"}</strong>
                                          </span>
                                        ))}
                                      </div>
                                    )}
                                    {tbl.provenance && (
                                      <details style={{ marginTop: 6 }}>
                                        <summary className="muted" style={{ cursor: "pointer", fontSize: 12 }}>Provenance</summary>
                                        <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                                          <div>mlst: {tbl.provenance.mlst_version || "—"}</div>
                                          <div>assembler: {tbl.provenance.assembler || "—"}</div>
                                          <div>DB schemes: {tbl.provenance.db_schemes ?? "—"}</div>
                                          <div>scheme forced: {tbl.provenance.scheme_forced ? "yes" : "no"}</div>
                                          <div>generated: {tbl.provenance.generated_at || "—"}</div>
                                        </div>
                                      </details>
                                    )}
                                  </div>
                                )}

                                {res?.loading ? (
                                  <div className="loading-text">Loading results…</div>
                                ) : !res || !res.present || (res.files || []).length === 0 ? (
                                  <div className="empty-msg" style={{ paddingLeft: 0 }}>
                                    {status === "running"
                                      ? "Running… results will appear here when finished."
                                      : "No MLST results yet for this sample."}
                                  </div>
                                ) : (
                                  <div className="results-list">
                                    {res.files.map((f) => {
                                      const base = `./api/projects/${encodeURIComponent(proj.name)}/file?path=${encodeURIComponent(f.path)}`;
                                      return (
                                        <div key={f.name} className="results-item">
                                          <span className="result-icon">{fileIcon(f.name)}</span>
                                          {f.openable ? (
                                            <a className="result-name result-link" href={`${base}&inline=1`}
                                               target="_blank" rel="noopener noreferrer" title={`Open ${f.name}`}>
                                              {f.label || f.name}
                                            </a>
                                          ) : (
                                            <a className="result-name result-link" href={`${base}&inline=0`}
                                               title={`Download ${f.name}`}>
                                              {f.label || f.name}
                                            </a>
                                          )}
                                          <span className="result-size">{fmtSize(f.size)}</span>
                                          <a className="result-download" href={`${base}&inline=0`} title={`Download ${f.name}`}>⬇</a>
                                        </div>
                                      );
                                    })}
                                  </div>
                                )}

                                {/* Cross-tool: vSNP results for this sample */}
                                <div className="vsnp-cross-tool" style={{ marginTop: 10, borderTop: "1px solid var(--border, #e2e2e2)", paddingTop: 8 }}>
                                  <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4 }}>vSNP results</div>
                                  {vres?.loading ? (
                                    <div className="loading-text">Loading vSNP results…</div>
                                  ) : !vres || !vres.step1_present ? (
                                    <div className="empty-msg" style={{ paddingLeft: 0 }}>No vSNP run for this sample yet.</div>
                                  ) : (
                                    <>
                                      <div className="results-list">
                                        {(vres.files || []).map((f) => {
                                          const vbase = `./api/projects/${encodeURIComponent(proj.name)}/file?path=${encodeURIComponent(f.path)}`;
                                          return (
                                            <div key={f.relpath} className="results-item">
                                              <span className="result-icon">{fileIcon(f.name)}</span>
                                              {f.openable ? (
                                                <a className="result-name result-link" href={`${vbase}&inline=1`}
                                                   target="_blank" rel="noopener noreferrer" title={`Open ${f.name}`}>
                                                  {f.relpath}
                                                </a>
                                              ) : (
                                                <a className="result-name result-link" href={`${vbase}&inline=0`}
                                                   title={`Download ${f.name}`}>
                                                  {f.relpath}
                                                </a>
                                              )}
                                              <span className="result-size">{fmtSize(f.size)}</span>
                                              <a className="result-download" href={`${vbase}&inline=0`} title={`Download ${f.name}`}>⬇</a>
                                            </div>
                                          );
                                        })}
                                      </div>
                                      {vres.step2 && vres.step2.present && (
                                        <div className="vsnp-step2" style={{ marginTop: 6 }}>
                                          {vres.step2.report_path ? (
                                            <a className="result-name result-link"
                                               href={`./api/projects/${encodeURIComponent(proj.name)}/file?path=${encodeURIComponent(vres.step2.report_path)}&inline=1`}
                                               target="_blank" rel="noopener noreferrer"
                                               title="Open the latest SNP comparison report this sample appears in">
                                              📊 Latest SNP comparison{vres.step2.started_at ? ` (${vres.step2.started_at})` : ""}
                                            </a>
                                          ) : (
                                            <span className="muted">
                                              In latest SNP comparison{vres.step2.started_at ? ` (${vres.step2.started_at})` : ""}
                                            </span>
                                          )}
                                        </div>
                                      )}
                                    </>
                                  )}
                                </div>
                              </div>
                            )}
                          </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </section>

            {/* RIGHT — Inputs + batch selection, stacked */}
            <div style={{ display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
              <section className="panel">
                <div className="panel-header">
                  <h2>Inputs</h2>
                  {projects.length > 0 && (
                    <select
                      value={activeProject}
                      onChange={(e) => selectProject(e.target.value)}
                      title="Project to add FASTQ / assembly files to"
                      style={{ width: "auto", maxWidth: "60%", padding: "6px 10px" }}
                    >
                      {projects.map((p) => (
                        <option key={p.name} value={p.name}>{p.name}</option>
                      ))}
                    </select>
                  )}
                </div>
                {!activeProject ? (
                  <div className="empty-msg">
                    Create a project first (top of the Projects panel), then import, upload, or download FASTQ / assembly files into it.
                  </div>
                ) : (
                  <div className="input-columns">
                    <div className="input-column">
                      <h3>Bring Your Own Reads / Assembly</h3>
                      <div className="row" style={{ margin: 0 }}>
                        <input
                          placeholder="/srv/kapurlab/… folder, .fastq.gz, or .fasta file"
                          value={addPath[activeProject] || ""}
                          onChange={(e) => setAddPath((m) => ({ ...m, [activeProject]: e.target.value }))}
                          onKeyDown={(e) => { if (e.key === "Enter") linkLocal(activeProject); }}
                        />
                        <button className="ghost action" onClick={() => linkLocal(activeProject)} disabled={!(addPath[activeProject] || "").trim()}>Link</button>
                      </div>
                      <div className="form-hint">Symlinks every .fastq.gz and .fasta found — no copying.</div>

                      <div className="block">
                        <h3>Upload / Drag &amp; Drop</h3>
                        <div
                          className="dropzone"
                          onDragOver={(e) => e.preventDefault()}
                          onDrop={(e) => { e.preventDefault(); uploadFiles(activeProject, e.dataTransfer.files); }}
                        >
                          <button type="button" onClick={() => pickFiles(activeProject)}>Choose Files</button>
                          <span className="drop-hint">Or drop FASTQ.GZ / FASTA files here</span>
                        </div>
                        {addStatus[activeProject] && <div className="note" style={{ marginBottom: 0 }}>{addStatus[activeProject]}</div>}
                      </div>

                      {inputsByProj[activeProject]?.files?.length > 0 && (
                        <div className="block">
                          <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}>
                            <span style={{ flex: 1 }}>
                              Files in download/
                              <span className="muted" style={{ marginLeft: 6, fontWeight: 400, fontSize: 12 }}>
                                ({inputsByProj[activeProject].count}, {fmtSize(inputsByProj[activeProject].total_bytes)})
                              </span>
                            </span>
                            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => loadInputs(activeProject)} title="Refresh">Refresh</button>
                          </h3>
                          <div className="input-files">
                            {inputsByProj[activeProject].files.map((f) => (
                              <div key={f.name} className="input-file-row">
                                <span className="file-name" title={f.name} style={{ flex: 1 }}>{f.name}</span>
                                <span className="file-size">{fmtSize(f.size)}</span>
                                <button className="ghost" style={{ fontSize: 11, padding: "2px 7px" }} title="Remove from download/" onClick={() => deleteInput(activeProject, f.name)}>✕</button>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>

                    <div className="input-column">
                      <h3>SRA Download</h3>
                      <textarea
                        rows={6}
                        placeholder={"SRR/ERR/DRR or SRX/SRS/PRJNA accessions\n(one per line)"}
                        value={sraText[activeProject] || ""}
                        onChange={(e) => setSraText((m) => ({ ...m, [activeProject]: e.target.value }))}
                        style={{ resize: "vertical", fontFamily: "inherit" }}
                      />
                      <button
                        style={{ width: "100%" }}
                        onClick={() => sraDownload(activeProject)}
                        disabled={!parseAccessions(sraText[activeProject]).length || running}
                      >
                        Download{parseAccessions(sraText[activeProject]).length ? ` (${parseAccessions(sraText[activeProject]).length})` : ""}
                      </button>
                      <div className="form-hint">Runs in the background; progress appears in the Pipeline Log.</div>
                    </div>

                    <div className="input-column">
                      <h3>Download genome FASTA by accession</h3>
                      <textarea
                        rows={6}
                        placeholder={"GenBank/RefSeq accessions (NC_/CP_…)\nor assembly (GCA_/GCF_) — one per line"}
                        value={fastaText[activeProject] || ""}
                        onChange={(e) => setFastaText((m) => ({ ...m, [activeProject]: e.target.value }))}
                        style={{ resize: "vertical", fontFamily: "inherit" }}
                      />
                      <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, margin: "4px 0" }}>
                        <input type="checkbox" checked={fastaRename} onChange={(e) => setFastaRename(e.target.checked)} />
                        Name files by organism / strain metadata (recommended)
                      </label>
                      <button
                        style={{ width: "100%" }}
                        onClick={() => fastaDownload(activeProject)}
                        disabled={!parseAccessions(fastaText[activeProject]).length || running}
                      >
                        Fetch FASTA{parseAccessions(fastaText[activeProject]).length ? ` (${parseAccessions(fastaText[activeProject]).length})` : ""}
                      </button>
                      <div className="form-hint">Fetches assembled genomes into download/. MLST can type an assembly directly — no reads/assembly step needed. Same downloader as kSNP/GenoFLU.</div>
                    </div>
                  </div>
                )}
              </section>

              <section className="panel">
                <div className="panel-header">
                  <h2>Selected for run</h2>
                  {Object.keys(checkedKeys).length > 0 && (
                    <button className="ghost action" onClick={() => setCheckedKeys({})}>Clear</button>
                  )}
                </div>
                {Object.keys(checkedKeys).length === 0 ? (
                  <div className="empty-msg">
                    Check one or more samples on the left, then run them as a batch from “Run MLST” below.
                    Click a sample’s name to view its typing result inline.
                  </div>
                ) : (
                  <div className="selection-box">
                    <div className="sel-title">{Object.keys(checkedKeys).length} sample(s) queued</div>
                    {Object.entries(checkedKeys).map(([key, samp]) => (
                      <div key={key} className="sel-row" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span className="sel-name" style={{ flex: 1 }}>{samp.sample}</span>
                        <span className="muted" style={{ fontSize: 11 }}>{samp.project}</span>
                        <button className="ghost" style={{ fontSize: 11 }}
                                onClick={() => toggleChecked(samp.project, samp)} title="Remove from batch">✕</button>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            </div>
          </div>
        )}

        {/* ════ SECTION: Run MLST ════ */}
        <div className="row-header">
          <h2>Run MLST</h2>
          <button className="ghost" onClick={() => setShowRun(!showRun)}>
            {showRun ? "Hide" : "Show"}
          </button>
        </div>
        {showRun && (
          <div className="row-grid row-grid-split">
            {/* LEFT — configure & run */}
            <section className="panel">
              <h2>Configure &amp; Run</h2>

              <div className="form-section">
                <label className="form-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ flex: 1 }}>Force scheme (optional — default autodetects)</span>
                  <button type="button" className="ghost" style={{ fontSize: 11 }} onClick={refreshSchemes}>↻ Refresh schemes</button>
                </label>
                <select
                  value={forceScheme}
                  onChange={(e) => setForceScheme(e.target.value)}
                  disabled={running}
                >
                  <option value="">Autodetect (recommended)</option>
                  {schemes.map((s) => (
                    <option key={s.scheme} value={s.scheme}>
                      {s.scheme}{s.loci?.length ? ` (${s.loci.length} loci)` : ""}
                    </option>
                  ))}
                </select>
                <div className="note" style={{ marginTop: 4 }}>
                  mlst autodetects the best-matching PubMLST scheme from the assembly — only force one if you know it.
                  {schemes.length === 0 && " (Scheme list unavailable — mlst may not be on PATH.)"}
                </div>
              </div>

              <div className="form-section">
                <label className="form-label">Assembly threads</label>
                <input
                  type="number"
                  min={1}
                  max={64}
                  value={threads}
                  onChange={(e) => setThreads(e.target.value)}
                  disabled={running}
                />
                <div className="form-hint">Used by shovill / SPAdes when a sample has reads (skipped for assembly FASTAs).</div>
              </div>

              <button
                className="run-btn"
                onClick={runSelected}
                disabled={running || Object.keys(checkedKeys).length === 0}
              >
                {running
                  ? `Running… ${queueInfo.total > 1 ? `(${queueInfo.done}/${queueInfo.total})` : ""}`
                  : `▶ Run selected${Object.keys(checkedKeys).length ? ` (${Object.keys(checkedKeys).length})` : ""}`}
              </button>
              {Object.keys(checkedKeys).length === 0 && (
                <div className="note">Check one or more samples on the left to enable the run. (Or use “Type this sample” under any sample.)</div>
              )}
            </section>

            {/* RIGHT — current run status */}
            <section className="panel">
              <div className="panel-header">
                <h2>Current run</h2>
                {jobId && <span className="muted" style={{ fontSize: 12 }}>job {jobId.slice(0, 8)}</span>}
              </div>
              {activeRun ? (
                <div className="selection-box">
                  <div className="sel-title">
                    {jobStatus === "running" ? "Running" : jobStatus === "succeeded" ? "Done" : jobStatus}
                    {queueInfo.total > 1 ? ` — ${queueInfo.done}/${queueInfo.total} in batch` : ""}
                  </div>
                  <div><span className="sel-name">{activeRun.sample}</span></div>
                  <div style={{ marginTop: 2 }}>
                    <span className="muted">Project:</span> <strong>{activeRun.project}</strong>
                  </div>
                  {currentStep && <div className="muted" style={{ marginTop: 4 }}>{currentStep}</div>}
                  <div className="note" style={{ marginTop: 8 }}>
                    Typing result + output files appear inline under each sample on the left (click a sample’s name to expand).
                  </div>
                </div>
              ) : (
                <div className="empty-msg">
                  No active run. Results for any sample are shown inline under that sample on the left.
                </div>
              )}
            </section>
          </div>
        )}

        {/* ════ SECTION: Pipeline Log ════ */}
        <div className="row-header">
          <h2>Pipeline Log</h2>
          <button className="ghost" onClick={() => setShowLogs(!showLogs)}>
            {showLogs ? "Hide" : "Show"}
          </button>
        </div>
        {showLogs && (
          <div className="row-grid row-grid-single">
            <section className="panel">
              <div className="log-meta">
                <span className="dot" data-state={jobStatus} />
                <span style={{ fontWeight: 600 }}>
                  {jobStatus === "idle" && "Idle"}
                  {jobStatus === "running" && "Running"}
                  {jobStatus === "succeeded" && "Done"}
                  {jobStatus === "failed" && "Failed"}
                </span>
                {jobStatus === "running" && currentStep && (
                  <span className="log-step" title={currentStep}>— {currentStep}</span>
                )}
              </div>
              <div className="log" ref={logRef}>
                {logLines.length === 0 ? (
                  <span className="log-placeholder">
                    {jobStatus === "idle"
                      ? "Select a sample and click Run to start."
                      : "Waiting for output…"}
                  </span>
                ) : (
                  logLines.map((line, i) => (
                    <div key={i} className={logLineClass(line)}>{line}</div>
                  ))
                )}
              </div>
            </section>
          </div>
        )}
      </main>

      {folderBrowser.open && (
        <div
          onClick={() => setFolderBrowser((s) => ({ ...s, open: false }))}
          style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ background: "var(--panel, #fff)", color: "inherit", borderRadius: 10, width: "min(640px, 92vw)", maxHeight: "80vh", display: "flex", flexDirection: "column", boxShadow: "0 10px 40px rgba(0,0,0,0.3)" }}
          >
            <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border, #ddd)", fontWeight: 700 }}>
              Select a projects root
            </div>
            <div style={{ padding: "10px 16px", display: "flex", gap: 6, alignItems: "center" }}>
              <button type="button" className="ghost" disabled={!folderBrowser.parent || folderBrowser.loading} onClick={() => browseDirs(folderBrowser.parent)}>↑ Up</button>
              <input
                style={{ flex: 1 }}
                value={folderBrowser.path}
                onChange={(e) => setFolderBrowser((s) => ({ ...s, path: e.target.value }))}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); browseDirs(folderBrowser.path); } }}
              />
              <button type="button" className="ghost" onClick={() => browseDirs(folderBrowser.path)}>Go</button>
            </div>
            <div style={{ flex: 1, overflow: "auto", padding: "0 16px", minHeight: 160 }}>
              {folderBrowser.loading ? (
                <div className="note" style={{ padding: 12 }}>Loading…</div>
              ) : folderBrowser.error ? (
                <div className="note" style={{ padding: 12, color: "var(--danger, #c00)" }}>{folderBrowser.error}</div>
              ) : folderBrowser.entries.length === 0 ? (
                <div className="note" style={{ padding: 12 }}>No sub-folders here.</div>
              ) : (
                folderBrowser.entries.map((e) => (
                  <div
                    key={e.path}
                    onClick={() => browseDirs(e.path)}
                    style={{ padding: "7px 8px", cursor: "pointer", borderRadius: 6, display: "flex", gap: 8, alignItems: "center" }}
                    onMouseEnter={(ev) => (ev.currentTarget.style.background = "var(--panel-2, #f0f0f0)")}
                    onMouseLeave={(ev) => (ev.currentTarget.style.background = "transparent")}
                  >
                    <span>📁</span><span>{e.name}</span>
                  </div>
                ))
              )}
            </div>
            <div style={{ padding: "12px 16px", borderTop: "1px solid var(--border, #ddd)", display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button type="button" className="ghost" onClick={() => setFolderBrowser((s) => ({ ...s, open: false }))}>Cancel</button>
              <button type="button" onClick={chooseFolder} disabled={folderBrowser.loading || !folderBrowser.path}>Select this folder</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
