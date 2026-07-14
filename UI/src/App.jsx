import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { Terminal, Briefcase, Play, ExternalLink, Loader2 } from 'lucide-react';

const API_BASE = 'http://localhost:8000';

function App() {
  const [isRunning, setIsRunning] = useState(false);
  const [logs, setLogs] = useState([]);
  const [jobs, setJobs] = useState([]);
  const logsEndRef = useRef(null);

  // Auto-scroll logs
  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  // Initial jobs fetch
  useEffect(() => {
    fetchJobs();
  }, []);

  const fetchJobs = async () => {
    try {
      const res = await axios.get(`${API_BASE}/jobs`);
      setJobs(res.data.jobs || []);
    } catch (err) {
      console.error("Error fetching jobs:", err);
    }
  };

  const startExtraction = async () => {
    setIsRunning(true);
    setLogs([]); // Clear previous logs
    
    try {
      const res = await axios.post(`${API_BASE}/extract`);
      if (res.data.status === 'success') {
        listenToLogs();
      } else {
        setLogs(prev => [...prev, `[ERROR] ${res.data.message}`]);
        setIsRunning(false);
      }
    } catch (err) {
      setLogs(prev => [...prev, `[ERROR] Failed to connect to server.`]);
      setIsRunning(false);
    }
  };

  const listenToLogs = () => {
    const eventSource = new EventSource(`${API_BASE}/logs`);
    
    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);
      
      if (data.status === 'completed') {
        eventSource.close();
        setIsRunning(false);
        fetchJobs(); // Refresh jobs at the end
      } else if (data.log) {
        setLogs(prev => [...prev, data.log]);
        
        // Optimistically parse logs for new jobs if it looks like a save message
        if (data.log.includes("Saved Naukri:") || data.log.includes("Saved LinkedIn:")) {
            fetchJobs(); // Fetch latest jobs whenever a save happens
        }
      }
    };

    eventSource.onerror = (err) => {
      console.error("EventSource failed:", err);
      eventSource.close();
      setIsRunning(false);
    };
  };

  return (
    <div className="app-container">
      <header className="header">
        <h1>
          <div style={{ background: 'linear-gradient(135deg, var(--accent), #8b5cf6)', padding: '8px', borderRadius: '8px', display: 'flex' }}>
            <Briefcase size={24} color="white" />
          </div>
          Job Scraper Control Panel
        </h1>
        
        <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
          <div className="status-indicator">
            <div className={`dot ${isRunning ? 'active' : ''}`}></div>
            {isRunning ? 'Scraping in progress...' : 'System Idle'}
          </div>
          <button 
            className="btn-primary"
            onClick={startExtraction}
            disabled={isRunning}
          >
            {isRunning ? <Loader2 size={18} className="animate-spin" /> : <Play size={18} />}
            {isRunning ? 'Extracting...' : 'Start Extraction'}
          </button>
        </div>
      </header>

      <div className="main-grid">
        {/* Terminal Panel */}
        <div className="panel">
          <div className="panel-header">
            <Terminal size={20} className="text-muted" />
            Live Execution Logs
          </div>
          <div className="terminal-container">
            {logs.length === 0 ? (
              <div style={{ color: 'var(--text-muted)', fontStyle: 'italic', opacity: 0.5 }}>
                Waiting for extraction to start...
              </div>
            ) : (
              logs.map((log, i) => (
                <div key={i} className={`log-line ${log.toLowerCase().includes('error') ? 'error' : log.includes('Saved') ? 'info' : ''}`}>
                  {log}
                </div>
              ))
            )}
            <div ref={logsEndRef} />
          </div>
        </div>

        {/* Jobs Panel */}
        <div className="panel">
          <div className="panel-header">
            <Briefcase size={20} className="text-muted" />
            Extracted Jobs ({jobs.length})
          </div>
          <div className="jobs-list">
            {jobs.length === 0 ? (
              <div style={{ textAlign: 'center', color: 'var(--text-muted)', marginTop: '2rem' }}>
                No jobs extracted yet.
              </div>
            ) : (
              [...jobs].reverse().map((job, i) => (
                <div key={i} className="job-card">
                  <div className="job-title">
                    {job.title}
                    <span style={{ fontSize: '0.8rem', opacity: 0.6, fontWeight: 'normal' }}>
                      {new Date(job.scraped_at).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}
                    </span>
                  </div>
                  <div className="job-company">{job.company} • {job.location || 'India'}</div>
                  <div className="job-meta">
                    <span className="badge" style={{ background: 'rgba(16, 185, 129, 0.1)', color: '#34d399', borderColor: 'rgba(16, 185, 129, 0.2)' }}>
                      {job.source.toUpperCase()}
                    </span>
                    <span className="badge">
                      {job.category || 'General'}
                    </span>
                    {job.experience && <span className="badge">{job.experience}</span>}
                  </div>
                  <a href={job.apply_link} target="_blank" rel="noopener noreferrer" className="job-link">
                    View & Apply <ExternalLink size={14} />
                  </a>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
