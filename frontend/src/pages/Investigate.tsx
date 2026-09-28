import React, { useState, useEffect, useCallback, useRef } from 'react';
import { investigate, getInvestigateSuggestions } from '../services/api';
import type { InvestigateResponse } from '../types';
import { ErrorState } from '../components/States';

/* ── Colours (matching the other pages' inline-style convention) ─────────── */
const SURFACE = '#111827';
const SURFACE2 = '#1a2234';
const BORDER = '#1e2d45';
const PANEL = '#0f1929';
const TEXT = '#f1f5f9';
const MUTED = '#64748b';
const MUTED2 = '#94a3b8';
const DIM = '#475569';
const BLUE = '#3b82f6';
const AMBER = '#f59e0b';
const GREEN = '#10b981';
const TEAL = '#14b8a6';

interface Turn {
  question: string;
  response?: InvestigateResponse;
  error?: boolean;
  pending?: boolean;
}

/* Where the answer came from — shown so nothing is taken on trust. */
const SourceBadge: React.FC<{ source: string }> = ({ source }) => {
  // `source` names the provider that actually answered, so the badge cannot claim
  // one engine while another produced the text.
  const color =
    source === 'cortex' ? TEAL
    : source === 'gemini' ? BLUE
    : source === 'cached' ? MUTED
    : GREEN;
  const label =
    source === 'cortex' ? 'SNOWFLAKE CORTEX'
    : source === 'gemini' ? 'GEMINI'
    : source === 'deterministic' ? 'RULE-BASED'
    : source === 'cached' ? 'CACHED'
    : source.toUpperCase();
  return (
    <span style={{
      padding: '2px 6px', borderRadius: 4, fontSize: 9, fontWeight: 700,
      letterSpacing: '0.5px', color, background: `${color}1f`, flexShrink: 0,
    }}>
      {label}
    </span>
  );
};

/** Renders the retrieved rows the answer was built from. */
const EvidenceTable: React.FC<{ rows: Record<string, unknown>[] }> = ({ rows }) => {
  const [open, setOpen] = useState(false);
  if (!rows.length) return null;

  const columns = Array.from(
    rows.reduce<Set<string>>((acc, row) => {
      Object.keys(row).forEach(k => acc.add(k));
      return acc;
    }, new Set())
  ).filter(c => c !== 'method');

  const fmt = (v: unknown) => {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2);
    if (typeof v === 'boolean') return v ? 'Yes' : 'No';
    if (Array.isArray(v)) return `${v.length} item(s)`;
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
  };

  return (
    <div style={{ marginTop: 10 }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          background: 'transparent', border: 'none', color: MUTED, fontSize: 11,
          cursor: 'pointer', padding: 0, display: 'flex', alignItems: 'center', gap: 5,
        }}
      >
        <span style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }}>▸</span>
        {open ? 'Hide' : 'Show'} evidence ({rows.length} row{rows.length === 1 ? '' : 's'})
      </button>

      {open && (
        <div style={{ marginTop: 8, overflowX: 'auto', border: `1px solid ${BORDER}`, borderRadius: 8 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead>
              <tr style={{ background: SURFACE2 }}>
                {columns.map(c => (
                  <th key={c} style={{
                    textAlign: 'left', padding: '7px 10px', color: MUTED,
                    fontWeight: 600, whiteSpace: 'nowrap', textTransform: 'uppercase',
                    fontSize: 9, letterSpacing: '0.4px',
                  }}>
                    {c.replace(/_/g, ' ')}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i} style={{ borderTop: `1px solid ${BORDER}` }}>
                  {columns.map(c => (
                    <td key={c} style={{ padding: '7px 10px', color: MUTED2, whiteSpace: 'nowrap' }}>
                      {fmt(row[c])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

/* ── Main ────────────────────────────────────────────────────────────────── */
const Investigate: React.FC = () => {
  const [question, setQuestion] = useState('');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    getInvestigateSuggestions()
      .then(s => { setSuggestions(s.questions); setLoadError(false); })
      .catch(() => setLoadError(true));
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [turns]);

  const ask = useCallback(async (q: string) => {
    const text = q.trim();
    if (!text || busy) return;

    setQuestion('');
    setBusy(true);
    setTurns(prev => [...prev, { question: text, pending: true }]);

    try {
      const response = await investigate(text);
      setTurns(prev => prev.map((t, i) =>
        i === prev.length - 1 ? { question: text, response } : t
      ));
    } catch {
      setTurns(prev => prev.map((t, i) =>
        i === prev.length - 1 ? { question: text, error: true } : t
      ));
    } finally {
      setBusy(false);
    }
  }, [busy]);

  if (loadError && !turns.length) {
    return <ErrorState onRetry={() => window.location.reload()} />;
  }

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: 18, height: '100%' }}>

      {/* ── Title ─────────────────────────────────────────────────────────── */}
      <div>
        <h1 style={{ fontSize: 20, fontWeight: 800, color: TEXT, letterSpacing: '-0.5px' }}>
          Root Cause Investigation
        </h1>
        <p style={{ fontSize: 12, color: DIM, marginTop: 3 }}>
          Ask about risk, trends, parts, cost or OEE. Answers are grounded in plant data —
          every claim can be checked against the evidence.
        </p>
      </div>

      {/* ── Starter questions ─────────────────────────────────────────────── */}
      {!turns.length && suggestions.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {suggestions.map(s => (
            <button
              key={s}
              onClick={() => ask(s)}
              style={{
                padding: '7px 12px', borderRadius: 8, background: SURFACE,
                border: `1px solid ${BORDER}`, color: MUTED2, fontSize: 12,
                cursor: 'pointer', transition: 'all .15s',
              }}
              onMouseEnter={e => {
                (e.currentTarget as HTMLButtonElement).style.borderColor = 'rgba(59,130,246,.4)';
                (e.currentTarget as HTMLButtonElement).style.color = TEXT;
              }}
              onMouseLeave={e => {
                (e.currentTarget as HTMLButtonElement).style.borderColor = BORDER;
                (e.currentTarget as HTMLButtonElement).style.color = MUTED2;
              }}
            >
              {s}
            </button>
          ))}
        </div>
      )}

      {/* ── Conversation ──────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, flex: 1 }}>
        {turns.map((turn, i) => (
          <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>

            {/* Question */}
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <div style={{
                maxWidth: '75%', padding: '9px 14px', borderRadius: 10,
                background: 'rgba(59,130,246,.1)', border: '1px solid rgba(59,130,246,.22)',
                color: TEXT, fontSize: 13,
              }}>
                {turn.question}
              </div>
            </div>

            {/* Answer */}
            <div style={{
              background: PANEL, border: '1px solid rgba(59,130,246,.2)',
              borderRadius: 12, padding: '14px 16px',
            }}>
              {turn.pending && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: MUTED, fontSize: 12 }}>
                  <div style={{
                    width: 14, height: 14, borderRadius: '50%',
                    border: '2px solid rgba(59,130,246,.25)', borderTopColor: BLUE,
                    animation: 'spin .8s linear infinite',
                  }} />
                  Investigating…
                  <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
                </div>
              )}

              {turn.error && (
                <div style={{ color: '#ef4444', fontSize: 12 }}>
                  Could not reach the investigation service. Please retry.
                </div>
              )}

              {turn.response && (
                <>
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8,
                    flexWrap: 'wrap',
                  }}>
                    <span style={{
                      fontSize: 9, fontWeight: 700, letterSpacing: '0.6px',
                      color: BLUE, textTransform: 'uppercase',
                    }}>
                      AI Investigation
                    </span>
                    <SourceBadge source={turn.response.source} />
                    <span style={{ fontSize: 10, color: DIM }}>
                      intent: {turn.response.intent.replace(/_/g, ' ')}
                    </span>
                    {turn.response.machine && (
                      <span style={{
                        fontSize: 10, color: AMBER, padding: '1px 5px',
                        borderRadius: 4, background: 'rgba(245,158,11,.12)',
                      }}>
                        {turn.response.machine}
                      </span>
                    )}
                  </div>

                  <p style={{ color: TEXT, fontSize: 13, lineHeight: 1.65 }}>
                    {turn.response.answer}
                  </p>

                  <EvidenceTable rows={turn.response.evidence} />
                </>
              )}
            </div>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {/* ── Composer ──────────────────────────────────────────────────────── */}
      <form
        onSubmit={e => { e.preventDefault(); ask(question); }}
        style={{ display: 'flex', gap: 10, position: 'sticky', bottom: 0, paddingTop: 6 }}
      >
        <input
          value={question}
          onChange={e => setQuestion(e.target.value)}
          placeholder="e.g. Why is M-102 at risk?"
          maxLength={500}
          style={{
            flex: 1, padding: '11px 14px', borderRadius: 10, background: SURFACE,
            border: `1px solid ${BORDER}`, color: TEXT, fontSize: 13, outline: 'none',
          }}
          onFocus={e => (e.currentTarget.style.borderColor = 'rgba(59,130,246,.45)')}
          onBlur={e => (e.currentTarget.style.borderColor = BORDER)}
        />
        <button
          type="submit"
          disabled={busy || !question.trim()}
          style={{
            padding: '11px 20px', borderRadius: 10,
            background: busy || !question.trim() ? SURFACE2 : BLUE,
            border: 'none', color: busy || !question.trim() ? MUTED : '#fff',
            fontSize: 13, fontWeight: 600,
            cursor: busy || !question.trim() ? 'default' : 'pointer',
          }}
        >
          Ask
        </button>
      </form>
    </div>
  );
};

export default Investigate;
