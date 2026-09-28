import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { getOeeLosses } from '../services/api';
import type { OeeLossItem, OeeLosses as OeeLossesPayload } from '../types';
import { ErrorState, LoadingState } from '../components/States';

/* Each loss factor keeps one colour everywhere on this page, so the stacked bar,
   the table and the totals can be read against each other without a lookup. */
const FACTOR_COLOR: Record<string, string> = {
  availability: '#ef4444',
  performance: '#f59e0b',
  quality: '#8b5cf6',
};
const FACTOR_LABEL: Record<string, string> = {
  availability: 'Availability',
  performance: 'Performance',
  quality: 'Quality',
};

const fmtMoney = (v?: number) => {
  if (v === undefined || v === null) return '—';
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${Math.round(v / 1_000)}k`;
  return `$${Math.round(v)}`;
};

const pct = (v: number) => `${(v * 100).toFixed(1)}%`;

/* ── Stacked loss bar ───────────────────────────────────────── */
const LossBar: React.FC<{ item: OeeLossItem }> = ({ item }) => {
  // Availability, performance and quality losses are already scaled so that
  // they sum with OEE to exactly 1.0 — so this bar is a true decomposition of
  // the machine's ideal output, not three independent percentages.
  const segments = [
    { key: 'oee', value: item.oee, color: '#10b981' },
    { key: 'availability', value: item.availability_loss, color: FACTOR_COLOR.availability },
    { key: 'performance', value: item.performance_loss, color: FACTOR_COLOR.performance },
    { key: 'quality', value: item.quality_loss, color: FACTOR_COLOR.quality },
  ];
  return (
    <div style={{ display: 'flex', height: 8, borderRadius: 4, overflow: 'hidden', background: '#1a2234' }}>
      {segments.map(s => s.value > 0 && (
        <div
          key={s.key}
          title={`${s.key === 'oee' ? 'Productive' : `${FACTOR_LABEL[s.key]} loss`}: ${pct(s.value)}`}
          style={{ width: `${s.value * 100}%`, background: s.color }}
        />
      ))}
    </div>
  );
};

/* ── Main page ──────────────────────────────────────────────── */
const OeeLosses: React.FC = () => {
  const navigate = useNavigate();
  const [data, setData] = useState<OeeLossesPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [targetOee, setTargetOee] = useState(0.85);

  const load = useCallback(async () => {
    try {
      setData(await getOeeLosses({ target_oee: targetOee }));
      setError(false);
    } catch { setError(true); }
    finally { setLoading(false); }
  }, [targetOee]);

  useEffect(() => { load(); }, [load]);

  if (loading) return <LoadingState message="Computing OEE losses…" />;
  if (error || !data) return <ErrorState onRetry={load} />;

  const machines = data.machines;
  // Worst first by cost of the gap, which is the order to actually act in — a
  // 4-point OEE gap on an expensive line outranks a 15-point gap on a cheap one.
  const ranked = [...machines].sort((a, b) => b.estimated_gap_cost - a.estimated_gap_cost);

  // Pareto: cumulative share of total recoverable cost.
  const totalCost = ranked.reduce((s, m) => s + m.estimated_gap_cost, 0);
  let running = 0;
  const paretoData = ranked.map(m => {
    running += m.estimated_gap_cost;
    return {
      name: m.machine_name,
      cost: Math.round(m.estimated_gap_cost),
      cumulative: totalCost > 0 ? +((running / totalCost) * 100).toFixed(1) : 0,
      factor: m.biggest_loss,
    };
  });

  // How many machines account for 80% of the recoverable cost.
  const vitalFew = paretoData.findIndex(p => p.cumulative >= 80) + 1;

  const factorTotals = Object.entries(data.loss_totals || {})
    .filter(([k]) => k in FACTOR_COLOR)
    .sort((a, b) => b[1] - a[1]);
  const dominantFactor = factorTotals[0]?.[0];

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* ── Header ──────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 800, color: '#f1f5f9', letterSpacing: '-0.5px' }}>
            OEE Loss Analysis
          </h1>
          <p style={{ fontSize: 12, color: '#475569', marginTop: 3 }}>
            Where the plant is losing output, ranked by what it costs to leave alone.
            Window: {data.window_hours}h.
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 11, color: '#64748b', fontWeight: 600 }}>TARGET OEE</span>
          {[0.75, 0.85, 0.95].map(t => (
            <button key={t} onClick={() => setTargetOee(t)}
              style={{
                padding: '5px 11px', borderRadius: 7, fontSize: 12, fontWeight: 600, cursor: 'pointer',
                background: targetOee === t ? 'rgba(59,130,246,.12)' : '#111827',
                border: `1px solid ${targetOee === t ? 'rgba(59,130,246,.35)' : '#1e2d45'}`,
                color: targetOee === t ? '#3b82f6' : '#64748b',
              }}>
              {(t * 100).toFixed(0)}%
            </button>
          ))}
        </div>
      </div>

      {/* ── Headline ────────────────────────────────────────── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14 }}>
        {[
          { l: 'Plant OEE', v: pct(data.plant_oee), s: `target ${pct(data.target_oee)}`, c: '#10b981' },
          {
            l: 'Recoverable / window', v: fmtMoney(data.total_gap_cost),
            s: 'modelled cost of the gap to target', c: '#14b8a6',
          },
          {
            l: 'Biggest loss factor',
            v: dominantFactor ? FACTOR_LABEL[dominantFactor] : '—',
            s: dominantFactor ? `${pct(factorTotals[0][1])} of ideal output` : 'no loss recorded',
            c: dominantFactor ? FACTOR_COLOR[dominantFactor] : '#64748b',
          },
          {
            l: 'Vital few', v: vitalFew > 0 ? `${vitalFew} of ${ranked.length}` : '—',
            s: 'machines carrying 80% of the cost', c: '#8b5cf6',
          },
        ].map(k => (
          <div key={k.l} style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, padding: '14px 16px' }}>
            <div style={{ fontSize: 10, color: '#64748b', fontWeight: 600, letterSpacing: '0.5px', textTransform: 'uppercase' }}>{k.l}</div>
            <div style={{ fontSize: 24, fontWeight: 800, color: k.c, marginTop: 6, lineHeight: 1 }}>{k.v}</div>
            <div style={{ fontSize: 10, color: '#475569', marginTop: 5 }}>{k.s}</div>
          </div>
        ))}
      </div>

      {/* ── Pareto chart ────────────────────────────────────── */}
      <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, padding: '18px 20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
          <h2 style={{ fontSize: 13, fontWeight: 700, color: '#94a3b8', letterSpacing: '0.5px', textTransform: 'uppercase' }}>
            Recoverable cost by machine
          </h2>
          <div style={{ display: 'flex', gap: 12 }}>
            {Object.entries(FACTOR_LABEL).map(([k, label]) => (
              <span key={k} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10, color: '#64748b' }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: FACTOR_COLOR[k] }} />
                {label}
              </span>
            ))}
          </div>
        </div>
        {paretoData.length === 0 ? (
          <div style={{ height: 200, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#475569', fontSize: 13 }}>
            No OEE snapshots in this window yet.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={230}>
            <BarChart data={paretoData} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1a2234" />
              <XAxis dataKey="name" tick={{ fill: '#475569', fontSize: 10 }} interval={0} angle={-25} textAnchor="end" height={54} />
              <YAxis tick={{ fill: '#475569', fontSize: 10 }} width={52}
                tickFormatter={(v: number) => fmtMoney(v)} />
              <Tooltip
                contentStyle={{ background: '#1a2234', border: '1px solid #263551', borderRadius: 8, fontSize: 11 }}
                formatter={(v: unknown, name?: unknown) =>
                  name === 'cost' ? [fmtMoney(v as number), 'Recoverable'] : [`${v}%`, 'Cumulative']}
              />
              {/* Bars coloured by the machine's dominant loss factor, so the chart
                  shows both where the money is and what kind of problem it is. */}
              <Bar dataKey="cost" radius={[3, 3, 0, 0]}>
                {paretoData.map(p => (
                  <Cell key={p.name} fill={FACTOR_COLOR[p.factor] || '#3b82f6'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* ── Per-machine breakdown ───────────────────────────── */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <h2 style={{ fontSize: 13, fontWeight: 700, color: '#94a3b8', letterSpacing: '0.5px', textTransform: 'uppercase' }}>
          Loss decomposition, worst first
        </h2>
        <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, overflow: 'hidden' }}>
          <div style={{
            display: 'grid', gridTemplateColumns: '1.4fr 70px 1fr 90px 100px',
            gap: 12, padding: '9px 16px', background: '#0f1520',
            fontSize: 10, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.4px',
          }}>
            <span>Machine</span><span style={{ textAlign: 'right' }}>OEE</span>
            <span>Decomposition</span><span>Biggest loss</span>
            <span style={{ textAlign: 'right' }}>Recoverable</span>
          </div>
          {ranked.length === 0 && (
            <div style={{ padding: 28, textAlign: 'center', color: '#475569', fontSize: 13 }}>No machines in scope.</div>
          )}
          {ranked.map(m => (
            <div key={m.machine_id}
              onClick={() => navigate(`/machines/${m.machine_id}`)}
              style={{
                display: 'grid', gridTemplateColumns: '1.4fr 70px 1fr 90px 100px', gap: 12,
                padding: '12px 16px', borderTop: '1px solid #1a2234', cursor: 'pointer',
                alignItems: 'center', transition: 'background .15s',
              }}
              onMouseEnter={e => (e.currentTarget as HTMLDivElement).style.background = '#1a2234'}
              onMouseLeave={e => (e.currentTarget as HTMLDivElement).style.background = 'transparent'}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: '#f1f5f9' }}>{m.machine_name}</div>
                <div style={{ fontSize: 10, color: '#475569', marginTop: 2 }}>
                  {m.cost_center_code ? `Cost centre ${m.cost_center_code}` : 'No cost centre'}
                </div>
              </div>
              <div style={{ textAlign: 'right', fontSize: 13, fontWeight: 700, color: m.oee >= 0.8 ? '#10b981' : m.oee >= 0.65 ? '#f59e0b' : '#ef4444' }}>
                {pct(m.oee)}
              </div>
              <div>
                <LossBar item={m} />
                <div style={{ fontSize: 9, color: '#475569', marginTop: 4 }}>
                  A {pct(m.availability_loss)} · P {pct(m.performance_loss)} · Q {pct(m.quality_loss)}
                </div>
              </div>
              <div>
                <span style={{
                  padding: '2px 7px', borderRadius: 5, fontSize: 10, fontWeight: 700,
                  color: FACTOR_COLOR[m.biggest_loss] || '#64748b',
                  background: `${FACTOR_COLOR[m.biggest_loss] || '#64748b'}1f`,
                }}>
                  {(FACTOR_LABEL[m.biggest_loss] || m.biggest_loss).toUpperCase()}
                </span>
                {/* `biggest_loss_pct` arrives already scaled 0-100, unlike the
                    loss fractions above it. */}
                <div style={{ fontSize: 9, color: '#475569', marginTop: 3 }}>{m.biggest_loss_pct.toFixed(1)}% of ideal</div>
              </div>
              <div style={{ textAlign: 'right', fontSize: 13, fontWeight: 700, color: '#14b8a6' }}>
                {fmtMoney(m.estimated_gap_cost)}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Basis ───────────────────────────────────────────── */}
      <div style={{
        background: 'rgba(245,158,11,.06)', border: '1px solid rgba(245,158,11,.2)',
        borderRadius: 10, padding: '12px 14px', fontSize: 11, color: '#94a3b8', lineHeight: 1.6,
      }}>
        <strong style={{ color: '#f59e0b' }}>Basis.</strong> {data.basis}
        {' '}Losses are scaled so availability, performance and quality losses plus OEE
        sum to exactly 1.0, so lost output is attributed once and not triple-counted.
        Every currency figure is a <strong>modelled</strong> estimate from ERP
        cost-centre rates — none of it is a measured saving.
      </div>
    </div>
  );
};

export default OeeLosses;
