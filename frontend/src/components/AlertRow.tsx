import React from 'react';
import { useNavigate } from 'react-router-dom';
import { RiskBadge } from './RiskBadge';
import type { Alert } from '../types';

interface Props {
  alert: Alert;
  onAcknowledge?: (id: number) => void;
}

const formatTime = (ts: string) => {
  const d = new Date(ts);
  return d.toLocaleString('en-IN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
};

const formatFailureMode = (mode?: string) =>
  mode ? mode.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) : '—';

export const AlertRow: React.FC<Props> = ({ alert, onAcknowledge }) => {
  const navigate = useNavigate();

  return (
    <div
      className={`flex items-center gap-3 px-4 py-3 rounded-lg border transition-all duration-200 hover:bg-slate-700/40 cursor-pointer
        ${alert.severity === 'Critical' ? 'border-red-500/30 bg-red-500/5' :
          alert.severity === 'Warning' ? 'border-amber-500/30 bg-amber-500/5' :
          'border-slate-700/40 bg-slate-800/30'}`}
      onClick={() => navigate(`/machines/${alert.machine_id}`)}
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-white text-sm">{alert.machine_name || `Machine-${alert.machine_id}`}</span>
          <RiskBadge severity={alert.severity} size="sm" pulse={alert.severity === 'Critical'} />
        </div>
        <div className="text-xs text-slate-400 mt-0.5">{formatFailureMode(alert.failure_mode)}</div>
      </div>

      <div className="text-center hidden sm:block">
        <div className="text-lg font-bold text-white">{alert.risk_score.toFixed(0)}%</div>
        <div className="text-xs text-slate-500">Risk</div>
      </div>

      <div className="text-center hidden md:block">
        <div className="text-sm font-semibold text-white">{alert.maintenance_priority?.toFixed(0) || '—'}</div>
        <div className="text-xs text-slate-500">Priority</div>
      </div>

      <div className="text-right">
        <div className="text-xs text-slate-400">{formatTime(alert.created_at)}</div>
        <div className={`text-xs mt-0.5 font-medium
          ${alert.status === 'Active' ? 'text-blue-400' :
            alert.status === 'Acknowledged' ? 'text-slate-400' : 'text-slate-500'}`}>
          {alert.status}
        </div>
      </div>

      {onAcknowledge && alert.status === 'Active' && (
        <button
          onClick={e => { e.stopPropagation(); onAcknowledge(alert.id); }}
          className="ml-2 text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors"
        >
          ACK
        </button>
      )}
    </div>
  );
};
