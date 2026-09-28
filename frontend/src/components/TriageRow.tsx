import React from 'react';
import { useNavigate } from 'react-router-dom';
import type { Alert } from '../types';

const SEV_COLOR: Record<string, string> = { Critical:'#ef4444', Warning:'#f59e0b', Watch:'#eab308', Normal:'#10b981' };
const SEV_BG:    Record<string, string> = { Critical:'rgba(239,68,68,.1)', Warning:'rgba(245,158,11,.1)', Watch:'rgba(234,179,8,.1)', Normal:'rgba(16,185,129,.1)' };
const SEV_BORDER:Record<string, string> = { Critical:'rgba(239,68,68,.25)', Warning:'rgba(245,158,11,.25)', Watch:'rgba(234,179,8,.2)', Normal:'rgba(16,185,129,.2)' };

const IMPACT_COLOR: Record<string, string> = { High:'#ef4444', Medium:'#f59e0b', Low:'#64748b' };

export interface TriageRowProps {
  alert: Alert;
  /** True while a mutation for this alert is in flight, so buttons can lock. */
  busy?: boolean;
  onAcknowledge: (id: number) => void;
  onDismiss: (id: number) => void;
  onCreateWorkOrder: (alert: Alert) => void;
  /** Set when a work order already exists, so the action is not offered twice. */
  hasWorkOrder?: boolean;
}

/**
 * One row in the triage queue.
 *
 * Carries the three decisions an operator actually makes on an alert —
 * acknowledge, dismiss, raise a work order — next to the evidence needed to make
 * them: priority, severity, what is committed to run on the machine, and whether
 * the part is on the shelf. Previously acknowledging was the only action
 * available, and only on a different screen.
 */
const TriageRow: React.FC<TriageRowProps> = ({
  alert, busy, onAcknowledge, onDismiss, onCreateWorkOrder, hasWorkOrder,
}) => {
  const navigate = useNavigate();
  const sev = alert.severity;
  const mode = (alert.failure_mode || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  const impact = alert.production_impact;

  const btn = (label: string, onClick: () => void, tone: 'primary' | 'muted' | 'danger') => {
    const tones = {
      primary: { bg:'rgba(59,130,246,.1)', bd:'rgba(59,130,246,.28)', fg:'#3b82f6' },
      muted:   { bg:'rgba(100,116,139,.1)', bd:'rgba(100,116,139,.25)', fg:'#94a3b8' },
      danger:  { bg:'rgba(239,68,68,.08)',  bd:'rgba(239,68,68,.22)',  fg:'#f87171' },
    }[tone];
    return (
      <button
        disabled={busy}
        onClick={e => { e.stopPropagation(); onClick(); }}
        style={{
          padding:'4px 10px', borderRadius:6, background:tones.bg,
          border:`1px solid ${tones.bd}`, color:tones.fg, fontSize:10, fontWeight:600,
          cursor: busy ? 'wait' : 'pointer', opacity: busy ? .5 : 1, whiteSpace:'nowrap',
        }}
      >
        {label}
      </button>
    );
  };

  return (
    <div
      onClick={() => navigate(`/machines/${alert.machine_id}`)}
      style={{
        display:'grid', gridTemplateColumns:'54px 1fr 150px auto', gap:12,
        alignItems:'center', padding:'11px 14px', borderRadius:10, background:'#111827',
        border:`1px solid ${sev === 'Critical' ? SEV_BORDER.Critical : '#1e2d45'}`,
        cursor:'pointer', transition:'background .15s',
      }}
      onMouseEnter={e => (e.currentTarget as HTMLDivElement).style.background = '#1a2234'}
      onMouseLeave={e => (e.currentTarget as HTMLDivElement).style.background = '#111827'}
    >
      {/* Priority — the number the queue is ordered by, so it leads. */}
      <div style={{ textAlign:'center' }}>
        <div style={{ fontSize:17, fontWeight:800, color:SEV_COLOR[sev] || '#94a3b8', lineHeight:1 }}>
          {alert.maintenance_priority != null ? alert.maintenance_priority.toFixed(0) : '—'}
        </div>
        <div style={{ fontSize:8, color:'#475569', marginTop:2, letterSpacing:'0.3px' }}>PRIORITY</div>
      </div>

      {/* Machine and cause */}
      <div style={{ minWidth:0 }}>
        <div style={{ display:'flex', alignItems:'center', gap:6, marginBottom:2 }}>
          {sev === 'Critical' && alert.status === 'Active' && (
            <span style={{ width:6, height:6, borderRadius:'50%', background:'#ef4444', flexShrink:0, animation:'pulse-dot 1.5s infinite' }} />
          )}
          <span style={{ fontSize:13, fontWeight:700, color:'#f1f5f9', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
            {alert.machine_name || `M-${alert.machine_id}`}
          </span>
          <span style={{ padding:'1px 6px', borderRadius:4, background:SEV_BG[sev], color:SEV_COLOR[sev], fontSize:9, fontWeight:700, letterSpacing:'0.4px', flexShrink:0 }}>
            {sev.toUpperCase()}
          </span>
          {alert.degraded_mode && (
            <span title="Risk estimated by the rules fallback — ML model unavailable"
              style={{ padding:'1px 5px', borderRadius:4, background:'rgba(245,158,11,.12)', color:'#f59e0b', fontSize:9, fontWeight:700 }}>
              DEGRADED
            </span>
          )}
        </div>
        <div style={{ fontSize:11, color:'#64748b', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
          {mode || 'Anomaly detected'}
          {alert.part_needed && (
            <>
              <span style={{ color:'#334155' }}> · </span>
              <span style={{ color: alert.part_available ? '#64748b' : '#f87171' }}>
                {alert.part_needed}{alert.part_available ? '' : ' (not in stock)'}
              </span>
            </>
          )}
        </div>
        <div style={{ fontSize:10, color:'#475569', marginTop:3, display:'flex', alignItems:'center', gap:6, flexWrap:'wrap' }}>
          <span>{new Date(alert.created_at).toLocaleString('en', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' })}</span>
          <span style={{ color:'#334155' }}>·</span>
          <span style={{ color: alert.status === 'Active' ? '#3b82f6' : '#64748b', fontWeight:600 }}>{alert.status}</span>
          {impact && (
            <>
              <span style={{ color:'#334155' }}>·</span>
              <span title="Production impact, resolved from the ERP schedule where orders exist">
                impact <strong style={{ color:IMPACT_COLOR[impact] || '#94a3b8' }}>{impact.toLowerCase()}</strong>
              </span>
            </>
          )}
        </div>
      </div>

      {/* Risk and modelled exposure */}
      <div style={{ textAlign:'right' }}>
        <div style={{ fontSize:18, fontWeight:800, color:SEV_COLOR[sev], lineHeight:1 }}>
          {alert.risk_score != null ? `${alert.risk_score.toFixed(0)}%` : 'n/a'}
        </div>
        <div style={{ fontSize:9, color:'#475569', marginTop:2 }}>
          {alert.risk_score != null ? 'ML risk' : 'no ML score'}
        </div>
        {alert.estimated_loss_avoided ? (
          <div style={{ fontSize:10, color:'#14b8a6', marginTop:4 }}>
            {alert.estimated_loss_avoided >= 1000
              ? `$${Math.round(alert.estimated_loss_avoided / 1000)}k`
              : `$${Math.round(alert.estimated_loss_avoided)}`} at stake
          </div>
        ) : null}
      </div>

      {/* Actions */}
      <div style={{ display:'flex', flexDirection:'column', gap:5, alignItems:'flex-end' }}>
        {alert.status === 'Active' && btn('Acknowledge', () => onAcknowledge(alert.id), 'primary')}
        {alert.status === 'Acknowledged' && (
          <span style={{ fontSize:10, color:'#475569', fontStyle:'italic' }}>Acknowledged</span>
        )}
        <div style={{ display:'flex', gap:5 }}>
          {hasWorkOrder
            ? <span title="A work order is already open for this machine"
                style={{ fontSize:10, color:'#10b981', fontWeight:600 }}>WO raised</span>
            : btn('+ Work order', () => onCreateWorkOrder(alert), 'muted')}
          {alert.status !== 'Dismissed' && btn('Dismiss', () => onDismiss(alert.id), 'danger')}
        </div>
      </div>
    </div>
  );
};

export default TriageRow;
