import axios from 'axios';

export const API_BASE =
  import.meta.env.VITE_API_BASE?.replace(/\/$/, '') || 'http://localhost:8000';

const client = axios.create({ baseURL: API_BASE, timeout: 20000 });

/** Turn filter state into repeated query params (?skill=Python&skill=AWS). */
export function toParams(filters, page, pageSize) {
  const params = new URLSearchParams();
  const add = (key, value) => {
    if (value === undefined || value === null || value === '') return;
    if (Array.isArray(value)) value.forEach((v) => v && params.append(key, v));
    else params.append(key, value);
  };

  add('q', filters.q);
  add('location', filters.location);
  add('category', filters.category);
  add('source', filters.source);
  add('skill', filters.skills);
  add('seniority', filters.seniority);
  add('work_mode', filters.workMode);
  add('employment_type', filters.employmentType);
  add('country', filters.country);
  add('is_abroad', filters.isAbroad);
  add('masters_match', filters.mastersMatch);
  add('education_requirement', filters.educationRequirement);
  add('visa_sponsorship', filters.visaSponsorship);
  add('relocation_support', filters.relocationSupport);
  add('work_authorization_required', filters.workAuthorizationRequired);
  add('max_experience', filters.maxExperience);
  add('posted_within_days', filters.postedWithinDays);
  add('sort', filters.sort);
  add('order', filters.order);
  params.append('page', page);
  params.append('page_size', pageSize);
  return params;
}

export const api = {
  jobs: (filters, page, pageSize, signal) =>
    client
      .get('/jobs', { params: toParams(filters, page, pageSize), signal })
      .then((r) => r.data),
  job: (id) => client.get(`/jobs/${id}`).then((r) => r.data),
  filters: () => client.get('/filters').then((r) => r.data),
  stats: () => client.get('/stats').then((r) => r.data),
  meta: () => client.get('/meta').then((r) => r.data),
  runs: (limit = 5) =>
    client.get('/scrape/runs', { params: { limit } }).then((r) => r.data),
  scrapeProfiles: () => client.get('/scrape/profiles').then((r) => r.data),
  startScrape: (body = {}) => client.post('/scrape/run', body).then((r) => r.data),
  streamUrl: (runId) => `${API_BASE}/scrape/runs/${runId}/stream`,
};

export default api;
