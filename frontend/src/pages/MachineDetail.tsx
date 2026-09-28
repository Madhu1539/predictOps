import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  AreaChart, Area, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine
} from 'recharts';
import { getMachineDetail, createWorkOrder, explainAlert, updateAlert, getMachineErpContext } from '../services/api';
import type { MachineDetail as MD } from '../types';
import type { AttributionFactor, MachineErpContext, SensorDeviations } from '../types';
import { LoadingState, ErrorState } from '../components/States';

/** Compact currency. All monetary values are modelled estimates. */
const fmtMoney = (v?: number | null) => {
  if (v === undefined || v === null) return '—';
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${Math.round(v / 1_000)}k`;
  return `$${Math.round(v)}`;
};

/* ── Colours ─────────────────────────────────────────────────── */
const riskColor = (r: number) => r >= 75 ? '#ef4444' : r >= 45 ? '#f59e0b' : r >= 25 ? '#eab308' : '#10b981';
const sevBg     = (s: string) => ({ Critical:'rgba(239,68,68,.1)', Warning:'rgba(245,158,11,.1)', Watch:'rgba(234,179,8,.08)', Normal:'rgba(16,185,129,.08)' }[s] || 'rgba(100,116,139,.08)');
const sevBorder = (s: string) => ({ Critical:'rgba(239,68,68,.25)', Warning:'rgba(245,158,11,.25)', Watch:'rgba(234,179,8,.2)', Normal:'rgba(16,185,129,.2)' }[s] || 'rgba(100,116,139,.2)');
const sevText   = (s: string) => ({ Critical:'#ef4444', Warning:'#f59e0b', Watch:'#eab308', Normal:'#10b981' }[s] || '#64748b');

/* ── Section header ──────────────────────────────────────────── */
const SectionTitle: React.FC<{ children: React.ReactNode; sub?: string }> = ({ children, sub }) => (
  <div style={{ marginBottom: 12 }}>
    <h2 style={{ fontSize: 13, fontWeight: 700, color: '#94a3b8', letterSpacing: '0.5px', textTransform: 'uppercase' }}>{children}</h2>
    {sub && <p style={{ fontSize: 11, color: '#475569', marginTop: 3 }}>{sub}</p>}
  </div>
);

/* ── Stat chip ───────────────────────────────────────────────── */
const Chip: React.FC<{ label: string; value: string | number; unit?: string; accent?: string }> = ({ label, value, unit, accent = '#3b82f6' }) => (
  <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 10, padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 6 }}>
    <span style={{ fontSize: 10, color: '#64748b', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.5px' }}>{label}</span>
    <span style={{ fontSize: 22, fontWeight: 800, color: accent, lineHeight: 1 }}>
      {value}<span style={{ fontSize: 12, fontWeight: 500, color: '#64748b', marginLeft: 3 }}>{unit}</span>
    </span>
  </div>
);

/* ── Sensor mini chart ───────────────────────────────────────── */
interface SensorChartProps { data: Array<{ time: string; value: number }>; label: string; unit: string; color: string; nominal?: number }
const SensorChart: React.FC<SensorChartProps> = ({ data, label, unit, color, nominal }) => (
  <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, padding: '16px 18px' }}>
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
      <span style={{ fontSize: 12, fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{label}</span>
      {data.length > 0 && (
        <span style={{ fontSize: 13, fontWeight: 700, color }}>
          {data[data.length - 1].value.toFixed(1)} <span style={{ fontSize: 10, fontWeight: 400, color: '#475569' }}>{unit}</span>
        </span>
      )}
    </div>
    <ResponsiveContainer width="100%" height={100}>
      <AreaChart data={data}>
        <defs>
          <linearGradient id={`g${label}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={color} stopOpacity={0.15} />
            <stop offset="95%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#1a2234" />
        <XAxis dataKey="time" tick={{ fill: '#475569', fontSize: 9 }} interval="preserveStartEnd" />
        <YAxis tick={{ fill: '#475569', fontSize: 9 }} width={32} />
        <Tooltip contentStyle={{ background: '#1a2234', border: '1px solid #263551', borderRadius: 8, fontSize: 11 }}
          formatter={(v: unknown) => [`${(v as number).toFixed(2)} ${unit}`, label]} />
        {nominal && <ReferenceLine y={nominal} stroke="#475569" strokeDasharray="4 2" label={{ value: 'Nominal', fill: '#64748b', fontSize: 9 }} />}
        <Area type="monotone" dataKey="value" stroke={color} strokeWidth={2} fill={`url(#g${label})`} dot={false} />
      </AreaChart>
    </ResponsiveContainer>
  </div>
);

/* ── Main ────────────────────────────────────────────────────── */
const MachineDetail: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [machine, setMachine]       = useState<MD | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error,   setError]         = useState<string | null>(null);
  const [explain, setExplain]       = useState<string | null>(null);
  const [explainSource, setExplainSource] = useState<string | null>(null);
  const [explaining, setExplaining] = useState(false);
  const [creating, setCreating]     = useState(false);
  const [woCreated, setWoCreated]   = useState(false);
  const [showExplain, setShowExplain] = useState(false);
  const [erp, setErp] = useState<MachineErpContext | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    try {
      const d = await getMachineDetail(Number(id));
      setMachine(d); setError(null);
    } catch { setError('Failed to load machine data'); }
    finally { setLoading(false); }
  }, [id]);

  // ERP context is fetched separately so a missing or empty ERP dataset degrades
  // to hiding one panel rather than failing the whole page.
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    getMachineErpContext(Number(id))
      .then(c => { if (!cancelled) setErp(c); })
      .catch(() => { if (!cancelled) setErp(null); });
    return () => { cancelled = true; };
  }, [id]);

  useEffect(() => { load(); }, [load]);

  const handleExplain = async () => {
    if (!machine?.latest_alert) return;
    setExplaining(true); setShowExplain(true);
    try {
      const r = await explainAlert(machine.latest_alert.id);
      setExplain(r.explanation);
      // Surface which path produced the text, matching the Investigation page.
      setExplainSource(r.source);
    } catch {
      setExplain('Unable to generate explanation. Please try again.');
      setExplainSource(null);
    }
    finally { setExplaining(false); }
  };

  const handleCreateWO = async () => {
    if (!machine?.latest_alert) return;
    setCreating(true);
    try {
      await createWorkOrder({ alert_id: machine.latest_alert.id, machine_id: machine.id });
      setWoCreated(true);
    } catch { window.alert('Failed to create work order'); }
    finally { setCreating(false); }
  };

  const handleAck = async () => {
    if (!machine?.latest_alert) return;
    await updateAlert(machine.latest_alert.id, 'Acknowledged');
    load();
  };

  if (loading) return <LoadingState message="Loading machine data…" />;
  if (error || !machine) return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12 }}>
      <ErrorState message={error || 'Machine not found.'} onRetry={load} />
      <button onClick={() => navigate('/')} style={{ padding: '8px 18px', borderRadius: 8, background: '#111827', border: '1px solid #1e2d45', color: '#94a3b8', cursor: 'pointer' }}>← Back</button>
    </div>
  );

  /* Data preparation */
  const readings  = (machine.readings || []).slice(-48);
  const vibData   = readings.map(r => ({ time: new Date(r.timestamp).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' }), value: r.vibration }));
  const tempData  = readings.map(r => ({ time: new Date(r.timestamp).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' }), value: r.temperature }));
  const rpmData   = readings.map(r => ({ time: new Date(r.timestamp).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' }), value: r.rpm }));
  const riskData  = (machine.risk_history || []).slice(-30).map(r => ({
    time: new Date(r.timestamp).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' }),
    risk: r.risk_score,
  }));

  const alert     = machine.latest_alert;
  const sev       = alert?.severity || 'Normal';
  const risk      = alert?.risk_score ?? 0;
  const color     = riskColor(risk);
  const lastR     = readings[readings.length - 1];
  const failMode  = (alert?.failure_mode || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

  /* Model attribution, stored as a JSON string on the alert. */
  let factors: AttributionFactor[] = [];
  if (alert?.attribution) {
    try { factors = JSON.parse(alert.attribution) as AttributionFactor[]; }
    catch { factors = []; }
  }
  const maxContribution = factors.reduce((m, f) => Math.max(m, Math.abs(f.contribution)), 0);

  /* Sensor deviations, including RPM. */
  let deviations: SensorDeviations = {};
  if (alert?.sensor_deviations) {
    try { deviations = JSON.parse(alert.sensor_deviations) as SensorDeviations; }
    catch { deviations = {}; }
  }

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* ── Breadcrumb + back ───────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button onClick={() => navigate('/')} style={{ display: 'flex', alignItems: 'center', gap: 6, background: 'transparent', border: 'none', color: '#64748b', cursor: 'pointer', fontSize: 12, padding: 0 }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M15 18l-6-6 6-6" /></svg>
          Command Center
        </button>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#1e2d45" strokeWidth="2"><path d="M9 18l6-6-6-6" /></svg>
        <span style={{ fontSize: 12, color: '#94a3b8', fontWeight: 600 }}>{machine.name}</span>
      </div>

      {/* ── Hero header ─────────────────────────────────────────── */}
      <div style={{ background: '#111827', border: `1px solid ${alert && risk >= 75 ? 'rgba(239,68,68,.3)' : '#1e2d45'}`, borderRadius: 14, padding: '20px 24px', display: 'flex', alignItems: 'flex-start', gap: 24, boxShadow: alert && risk >= 75 ? '0 0 32px rgba(239,68,68,.06)' : 'none' }}>

        {/* Risk ring */}
        <div style={{ flexShrink: 0 }}>
          {(() => {
            const r = 38, c = 2 * Math.PI * r;
            const pct = Math.min(risk, 100);
            return (
              <div style={{ position: 'relative' }}>
                <svg width={94} height={94} style={{ transform: 'rotate(-90deg)' }}>
                  <circle cx={47} cy={47} r={r} stroke="#1a2234" strokeWidth={8} fill="none" />
                  <circle cx={47} cy={47} r={r} stroke={color} strokeWidth={8} fill="none"
                    strokeDasharray={c} strokeDashoffset={c - (pct / 100) * c}
                    strokeLinecap="round" style={{ transition: 'stroke-dashoffset 1s ease' }} />
                </svg>
                <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
                  <span style={{ fontSize: 18, fontWeight: 800, color, lineHeight: 1 }}>{risk.toFixed(0)}%</span>
                  <span style={{ fontSize: 9, color: '#475569', marginTop: 2 }}>RISK</span>
                </div>
              </div>
            );
          })()}
        </div>

        {/* Info */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
            <h1 style={{ fontSize: 22, fontWeight: 800, color: '#f1f5f9', letterSpacing: '-0.5px' }}>{machine.name}</h1>
            {alert && (
              <span style={{ padding: '3px 10px', borderRadius: 6, background: sevBg(sev), border: `1px solid ${sevBorder(sev)}`, color: sevText(sev), fontSize: 11, fontWeight: 700, letterSpacing: '0.5px' }}>
                {sev.toUpperCase()}
              </span>
            )}
            <span style={{ padding: '3px 10px', borderRadius: 6, background: machine.criticality === 'High' ? 'rgba(139,92,246,.1)' : 'rgba(100,116,139,.08)', border: `1px solid ${machine.criticality === 'High' ? 'rgba(139,92,246,.2)' : 'rgba(100,116,139,.15)'}`, color: machine.criticality === 'High' ? '#8b5cf6' : '#64748b', fontSize: 11, fontWeight: 600 }}>
              {machine.criticality} Criticality
            </span>
          </div>
          <div style={{ display: 'flex', gap: 16, fontSize: 12, color: '#64748b', marginBottom: 12 }}>
            <span style={{ fontFamily: 'ui-monospace, monospace', color: '#94a3b8' }}>ID #{machine.id}</span>
            <span>·</span>
            <span>{machine.type}</span>
            <span>·</span>
            <span>{machine.location}</span>
            <span>·</span>
            <span>Installed {machine.install_date?.slice(0, 10)}</span>
            {alert && (
              <>
                <span>·</span>
                <span>Priority <strong style={{ color: '#f1f5f9' }}>{alert.maintenance_priority.toFixed(0)}</strong></span>
              </>
            )}
          </div>
          {failMode && (
            <div style={{ fontSize: 13, color: '#94a3b8', background: '#1a2234', padding: '8px 12px', borderRadius: 8, borderLeft: `3px solid ${color}` }}>
              <span style={{ color: '#64748b', fontSize: 11, fontWeight: 600 }}>FAILURE MODE: </span>
              {failMode}
            </div>
          )}
        </div>

        {/* Actions */}
        {alert && (
          <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <button onClick={handleExplain} disabled={explaining} style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '9px 16px', borderRadius: 9, background: 'rgba(59,130,246,.1)', border: '1px solid rgba(59,130,246,.25)', color: '#3b82f6', cursor: explaining ? 'wait' : 'pointer', fontSize: 12, fontWeight: 600, whiteSpace: 'nowrap' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><path d="M12 16v-4M12 8h.01" /></svg>
              {explaining ? 'Analyzing…' : 'Explain Alert'}
            </button>
            {!woCreated ? (
              <button onClick={handleCreateWO} disabled={creating} style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '9px 16px', borderRadius: 9, background: 'rgba(16,185,129,.1)', border: '1px solid rgba(16,185,129,.25)', color: '#10b981', cursor: creating ? 'wait' : 'pointer', fontSize: 12, fontWeight: 600, whiteSpace: 'nowrap' }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2" /><rect x="9" y="3" width="6" height="4" rx="1" /></svg>
                {creating ? 'Creating…' : 'Create Work Order'}
              </button>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '9px 16px', borderRadius: 9, background: 'rgba(16,185,129,.08)', border: '1px solid rgba(16,185,129,.2)', color: '#10b981', fontSize: 12, fontWeight: 600 }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="20 6 9 17 4 12" /></svg>
                Work Order Created
              </div>
            )}
            {alert.status === 'Active' && (
              <button onClick={handleAck} style={{ padding: '7px 16px', borderRadius: 9, background: 'transparent', border: '1px solid #1e2d45', color: '#64748b', cursor: 'pointer', fontSize: 12 }}>
                Acknowledge
              </button>
            )}
          </div>
        )}
      </div>

      {/* ── AI Explanation panel ─────────────────────────────────── */}
      {showExplain && (
        <div style={{ background: '#0f1929', border: '1px solid rgba(59,130,246,.2)', borderRadius: 12, padding: '16px 20px', display: 'flex', gap: 14 }}>
          <div style={{ width: 32, height: 32, borderRadius: 8, background: 'rgba(59,130,246,.12)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, color: '#3b82f6' }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a2 2 0 012 2v2a2 2 0 01-2 2 2 2 0 01-2-2V4a2 2 0 012-2zm0 8a6 6 0 016 6v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-1a6 6 0 016-6z" /></svg>
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: '#3b82f6', letterSpacing: '0.5px' }}>AI ROOT CAUSE ANALYSIS</span>
              {explainSource && (
                <span style={{
                  padding: '2px 6px', borderRadius: 4, fontSize: 9, fontWeight: 700, letterSpacing: '0.5px',
                  color: explainSource === 'gemini' ? '#3b82f6' : explainSource === 'cached' ? '#64748b' : '#10b981',
                  background: explainSource === 'gemini' ? 'rgba(59,130,246,.12)' : explainSource === 'cached' ? 'rgba(100,116,139,.12)' : 'rgba(16,185,129,.12)',
                }}>
                  {explainSource === 'gemini' ? 'GEMINI' : explainSource === 'cached' ? 'CACHED' : 'RULE-BASED'}
                </span>
              )}
            </div>
            {explaining ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: '#64748b', fontSize: 13 }}>
                <div style={{ width: 14, height: 14, borderRadius: '50%', border: '2px solid rgba(59,130,246,.2)', borderTopColor: '#3b82f6', animation: 'spin .8s linear infinite' }} />
                Analyzing sensor patterns and maintenance history…
              </div>
            ) : (
              <p style={{ fontSize: 13, color: '#94a3b8', lineHeight: 1.7 }}>{explain}</p>
            )}
          </div>
          <button onClick={() => setShowExplain(false)} style={{ background: 'transparent', border: 'none', color: '#475569', cursor: 'pointer', fontSize: 18, lineHeight: 1, flexShrink: 0 }}>×</button>
        </div>
      )}

      {/* ── Live readings KPIs ───────────────────────────────────── */}
      {lastR && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12 }}>
          <Chip label="Vibration" value={lastR.vibration.toFixed(2)} unit="mm/s" accent={lastR.vibration > machine.nominal_vibration * 1.3 ? '#ef4444' : '#10b981'} />
          <Chip label="Temperature" value={lastR.temperature.toFixed(1)} unit="°C" accent={lastR.temperature > machine.nominal_temperature * 1.15 ? '#f59e0b' : '#10b981'} />
          <Chip label="RPM" value={Math.round(lastR.rpm)} unit={deviations.rpm_pct != null ? `rpm (${deviations.rpm_pct > 0 ? '+' : ''}${deviations.rpm_pct.toFixed(0)}%)` : 'rpm'} accent={deviations.rpm_cv != null && deviations.rpm_cv > 0.02 ? '#f59e0b' : '#3b82f6'} />
          <Chip label="Downtime" value={lastR.downtime_minutes.toFixed(0)} unit="min" accent={lastR.downtime_minutes > 10 ? '#ef4444' : '#10b981'} />
        </div>
      )}

      {/* ── Context chips ───────────────────────────────────────── */}
      {alert && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12 }}>
            <Chip label="Days Since Maintenance" value={Math.round(alert.days_since_maintenance ?? 0)} unit="days" accent="#8b5cf6" />
            <Chip label="Maintenance Priority" value={alert.maintenance_priority.toFixed(0)} accent={color} />
            <Chip label="Part Needed" value={alert.part_needed || '—'} accent="#14b8a6" />
            <Chip label="Part Available" value={alert.part_available ? 'Yes' : 'No'} accent={alert.part_available ? '#10b981' : '#ef4444'} />
          </div>
          {/* Previous failures + production impact (spec §43) */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: 12 }}>
            <Chip
              label="Previous Failures"
              value={alert.previous_failure_context || 'No prior failures recorded'}
              accent={alert.previous_failure_context ? '#f59e0b' : '#10b981'}
            />
            <Chip
              label="Production Impact"
              value={alert.production_impact || '—'}
              accent={alert.production_impact === 'High' ? '#ef4444' : alert.production_impact === 'Medium' ? '#f59e0b' : '#10b981'}
            />
          </div>
          {alert.part_available === false && (
            <div style={{ padding: '10px 14px', borderRadius: 10, background: 'rgba(239,68,68,.08)', border: '1px solid rgba(239,68,68,.22)', color: '#ef4444', fontSize: 12, fontWeight: 600 }}>
              Required part currently unavailable.
            </div>
          )}

          {/* ── Modelled business impact ────────────────────────── */}
          {(alert.estimated_loss_avoided != null || alert.estimated_downtime_cost != null) && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: 12 }}>
              <Chip
                label="Loss Avoided If Actioned"
                value={fmtMoney(alert.estimated_loss_avoided)}
                accent="#14b8a6"
              />
              <Chip
                label="Downtime Cost Incurred"
                value={fmtMoney(alert.estimated_downtime_cost)}
                accent={alert.estimated_downtime_cost ? '#ef4444' : '#10b981'}
              />
            </div>
          )}

          {/* ── ERP context: the IT half of the decision ─────────── */}
          {erp && (
            <>
              <SectionTitle sub="Read from ERP master data and the production schedule — what this failure would actually cost and disrupt">
                Business &amp; ERP Context
              </SectionTitle>
              <div style={{ background:'#111827', border:'1px solid #1e2d45', borderRadius:12, padding:'16px 18px', display:'flex', flexDirection:'column', gap:14 }}>

                <div style={{ display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:14 }}>
                  {/* Cost centre */}
                  <div>
                    <div style={{ fontSize:9, color:'#475569', fontWeight:700, letterSpacing:'0.4px', textTransform:'uppercase', marginBottom:5 }}>Cost Centre</div>
                    {erp.cost_center ? (
                      <>
                        <div style={{ fontSize:13, fontWeight:700, color:'#f1f5f9' }}>
                          {erp.cost_center.code} · {erp.cost_center.name}
                        </div>
                        <div style={{ fontSize:11, color:'#14b8a6', marginTop:3 }}>
                          {erp.cost_center.currency} {erp.cost_center.downtime_cost_per_hour.toLocaleString()} / hour of downtime
                        </div>
                        <div style={{ fontSize:10, color:'#475569', marginTop:2 }}>source: {erp.cost_center.erp_source}</div>
                      </>
                    ) : (
                      <div style={{ fontSize:12, color:'#64748b' }}>Not assigned to a cost centre, so downtime cost falls back to a plant default.</div>
                    )}
                  </div>

                  {/* Spare part */}
                  <div>
                    <div style={{ fontSize:9, color:'#475569', fontWeight:700, letterSpacing:'0.4px', textTransform:'uppercase', marginBottom:5 }}>Spare Part</div>
                    {erp.part ? (
                      <>
                        <div style={{ fontSize:13, fontWeight:700, color:'#f1f5f9' }}>{erp.part.part_name}</div>
                        <div style={{ fontSize:11, color: erp.part.is_available ? '#10b981' : '#f87171', marginTop:3, fontWeight:600 }}>
                          {erp.part.is_available
                            ? `In stock${erp.part.stock_quantity != null ? ` · ${erp.part.stock_quantity} on hand` : ''}`
                            : `Not in stock${erp.part.lead_time_days != null ? ` · ${erp.part.lead_time_days}-day lead time` : ''}`}
                        </div>
                        <div style={{ fontSize:10, color:'#475569', marginTop:2 }}>
                          {erp.part.erp_material_no ? `${erp.part.erp_material_no} · ` : ''}
                          {erp.part.unit_cost != null ? `$${erp.part.unit_cost.toLocaleString()} each` : 'cost not in ERP'}
                          {erp.part.supplier ? ` · ${erp.part.supplier}` : ''}
                        </div>
                      </>
                    ) : (
                      <div style={{ fontSize:12, color:'#64748b' }}>No part required — there is no open alert calling for one.</div>
                    )}
                  </div>

                  {/* Production impact */}
                  <div>
                    <div style={{ fontSize:9, color:'#475569', fontWeight:700, letterSpacing:'0.4px', textTransform:'uppercase', marginBottom:5 }}>Production Impact</div>
                    <div style={{ fontSize:13, fontWeight:700, color: erp.production_impact === 'High' ? '#ef4444' : erp.production_impact === 'Medium' ? '#f59e0b' : '#10b981' }}>
                      {erp.production_impact}
                      {erp.committed_units > 0 && (
                        <span style={{ fontSize:11, fontWeight:500, color:'#94a3b8' }}> · {erp.committed_units.toLocaleString()} units committed</span>
                      )}
                    </div>
                    <div style={{ fontSize:10, color:'#475569', marginTop:3, lineHeight:1.5 }}>{erp.production_impact_detail}</div>
                    {/* Says plainly whether this came from real orders or a fallback,
                        because the two are not equally strong evidence. */}
                    <span style={{
                      display:'inline-block', marginTop:5, padding:'1px 6px', borderRadius:4, fontSize:9, fontWeight:700,
                      color: erp.production_impact_basis === 'erp_production_orders' ? '#3b82f6' : '#f59e0b',
                      background: erp.production_impact_basis === 'erp_production_orders' ? 'rgba(59,130,246,.12)' : 'rgba(245,158,11,.12)',
                    }}>
                      {erp.production_impact_basis === 'erp_production_orders' ? 'FROM ERP ORDERS' : 'CRITICALITY FALLBACK'}
                    </span>
                  </div>
                </div>

                {/* Orders a failure here would disrupt */}
                {erp.open_orders.length > 0 && (
                  <div style={{ borderTop:'1px solid #1a2234', paddingTop:12 }}>
                    <div style={{ fontSize:9, color:'#475569', fontWeight:700, letterSpacing:'0.4px', textTransform:'uppercase', marginBottom:8 }}>
                      Open production orders on this machine
                    </div>
                    <div style={{ display:'flex', flexDirection:'column', gap:6 }}>
                      {erp.open_orders.map(o => (
                        <div key={o.order_no} style={{ display:'grid', gridTemplateColumns:'1fr 1.2fr 100px 110px', gap:10, alignItems:'center', fontSize:11 }}>
                          <span style={{ fontWeight:700, color:'#f1f5f9' }}>{o.order_no}</span>
                          <span style={{ color:'#94a3b8' }}>{o.product}</span>
                          <span style={{
                            color: o.status === 'InProgress' ? '#f59e0b' : '#64748b', fontWeight:600,
                          }}>{o.status}</span>
                          <span style={{ textAlign:'right', color:'#94a3b8' }}>
                            {o.remaining_qty.toLocaleString()} of {o.planned_qty.toLocaleString()} left
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </>
          )}

          {/* ── Why the model scored this ───────────────────────── */}
          {factors.length > 0 && (
            <>
              <SectionTitle sub="Ranked by the model's own contribution to this prediction">
                Top Risk Factors
              </SectionTitle>
              <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, padding: '14px 18px', display: 'flex', flexDirection: 'column', gap: 10 }}>
                {/* Column labels, so the two numbers are not ambiguous */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 9, color: '#475569', fontWeight: 700, letterSpacing: '0.4px', textTransform: 'uppercase' }}>
                  <span style={{ width: 190, flexShrink: 0 }}>Factor</span>
                  <span style={{ flex: 1 }}>Relative contribution</span>
                  <span style={{ width: 74, textAlign: 'right', flexShrink: 0 }}>Effect</span>
                  <span style={{ width: 78, textAlign: 'right', flexShrink: 0 }}>Feature value</span>
                </div>
                {factors.map(f => {
                  const pct = maxContribution > 0 ? Math.abs(f.contribution) / maxContribution * 100 : 0;
                  const up = f.direction === 'increases_risk';
                  return (
                    <div key={f.feature} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                      <span style={{ fontSize: 12, color: '#94a3b8', width: 190, flexShrink: 0 }}>{f.label}</span>
                      <div style={{ flex: 1, height: 6, borderRadius: 3, background: '#1a2234', overflow: 'hidden' }}
                        title={`contribution ${f.contribution}`}>
                        <div style={{ height: '100%', borderRadius: 3, width: `${pct}%`, background: up ? '#ef4444' : '#10b981', transition: 'width .5s' }} />
                      </div>
                      <span style={{ fontSize: 11, color: up ? '#ef4444' : '#10b981', width: 74, textAlign: 'right', flexShrink: 0 }}>
                        {up ? 'raises' : 'lowers'}
                      </span>
                      <span style={{ fontSize: 11, color: '#475569', width: 78, textAlign: 'right', flexShrink: 0, fontFamily: 'ui-monospace, monospace' }}>
                        {f.value}
                      </span>
                    </div>
                  );
                })}
                <p style={{ fontSize: 10, color: '#475569', marginTop: 2 }}>
                  Method: {factors[0]?.method === 'linear_coefficient'
                    ? 'exact linear contribution (coefficient x standardised value)'
                    : 'tree importance weighted by feature value (approximation)'}
                </p>
              </div>
            </>
          )}
        </>
      )}

      {/* ── Sensor charts ───────────────────────────────────────── */}
      <SectionTitle sub="Last 48 readings">Sensor Telemetry</SectionTitle>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 14 }}>
        <SensorChart data={vibData}  label="Vibration"   unit="mm/s" color="#ef4444" nominal={machine.nominal_vibration} />
        <SensorChart data={tempData} label="Temperature" unit="°C"   color="#f59e0b" nominal={machine.nominal_temperature} />
        <SensorChart data={rpmData}  label="RPM"         unit="rpm"  color="#3b82f6" nominal={machine.nominal_rpm} />
      </div>

      {/* ── Risk trend ──────────────────────────────────────────── */}
      {riskData.length > 0 && (
        <>
          <SectionTitle sub="Predicted failure probability over time">Risk Score History</SectionTitle>
          <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, padding: '16px 20px' }}>
            <ResponsiveContainer width="100%" height={140}>
              <AreaChart data={riskData}>
                <defs>
                  <linearGradient id="riskGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor={color} stopOpacity={0.2} />
                    <stop offset="95%" stopColor={color} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1a2234" />
                <XAxis dataKey="time" tick={{ fill: '#475569', fontSize: 9 }} />
                <YAxis domain={[0, 100]} tick={{ fill: '#475569', fontSize: 9 }} unit="%" width={33} />
                <Tooltip contentStyle={{ background: '#1a2234', border: '1px solid #263551', borderRadius: 8, fontSize: 11 }}
                  formatter={(v: unknown) => [`${(v as number).toFixed(1)}%`, 'Risk']} />
                <ReferenceLine y={75} stroke="#ef4444" strokeDasharray="4 2" label={{ value: 'Critical', fill: '#ef4444', fontSize: 9 }} />
                <ReferenceLine y={45} stroke="#f59e0b" strokeDasharray="4 2" label={{ value: 'Warning', fill: '#f59e0b', fontSize: 9 }} />
                <Area type="monotone" dataKey="risk" stroke={color} strokeWidth={2} fill="url(#riskGrad)" dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </>
      )}

      {/* ── Maintenance history ─────────────────────────────────── */}
      {(machine.maintenance || []).length > 0 && (
        <>
          <SectionTitle sub="Past maintenance and failure events">Maintenance History</SectionTitle>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {machine.maintenance.slice(0, 6).map(m => (
              <div key={m.id} style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 10, padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 14 }}>
                <div style={{ width: 8, height: 8, borderRadius: '50%', background: m.failure_mode ? '#ef4444' : '#10b981', flexShrink: 0 }} />
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: '#f1f5f9' }}>
                    {m.maintenance_type}
                    {m.failure_mode && <span style={{ marginLeft: 8, fontSize: 11, color: '#ef4444' }}>({m.failure_mode.replace(/_/g,' ')})</span>}
                  </div>
                  {m.description && <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>{m.description}</div>}
                </div>
                <div style={{ fontSize: 11, color: '#475569', flexShrink: 0 }}>
                  {new Date(m.maintenance_date).toLocaleDateString('en', { day:'2-digit', month:'short', year:'numeric' })}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {/* ── Recommended action ──────────────────────────────────── */}
      {alert?.recommended_action && (
        <div style={{ background: 'rgba(16,185,129,.05)', border: '1px solid rgba(16,185,129,.15)', borderRadius: 12, padding: '16px 20px', display: 'flex', gap: 14 }}>
          <div style={{ color: '#10b981', flexShrink: 0, marginTop: 1 }}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="9 11 12 14 22 4" /><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" /></svg>
          </div>
          <div>
            <div style={{ fontSize: 11, fontWeight: 700, color: '#10b981', letterSpacing: '0.5px', marginBottom: 5 }}>RECOMMENDED ACTION</div>
            <p style={{ fontSize: 13, color: '#94a3b8', lineHeight: 1.6 }}>{alert.recommended_action}</p>
          </div>
        </div>
      )}
    </div>
  );
};

export default MachineDetail;
