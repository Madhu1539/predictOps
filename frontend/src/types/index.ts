// PredictOps API types matching backend schemas

export interface Machine {
  id: number;
  name: string;
  type: string;
  location: string;
  criticality: 'Low' | 'Medium' | 'High';
  latest_risk?: number;
  severity?: 'Normal' | 'Watch' | 'Warning' | 'Critical';
  maintenance_priority?: number;
}

export interface SensorReading {
  id: number;
  machine_id: number;
  timestamp: string;
  vibration: number;
  temperature: number;
  rpm: number;
  machine_status: string;
  production_count: number;
  good_count: number;
  planned_production_time: number;
  actual_run_time: number;
  downtime_minutes: number;
}

export interface MaintenanceRecord {
  id: number;
  machine_id: number;
  maintenance_date: string;
  maintenance_type: string;
  failure_mode?: string;
  description?: string;
  part_used?: string;
}

export interface RiskHistoryPoint {
  timestamp: string;
  risk_score: number;
}

export interface AlertSummary {
  id: number;
  risk_score: number;
  severity: string;
  maintenance_priority: number;
  failure_mode?: string;
  recommended_action?: string;
  status: string;
  created_at: string;
  sensor_deviations?: string;
  days_since_maintenance?: number;
  previous_failure_context?: string;
  machine_criticality?: string;
  production_impact?: string;
  part_needed?: string;
  part_available?: boolean;
  explanation_text?: string;
  degraded_mode?: boolean;
  estimated_downtime_cost?: number;
  estimated_loss_avoided?: number;
  attribution?: string;
}

export interface MachineDetail {
  id: number;
  name: string;
  type: string;
  location: string;
  install_date: string;
  criticality: string;
  nominal_vibration: number;
  nominal_temperature: number;
  nominal_rpm: number;
  readings: SensorReading[];
  maintenance: MaintenanceRecord[];
  risk_history: RiskHistoryPoint[];
  latest_alert?: AlertSummary;
}

export interface Alert {
  id: number;
  machine_id: number;
  machine_name?: string;
  machine_type?: string;
  risk_score: number;
  severity: string;
  maintenance_priority: number;
  top_sensors?: string;
  sensor_deviations?: string;
  failure_mode?: string;
  days_since_maintenance?: number;
  previous_failure_context?: string;
  machine_criticality?: string;
  production_impact?: string;
  part_needed?: string;
  part_available?: boolean;
  recommended_action?: string;
  status: string;
  created_at: string;
  explanation_text?: string;
  degraded_mode?: boolean;
  estimated_downtime_cost?: number;
  estimated_loss_avoided?: number;
  attribution?: string;
}

export interface ExplainResponse {
  alert_id: number;
  explanation: string;
  source: string;
}

export interface WorkOrder {
  id: number;
  alert_id?: number;
  machine_id: number;
  machine_name?: string;
  technician?: string;
  part_needed?: string;
  priority: string;
  due_date?: string;
  status: string;
  created_at: string;
  completed_at?: string;
}

export interface OeeTrendPoint {
  timestamp: string;
  oee: number;
  availability: number;
  performance: number;
  quality: number;
}

export interface MachineOeeSummary {
  machine_id: number;
  machine_name: string;
  oee: number;
  availability: number;
  performance: number;
  quality: number;
}

export interface OeeDashboard {
  plant_oee: number;
  trend: OeeTrendPoint[];
  per_machine: MachineOeeSummary[];
}

export interface HealthStatus {
  status: string;
  demo_mode: boolean;
  database: string;
  ml_model?: string;
  degraded_mode?: boolean;
}

// ─── Model attribution ───────────────────────────────────────────────────────

export interface AttributionFactor {
  feature: string;
  label: string;
  value: number;
  contribution: number;
  direction: 'increases_risk' | 'reduces_risk';
  method: string;
}

/** Deviations stored as a JSON string on the alert. RPM included per spec. */
export interface SensorDeviations {
  vibration_pct?: number;
  temperature_pct?: number;
  rpm_pct?: number;
  rpm_cv?: number;
}

// ─── Business impact (all figures are modelled estimates) ────────────────────

export interface MachineExposure {
  machine_id: number;
  machine_name: string;
  cost_center_code?: string;
  risk_score: number;
  severity: string;
  downtime_cost_per_hour: number;
  value_at_risk: number;
  loss_avoided_if_actioned: number;
}

export interface Impact {
  currency: string;
  value_at_risk: number;
  value_protected: number;
  downtime_cost_incurred: number;
  window_days: number;
  open_alert_count: number;
  completed_work_order_count: number;
  top_exposure: MachineExposure[];
  assumptions: Record<string, unknown>;
  basis: string;
}

// ─── Natural-language investigation ──────────────────────────────────────────

export interface InvestigateResponse {
  answer: string;
  source: string;
  intent: string;
  intent_source: string;
  machine?: string;
  evidence: Record<string, unknown>[];
}

export interface InvestigateSuggestions {
  intents: string[];
  questions: string[];
}

// ─── OEE loss analysis ───────────────────────────────────────────────────────

export interface OeeLossItem {
  machine_id: number;
  machine_name: string;
  cost_center_code?: string;
  oee: number;
  availability: number;
  performance: number;
  quality: number;
  availability_loss: number;
  performance_loss: number;
  quality_loss: number;
  biggest_loss: string;
  biggest_loss_pct: number;
  estimated_gap_cost: number;
}

export interface OeeLosses {
  plant_oee: number;
  target_oee: number;
  window_hours: number;
  currency: string;
  machines: OeeLossItem[];
  loss_totals: Record<string, number>;
  total_gap_cost: number;
  basis: string;
}

// ─── Model transparency ──────────────────────────────────────────────────────

export interface ModelMetrics {
  precision: number;
  recall: number;
  f1: number;
  roc_auc: number;
  brier_score?: number;
  saturated_fraction?: number;
  confusion_matrix?: Record<string, number>;
}

export interface ModelInfo {
  available: boolean;
  model_loaded: boolean;
  selected_model?: string;
  selection_rule?: string;
  trained_at?: string;
  train_samples?: number;
  test_samples?: number;
  selected_metrics?: ModelMetrics;
  all_candidates?: Record<string, ModelMetrics>;
  feature_count?: number;
  global_importance?: { feature: string; label: string; weight: number; method: string }[];
  risk_bands?: { severity: string; min: number; max: number }[];
  note?: string;
  degraded_mode_note?: string;
}

// ─── Bring Your Own Data ─────────────────────────────────────────────────────

export interface Dataset {
  id: number;
  name: string;
  description?: string;
  source: string;
  status: string;
  row_count: number;
  machine_count: number;
  column_mapping?: string;
  notes?: string;
  created_at: string;
}

export interface ColumnMappingPreview {
  headers: string[];
  mapping: Record<string, string>;
  confidence: Record<string, string>;
  unmapped_headers: string[];
  missing_required: string[];
  present_sensors: string[];
  missing_sensors: string[];
  ml_scoreable: boolean;
  detected_machines: string[];
  sample_rows: Record<string, string | null>[];
  total_rows_previewed: number;
  timestamp_parse_failures: number;
  temperature_looks_fahrenheit: boolean;
  ready_to_commit: boolean;
  notes: string[];
}

export interface UploadResult {
  dataset_id: number;
  machines_created: number;
  machines_matched: number;
  readings_accepted: number;
  readings_rejected: number;
  errors: { row: number; error: string }[];
  machines: string[];
}

export interface QualityIssue {
  check: string;
  severity: string;
  detail: string;
}

export interface QualityReport {
  overall: string;                 // ok | warnings | unusable
  summary: {
    machines: number;
    readings: number;
    first_reading?: string;
    last_reading?: string;
    error_count: number;
    warning_count: number;
    unusable_machines: string[];
  };
  machines: Record<string, unknown>[];
  issues: QualityIssue[];
  guidance: string;
}

export interface AbsoluteConcern {
  sensor: string;
  value: number;
  severity: string;
  threshold: number;
  basis: string;
}

export interface ReportMachine {
  machine: string;
  machine_id: number;
  type: string;
  criticality: string;
  data_origin: string;
  readings: number;
  maintenance_records: number;
  ml_risk_score?: number | null;
  severity: string;
  deviation_score?: number | null;
  deviation_components?: { sensor: string; fraction: number; score: number }[];
  absolute_concerns: AbsoluteConcern[];
  absolute_score: number;
  present_sensors: string[];
  missing_sensors: string[];
  ml_available: boolean;
  confidence?: string | null;
  confidence_reasons?: string[];
  signal_disagreement?: { kind: string; message: string; recommended_signal: string } | null;
  failure_mode?: string | null;
  recommended_action?: string | null;
  reference_source: string;
  reference: { vibration?: number; temperature?: number; rpm?: number };
  baseline_method?: string | null;
  baseline_sample_count?: number | null;
  current: { vibration?: number | null; temperature?: number | null; rpm?: number | null };
  oee?: number | null;
  value_at_risk: number;
  top_factors: { label: string; contribution: number; direction: string }[];
}

export interface AnalysisReport {
  scope: {
    dataset_id?: number | null;
    dataset_name: string;
    machine_count: number;
    total_readings: number;
    contains_external_data: boolean;
  };
  headline: {
    machines_needing_attention: number;
    highest_risk_machine?: string | null;
    total_value_at_risk: number;
    signal_disagreements: number;
    machines_over_absolute_limits: number;
    machines_without_ml_score: number;
  };
  data_quality: QualityReport;
  machines: ReportMachine[];
  assumptions: Record<string, unknown>;
  limitations: string[];
}

// ─── Ingestion provenance ────────────────────────────────────────────────────

export interface IngestSourceStat {
  source: string;
  readings: number;
  latest_timestamp?: string;
}

export interface IngestStatus {
  total_readings: number;
  by_source: IngestSourceStat[];
}

// ─── ERP ─────────────────────────────────────────────────────────────────────

export interface CostCenter {
  id: number;
  code: string;
  name: string;
  downtime_cost_per_hour: number;
  currency: string;
  erp_source: string;
}

export interface Material {
  id: number;
  part_name: string;
  erp_material_no?: string;
  unit_cost: number;
  lead_time_days: number;
  supplier?: string;
  erp_source: string;
  stock_quantity?: number;
  is_available?: boolean;
}

export interface ProductionOrder {
  id: number;
  order_no: string;
  machine_id: number;
  machine_name?: string;
  product: string;
  planned_qty: number;
  actual_qty: number;
  scheduled_start: string;
  scheduled_end?: string;
  status: string;
  erp_source: string;
}

/** An open order as returned inside the machine ERP context (trimmed shape). */
export interface OpenOrderSummary {
  order_no: string;
  product: string;
  status: string;
  planned_qty: number;
  actual_qty: number;
  remaining_qty: number;
  scheduled_start?: string | null;
  scheduled_end?: string | null;
}

/** The spare part a machine's current failure mode calls for. */
export interface MachinePartContext {
  part_name: string;
  erp_material_no?: string | null;
  unit_cost?: number | null;
  lead_time_days?: number | null;
  supplier?: string | null;
  stock_quantity?: number | null;
  is_available: boolean;
}

/** Every IT-side fact bearing on one machine's maintenance decision. */
export interface MachineErpContext {
  machine_id: number;
  machine_name: string;
  cost_center?: CostCenter | null;
  part?: MachinePartContext | null;
  production_impact: string;
  production_impact_basis: 'erp_production_orders' | 'criticality_fallback' | string;
  production_impact_detail: string;
  committed_units: number;
  open_orders: OpenOrderSummary[];
  assumptions: Record<string, unknown>;
}

// ─── Auth ────────────────────────────────────────────────────────────────────

export interface CurrentUser {
  username?: string | null;
  role: string;
  permissions: string[];
  auth_enabled: boolean;
  authenticated: boolean;
}

export interface LoginResponse {
  token: string;
  username: string;
  role: string;
  full_name?: string | null;
  permissions: string[];
}

