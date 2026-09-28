import React from 'react';

interface Props {
  title: string;
  value: string | number;
  subtitle?: string;
  icon: React.ReactNode;
  trend?: 'up' | 'down' | 'neutral';
  color?: 'red' | 'amber' | 'blue' | 'emerald' | 'violet';
}

const COLOR_MAP = {
  red:     { bg: 'from-red-500/20 to-red-600/10', border: 'border-red-500/30', icon: 'text-red-400', glow: 'shadow-red-500/10' },
  amber:   { bg: 'from-amber-500/20 to-amber-600/10', border: 'border-amber-500/30', icon: 'text-amber-400', glow: 'shadow-amber-500/10' },
  blue:    { bg: 'from-blue-500/20 to-blue-600/10', border: 'border-blue-500/30', icon: 'text-blue-400', glow: 'shadow-blue-500/10' },
  emerald: { bg: 'from-emerald-500/20 to-emerald-600/10', border: 'border-emerald-500/30', icon: 'text-emerald-400', glow: 'shadow-emerald-500/10' },
  violet:  { bg: 'from-violet-500/20 to-violet-600/10', border: 'border-violet-500/30', icon: 'text-violet-400', glow: 'shadow-violet-500/10' },
};

export const KpiCard: React.FC<Props> = ({ title, value, subtitle, icon, color = 'blue' }) => {
  const c = COLOR_MAP[color];
  return (
    <div className={`relative overflow-hidden bg-gradient-to-br ${c.bg} backdrop-blur border ${c.border} rounded-xl p-4 shadow-lg ${c.glow}`}>
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <p className="text-xs font-medium text-slate-400 uppercase tracking-wider mb-1">{title}</p>
          <p className="text-2xl font-bold text-white leading-none">{value}</p>
          {subtitle && <p className="text-xs text-slate-400 mt-1">{subtitle}</p>}
        </div>
        <div className={`${c.icon} opacity-80`}>{icon}</div>
      </div>
    </div>
  );
};
