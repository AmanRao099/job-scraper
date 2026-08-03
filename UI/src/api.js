import axios from 'axios';

export const API_BASE =
  import.meta.env.VITE_API_BASE?.replace(/\/$/, '') || 'http://localhost:8000';

// Set VITE_ADMIN_TOKEN only for a local/private dashboard. A token shipped in a
// public bundle is readable by anyone; for a public site, proxy /scrape/run
// through your own backend instead.
const ADMIN_TOKEN = import.meta.env.VITE_ADMIN_TOKEN || '';

const client = axios.create({ baseURL: API_BASE, timeout: 20000 });

if (ADMIN_TOKEN) {
  client.defaults.headers.common['X-Admin-Token'] = ADMIN_TOKEN;
}

/** Turn filter state into repeated query params (?skill=Python&skill=AWS). */
function toParams(filters, page, pageSize) {
  const params = new URLSearchParams();
  const add = (key, value) => {
    if (value === undefined || value === null || value === '') return;
    if (Array.isArray(value)) value.forEach((v) => v && params.append(key, v));
    else params.append(key, value);
  };

  add('q', filters.q);
  add('category', filters.category);
  add('source', filters.source);
  add('skill', filters.skills);
  add('seniority', filters.seniority);
  add('work_mode', filters.workMode);
  add('max_experience', filters.maxExperience);
  add('posted_within_days', filters.postedWithinDays);
  add('sort', filters.sort);
  add('order', filters.order);
  params.append('page', page);
  params.append('page_size', pageSize);
  return params;
}

export const api = {
  jobs: (filters, page, pageSize) =>
    client.get('/jobs', { params: toParams(filters, page, pageSize) }).then((r) => r.data),
  job: (id) => client.get(`/jobs/${id}`).then((r) => r.data),
  filters: () => client.get('/filters').then((r) => r.data),
  stats: () => client.get('/stats').then((r) => r.data),
  meta: () => client.get('/meta').then((r) => r.data),
  runs: (limit = 5) =>
    client.get('/scrape/runs', { params: { limit } }).then((r) => r.data),
  startScrape: (body = {}) => client.post('/scrape/run', body).then((r) => r.data),
  streamUrl: (runId) => `${API_BASE}/scrape/runs/${runId}/stream`,
};

export default api;
