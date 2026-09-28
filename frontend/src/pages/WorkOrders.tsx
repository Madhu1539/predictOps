import React, { useState, useEffect, useCallback } from 'react';
import { getWorkOrders, updateWorkOrderStatus } from '../services/api';
import type { WorkOrder } from '../types';
import { LoadingState, ErrorState } from '../components/States';

/* ── Status config ───────────────────────────────────────────── */
const STATUS_COLOR:  Record<string, string> = { Open:'#f59e0b', InProgress:'#3b82f6', Completed:'#10b981', Cancelled:'#64748b' };
const STATUS_BG:     Record<string, string> = { Open:'rgba(245,158,11,.1)', InProgress:'rgba(59,130,246,.1)', Completed:'rgba(16,185,129,.08)', Cancelled:'rgba(100,116,139,.08)' };
const STATUS_BORDER: Record<string, string> = { Open:'rgba(245,158,11,.25)', InProgress:'rgba(59,130,246,.25)', Completed:'rgba(16,185,129,.2)', Cancelled:'rgba(100,116,139,.15)' };
const PRIORITY_COLOR: Record<string, string> = { Urgent:'#ef4444', High:'#f59e0b', Medium:'#3b82f6', Low:'#64748b' };

const NEXT_STATUS: Record<string, string | null> = { Open:'InProgress', InProgress:'Completed', Completed:null };
const NEXT_LABEL:  Record<string, string>        = { Open:'Start Work', InProgress:'Mark Complete', Completed:'Done' };

/* ── WO Card ─────────────────────────────────────────────────── */
const WoCard: React.FC<{ wo: WorkOrder; onProgress: (id: number, status: string) => void }> = ({ wo, onProgress }) => {
  const next = NEXT_STATUS[wo.status];
  const priorityColor = PRIORITY_COLOR[wo.priority] || '#64748b';

  const formatDate = (d: string | null | undefined) => {
    if (!d) return null;
    return new Date(d).toLocaleDateString('en', { day: '2-digit', month: 'short', year: 'numeric' });
  };

  return (
    <div style={{ background: '#111827', border: `1px solid ${wo.status === 'Completed' ? 'rgba(16,185,129,.15)' : '#1e2d45'}`, borderRadius: 12, padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: 12, transition: 'border-color .2s' }}>

      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <span style={{ fontSize: 15, fontWeight: 700, color: '#f1f5f9' }}>{wo.machine_name || `Machine ${wo.machine_id}`}</span>
            <span style={{ padding: '2px 8px', borderRadius: 5, background: STATUS_BG[wo.status], border: `1px solid ${STATUS_BORDER[wo.status]}`, color: STATUS_COLOR[wo.status], fontSize: 10, fontWeight: 700, letterSpacing: '0.4px', flexShrink: 0 }}>
              {wo.status === 'InProgress' ? 'IN PROGRESS' : wo.status.toUpperCase()}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 11, color: priorityColor, background: `${priorityColor}14`, border: `1px solid ${priorityColor}33`, padding: '2px 7px', borderRadius: 5, fontWeight: 600 }}>
              {wo.priority.toUpperCase()} PRIORITY
            </span>
            {wo.technician && (
              <span style={{ fontSize: 11, color: '#64748b', display: 'flex', alignItems: 'center', gap: 4 }}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2" /><circle cx="12" cy="7" r="4" /></svg>
                {wo.technician}
              </span>
            )}
          </div>
        </div>
        <div style={{ textAlign: 'right', flexShrink: 0 }}>
          <div style={{ fontSize: 11, color: '#475569' }}>WO-{wo.id.toString().padStart(4, '0')}</div>
          <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>Created {formatDate(wo.created_at)}</div>
        </div>
      </div>

      {/* Details row */}
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        {wo.part_needed && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#94a3b8' }}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#64748b" strokeWidth="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" /><polyline points="14 2 14 8 20 8" /></svg>
            Part: <strong style={{ color: '#f1f5f9' }}>{wo.part_needed}</strong>
          </div>
        )}
        {wo.due_date && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#94a3b8' }}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#64748b" strokeWidth="2"><rect x="3" y="4" width="18" height="18" rx="2" /><line x1="16" y1="2" x2="16" y2="6" /><line x1="8" y1="2" x2="8" y2="6" /><line x1="3" y1="10" x2="21" y2="10" /></svg>
            Due: <strong style={{ color: new Date(wo.due_date) < new Date() && wo.status !== 'Completed' ? '#ef4444' : '#f1f5f9' }}>{formatDate(wo.due_date)}</strong>
          </div>
        )}
        {wo.completed_at && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#10b981' }}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="20 6 9 17 4 12" /></svg>
            Completed {formatDate(wo.completed_at)}
          </div>
        )}
      </div>

      {/* Progress button */}
      {next && (
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button
            onClick={() => onProgress(wo.id, next)}
            style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '8px 16px', borderRadius: 8,
              background: next === 'Completed' ? 'rgba(16,185,129,.1)' : 'rgba(59,130,246,.1)',
              border: `1px solid ${next === 'Completed' ? 'rgba(16,185,129,.25)' : 'rgba(59,130,246,.25)'}`,
              color: next === 'Completed' ? '#10b981' : '#3b82f6',
              cursor: 'pointer', fontSize: 12, fontWeight: 600,
            }}
          >
            {next === 'Completed' ? (
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="20 6 9 17 4 12" /></svg>
            ) : (
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="5 3 19 12 5 21 5 3" /></svg>
            )}
            {NEXT_LABEL[wo.status]}
          </button>
        </div>
      )}
    </div>
  );
};

/* ── Stats bar ───────────────────────────────────────────────── */
const WoStat: React.FC<{ label: string; count: number; color: string }> = ({ label, count, color }) => (
  <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 10, padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 4 }}>
    <span style={{ fontSize: 26, fontWeight: 800, color, lineHeight: 1 }}>{count}</span>
    <span style={{ fontSize: 11, color: '#64748b', fontWeight: 500 }}>{label}</span>
  </div>
);

/* ── Main ────────────────────────────────────────────────────── */
const WorkOrders: React.FC = () => {
  const [orders,  setOrders]  = useState<WorkOrder[]>([]);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(false);
  const [filter,  setFilter]  = useState<string>('All');

  const load = useCallback(async () => {
    try { setOrders(await getWorkOrders()); setError(false); }
    catch { setError(true); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleProgress = async (id: number, status: string) => {
    try {
      // Use the server's response rather than guessing the resulting state.
      const updated = await updateWorkOrderStatus(id, status);
      setOrders(prev => prev.map(wo => wo.id === id ? updated : wo));
    } catch {
      window.alert('Could not update the work order. Please retry.');
    }
  };

  const tabs = ['All', 'Open', 'InProgress', 'Completed'];
  const counts = {
    All: orders.length,
    Open: orders.filter(o => o.status === 'Open').length,
    InProgress: orders.filter(o => o.status === 'InProgress').length,
    Completed: orders.filter(o => o.status === 'Completed').length,
  };
  const visible = filter === 'All' ? orders : orders.filter(o => o.status === filter);

  if (loading) return <LoadingState message="Loading work orders…" />;

  if (error) return <ErrorState onRetry={load} />;

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* ── Title ─────────────────────────────────────────────── */}
      <div>
        <h1 style={{ fontSize: 20, fontWeight: 800, color: '#f1f5f9', letterSpacing: '-0.5px' }}>Work Orders</h1>
        <p style={{ fontSize: 12, color: '#475569', marginTop: 3 }}>Maintenance tasks generated from predictive alerts</p>
      </div>

      {/* ── Stats ─────────────────────────────────────────────── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12 }}>
        <WoStat label="Total"       count={counts.All}        color="#94a3b8" />
        <WoStat label="Open"        count={counts.Open}       color="#f59e0b" />
        <WoStat label="In Progress" count={counts.InProgress} color="#3b82f6" />
        <WoStat label="Completed"   count={counts.Completed}  color="#10b981" />
      </div>

      {/* ── Filter tabs ───────────────────────────────────────── */}
      <div style={{ display: 'flex', gap: 6 }}>
        {tabs.map(t => (
          <button key={t}
            onClick={() => setFilter(t)}
            style={{
              padding: '7px 16px', borderRadius: 8, border: `1px solid ${filter===t?'rgba(59,130,246,.3)':'#1e2d45'}`,
              background: filter===t?'rgba(59,130,246,.1)':'transparent',
              color: filter===t?'#3b82f6':'#64748b',
              cursor: 'pointer', fontSize: 12, fontWeight: filter===t?600:400, transition:'all .15s',
            }}
          >
            {t === 'InProgress' ? 'In Progress' : t}
            {counts[t as keyof typeof counts] > 0 && (
              <span style={{ marginLeft: 6, fontSize: 10, background: filter===t?'rgba(59,130,246,.2)':'#1a2234', borderRadius: 4, padding: '1px 5px', color: filter===t?'#3b82f6':'#475569' }}>
                {counts[t as keyof typeof counts]}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* ── List ──────────────────────────────────────────────── */}
      {visible.length === 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 14, padding: '60px 0', background: '#111827', borderRadius: 12, border: '1px solid #1e2d45' }}>
          <div style={{ width: 52, height: 52, borderRadius: '50%', background: '#1a2234', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="1.5"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2" /><rect x="9" y="3" width="6" height="4" rx="1" /><path d="M9 12h6M9 16h4" /></svg>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: '#94a3b8', marginBottom: 5 }}>
              {filter === 'All' ? 'No work orders yet' : `No ${filter === 'InProgress' ? 'in-progress' : filter.toLowerCase()} work orders`}
            </div>
            <div style={{ fontSize: 12, color: '#475569' }}>
              Work orders are created from the Machine Detail page when an alert is raised.
            </div>
          </div>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {visible.map(wo => <WoCard key={wo.id} wo={wo} onProgress={handleProgress} />)}
        </div>
      )}
    </div>
  );
};

export default WorkOrders;
