import React from 'react';

type Severity = 'Normal' | 'Watch' | 'Warning' | 'Critical' | string;

const SEVERITY_CONFIG: Record<string, { bg: string; text: string; dot: string; label: string }> = {
  Critical: { bg: 'bg-red-500/20 border border-red-500/40', text: 'text-red-400', dot: 'bg-red-500', label: 'CRITICAL' },
  Warning:  { bg: 'bg-amber-500/20 border border-amber-500/40', text: 'text-amber-400', dot: 'bg-amber-500', label: 'WARNING' },
  Watch:    { bg: 'bg-yellow-500/20 border border-yellow-500/40', text: 'text-yellow-400', dot: 'bg-yellow-500', label: 'WATCH' },
  Normal:   { bg: 'bg-emerald-500/20 border border-emerald-500/40', text: 'text-emerald-400', dot: 'bg-emerald-500', label: 'NORMAL' },
};

interface Props {
  severity: Severity;
  size?: 'sm' | 'md' | 'lg';
  pulse?: boolean;
}

export const RiskBadge: React.FC<Props> = ({ severity, size = 'md', pulse = false }) => {
  const config = SEVERITY_CONFIG[severity] || SEVERITY_CONFIG.Normal;
  const sizeClass = size === 'sm' ? 'text-xs px-2 py-0.5' : size === 'lg' ? 'text-sm px-4 py-1.5' : 'text-xs px-3 py-1';

  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full font-bold tracking-wider ${config.bg} ${config.text} ${sizeClass}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${config.dot} ${pulse && severity !== 'Normal' ? 'animate-pulse' : ''}`} />
      {config.label}
    </span>
  );
};
