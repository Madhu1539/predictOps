import React, { useCallback, useEffect, useState } from 'react';
import { getModelInfo } from '../services/api';
import type { ModelInfo as ModelInfoPayload, ModelMetrics } from '../types';
import { ErrorState, LoadingState } from '../components/States';

const SEV_COLOR: Record<string, string> = {
  Critical: '#ef4444', Warning: '#f59e0b', Watch: '#eab308', Normal: '#10b981',
};

const fmt = (v?: number, digits = 3) =>
  v === undefined || v === null ? '—' : v.toFixed(digits);

/* ── Metric tile ────────────────────────────────────────────── */
const Metric: React.FC<{ label: string; value: string; note?: string; color?: string }> =
  ({ label, value, note, color = '#f1f5f9' }) => (
    <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, padding: '14px 16px' }}>
      <div style={{ fontSize: 10, color: '#64748b', fontWeight: 600, letterSpacing: '0.5px', textTransform: 'uppercase' }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 800, color, marginTop: 6, lineHeight: 1 }}>{value}</div>
      {note && <div style={{ fontSize: 10, color: '#475569', marginTop: 5 }}>{note}</div>}
    </div>
  );

/* ── Main page ──────────────────────────────────────────────── */
const ModelCard: React.FC = () => {
  const [info, setInfo] = useState<ModelInfoPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(async () => {
    try { setInfo(await getModelInfo()); setError(false); }
    catch { setError(true); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) return <LoadingState message="Loading model report…" />;
  if (error || !info) return <ErrorState onRetry={load} />;

  const m: ModelMetrics | undefined = info.selected_metrics;
  const importance = (info.global_importance || []).slice(0, 12);
  const maxWeight = Math.max(...importance.map(i => Math.abs(i.weight)), 0.0001);
  const candidates = Object.entries(info.all_candidates || {});

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>

      <div>
        <h1 style={{ fontSize: 20, fontWeight: 800, color: '#f1f5f9', letterSpacing: '-0.5px' }}>
          Model Transparency
        </h1>
        <p style={{ fontSize: 12, color: '#475569', marginTop: 3 }}>
          What the risk model is, how well it scores, and what drives it. A prediction
          nobody can interrogate is not evidence.
        </p>
      </div>

      {!info.available && (
        <div style={{
          background: 'rgba(239,68,68,.07)', border: '1px solid rgba(239,68,68,.25)',
          borderRadius: 10, padding: '12px 14px', fontSize: 12, color: '#fca5a5',
        }}>
          No trained model report is available. Risk scores are coming from the
          rules-based fallback, and the system is running degraded.
          {info.degraded_mode_note ? ` ${info.degraded_mode_note}` : ''}
        </div>
      )}

      {/* ── Identity ────────────────────────────────────────── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14 }}>
        <Metric label="Selected model" value={info.selected_model || '—'}
          note={info.model_loaded ? 'loaded and scoring' : 'not loaded'}
          color={info.model_loaded ? '#10b981' : '#ef4444'} />
        <Metric label="Recall" value={fmt(m?.recall)}
          note="share of real failures caught" color="#3b82f6" />
        <Metric label="Precision" value={fmt(m?.precision)}
          note="share of alerts that were real" color="#8b5cf6" />
        <Metric label="ROC AUC" value={fmt(m?.roc_auc)}
          note={`${info.train_samples ?? '—'} train / ${info.test_samples ?? '—'} test rows`}
          color="#14b8a6" />
      </div>

      {/* ── Why this model ─────────────────────────────────── */}
      {info.selection_rule && (
        <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, padding: '16px 18px' }}>
          <h2 style={{ fontSize: 13, fontWeight: 700, color: '#94a3b8', letterSpacing: '0.5px', textTransform: 'uppercase', marginBottom: 8 }}>
            Why this model was chosen
          </h2>
          <p style={{ fontSize: 12, color: '#94a3b8', lineHeight: 1.65, margin: 0 }}>{info.selection_rule}</p>
          {info.trained_at && (
            <p style={{ fontSize: 11, color: '#475569', marginTop: 8, marginBottom: 0 }}>
              Trained {new Date(info.trained_at).toLocaleString()} · {info.feature_count ?? '—'} features
            </p>
          )}
        </div>
      )}

      {/* ── Candidates ─────────────────────────────────────── */}
      {candidates.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <h2 style={{ fontSize: 13, fontWeight: 700, color: '#94a3b8', letterSpacing: '0.5px', textTransform: 'uppercase' }}>
            Candidates considered
          </h2>
          <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, overflow: 'hidden' }}>
            <div style={{
              display: 'grid', gridTemplateColumns: '1.6fr repeat(5, 1fr)', gap: 10,
              padding: '9px 16px', background: '#0f1520', fontSize: 10, fontWeight: 600,
              color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.4px',
            }}>
              <span>Model</span>
              <span style={{ textAlign: 'right' }}>Recall</span>
              <span style={{ textAlign: 'right' }}>Precision</span>
              <span style={{ textAlign: 'right' }}>F1</span>
              <span style={{ textAlign: 'right' }}>ROC AUC</span>
              <span style={{ textAlign: 'right' }}>Brier</span>
            </div>
            {candidates.map(([name, metrics]) => {
              const selected = name === info.selected_model;
              return (
                <div key={name} style={{
                  display: 'grid', gridTemplateColumns: '1.6fr repeat(5, 1fr)', gap: 10,
                  padding: '11px 16px', borderTop: '1px solid #1a2234', alignItems: 'center',
                  background: selected ? 'rgba(59,130,246,.05)' : 'transparent',
                }}>
                  <span style={{ fontSize: 12, fontWeight: selected ? 700 : 500, color: selected ? '#3b82f6' : '#94a3b8' }}>
                    {name}{selected && ' · selected'}
                  </span>
                  <span style={{ textAlign: 'right', fontSize: 12, color: '#f1f5f9' }}>{fmt(metrics.recall)}</span>
                  <span style={{ textAlign: 'right', fontSize: 12, color: '#f1f5f9' }}>{fmt(metrics.precision)}</span>
                  <span style={{ textAlign: 'right', fontSize: 12, color: '#94a3b8' }}>{fmt(metrics.f1)}</span>
                  <span style={{ textAlign: 'right', fontSize: 12, color: '#94a3b8' }}>{fmt(metrics.roc_auc)}</span>
                  <span style={{ textAlign: 'right', fontSize: 12, color: '#94a3b8' }}>{fmt(metrics.brier_score)}</span>
                </div>
              );
            })}
          </div>
          <p style={{ fontSize: 11, color: '#475569', margin: 0, lineHeight: 1.6 }}>
            Recall is prioritised: a missed failure costs a breakdown, a false alarm costs
            an inspection. Note that the best-calibrated candidate by Brier score is not
            the selected one — better calibration came at a large cost in recall, so it
            was rejected and kept here so the trade-off stays visible.
          </p>
        </div>
      )}

      {/* ── Global importance ──────────────────────────────── */}
      {importance.length > 0 && (
        <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, padding: '18px 20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
            <h2 style={{ fontSize: 13, fontWeight: 700, color: '#94a3b8', letterSpacing: '0.5px', textTransform: 'uppercase' }}>
              What drives the model, fleet-wide
            </h2>
            <span style={{ fontSize: 10, color: '#475569' }}>{importance[0]?.method}</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
            {importance.map(f => (
              <div key={f.feature}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
                  <span style={{ fontSize: 11, color: '#94a3b8' }}>{f.label || f.feature}</span>
                  <span style={{ fontSize: 11, fontWeight: 700, color: '#f1f5f9' }}>{f.weight.toFixed(4)}</span>
                </div>
                <div style={{ height: 5, borderRadius: 3, background: '#1a2234', overflow: 'hidden' }}>
                  <div style={{
                    height: '100%', borderRadius: 3, background: '#3b82f6',
                    width: `${(Math.abs(f.weight) / maxWeight) * 100}%`,
                  }} />
                </div>
              </div>
            ))}
          </div>
          <p style={{ fontSize: 11, color: '#475569', marginTop: 14, marginBottom: 0, lineHeight: 1.6 }}>
            Fleet-wide importance, not a per-machine explanation. For one machine, the
            Top Risk Factors panel on its detail page shows that alert's own attribution.
          </p>
        </div>
      )}

      {/* ── Risk bands ─────────────────────────────────────── */}
      {(info.risk_bands || []).length > 0 && (
        <div style={{ background: '#111827', border: '1px solid #1e2d45', borderRadius: 12, padding: '18px 20px' }}>
          <h2 style={{ fontSize: 13, fontWeight: 700, color: '#94a3b8', letterSpacing: '0.5px', textTransform: 'uppercase', marginBottom: 12 }}>
            Risk score to severity
          </h2>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {info.risk_bands!.map(b => (
              <div key={b.severity} style={{
                padding: '8px 14px', borderRadius: 9,
                background: `${SEV_COLOR[b.severity] || '#64748b'}14`,
                border: `1px solid ${SEV_COLOR[b.severity] || '#64748b'}3a`,
              }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: SEV_COLOR[b.severity] || '#94a3b8', letterSpacing: '0.4px' }}>
                  {b.severity.toUpperCase()}
                </div>
                <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 2 }}>{b.min}–{b.max}%</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Limitations ────────────────────────────────────── */}
      <div style={{
        background: 'rgba(245,158,11,.06)', border: '1px solid rgba(245,158,11,.2)',
        borderRadius: 10, padding: '12px 14px', fontSize: 11, color: '#94a3b8', lineHeight: 1.65,
      }}>
        <strong style={{ color: '#f59e0b' }}>Limitations.</strong> This model is trained on
        SYNTHETIC data with authored degradation patterns. Applied to a real factory it is
        unvalidated transfer: the scores are indicative, not proven for your equipment. The
        fitted model is also non-monotonic at extreme sensor values, which is why every
        machine additionally carries a model-free deviation score and a check against
        published absolute limits (ISO 10816-1 vibration zones, lubricant temperature).
        Where the signals disagree, prefer the deviation score.
        {info.note ? ` ${info.note}` : ''}
      </div>
    </div>
  );
};

export default ModelCard;
