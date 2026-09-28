import React from 'react';
import { useNavigate } from 'react-router-dom';
import { RiskBadge } from './RiskBadge';
import type { Machine } from '../types';

const RISK_RING: Record<string, string> = {
  Critical: 'ring-red-500/60 shadow-red-500/20',
  Warning:  'ring-amber-500/60 shadow-amber-500/20',
  Watch:    'ring-yellow-500/40 shadow-yellow-500/10',
  Normal:   'ring-slate-700/40 shadow-none',
};

const CRITICALITY_COLOR: Record<string, string> = {
  High:   'text-red-400',
  Medium: 'text-amber-400',
  Low:    'text-slate-400',
};

interface Props {
  machine: Machine;
}

export const MachineCard: React.FC<Props> = ({ machine }) => {
  const navigate = useNavigate();
  const severity = machine.severity || 'Normal';
  const ring = RISK_RING[severity] || RISK_RING.Normal;
  const risk = machine.latest_risk ?? 0;
  const priority = machine.maintenance_priority ?? 0;

  const getRiskColor = (r: number) => {
    if (r >= 75) return '#ef4444';
    if (r >= 45) return '#f59e0b';
    if (r >= 25) return '#eab308';
    return '#10b981';
  };

  return (
    <div
      onClick={() => navigate(`/machines/${machine.id}`)}
      className={`relative bg-slate-800/60 backdrop-blur border border-slate-700/50 rounded-xl p-4 cursor-pointer
        hover:bg-slate-800/80 hover:border-slate-600/60 transition-all duration-200
        ring-1 shadow-lg ${ring} group`}
    >
      {/* Status indicator */}
      {severity === 'Critical' && (
        <span className="absolute top-3 right-3 w-2.5 h-2.5 rounded-full bg-red-500 animate-ping" />
      )}

      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="font-bold text-white text-base group-hover:text-blue-300 transition-colors">
            {machine.name}
          </div>
          <div className="text-xs text-slate-400 mt-0.5">{machine.type}</div>
          <div className="text-xs text-slate-500">{machine.location}</div>
        </div>
        <RiskBadge severity={severity} pulse={severity === 'Critical'} />
      </div>

      {/* Risk gauge */}
      <div className="mb-3">
        <div className="flex justify-between text-xs mb-1">
          <span className="text-slate-400">Failure Risk</span>
          <span className="font-bold" style={{ color: getRiskColor(risk) }}>
            {risk.toFixed(0)}%
          </span>
        </div>
        <div className="w-full bg-slate-700 rounded-full h-1.5 overflow-hidden">
          <div
            className="h-full rounded-full transition-all duration-500"
            style={{
              width: `${Math.min(risk, 100)}%`,
              backgroundColor: getRiskColor(risk),
              boxShadow: risk >= 75 ? `0 0 8px ${getRiskColor(risk)}60` : undefined,
            }}
          />
        </div>
      </div>

      {/* Metrics row */}
      <div className="flex items-center justify-between text-xs">
        <div>
          <span className="text-slate-500">Priority </span>
          <span className="font-semibold text-white">{priority.toFixed(0)}</span>
        </div>
        <div>
          <span className="text-slate-500">Criticality </span>
          <span className={`font-semibold ${CRITICALITY_COLOR[machine.criticality] || 'text-slate-300'}`}>
            {machine.criticality.toUpperCase()}
          </span>
        </div>
      </div>
    </div>
  );
};
