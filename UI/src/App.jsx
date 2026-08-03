import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  Briefcase,
  ChevronLeft,
  ChevronRight,
  Clock,
  Database,
  ExternalLink,
  Loader2,
  MapPin,
  Play,
  Search,
  Terminal,
  X,
} from 'lucide-react';
import api, { API_BASE } from './api';

const PAGE_SIZE = 20;

const EMPTY_FILTERS = {
  q: '',
  category: '',
  source: '',
  skills: [],
  seniority: '',
  workMode: '',
  maxExperience: '',
  postedWithinDays: '',
  sort: 'posted_at',
  order: 'desc',
};

function relativeTime(iso) {
  if (!iso) return 'unknown';
  const diffMs = Date.now() - new Date(iso).getTime();
  const days = Math.floor(diffMs / 86400000);
  if (days > 30) return `${Math.floor(days / 30)}mo ago`;
  if (days >= 1) return `${days}d ago`;
  const hours = Math.floor(diffMs / 3600000);
  if (hours >= 1) return `${hours}h ago`;
  return 'just now';
}

/* ------------------------------------------------------------------ pieces */

function StatTile({ icon: Icon, label, value }) {
  return (
    <div className="stat-tile">
      <Icon size={18} className="stat-icon" />
      <div>
        <div className="stat-value">{value}</div>
        <div className="stat-label">{label}</div>
      </div>
    </div>
  );
}

function Select({ label, value, onChange, options, allLabel }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{allLabel}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.value} {option.count != null ? `(${option.count})` : ''}
          </option>
        ))}
      </select>
    </label>
  );
}

function JobCard({ job }) {
  return (
    <article className="job-card">
      <header className="job-card-head">
        <h3>{job.title}</h3>
        <span className="job-age">
          <Clock size={12} /> {relativeTime(job.posted_at || job.first_seen_at)}
        </span>
      </header>

      <p className="job-company">
        {job.company}
        {job.location ? (
          <>
            {' '}
            <MapPin size={12} /> {job.location}
          </>
        ) : null}
      </p>

      <div className="badges">
        <span className={`badge badge-source badge-${job.source}`}>{job.source}</span>
        <span className="badge">{job.category}</span>
        <span className="badge">{job.seniority}</span>
        {job.work_mode !== 'onsite' && <span className="badge">{job.work_mode}</span>}
        {job.experience_text && <span className="badge">{job.experience_text}</span>}
        {job.salary_text && <span className="badge">{job.salary_text}</span>}
      </div>

      {job.skills?.length > 0 && (
        <div className="skills">
          {job.skills.slice(0, 8).map((skill) => (
            <span key={skill} className="skill">
              {skill}
            </span>
          ))}
          {job.skills.length > 8 && <span className="skill muted">+{job.skills.length - 8}</span>}
        </div>
      )}

      <a href={job.apply_link} target="_blank" rel="noopener noreferrer" className="job-link">
        View &amp; apply <ExternalLink size={13} />
      </a>
    </article>
  );
}

/** Live log panel. Only rendered while a run is active or just finished. */
function RunConsole({ runId, onFinished }) {
  const [lines, setLines] = useState([]);
  const [progress, setProgress] = useState(0);
  const endRef = useRef(null);

  useEffect(() => {
    if (!runId) return undefined;

    setLines([]);
    setProgress(0);
    const source = new EventSource(api.streamUrl(runId));

    source.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'progress') {
        setProgress(data.percent);
      } else if (data.type === 'log') {
        setLines((prev) => [...prev.slice(-400), data]);
      } else if (data.type === 'done') {
        source.close();
        onFinished(data);
      }
    };

    source.onerror = () => {
      source.close();
      onFinished({ status: 'disconnected' });
    };

    return () => source.close();
  }, [runId, onFinished]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [lines]);

  return (
    <section className="panel console-panel">
      <div className="panel-head">
        <Terminal size={17} />
        <span>Run #{runId}</span>
        <div className="progress-track">
          <div className="progress-bar" style={{ width: `${progress}%` }} />
        </div>
        <span className="progress-text">{progress}%</span>
      </div>
      <div className="console">
        {lines.length === 0 && <div className="console-idle">Waiting for output…</div>}
        {lines.map((line, index) => (
          <div key={index} className={`console-line ${line.level}`}>
            {line.message}
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </section>
  );
}

/* --------------------------------------------------------------------- app */

export default function App() {
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [debouncedQ, setDebouncedQ] = useState('');
  const [page, setPage] = useState(1);

  const [jobs, setJobs] = useState([]);
  const [meta, setMeta] = useState(null);
  const [facets, setFacets] = useState(null);
  const [stats, setStats] = useState(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [runId, setRunId] = useState(null);
  const [starting, setStarting] = useState(false);

  // Debounce the search box so typing does not fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQ(filters.q), 350);
    return () => clearTimeout(timer);
  }, [filters.q]);

  const activeFilters = useMemo(() => ({ ...filters, q: debouncedQ }), [filters, debouncedQ]);

  const loadJobs = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.jobs(activeFilters, page, PAGE_SIZE);
      setJobs(data.items);
      setMeta(data.meta);
      setError('');
    } catch (err) {
      setError(
        err.response
          ? `API error ${err.response.status}`
          : `Cannot reach the API. Is it running on ${API_BASE}?`,
      );
    } finally {
      setLoading(false);
    }
  }, [activeFilters, page]);

  const loadSidebars = useCallback(async () => {
    try {
      const [facetData, statData] = await Promise.all([api.filters(), api.stats()]);
      setFacets(facetData);
      setStats(statData);
    } catch {
      /* the jobs list already surfaces connectivity problems */
    }
  }, []);

  useEffect(() => {
    loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    loadSidebars();
  }, [loadSidebars]);

  // Reset to page 1 whenever the result set changes shape.
  useEffect(() => {
    setPage(1);
  }, [
    debouncedQ,
    filters.category,
    filters.source,
    filters.seniority,
    filters.workMode,
    filters.maxExperience,
    filters.postedWithinDays,
    filters.skills,
  ]);

  // Reattach to a scrape that is already running (e.g. after a page refresh).
  useEffect(() => {
    api
      .runs(3)
      .then((runs) => {
        const active = runs.find((run) => run.status === 'running');
        if (active) setRunId(active.id);
      })
      .catch(() => {});
  }, []);

  const update = (key, value) => setFilters((prev) => ({ ...prev, [key]: value }));

  const toggleSkill = (skill) =>
    setFilters((prev) => ({
      ...prev,
      skills: prev.skills.includes(skill)
        ? prev.skills.filter((s) => s !== skill)
        : [...prev.skills, skill],
    }));

  const startScrape = async () => {
    setStarting(true);
    try {
      const { run_id: id } = await api.startScrape({});
      setRunId(id);
    } catch (err) {
      const status = err.response?.status;
      setError(
        status === 409
          ? 'A scrape is already running.'
          : status === 401
            ? 'Scraping requires an admin token (set VITE_ADMIN_TOKEN).'
            : 'Could not start the scrape.',
      );
    } finally {
      setStarting(false);
    }
  };

  const onRunFinished = useCallback(() => {
    loadJobs();
    loadSidebars();
  }, [loadJobs, loadSidebars]);

  const hasFilters =
    debouncedQ ||
    filters.category ||
    filters.source ||
    filters.seniority ||
    filters.workMode ||
    filters.maxExperience ||
    filters.postedWithinDays ||
    filters.skills.length > 0;

  return (
    <div className="app">
      <header className="topbar">
        <h1>
          <span className="logo">
            <Briefcase size={20} />
          </span>
          Tech Job Explorer
        </h1>

        <div className="topbar-actions">
          {stats && (
            <div className="stat-row">
              <StatTile icon={Database} label="active jobs" value={stats.active_jobs} />
              <StatTile icon={Clock} label="added today" value={stats.jobs_added_today} />
            </div>
          )}
          <button className="btn" onClick={startScrape} disabled={starting || !!runId}>
            {starting ? <Loader2 size={16} className="spin" /> : <Play size={16} />}
            {runId ? 'Scraping…' : 'Refresh now'}
          </button>
        </div>
      </header>

      {error && (
        <div className="alert">
          <AlertCircle size={16} /> {error}
        </div>
      )}

      {runId && <RunConsole runId={runId} onFinished={onRunFinished} />}

      <div className="layout">
        <aside className="sidebar">
          <label className="field search-field">
            <Search size={15} />
            <input
              type="search"
              placeholder="Search title, company, skills…"
              value={filters.q}
              onChange={(e) => update('q', e.target.value)}
            />
          </label>

          <Select
            label="Category"
            allLabel="All categories"
            value={filters.category}
            onChange={(v) => update('category', v)}
            options={facets?.categories || []}
          />
          <Select
            label="Source"
            allLabel="All sources"
            value={filters.source}
            onChange={(v) => update('source', v)}
            options={facets?.sources || []}
          />
          <Select
            label="Seniority"
            allLabel="Any level"
            value={filters.seniority}
            onChange={(v) => update('seniority', v)}
            options={facets?.seniorities || []}
          />
          <Select
            label="Work mode"
            allLabel="Anywhere"
            value={filters.workMode}
            onChange={(v) => update('workMode', v)}
            options={facets?.work_modes || []}
          />

          <label className="field">
            <span className="field-label">Max experience</span>
            <select
              value={filters.maxExperience}
              onChange={(e) => update('maxExperience', e.target.value)}
            >
              <option value="">Any</option>
              <option value="0">Fresher only (0 yrs)</option>
              <option value="1">Up to 1 year</option>
              <option value="2">Up to 2 years</option>
              <option value="3">Up to 3 years</option>
            </select>
          </label>

          <label className="field">
            <span className="field-label">Posted within</span>
            <select
              value={filters.postedWithinDays}
              onChange={(e) => update('postedWithinDays', e.target.value)}
            >
              <option value="">Any time</option>
              <option value="1">Last 24 hours</option>
              <option value="7">Last 7 days</option>
              <option value="30">Last 30 days</option>
            </select>
          </label>

          {facets?.skills?.length > 0 && (
            <div className="field">
              <span className="field-label">Skills</span>
              <div className="skill-cloud">
                {facets.skills.slice(0, 30).map((skill) => (
                  <button
                    key={skill.value}
                    type="button"
                    className={`skill-chip ${filters.skills.includes(skill.value) ? 'on' : ''}`}
                    onClick={() => toggleSkill(skill.value)}
                  >
                    {skill.value}
                    <em>{skill.count}</em>
                  </button>
                ))}
              </div>
            </div>
          )}

          {hasFilters && (
            <button className="btn ghost" onClick={() => setFilters(EMPTY_FILTERS)}>
              <X size={14} /> Clear filters
            </button>
          )}
        </aside>

        <main className="results">
          <div className="results-head">
            <span>
              {loading ? 'Loading…' : `${meta?.total ?? 0} job${meta?.total === 1 ? '' : 's'}`}
            </span>
            <select value={filters.sort} onChange={(e) => update('sort', e.target.value)}>
              <option value="posted_at">Newest posted</option>
              <option value="first_seen_at">Recently discovered</option>
              <option value="experience">Least experience</option>
              <option value="company">Company A–Z</option>
            </select>
          </div>

          {!loading && jobs.length === 0 && (
            <div className="empty">
              <Briefcase size={28} />
              <p>No jobs match these filters.</p>
              {!stats?.active_jobs && <p>Run a scrape to populate the database.</p>}
            </div>
          )}

          <div className="job-grid">
            {jobs.map((job) => (
              <JobCard key={job.id} job={job} />
            ))}
          </div>

          {meta && meta.total_pages > 1 && (
            <nav className="pager">
              <button className="btn ghost" disabled={!meta.has_prev} onClick={() => setPage(page - 1)}>
                <ChevronLeft size={15} /> Previous
              </button>
              <span>
                Page {meta.page} of {meta.total_pages}
              </span>
              <button className="btn ghost" disabled={!meta.has_next} onClick={() => setPage(page + 1)}>
                Next <ChevronRight size={15} />
              </button>
            </nav>
          )}
        </main>
      </div>
    </div>
  );
}
