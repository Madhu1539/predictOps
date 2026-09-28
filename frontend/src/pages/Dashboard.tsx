import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend, AreaChart, Area } from 'recharts';
import { getMachines, getAlerts, getOee, updateAlert, getWorkOrders, getImpact, getIngestStatus, subscribeToUpdates, createWorkOrder } from '../services/api';
import type { Machine, Alert, OeeDashboard, WorkOrder, Impact, IngestStatus } from '../types';
import { LoadingState, ErrorState, EmptyState } from '../components/States';
import TriageRow from '../components/TriageRow';

/* ── Colour helpers ─────────────────────────────────────────── */
const SEV_COLOR: Record<string, string>  = { Critical:'#ef4444', Warning:'#f59e0b', Watch:'#eab308', Normal:'#10b981' };
const SEV_BG:    Record<string, string>  = { Critical:'rgba(239,68,68,.1)', Warning:'rgba(245,158,11,.1)', Watch:'rgba(234,179,8,.1)', Normal:'rgba(16,185,129,.1)' };
const SEV_BORDER:Record<string, string>  = { Critical:'rgba(239,68,68,.25)', Warning:'rgba(245,158,11,.25)', Watch:'rgba(234,179,8,.2)', Normal:'rgba(16,185,129,.2)' };
const riskColor = (r: number) => r>=75 ? '#ef4444' : r>=45 ? '#f59e0b' : r>=25 ? '#eab308' : '#10b981';
const CRIT_COLOR: Record<string, string> = { High:'#ef4444', Medium:'#f59e0b', Low:'#10b981' };

/** Compact currency, e.g. $233k. Values are modelled estimates. */
const fmtMoney = (v?: number) => {
  if (v === undefined || v === null) return '—';
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${Math.round(v / 1_000)}k`;
  return `$${Math.round(v)}`;
};

/* ── Stat Card ──────────────────────────────────────────────── */
const StatCard: React.FC<{ label: string; value: string|number; sub?: string; accent: string; icon: React.ReactNode }> = ({ label, value, sub, accent, icon }) => (
  <div style={{ background:'#111827', border:'1px solid #1e2d45', borderRadius:12, padding:'16px 18px', display:'flex', flexDirection:'column', gap:10 }}>
    <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
      <span style={{ fontSize:11, color:'#64748b', fontWeight:600, letterSpacing:'0.5px', textTransform:'uppercase' }}>{label}</span>
      <div style={{ color:accent, opacity:.7 }}>{icon}</div>
    </div>
    <div>
      <div style={{ fontSize:28, fontWeight:800, color:'#f1f5f9', lineHeight:1 }}>{value}</div>
      {sub && <div style={{ fontSize:11, color:'#64748b', marginTop:5 }}>{sub}</div>}
    </div>
    <div style={{ height:3, borderRadius:2, background:'#1a2234' }}>
      <div style={{ height:'100%', borderRadius:2, background:accent, width:typeof value==='number' ? `${Math.min(value,100)}%` : '60%' }} />
    </div>
  </div>
);

/* ── OEE Ring ───────────────────────────────────────────────── */
const OEERing: React.FC<{ value: number }> = ({ value }) => {
  const r = 52, c = 2*Math.PI*r, pct = Math.min(value,100);
  const color = pct>=80 ? '#10b981' : pct>=65 ? '#f59e0b' : '#ef4444';
  const label = pct>=80 ? 'World Class' : pct>=65 ? 'Acceptable' : 'Critical';
  return (
    <div style={{ display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center', gap:6, padding:'16px 0' }}>
      <div style={{ position:'relative', display:'inline-block' }}>
        <svg width={130} height={130} style={{ transform:'rotate(-90deg)' }}>
          <circle cx={65} cy={65} r={r} stroke="#1a2234" strokeWidth={10} fill="none" />
          <circle cx={65} cy={65} r={r} stroke={color} strokeWidth={10} fill="none"
            strokeDasharray={c} strokeDashoffset={c - (pct/100)*c}
            strokeLinecap="round" style={{ transition:'stroke-dashoffset 1s ease, stroke .5s' }} />
        </svg>
        <div style={{ position:'absolute', inset:0, display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center' }}>
          <span style={{ fontSize:22, fontWeight:800, color:'#f1f5f9' }}>{value.toFixed(1)}%</span>
        </div>
      </div>
      <div style={{ textAlign:'center' }}>
        <div style={{ fontSize:13, fontWeight:700, color:'#f1f5f9' }}>Plant OEE</div>
        <div style={{ fontSize:11, color, fontWeight:600, marginTop:2 }}>{label}</div>
      </div>
    </div>
  );
};

/* ── Machine Risk Row ───────────────────────────────────────── */
const MachineRow: React.FC<{ machine: Machine; rank: number }> = ({ machine, rank }) => {
  const navigate = useNavigate();
  const sev = machine.severity || 'Normal';
  const risk = machine.latest_risk ?? 0;
  const color = riskColor(risk);
  return (
    <div onClick={() => navigate(`/machines/${machine.id}`)} style={{ display:'flex', alignItems:'center', gap:12, padding:'10px 14px', borderRadius:10, background:'#111827', border:`1px solid ${risk>=75 ? SEV_BORDER.Critical : '#1e2d45'}`, cursor:'pointer', transition:'all .15s' }}
      onMouseEnter={e => { (e.currentTarget as HTMLDivElement).style.background='#1a2234'; (e.currentTarget as HTMLDivElement).style.borderColor='#263551'; }}
      onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.background='#111827'; (e.currentTarget as HTMLDivElement).style.borderColor=risk>=75?SEV_BORDER.Critical:'#1e2d45'; }}
    >
      {/* Rank */}
      <div style={{ width:22, height:22, borderRadius:6, background:'#1a2234', display:'flex', alignItems:'center', justifyContent:'center', fontSize:11, fontWeight:700, color:'#475569', flexShrink:0 }}>{rank}</div>

      {/* Pulse for Critical */}
      {sev === 'Critical' && <span style={{ width:7, height:7, borderRadius:'50%', background:'#ef4444', flexShrink:0, animation:'pulse-dot 1.5s infinite' }} />}

      {/* Name + type */}
      <div style={{ flex:1, minWidth:0 }}>
        <div style={{ fontSize:13, fontWeight:700, color:'#f1f5f9' }}>{machine.name}</div>
        <div style={{ fontSize:11, color:'#475569', marginTop:1, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis' }}>{machine.type} · {machine.location}</div>
      </div>

      {/* Risk bar */}
      <div style={{ width:100, flexShrink:0 }}>
        <div style={{ display:'flex', justifyContent:'space-between', marginBottom:4 }}>
          <span style={{ fontSize:10, color:'#64748b' }}>Risk</span>
          <span style={{ fontSize:11, fontWeight:700, color }}>{risk.toFixed(0)}%</span>
        </div>
        <div style={{ height:4, borderRadius:2, background:'#1a2234', overflow:'hidden' }}>
          <div style={{ height:'100%', borderRadius:2, background:color, width:`${Math.min(risk,100)}%`, transition:'width .5s', boxShadow: risk>=75 ? `0 0 6px ${color}` : undefined }} />
        </div>
      </div>

      {/* Priority + criticality (spec §42) */}
      <div style={{ width:74, flexShrink:0, textAlign:'right' }}>
        <div style={{ fontSize:11, fontWeight:700, color:'#f1f5f9' }}>
          {machine.maintenance_priority != null ? machine.maintenance_priority.toFixed(0) : '—'}
        </div>
        <div style={{ fontSize:9, color:'#475569', marginTop:1 }}>priority</div>
      </div>
      <div style={{ width:62, flexShrink:0, textAlign:'right' }}>
        <div style={{ fontSize:10, fontWeight:700, color:CRIT_COLOR[machine.criticality] || '#94a3b8', letterSpacing:'0.3px' }}>
          {machine.criticality.toUpperCase()}
        </div>
        <div style={{ fontSize:9, color:'#475569', marginTop:1 }}>criticality</div>
      </div>

      {/* Severity pill */}
      <div style={{ padding:'3px 8px', borderRadius:6, background:SEV_BG[sev], border:`1px solid ${SEV_BORDER[sev]}`, color:SEV_COLOR[sev], fontSize:10, fontWeight:700, letterSpacing:'0.4px', flexShrink:0 }}>
        {sev.toUpperCase()}
      </div>

      {/* Arrow */}
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" style={{ flexShrink:0 }}>
        <path d="M9 18l6-6-6-6"/>
      </svg>
    </div>
  );
};

/* ── Main Dashboard ─────────────────────────────────────────── */
type SevFilter = 'all' | 'Critical' | 'Warning' | 'Watch';
type StatusFilter = 'open' | 'Active' | 'Acknowledged' | 'all';

const Dashboard: React.FC = () => {
  const [machines, setMachines]     = useState<Machine[]>([]);
  const [alerts,   setAlerts]       = useState<Alert[]>([]);
  const [oee,      setOee]          = useState<OeeDashboard|null>(null);
  const [workOrders, setWorkOrders] = useState<WorkOrder[]>([]);
  const [impact,   setImpact]       = useState<Impact|null>(null);
  const [ingest,   setIngest]       = useState<IngestStatus|null>(null);
  const [loading,  setLoading]      = useState(true);
  const [error,    setError]        = useState<boolean>(false);
  const [lastUpdate, setLastUpdate] = useState<Date>(new Date());
  const [sevFilter, setSevFilter]   = useState<SevFilter>('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('open');
  const [busyAlert, setBusyAlert]   = useState<number|null>(null);
  const [actionNote, setActionNote] = useState<string|null>(null);

  const fetchData = useCallback(async () => {
    try {
      const [m, a, o, w, imp, ing] = await Promise.all([
        getMachines(), getAlerts(), getOee(), getWorkOrders(), getImpact(), getIngestStatus(),
      ]);
      setMachines(m); setAlerts(a); setOee(o); setWorkOrders(w);
      setImpact(imp); setIngest(ing);
      setError(false); setLastUpdate(new Date());
    } catch { setError(true); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    fetchData();
    // Poll stays as the fallback path; SSE just makes updates arrive sooner.
    const t = setInterval(fetchData, 15000);
    const unsubscribe = subscribeToUpdates(fetchData);
    return () => { clearInterval(t); unsubscribe(); };
  }, [fetchData]);

  const handleAck = async (id: number) => {
    setBusyAlert(id);
    try {
      await updateAlert(id, 'Acknowledged');
      setAlerts(prev => prev.map(a => a.id === id ? { ...a, status:'Acknowledged' } : a));
    } catch (e) {
      setActionNote(e instanceof Error ? e.message : 'Could not acknowledge that alert.');
    } finally { setBusyAlert(null); }
  };

  const handleDismiss = async (id: number) => {
    setBusyAlert(id);
    try {
      await updateAlert(id, 'Dismissed');
      // Dropped from local state rather than marked: a dismissed alert has left
      // the queue, and the default filter would hide it on the next fetch anyway.
      setAlerts(prev => prev.filter(a => a.id !== id));
    } catch (e) {
      setActionNote(e instanceof Error ? e.message : 'Could not dismiss that alert.');
    } finally { setBusyAlert(null); }
  };

  const handleCreateWorkOrder = async (alert: Alert) => {
    setBusyAlert(alert.id);
    try {
      const wo = await createWorkOrder({ alert_id: alert.id, machine_id: alert.machine_id });
      setWorkOrders(prev => [wo, ...prev]);
      setActionNote(
        `Work order raised for ${alert.machine_name || `M-${alert.machine_id}`}`
        + `${wo.technician ? ` · assigned to ${wo.technician}` : ''}`
        + `${wo.due_date ? ` · due ${new Date(wo.due_date).toLocaleDateString()}` : ''}`,
      );
    } catch (e) {
      setActionNote(e instanceof Error ? e.message : 'Could not create that work order.');
    } finally { setBusyAlert(null); }
  };

  if (loading) return <LoadingState message="Loading PredictOps data…" />;

  if (error) return <ErrorState onRetry={fetchData} />;

  if (!machines.length) return <EmptyState message="No machine data available." />;

  /* Derived */
  const criticalAlerts = alerts.filter(a=>a.severity==='Critical' && a.status==='Active');
  const warningAlerts  = alerts.filter(a=>a.severity==='Warning'  && a.status==='Active');
  const activeAlerts   = alerts.filter(a=>a.status==='Active');
  const highRisk       = machines.filter(m=>(m.latest_risk||0)>=75);
  const openWorkOrders = workOrders.filter(w=>w.status==='Open');
  const inProgressWorkOrders = workOrders.filter(w=>w.status==='InProgress');

  /* Machines that already have a work order awaiting action, so the queue does
     not offer to raise a second one. Matches the backend's duplicate guard. */
  const machinesWithOpenWo = new Set(
    workOrders.filter(w => w.status === 'Open' || w.status === 'InProgress').map(w => w.machine_id),
  );

  /* Triage queue. Ordered by maintenance priority (risk x criticality x
     production impact), NOT by arrival time: a chronological feed lets a Critical
     alert be pushed out of view by newer low-severity ones. */
  const triageQueue = alerts
    .filter(a => sevFilter === 'all' || a.severity === sevFilter)
    .filter(a => {
      if (statusFilter === 'all') return true;
      if (statusFilter === 'open') return a.status === 'Active' || a.status === 'Acknowledged';
      return a.status === statusFilter;
    })
    .sort((a, b) => {
      // Unactioned work first, then by priority, then by risk as the tie-break.
      if (a.status !== b.status) {
        if (a.status === 'Active') return -1;
        if (b.status === 'Active') return 1;
      }
      const pa = a.maintenance_priority ?? -1, pb = b.maintenance_priority ?? -1;
      if (pb !== pa) return pb - pa;
      return (b.risk_score ?? 0) - (a.risk_score ?? 0);
    });

  /* OEE trend — API returns 0-1 (spec §37); convert to % for display */
  const trendData = (oee?.trend || []).slice(-20).map(t => ({
    time: new Date(t.timestamp).toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit'}),
    OEE: +(t.oee * 100).toFixed(1),
    Avail: +(t.availability * 100).toFixed(1),
    Perf: +(t.performance * 100).toFixed(1),
    Qual: +(t.quality * 100).toFixed(1),
  }));

  /* Per-machine OEE — sort BEFORE slicing, or the panel shows the first eight
     machines in API order rather than the eight actually performing worst. */
  const machineOee = [...(oee?.per_machine || [])].sort((a,b)=>a.oee-b.oee).slice(0,8);

  return (
    <div className="animate-in" style={{ display:'flex', flexDirection:'column', gap:20 }}>

      {/* ── Top: Last updated ─────────────────────────────────── */}
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
        <div>
          <h1 style={{ fontSize:20, fontWeight:800, color:'#f1f5f9', letterSpacing:'-0.5px' }}>Command Center</h1>
          <p style={{ fontSize:12, color:'#475569', marginTop:3 }}>
            Last updated: {lastUpdate.toLocaleTimeString()}
            <span style={{ display:'inline-flex', alignItems:'center', gap:5, marginLeft:10, color:'#10b981', fontSize:11 }}>
              <span style={{ width:5, height:5, borderRadius:'50%', background:'#10b981', display:'inline-block', animation:'pulse-dot 2s infinite' }} />
              Live
            </span>
          </p>
        </div>
        <div style={{ display:'flex', alignItems:'center', gap:10 }}>
          {/* Data provenance — proves OT ingestion is real, not just simulated */}
          {ingest && (
            <div style={{ display:'flex', alignItems:'center', gap:6, padding:'6px 10px', borderRadius:8, background:'#111827', border:'1px solid #1e2d45' }}>
              <span style={{ fontSize:10, color:'#475569', fontWeight:600, textTransform:'uppercase', letterSpacing:'0.4px' }}>Feeds</span>
              {ingest.by_source.map(s => (
                <span key={s.source} title={`${s.readings.toLocaleString()} readings`}
                  style={{ fontSize:10, fontWeight:600, padding:'2px 6px', borderRadius:4,
                    color: s.source === 'simulator' ? '#64748b' : '#14b8a6',
                    background: s.source === 'simulator' ? 'rgba(100,116,139,.12)' : 'rgba(20,184,166,.12)' }}>
                  {s.source}
                </span>
              ))}
            </div>
          )}
          <button onClick={fetchData} style={{ display:'flex', alignItems:'center', gap:6, padding:'7px 14px', borderRadius:8, background:'#111827', border:'1px solid #1e2d45', color:'#94a3b8', cursor:'pointer', fontSize:12 }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>
            Refresh
          </button>
        </div>
      </div>

      {/* ── KPI strip ─────────────────────────────────────────── */}
      <div style={{ display:'grid', gridTemplateColumns:'130px repeat(5, 1fr)', gap:14 }}>
        {/* OEE ring */}
        <div style={{ background:'#111827', border:'1px solid #1e2d45', borderRadius:12 }}>
          <OEERing value={(oee?.plant_oee || 0) * 100} />
        </div>

        <StatCard label="Critical Alerts" value={criticalAlerts.length}
          sub={criticalAlerts.length ? 'Need immediate action' : 'All clear'}
          accent="#ef4444"
          icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>}
        />
        <StatCard label="Warning Alerts" value={warningAlerts.length}
          sub="Action recommended"
          accent="#f59e0b"
          icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>}
        />
        <StatCard label="High Risk Machines" value={`${highRisk.length}/${machines.length}`}
          sub={highRisk.length ? `${highRisk.map(m=>m.name).join(', ')}` : 'All machines healthy'}
          accent="#8b5cf6"
          icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>}
        />
        <StatCard label="Open Work Orders" value={openWorkOrders.length}
          sub={openWorkOrders.length ? `${inProgressWorkOrders.length} in progress` : 'No open work orders'}
          accent="#3b82f6"
          icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/><path d="M9 15h6"/></svg>}
        />
        <StatCard label="Value at Risk" value={fmtMoney(impact?.value_at_risk)}
          sub={impact ? `${impact.open_alert_count} open alerts · modelled` : 'modelled estimate'}
          accent="#14b8a6"
          icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/></svg>}
        />
      </div>

      {/* ── Triage queue ───────────────────────────────────────── */}
      <div style={{ display:'flex', flexDirection:'column', gap:10 }}>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap:12, flexWrap:'wrap' }}>
          <div style={{ display:'flex', alignItems:'baseline', gap:10 }}>
            <h2 style={{ fontSize:13, fontWeight:700, color:'#94a3b8', letterSpacing:'0.5px', textTransform:'uppercase' }}>
              Triage Queue
            </h2>
            <span style={{ fontSize:11, color:'#475569' }}>
              {triageQueue.length} shown · ordered by maintenance priority
            </span>
          </div>
          <div style={{ display:'flex', alignItems:'center', gap:6 }}>
            {(['all','Critical','Warning','Watch'] as SevFilter[]).map(s => (
              <button key={s} onClick={() => setSevFilter(s)}
                style={{
                  padding:'4px 10px', borderRadius:6, fontSize:11, fontWeight:600, cursor:'pointer',
                  background: sevFilter===s ? SEV_BG[s] || 'rgba(59,130,246,.12)' : '#111827',
                  border:`1px solid ${sevFilter===s ? (SEV_BORDER[s] || 'rgba(59,130,246,.3)') : '#1e2d45'}`,
                  color: sevFilter===s ? (SEV_COLOR[s] || '#3b82f6') : '#64748b',
                }}>
                {s === 'all' ? 'All severities' : s}
              </button>
            ))}
            <span style={{ width:1, height:18, background:'#1e2d45', margin:'0 3px' }} />
            {([['open','Needs action'],['Active','Active'],['Acknowledged','Acknowledged'],['all','All']] as [StatusFilter,string][]).map(([v,label]) => (
              <button key={v} onClick={() => setStatusFilter(v)}
                style={{
                  padding:'4px 10px', borderRadius:6, fontSize:11, fontWeight:600, cursor:'pointer',
                  background: statusFilter===v ? 'rgba(59,130,246,.12)' : '#111827',
                  border:`1px solid ${statusFilter===v ? 'rgba(59,130,246,.3)' : '#1e2d45'}`,
                  color: statusFilter===v ? '#3b82f6' : '#64748b',
                }}>
                {label}
              </button>
            ))}
          </div>
        </div>

        {actionNote && (
          <div style={{
            display:'flex', alignItems:'center', justifyContent:'space-between', gap:12,
            background:'rgba(59,130,246,.07)', border:'1px solid rgba(59,130,246,.22)',
            borderRadius:8, padding:'9px 12px', fontSize:12, color:'#93c5fd',
          }}>
            <span>{actionNote}</span>
            <button onClick={() => setActionNote(null)}
              style={{ background:'transparent', border:'none', color:'#64748b', cursor:'pointer', fontSize:16, lineHeight:1 }}>
              ×
            </button>
          </div>
        )}

        <div style={{ display:'flex', flexDirection:'column', gap:6, maxHeight:520, overflowY:'auto', paddingRight:2 }}>
          {triageQueue.length === 0 && (
            <div style={{ color:'#475569', fontSize:13, padding:'28px', textAlign:'center', background:'#111827', borderRadius:10, border:'1px solid #1e2d45' }}>
              Nothing matches this filter.
            </div>
          )}
          {triageQueue.map(a => (
            <TriageRow
              key={a.id}
              alert={a}
              busy={busyAlert === a.id}
              hasWorkOrder={machinesWithOpenWo.has(a.machine_id)}
              onAcknowledge={handleAck}
              onDismiss={handleDismiss}
              onCreateWorkOrder={handleCreateWorkOrder}
            />
          ))}
        </div>
      </div>

      {/* ── Machine risk ranking ───────────────────────────────── */}
      <div style={{ display:'flex', flexDirection:'column', gap:10 }}>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <h2 style={{ fontSize:13, fontWeight:700, color:'#94a3b8', letterSpacing:'0.5px', textTransform:'uppercase' }}>
            Machine Risk Ranking
          </h2>
          <span style={{ fontSize:11, color:'#475569' }}>{machines.length} monitored · {activeAlerts.length} active alerts</span>
        </div>
        <div style={{ display:'flex', flexDirection:'column', gap:6 }}>
          {machines.length === 0 && <div style={{ color:'#475569', fontSize:13, padding:'24px', textAlign:'center' }}>No machine data</div>}
          {machines.map((m, i) => <MachineRow key={m.id} machine={m} rank={i+1} />)}
        </div>
      </div>

      {/* ── OEE Trend + Machine OEE bar ───────────────────────── */}
      <div style={{ display:'grid', gridTemplateColumns:'1fr 300px', gap:16 }}>

        {/* Trend chart */}
        <div style={{ background:'#111827', border:'1px solid #1e2d45', borderRadius:12, padding:'18px 20px' }}>
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:16 }}>
            <h2 style={{ fontSize:13, fontWeight:700, color:'#94a3b8', letterSpacing:'0.5px', textTransform:'uppercase' }}>OEE Trend</h2>
            <span style={{ fontSize:11, color:'#475569' }}>Last 20 snapshots</span>
          </div>
          {trendData.length === 0 ? (
            <div style={{ display:'flex', alignItems:'center', justifyContent:'center', height:180, color:'#475569', fontSize:13 }}>
              OEE data will appear after the live feed runs…
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={trendData}>
                <defs>
                  <linearGradient id="oeeGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor="#10b981" stopOpacity={0.15}/>
                    <stop offset="95%" stopColor="#10b981" stopOpacity={0}/>
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1a2234" />
                <XAxis dataKey="time" tick={{ fill:'#475569', fontSize:10 }} />
                <YAxis domain={[0,100]} tick={{ fill:'#475569', fontSize:10 }} unit="%" width={35} />
                <Tooltip
                  contentStyle={{ background:'#1a2234', border:'1px solid #263551', borderRadius:8, fontSize:11 }}
                  formatter={(v: unknown) => [`${(v as number).toFixed(1)}%`]}
                />
                <Legend />
                <Area type="monotone" dataKey="OEE" stroke="#10b981" strokeWidth={2} fill="url(#oeeGrad)" dot={false} />
                <Line type="monotone" dataKey="Avail" stroke="#3b82f6" strokeWidth={1.5} dot={false} strokeDasharray="4 3" />
                <Line type="monotone" dataKey="Perf"  stroke="#8b5cf6" strokeWidth={1.5} dot={false} strokeDasharray="4 3" />
                {/* Quality was computed into the series and never drawn, which
                    made one of the three OEE factors invisible. */}
                <Line type="monotone" dataKey="Qual"  stroke="#14b8a6" strokeWidth={1.5} dot={false} strokeDasharray="4 3" />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* Per-machine OEE bars */}
        <div style={{ background:'#111827', border:'1px solid #1e2d45', borderRadius:12, padding:'18px 20px' }}>
          <h2 style={{ fontSize:13, fontWeight:700, color:'#94a3b8', letterSpacing:'0.5px', textTransform:'uppercase', marginBottom:14 }}>Machine OEE</h2>
          <div style={{ display:'flex', flexDirection:'column', gap:10 }}>
            {machineOee.length === 0 && <div style={{ color:'#475569', fontSize:12, paddingTop:40, textAlign:'center' }}>No data yet</div>}
            {machineOee.map(m => {
              const pct = m.oee * 100;
              const color = pct>=80?'#10b981':pct>=65?'#f59e0b':'#ef4444';
              return (
                <div key={m.machine_id}>
                  <div style={{ display:'flex', justifyContent:'space-between', marginBottom:4 }}>
                    <span style={{ fontSize:12, color:'#94a3b8', fontWeight:500 }}>{m.machine_name}</span>
                    <span style={{ fontSize:12, fontWeight:700, color }}>{pct.toFixed(1)}%</span>
                  </div>
                  <div style={{ height:5, borderRadius:3, background:'#1a2234', overflow:'hidden' }}>
                    <div style={{ height:'100%', borderRadius:3, background:color, width:`${Math.min(pct,100)}%`, transition:'width .8s' }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
};

export default Dashboard;
