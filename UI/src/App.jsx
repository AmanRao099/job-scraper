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
  Rocket,
  Search,
  Terminal,
  X,
} from 'lucide-react';
import api, { API_BASE } from './api';

const PAGE_SIZE = 20;

const EMPTY_FILTERS = {
  q: '',
  location: '',
  category: '',
  source: '',
  skills: [],
  seniority: '',
  workMode: '',
  employmentType: '',
  country: '',
  isAbroad: '',
  mastersMatch: '',
  educationRequirement: '',
  visaSponsorship: '',
  relocationSupport: '',
  workAuthorizationRequired: '',
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
        {job.country && <span className="badge">{job.country}</span>}
        {job.is_abroad && <span className="badge badge-international">International</span>}
        {job.masters_match && (
          <span className="badge badge-masters">Masters · {job.education_requirement}</span>
        )}
        {job.employment_type !== 'unknown' && (
          <span className="badge">{job.employment_type.replace('_', ' ')}</span>
        )}
        <span className="badge">Visa · {job.visa_sponsorship.replace('_', ' ')}</span>
        <span className="badge">Relocation · {job.relocation_support.replace('_', ' ')}</span>
        {job.work_authorization_required && <span className="badge">Work authorization required</span>}
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
        <div
          className="progress-track"
          role="progressbar"
          aria-label="Scrape progress"
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow={progress}
        >
          <div className="progress-bar" style={{ width: `${progress}%` }} />
        </div>
        <span className="progress-text">{progress}%</span>
      </div>
      <div className="console" role="log" aria-live="polite">
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
  const [debouncedLocation, setDebouncedLocation] = useState('');
  const [page, setPage] = useState(1);

  const [jobs, setJobs] = useState([]);
  const [meta, setMeta] = useState(null);
  const [facets, setFacets] = useState(null);
  const [stats, setStats] = useState(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [runId, setRunId] = useState(null);
  const [starting, setStarting] = useState(null);
  const [profiles, setProfiles] = useState([]);
  const requestSequence = useRef(0);

  // Debounce the text boxes so typing does not fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQ(filters.q);
      setDebouncedLocation(filters.location);
    }, 350);
    return () => clearTimeout(timer);
  }, [filters.q, filters.location]);

  const activeFilters = useMemo(
    () => ({ ...filters, q: debouncedQ, location: debouncedLocation }),
    [filters, debouncedQ, debouncedLocation],
  );

  const loadJobs = useCallback(async (signal) => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    try {
      const data = await api.jobs(activeFilters, page, PAGE_SIZE, signal);
      if (sequence !== requestSequence.current) return;
      setJobs(data.items);
      setMeta(data.meta);
      setError('');
    } catch (err) {
      if (err.code === 'ERR_CANCELED' || sequence !== requestSequence.current) return;
      setError(
        err.response
          ? `API error ${err.response.status}`
          : `Cannot reach the API. Is it running on ${API_BASE}?`,
      );
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
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
    const controller = new AbortController();
    loadJobs(controller.signal);
    return () => controller.abort();
  }, [loadJobs]);

  useEffect(() => {
    loadSidebars();
  }, [loadSidebars]);

  // Reset to page 1 whenever the result set changes shape.
  useEffect(() => {
    setPage(1);
  }, [
    debouncedQ,
    debouncedLocation,
    filters.category,
    filters.source,
    filters.seniority,
    filters.workMode,
    filters.employmentType,
    filters.country,
    filters.isAbroad,
    filters.mastersMatch,
    filters.educationRequirement,
    filters.visaSponsorship,
    filters.relocationSupport,
    filters.workAuthorizationRequired,
    filters.maxExperience,
    filters.postedWithinDays,
    filters.skills,
  ]);

  // Targeted scrapes are defined server-side, so render whatever it offers
  // rather than hard-coding one button per profile here.
  useEffect(() => {
    api
      .scrapeProfiles()
      .then(setProfiles)
      .catch(() => {});
  }, []);

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

  /** `profileKey` runs a targeted profile; omit it for the nationwide sweep. */
  const startScrape = async (profileKey = null) => {
    setStarting(profileKey ?? 'all');
    try {
      const { run_id: id } = await api.startScrape(profileKey ? { profile: profileKey } : {});
      setRunId(id);
    } catch (err) {
      const status = err.response?.status;
      setError(
        status === 409
          ? 'A scrape is already running.'
          : status === 401
            ? 'Scraping is restricted to an authenticated admin client.'
            : 'Could not start the scrape.',
      );
    } finally {
      setStarting(null);
    }
  };

  const onRunFinished = useCallback((result) => {
    setRunId(null);
    if (result?.status === 'disconnected') {
      setError('The live scrape connection was interrupted. Retry or check run status later.');
    }
    loadJobs();
    loadSidebars();
  }, [loadJobs, loadSidebars]);

  const hasFilters =
    debouncedQ ||
    debouncedLocation ||
    filters.category ||
    filters.source ||
    filters.seniority ||
    filters.workMode ||
    filters.employmentType ||
    filters.country ||
    filters.isAbroad ||
    filters.mastersMatch ||
    filters.educationRequirement ||
    filters.visaSponsorship ||
    filters.relocationSupport ||
    filters.workAuthorizationRequired ||
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
          <button className="btn" onClick={() => startScrape()} disabled={!!starting || !!runId}>
            {starting === 'all' ? <Loader2 size={16} className="spin" /> : <Play size={16} />}
            {runId ? 'Scraping…' : 'Refresh now'}
          </button>

          {profiles.map((profile) => (
            <button
              key={profile.key}
              className="btn focus"
              title={profile.description}
              onClick={() => startScrape(profile.key)}
              disabled={!!starting || !!runId}
            >
              {starting === profile.key ? (
                <Loader2 size={16} className="spin" />
              ) : (
                <Rocket size={16} />
              )}
              {profile.label}
            </button>
          ))}
        </div>
      </header>

      {error && (
        <div className="alert" role="alert">
          <AlertCircle size={16} /> {error}
          <button type="button" className="btn ghost" onClick={() => loadJobs()}>
            Retry
          </button>
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

          <label className="field">
            <span className="field-label">Location</span>
            <input
              type="search"
              placeholder="e.g. Bengaluru"
              value={filters.location}
              onChange={(e) => update('location', e.target.value)}
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
          <Select
            label="Employment type"
            allLabel="Any type"
            value={filters.employmentType}
            onChange={(v) => update('employmentType', v)}
            options={facets?.employment_types || []}
          />
          <Select
            label="Country"
            allLabel="All countries"
            value={filters.country}
            onChange={(v) => update('country', v)}
            options={facets?.countries || []}
          />
          <label className="field">
            <span className="field-label">International</span>
            <select value={filters.isAbroad} onChange={(e) => update('isAbroad', e.target.value)}>
              <option value="">All jobs</option>
              <option value="true">International only</option>
              <option value="false">India / unknown only</option>
            </select>
          </label>
          <label className="field">
            <span className="field-label">Masters qualification</span>
            <select
              value={filters.mastersMatch}
              onChange={(e) => update('mastersMatch', e.target.value)}
            >
              <option value="">Any</option>
              <option value="true">Masters mentioned</option>
              <option value="false">Not stated</option>
            </select>
          </label>
          <Select
            label="Education status"
            allLabel="Any status"
            value={filters.educationRequirement}
            onChange={(v) => update('educationRequirement', v)}
            options={facets?.education_requirements || []}
          />
          <Select
            label="Visa sponsorship"
            allLabel="Any status"
            value={filters.visaSponsorship}
            onChange={(v) => update('visaSponsorship', v)}
            options={facets?.visa_sponsorships || []}
          />
          <Select
            label="Relocation support"
            allLabel="Any status"
            value={filters.relocationSupport}
            onChange={(v) => update('relocationSupport', v)}
            options={facets?.relocation_supports || []}
          />
          <label className="field">
            <span className="field-label">Work authorization</span>
            <select
              value={filters.workAuthorizationRequired}
              onChange={(e) => update('workAuthorizationRequired', e.target.value)}
            >
              <option value="">Any</option>
              <option value="true">Requirement stated</option>
              <option value="false">Not detected</option>
            </select>
          </label>

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
            <nav className="pager" aria-label="Job result pages">
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
