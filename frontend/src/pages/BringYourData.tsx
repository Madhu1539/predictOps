import React, { useState, useCallback, useRef } from 'react';
import {
  createDataset, previewDataset, uploadDataset, getReport, deleteDataset, reportHtmlUrl,
} from '../services/api';
import type { ColumnMappingPreview, UploadResult, AnalysisReport, ReportMachine } from '../types';

/* ── Colours, matching the inline-style convention used across the app ────── */
const SURFACE = '#111827';
const SURFACE2 = '#1a2234';
const BORDER = '#1e2d45';
const TEXT = '#f1f5f9';
const MUTED = '#64748b';
const MUTED2 = '#94a3b8';
const DIM = '#475569';
const BLUE = '#3b82f6';
const GREEN = '#10b981';
const AMBER = '#f59e0b';
const RED = '#ef4444';
const TEAL = '#14b8a6';

const SEVERITY_COLOUR: Record<string, string> = {
  Critical: RED, Warning: AMBER, Watch: BLUE, Normal: GREEN,
};
const CONFIDENCE_COLOUR: Record<string, string> = {
  high: GREEN, medium: AMBER, low: RED,
};
const QUALITY_COLOUR: Record<string, string> = {
  ok: GREEN, warnings: AMBER, unusable: RED,
};

type Step = 'idle' | 'previewing' | 'previewed' | 'uploading' | 'done';

const Pill: React.FC<{ text: string; colour: string }> = ({ text, colour }) => (
  <span style={{
    padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 700,
    letterSpacing: '0.4px', color: colour, background: `${colour}1f`,
    textTransform: 'uppercase', whiteSpace: 'nowrap',
  }}>{text}</span>
);

const Section: React.FC<{ title: string; sub?: string; children: React.ReactNode }> = ({ title, sub, children }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
    <div>
      <div style={{ fontSize: 13, fontWeight: 700, color: TEXT }}>{title}</div>
      {sub && <div style={{ fontSize: 11, color: DIM, marginTop: 2 }}>{sub}</div>}
    </div>
    {children}
  </div>
);

const BringYourData: React.FC = () => {
  const [step, setStep] = useState<Step>('idle');
  const [datasetName, setDatasetName] = useState('My Factory');
  const [csvText, setCsvText] = useState<string>('');
  const [fileName, setFileName] = useState<string>('');
  const [datasetId, setDatasetId] = useState<number | null>(null);
  const [preview, setPreview] = useState<ColumnMappingPreview | null>(null);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [convertF, setConvertF] = useState(false);
  const [machineType, setMachineType] = useState('Unknown');
  const [expanded, setExpanded] = useState<number | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const readFile = useCallback((file: File) => {
    setError(null);
    const reader = new FileReader();
    reader.onload = () => {
      setCsvText(String(reader.result ?? ''));
      setFileName(file.name);
      setStep('idle');
      setPreview(null);
      setResult(null);
      setReport(null);
    };
    reader.onerror = () => setError('Could not read that file.');
    // Read client-side, then POST the text: the API takes text/csv, which avoids a
    // multipart dependency on the backend.
    reader.readAsText(file);
  }, []);

  const runPreview = useCallback(async () => {
    if (!csvText) return;
    setStep('previewing');
    setError(null);
    try {
      const ds = datasetId
        ? { id: datasetId }
        : await createDataset(datasetName || 'Uploaded dataset');
      setDatasetId(ds.id);
      const p = await previewDataset(ds.id, csvText);
      setPreview(p);
      setConvertF(p.temperature_looks_fahrenheit);
      setStep('previewed');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Preview failed.');
      setStep('idle');
    }
  }, [csvText, datasetId, datasetName]);

  const runUpload = useCallback(async () => {
    if (!datasetId || !csvText) return;
    setStep('uploading');
    setError(null);
    try {
      const r = await uploadDataset(datasetId, csvText, {
        convertFahrenheit: convertF,
        machineType,
      });
      setResult(r);
      setReport(await getReport(datasetId));
      setStep('done');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Upload failed.');
      setStep('previewed');
    }
  }, [datasetId, csvText, convertF, machineType]);

  const reset = useCallback(async () => {
    if (datasetId) {
      try { await deleteDataset(datasetId); } catch { /* already gone */ }
    }
    setStep('idle'); setDatasetId(null); setPreview(null);
    setResult(null); setReport(null); setCsvText(''); setFileName('');
    setError(null); setExpanded(null);
  }, [datasetId]);

  const busy = step === 'previewing' || step === 'uploading';

  return (
    <div className="animate-in" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>

      <div>
        <h1 style={{ fontSize: 20, fontWeight: 800, color: TEXT, letterSpacing: '-0.5px' }}>
          Bring Your Own Data
        </h1>
        <p style={{ fontSize: 12, color: DIM, marginTop: 3, maxWidth: 780, lineHeight: 1.6 }}>
          Upload a CSV of real sensor readings and get a report on your machines. Your data
          goes through the same pipeline as the demo fleet — and the built-in simulator is
          never allowed to write to it, so every reading in your report is yours.
        </p>
      </div>

      {error && (
        <div style={{
          padding: '10px 14px', borderRadius: 8, background: 'rgba(239,68,68,.08)',
          border: '1px solid rgba(239,68,68,.25)', color: RED, fontSize: 12,
        }}>{error}</div>
      )}

      {/* ── Step 1: choose a file ─────────────────────────────────────────── */}
      <Section
        title="1. Choose a CSV"
        sub="Column names are detected automatically — vibration, temperature, RPM are required. A machine/asset column and a timestamp are used if present."
      >
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            value={datasetName}
            onChange={e => setDatasetName(e.target.value)}
            placeholder="Dataset name"
            disabled={!!datasetId}
            style={{
              padding: '9px 12px', borderRadius: 8, background: SURFACE,
              border: `1px solid ${BORDER}`, color: TEXT, fontSize: 12, width: 190,
            }}
          />
          <input
            ref={fileInput}
            type="file"
            accept=".csv,text/csv"
            onChange={e => { const f = e.target.files?.[0]; if (f) readFile(f); }}
            style={{ display: 'none' }}
          />
          <button
            onClick={() => fileInput.current?.click()}
            style={{
              padding: '9px 16px', borderRadius: 8, background: SURFACE,
              border: `1px solid ${BORDER}`, color: MUTED2, fontSize: 12, cursor: 'pointer',
            }}
          >
            {fileName || 'Select CSV file…'}
          </button>
          <button
            onClick={runPreview}
            disabled={!csvText || busy}
            style={{
              padding: '9px 18px', borderRadius: 8,
              background: !csvText || busy ? SURFACE2 : BLUE,
              border: 'none', color: !csvText || busy ? MUTED : '#fff',
              fontSize: 12, fontWeight: 600,
              cursor: !csvText || busy ? 'default' : 'pointer',
            }}
          >
            {step === 'previewing' ? 'Checking…' : 'Check mapping'}
          </button>
          {(datasetId || csvText) && (
            <button
              onClick={reset}
              style={{
                padding: '9px 14px', borderRadius: 8, background: 'transparent',
                border: `1px solid ${BORDER}`, color: MUTED, fontSize: 12, cursor: 'pointer',
              }}
            >
              Start over
            </button>
          )}
        </div>
      </Section>

      {/* ── Step 2: confirm the mapping ───────────────────────────────────── */}
      {preview && (
        <Section
          title="2. Confirm how your columns were read"
          sub="Nothing has been saved yet. This is what the importer thinks your headers mean."
        >
          <div style={{ background: SURFACE, border: `1px solid ${BORDER}`, borderRadius: 10, padding: 14 }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(250px,1fr))', gap: 8 }}>
              {Object.entries(preview.mapping).map(([field, column]) => (
                <div key={field} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
                  <span style={{ color: MUTED2, minWidth: 130 }}>{field.replace(/_/g, ' ')}</span>
                  <span style={{ color: DIM }}>←</span>
                  <span style={{ color: TEXT, fontFamily: 'ui-monospace, monospace', fontSize: 11 }}>
                    {column}
                  </span>
                  {preview.confidence[field] === 'partial' && <Pill text="fuzzy" colour={AMBER} />}
                </div>
              ))}
            </div>

            {preview.unmapped_headers.length > 0 && (
              <div style={{ marginTop: 12, fontSize: 11, color: DIM }}>
                Ignored columns: {preview.unmapped_headers.join(', ')}
              </div>
            )}

            {preview.detected_machines.length > 0 && (
              <div style={{ marginTop: 8, fontSize: 11, color: MUTED2 }}>
                {preview.detected_machines.length} machine(s) detected:{' '}
                {preview.detected_machines.slice(0, 8).join(', ')}
                {preview.detected_machines.length > 8 ? '…' : ''}
              </div>
            )}

            {preview.notes.length > 0 && (
              <ul style={{ marginTop: 10, paddingLeft: 18, fontSize: 11, color: AMBER, lineHeight: 1.7 }}>
                {preview.notes.map((n, i) => <li key={i}>{n}</li>)}
              </ul>
            )}

            {preview.missing_sensors?.length > 0 && preview.missing_required.length === 0 && (
              <div style={{
                marginTop: 10, padding: '8px 10px', borderRadius: 6, fontSize: 11,
                background: SURFACE2, border: `1px solid ${BORDER}`, color: MUTED2,
              }}>
                Sensors found: <strong style={{ color: TEXT }}>{preview.present_sensors.join(', ')}</strong>.
                No {preview.missing_sensors.join(' or ')} column — that's accepted. Deviation
                scoring and published absolute limits work from what you have. The ML risk
                score needs all three, so it will read <em>n/a</em> rather than being guessed.
              </div>
            )}

            {preview.missing_required.length > 0 && (
              <div style={{
                marginTop: 10, padding: '8px 10px', borderRadius: 6, fontSize: 11,
                background: 'rgba(239,68,68,.08)', border: '1px solid rgba(239,68,68,.25)', color: RED,
              }}>
                No sensor columns found. At least one of vibration, temperature or RPM is
                needed — there is nothing to assess without one.
              </div>
            )}

            <div style={{ display: 'flex', gap: 14, alignItems: 'center', marginTop: 14, flexWrap: 'wrap' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: MUTED2 }}>
                <input type="checkbox" checked={convertF} onChange={e => setConvertF(e.target.checked)} />
                Temperatures are Fahrenheit (convert to Celsius)
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: MUTED2 }}>
                Machine type
                <select
                  value={machineType}
                  onChange={e => setMachineType(e.target.value)}
                  style={{
                    padding: '5px 8px', borderRadius: 6, background: SURFACE2,
                    border: `1px solid ${BORDER}`, color: TEXT, fontSize: 11,
                  }}
                >
                  <option value="Unknown">Unknown</option>
                  <option value="CNC Machine">CNC Machine</option>
                  <option value="Hydraulic Press">Hydraulic Press</option>
                  <option value="Conveyor Motor">Conveyor Motor</option>
                  <option value="Industrial Pump">Industrial Pump</option>
                </select>
              </label>
              <button
                onClick={runUpload}
                disabled={!preview.ready_to_commit || busy}
                style={{
                  padding: '9px 18px', borderRadius: 8,
                  background: !preview.ready_to_commit || busy ? SURFACE2 : TEAL,
                  border: 'none', color: !preview.ready_to_commit || busy ? MUTED : '#04211d',
                  fontSize: 12, fontWeight: 700,
                  cursor: !preview.ready_to_commit || busy ? 'default' : 'pointer',
                }}
              >
                {step === 'uploading' ? 'Analysing…' : 'Import and analyse'}
              </button>
            </div>
            {machineType === 'Unknown' && (
              <div style={{ marginTop: 8, fontSize: 10, color: DIM }}>
                An unknown machine type is honest but lowers confidence: the model has no
                type signal for it. Pick the closest match if you know it.
              </div>
            )}
          </div>
        </Section>
      )}

      {/* ── Step 3: results ───────────────────────────────────────────────── */}
      {result && report && (
        <Section
          title="3. Your machines"
          sub={`${result.readings_accepted} readings imported across ${result.machines.length} machine(s)`
            + (result.readings_rejected ? `, ${result.readings_rejected} row(s) rejected` : '')}
        >
          {/* Data quality gate */}
          <div style={{
            background: SURFACE, border: `1px solid ${QUALITY_COLOUR[report.data_quality.overall]}44`,
            borderRadius: 10, padding: '12px 14px',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: MUTED2 }}>DATA QUALITY</span>
              <Pill text={report.data_quality.overall} colour={QUALITY_COLOUR[report.data_quality.overall]} />
            </div>
            <div style={{ fontSize: 12, color: MUTED2 }}>{report.data_quality.guidance}</div>
            {report.data_quality.issues.length > 0 && (
              <ul style={{ marginTop: 8, paddingLeft: 18, fontSize: 11, color: DIM, lineHeight: 1.7 }}>
                {report.data_quality.issues.slice(0, 6).map((i, k) => (
                  <li key={k}>
                    <span style={{ color: i.severity === 'error' ? RED : AMBER }}>{i.severity}</span>
                    {' · '}{i.detail}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* Headline */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 12 }}>
            {[
              ['Need attention', String(report.headline.machines_needing_attention), AMBER],
              ['Over published limits', String(report.headline.machines_over_absolute_limits), RED],
              ['No ML score', String(report.headline.machines_without_ml_score), MUTED2],
              ['Signal disagreements', String(report.headline.signal_disagreements), BLUE],
              ['Machines analysed', String(report.scope.machine_count), TEAL],
            ].map(([label, value, colour]) => (
              <div key={label} style={{
                background: SURFACE, border: `1px solid ${BORDER}`, borderRadius: 10, padding: '12px 14px',
              }}>
                <div style={{ fontSize: 10, color: MUTED, textTransform: 'uppercase', letterSpacing: '0.4px' }}>{label}</div>
                <div style={{ fontSize: 22, fontWeight: 800, color: colour as string, marginTop: 4 }}>{value}</div>
              </div>
            ))}
          </div>

          {/* Machines table */}
          <div style={{ border: `1px solid ${BORDER}`, borderRadius: 10, overflow: 'hidden' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ background: SURFACE2 }}>
                  {['Machine', 'ML risk', 'Deviation', 'Severity', 'Confidence', 'Reference', ''].map(h => (
                    <th key={h} style={{
                      textAlign: h === 'ML risk' || h === 'Deviation' ? 'right' : 'left',
                      padding: '8px 11px', color: MUTED, fontSize: 9,
                      textTransform: 'uppercase', letterSpacing: '0.4px', fontWeight: 700,
                    }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {report.machines.map((m: ReportMachine) => (
                  <React.Fragment key={m.machine_id}>
                    <tr style={{ borderTop: `1px solid ${BORDER}` }}>
                      <td style={{ padding: '9px 11px', color: TEXT }}>
                        {m.machine}
                        <div style={{ fontSize: 10, color: DIM }}>
                          {m.type} · {m.readings} readings
                          {m.missing_sensors?.length
                            ? ` · no ${m.missing_sensors.join('/')}`
                            : ''}
                        </div>
                      </td>
                      <td style={{ padding: '9px 11px', textAlign: 'right', color: m.ml_available ? MUTED2 : DIM }}>
                        {m.ml_risk_score ?? 'n/a'}
                      </td>
                      <td style={{ padding: '9px 11px', textAlign: 'right', color: MUTED2 }}>
                        {m.deviation_score ?? '—'}
                      </td>
                      <td style={{ padding: '9px 11px' }}>
                        <Pill text={m.severity} colour={SEVERITY_COLOUR[m.severity] ?? MUTED} />
                      </td>
                      <td style={{ padding: '9px 11px' }}>
                        {m.confidence
                          ? <Pill text={m.confidence} colour={CONFIDENCE_COLOUR[m.confidence] ?? MUTED} />
                          : <span style={{ color: DIM }}>—</span>}
                      </td>
                      <td style={{ padding: '9px 11px', color: DIM, fontSize: 11 }}>
                        {m.reference_source.replace(/_/g, ' ')}
                      </td>
                      <td style={{ padding: '9px 11px', textAlign: 'right' }}>
                        <button
                          onClick={() => setExpanded(expanded === m.machine_id ? null : m.machine_id)}
                          style={{
                            background: 'transparent', border: 'none', color: MUTED,
                            cursor: 'pointer', fontSize: 11,
                          }}
                        >
                          {expanded === m.machine_id ? 'Hide' : 'Why'}
                        </button>
                      </td>
                    </tr>
                    {expanded === m.machine_id && (
                      <tr style={{ background: '#0f1929' }}>
                        <td colSpan={7} style={{ padding: '12px 14px' }}>
                          {m.missing_sensors?.length > 0 && (
                            <div style={{
                              padding: '8px 10px', borderRadius: 6, marginBottom: 10, fontSize: 11,
                              background: SURFACE2, border: `1px solid ${BORDER}`, color: MUTED2,
                            }}>
                              This machine reports {m.present_sensors.join(', ')} only. The ML
                              model needs vibration, temperature and RPM together, so its risk
                              score is unavailable rather than estimated from substituted values.
                              The deviation score and published limits below use the channels
                              that are present.
                            </div>
                          )}
                          {m.signal_disagreement && (
                            <div style={{
                              padding: '8px 10px', borderRadius: 6, marginBottom: 10, fontSize: 11,
                              background: 'rgba(239,68,68,.08)', border: '1px solid rgba(239,68,68,.25)', color: '#fca5a5',
                            }}>
                              {m.signal_disagreement.message}
                            </div>
                          )}
                          {m.absolute_concerns.length > 0 && (
                            <div style={{ marginBottom: 10 }}>
                              <div style={{ fontSize: 10, color: MUTED, textTransform: 'uppercase', marginBottom: 4 }}>
                                Exceeds published limits
                              </div>
                              {m.absolute_concerns.map((c, i) => (
                                <div key={i} style={{ fontSize: 11, color: c.severity === 'severe' ? RED : AMBER }}>
                                  {c.sensor} {c.value} (limit {c.threshold}) — {c.basis}
                                </div>
                              ))}
                            </div>
                          )}
                          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(200px,1fr))', gap: 12 }}>
                            <div>
                              <div style={{ fontSize: 10, color: MUTED, textTransform: 'uppercase', marginBottom: 4 }}>
                                Sensors vs reference
                              </div>
                              {(['vibration', 'temperature', 'rpm'] as const).map(s => (
                                <div key={s} style={{ fontSize: 11, color: m.current[s] == null ? DIM : MUTED2 }}>
                                  {s}: {m.current[s] ?? 'not instrumented'}
                                  {m.current[s] != null ? ` (ref ${m.reference[s] ?? '—'})` : ''}
                                </div>
                              ))}
                              <div style={{ fontSize: 10, color: DIM, marginTop: 4 }}>
                                {m.baseline_method ?? 'no baseline derived'}
                              </div>
                            </div>
                            <div>
                              <div style={{ fontSize: 10, color: MUTED, textTransform: 'uppercase', marginBottom: 4 }}>
                                Confidence reasons
                              </div>
                              {(m.confidence_reasons ?? []).length
                                ? (m.confidence_reasons ?? []).map((r, i) => (
                                    <div key={i} style={{ fontSize: 11, color: MUTED2 }}>· {r}</div>
                                  ))
                                : <div style={{ fontSize: 11, color: GREEN }}>Nothing reduces confidence.</div>}
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>

          {/* Rejected rows */}
          {result.errors.length > 0 && (
            <div style={{ fontSize: 11, color: DIM }}>
              Rejected rows: {result.errors.slice(0, 5).map(e => `line ${e.row} (${e.error})`).join('; ')}
              {result.errors.length > 5 ? ` …and ${result.errors.length - 5} more` : ''}
            </div>
          )}

          {/* Limitations — always shown, never collapsed away */}
          <div style={{
            background: 'rgba(245,158,11,.06)', border: '1px solid rgba(245,158,11,.22)',
            borderRadius: 10, padding: '12px 14px',
          }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: AMBER, marginBottom: 6 }}>
              BASIS AND LIMITATIONS
            </div>
            <ul style={{ paddingLeft: 18, fontSize: 11, color: MUTED2, lineHeight: 1.75 }}>
              {report.limitations.map((l, i) => <li key={i}>{l}</li>)}
            </ul>
          </div>

          <div style={{ display: 'flex', gap: 10 }}>
            <a
              href={reportHtmlUrl(report.scope.dataset_id ?? undefined)}
              target="_blank"
              rel="noreferrer"
              style={{
                padding: '9px 16px', borderRadius: 8, background: SURFACE,
                border: `1px solid ${BORDER}`, color: MUTED2, fontSize: 12,
                textDecoration: 'none',
              }}
            >
              Download report (HTML)
            </a>
            <button
              onClick={reset}
              style={{
                padding: '9px 16px', borderRadius: 8, background: 'transparent',
                border: `1px solid ${BORDER}`, color: MUTED, fontSize: 12, cursor: 'pointer',
              }}
            >
              Delete this dataset
            </button>
          </div>
        </Section>
      )}
    </div>
  );
};

export default BringYourData;
