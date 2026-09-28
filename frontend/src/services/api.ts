import type {
  Machine, MachineDetail, Alert, ExplainResponse,
  WorkOrder, OeeDashboard, HealthStatus,
  Impact, InvestigateResponse, InvestigateSuggestions,
  IngestStatus, CostCenter, Material, ProductionOrder,
  OeeLosses, ModelInfo, MachineErpContext, CurrentUser, LoginResponse,
  Dataset, ColumnMappingPreview, UploadResult, QualityReport, AnalysisReport
} from '../types';

const API_BASE = import.meta.env.VITE_API_BASE_URL
  ? import.meta.env.VITE_API_BASE_URL
  : '';   // empty = use Vite proxy (same origin)

// ─── Session token ───────────────────────────────────────────────────────────
// Held in sessionStorage rather than localStorage: it is scoped to the tab and
// cleared when the tab closes, so a shared demo machine does not leave a planner
// session behind. The backend token is HMAC-signed and expires in 12 hours.
const TOKEN_KEY = 'predictops.token';

export const getToken = (): string | null => {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
};

export const setToken = (token: string | null): void => {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* storage disabled; the session simply won't persist */ }
};

/** Thrown on 401 so callers can prompt for a login instead of showing a generic error. */
export class UnauthorizedError extends Error {
  constructor(message = 'Authentication required') {
    super(message);
    this.name = 'UnauthorizedError';
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  // Spread options first so caller-supplied headers merge with the defaults
  // instead of replacing them.
  const { headers: callerHeaders, ...rest } = options ?? {};
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    ...rest,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(callerHeaders ?? {}),
    },
  });
  if (!res.ok) {
    const body = await res.text();
    if (res.status === 401) {
      // A stale or absent token: drop it so the UI stops sending it.
      setToken(null);
      throw new UnauthorizedError('Sign in to perform this action.');
    }
    if (res.status === 403) throw new Error('Your role does not permit this action.');
    throw new Error(`API ${res.status}: ${body}`);
  }
  return res.json() as Promise<T>;
}

// ─── Auth ────────────────────────────────────────────────────────────────────
export const login = async (username: string, password: string): Promise<LoginResponse> => {
  const res = await request<LoginResponse>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
  setToken(res.token);
  return res;
};

export const logout = (): void => setToken(null);

export const getCurrentUser = () => request<CurrentUser>('/api/auth/me');

// ─── Health ───────────────────────────────────────────────────────────────────
export const getHealth = () => request<HealthStatus>('/api/health');

// ─── Machines ─────────────────────────────────────────────────────────────────
export const getMachines     = ()          => request<Machine[]>('/api/machines');
export const getMachine      = (id:number) => request<MachineDetail>(`/api/machines/${id}`);
export const getMachineDetail = getMachine; // alias used by MachineDetail page

// ─── Alerts ───────────────────────────────────────────────────────────────────
export const getAlerts = (params?: { status?: string; severity?: string }) => {
  const qs = params ? '?' + new URLSearchParams(params as Record<string,string>).toString() : '';
  return request<Alert[]>(`/api/alerts${qs}`);
};

export const getAlert    = (id:number) => request<Alert>(`/api/alerts/${id}`);

export const explainAlert = (id:number) =>
  request<ExplainResponse>(`/api/alerts/${id}/explain`, { method: 'POST' });

export const updateAlert = (id:number, status:string) =>
  request<Alert>(`/api/alerts/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  });

// Create a WO directly from an alert (quick path)
export const createWorkOrderFromAlert = (alertId:number) =>
  request<WorkOrder>(`/api/alerts/${alertId}/workorder`, { method: 'POST' });

// Create a WO with full payload
export const createWorkOrder = (payload: { alert_id?: number; machine_id: number }) =>
  request<WorkOrder>('/api/workorders', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

// ─── Work Orders ──────────────────────────────────────────────────────────────
export const getWorkOrders = (params?: { status?: string }) => {
  const qs = params ? '?' + new URLSearchParams(params as Record<string,string>).toString() : '';
  return request<WorkOrder[]>(`/api/workorders${qs}`);
};

export const updateWorkOrderStatus = (id:number, status:string) =>
  request<WorkOrder>(`/api/workorders/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  });

// ─── OEE ─────────────────────────────────────────────────────────────────────
export const getOee = () => request<OeeDashboard>('/api/oee');

export const getOeeLosses = (params?: { target_oee?: number; window_hours?: number }) => {
  const qs = params
    ? '?' + new URLSearchParams(
        Object.fromEntries(
          Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)])
        )
      ).toString()
    : '';
  return request<OeeLosses>(`/api/oee/losses${qs}`);
};

// ─── Model transparency ──────────────────────────────────────────────────────
export const getModelInfo = () => request<ModelInfo>('/api/model');

// ─── Bring Your Own Data ─────────────────────────────────────────────────────

export const getDatasets = () => request<Dataset[]>('/api/datasets');

export const createDataset = (name: string, description?: string) =>
  request<Dataset>('/api/datasets', {
    method: 'POST',
    body: JSON.stringify({ name, description }),
  });

export const deleteDataset = (id: number) =>
  request<unknown>(`/api/datasets/${id}`, { method: 'DELETE' });

/** Interpret a CSV without writing anything, so the mapping can be confirmed. */
export const previewDataset = (id: number, csvText: string) =>
  request<ColumnMappingPreview>(`/api/datasets/${id}/preview`, {
    method: 'POST',
    body: csvText,
    headers: { 'Content-Type': 'text/csv' },
  });

export const uploadDataset = (
  id: number,
  csvText: string,
  opts?: { convertFahrenheit?: boolean; machineType?: string },
) => {
  const params = new URLSearchParams();
  if (opts?.convertFahrenheit) params.set('convert_fahrenheit', 'true');
  if (opts?.machineType) params.set('machine_type', opts.machineType);
  const qs = params.toString() ? `?${params.toString()}` : '';
  return request<UploadResult>(`/api/datasets/${id}/upload${qs}`, {
    method: 'POST',
    body: csvText,
    headers: { 'Content-Type': 'text/csv' },
  });
};

/** Pass 0 for the built-in demo fleet — the same checks run either way. */
export const getDatasetQuality = (id: number) =>
  request<QualityReport>(`/api/datasets/${id}/quality`);

export const getReport = (datasetId?: number) => {
  const qs = datasetId ? `?dataset_id=${datasetId}` : '';
  return request<AnalysisReport>(`/api/report${qs}`);
};

export const reportHtmlUrl = (datasetId?: number) =>
  `${API_BASE}/api/report/html${datasetId ? `?dataset_id=${datasetId}` : ''}`;

// ─── Live stream (SSE) ───────────────────────────────────────────────────────
/**
 * Subscribe to server-push updates. Returns an unsubscribe function.
 * Polling stays in place as a fallback, so if SSE is blocked by a proxy the
 * dashboard still refreshes.
 */
export function subscribeToUpdates(onUpdate: () => void): () => void {
  let source: EventSource | null = null;
  try {
    source = new EventSource(`${API_BASE}/api/stream`);
    source.addEventListener('update', () => onUpdate());
  } catch {
    return () => {};
  }
  return () => source?.close();
}

// ─── Business Impact ─────────────────────────────────────────────────────────
export const getImpact = (windowDays?: number) => {
  const qs = windowDays ? `?window_days=${windowDays}` : '';
  return request<Impact>(`/api/impact${qs}`);
};

// ─── Investigation ───────────────────────────────────────────────────────────
export const investigate = (question: string, machineId?: number) =>
  request<InvestigateResponse>('/api/investigate', {
    method: 'POST',
    body: JSON.stringify({ question, machine_id: machineId }),
  });

export const getInvestigateSuggestions = () =>
  request<InvestigateSuggestions>('/api/investigate/suggestions');

// ─── Ingestion ───────────────────────────────────────────────────────────────
export const getIngestStatus = () => request<IngestStatus>('/api/readings/status');

/** Upload CSV text as the raw body — the backend takes text/csv, not multipart. */
export const uploadReadingsCsv = (machineName: string, csvText: string) =>
  request<unknown>(`/api/readings/csv?machine_name=${encodeURIComponent(machineName)}`, {
    method: 'POST',
    body: csvText,
    headers: { 'Content-Type': 'text/csv' },
  });

// ─── ERP ─────────────────────────────────────────────────────────────────────
export const getCostCenters = () => request<CostCenter[]>('/api/erp/cost-centers');
export const getMaterials = () => request<Material[]>('/api/erp/materials');

/** All IT-side context for one machine in a single call: cost centre, spare part,
 *  and the production orders a failure would disrupt. */
export const getMachineErpContext = (machineId: number) =>
  request<MachineErpContext>(`/api/erp/machine-context/${machineId}`);

export const getProductionOrders = (params?: { machine_id?: number; status?: string }) => {
  const qs = params
    ? '?' + new URLSearchParams(
        Object.fromEntries(
          Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)])
        )
      ).toString()
    : '';
  return request<ProductionOrder[]>(`/api/erp/production-orders${qs}`);
};
