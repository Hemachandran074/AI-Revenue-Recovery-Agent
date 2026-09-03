export interface BatchMetrics {
  money: {
    currency: string;
    at_risk_minor: number;
    recovered_minor: number;
    actioned_at_risk_minor: number;
    recovery_rate: number | null;
    actioned_recovery_rate: number | null;
  };
  events_total: number;
  classifier_unavailable: number;
  decision_latency: {
    count: number;
    mean_ms: number | null;
    p50_ms: number | null;
    p95_ms: number | null;
    max_ms: number | null;
    over_budget: number;
  };
  send_latency: {
    count: number;
    mean_ms: number | null;
    p50_ms: number | null;
    p95_ms: number | null;
    max_ms: number | null;
    over_budget: number;
  };
  violation_count: number;
  violations: Array<{
    event_id: string;
    rule: string;
    detail: string;
  }>;
  audit: {
    events: number;
    fully_covered: number;
    rate: number;
  };
  disposition: Record<string, number>;
  by_root_cause: Record<string, number>;
  by_action: Record<string, number>;
  by_delivery_status: Record<string, number>;
  by_event_type: Record<string, number>;
}

export interface GuardrailCheck {
  kind: 'terminal' | 'deferrable';
  name: string;
  detail: string;
  passed: boolean;
  applied_to_action: boolean;
}

export interface EventRow {
  event_id: string;
  customer_id: string;
  event_type: string;
  decline_code: string | null;
  amount_minor: number;
  currency: string;
  prior_attempts: number;
  first_failure_at: string;
  received_at: string | null;
  customer_timezone: string;
  root_cause: string | null;
  confidence: number | null;
  reasoning: string | null;
  classifier_unavailable: boolean;
  action: string | null;
  channel: string | null;
  scheduled_for: string | null;
  blocked_reason: string | null;
  guardrail_checks: GuardrailCheck[];
  delivery_status: string | null;
  customer_outcome: string | null;
  amount_recovered_minor: number | null;
  recovery_link_id: string | null;
  skip_reason: string | null;
  failure_reason: string | null;
  executed_at: string | null;
  decision_latency_ms: number | null;
  send_latency_ms: number | null;
  stages: string[];
  disposition: string;
}

export interface AuditStage {
  stage: string;
  timestamp: string;
  input_summary: Record<string, any> | null;
  output_summary: Record<string, any> | null;
  notes: string | null;
}

export interface AuditTrailResponse {
  event_id: string;
  stages: AuditStage[];
  stage_count: number;
}

export interface ReadinessResponse {
  app_env: string;
  capabilities: {
    payment_provider_configured: boolean;
    diagnose_llm_configured: boolean;
    messaging_configured: boolean;
    postgres_configured: boolean;
  };
  providers: {
    payment: string;
    diagnose_llm: string;
    messaging: string;
  };
  guardrail_config: {
    max_recovery_attempts: number;
    min_hours_between_contacts: number;
    quiet_hours_start_local: number;
    quiet_hours_end_local: number;
    hard_stop_days: number;
  };
}

export interface SimulationResult {
  event_id: string;
  status: string;
  duplicate: boolean;
  root_cause: string | null;
  reasoning?: string;
  confidence?: number;
  action: string | null;
  channel?: string;
  blocked: boolean;
  delivery_status: string | null;
  recovery_link_url?: string;
  provider_message_id?: string;
  skip_reason?: string;
  failure_reason?: string;
  amount_inr?: number;
  customer_id?: string;
  contact?: string;
  decision_latency_ms?: number;
  send_latency_ms?: number;
}

export interface CreateOrderResult {
  order_id: string;
  amount_inr: number;
  customer_id: string;
  checkout_url: string;
  key_id: string;
}

const API_BASE = '';

export async function fetchMetrics(limit = 100): Promise<BatchMetrics> {
  const res = await fetch(`${API_BASE}/api/metrics?limit=${limit}`);
  if (!res.ok) throw new Error(`Failed to fetch metrics: ${res.statusText}`);
  return res.json();
}

export async function fetchEvents(limit = 100): Promise<{ count: number; events: EventRow[] }> {
  const res = await fetch(`${API_BASE}/api/events?limit=${limit}`);
  if (!res.ok) throw new Error(`Failed to fetch events: ${res.statusText}`);
  return res.json();
}

export async function fetchEventAudit(eventId: string): Promise<AuditTrailResponse> {
  const res = await fetch(`${API_BASE}/events/${eventId}/audit`);
  if (!res.ok) throw new Error(`Failed to fetch event audit: ${res.statusText}`);
  return res.json();
}

export async function fetchReadiness(): Promise<ReadinessResponse> {
  const res = await fetch(`${API_BASE}/readiness`);
  if (!res.ok) throw new Error(`Failed to fetch readiness: ${res.statusText}`);
  return res.json();
}

export async function triggerSimulation(params: {
  cause: string;
  amount: number;
  contact: string;
  customer_id?: string;
}): Promise<SimulationResult> {
  const res = await fetch(`${API_BASE}/api/simulate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
  if (!res.ok) throw new Error(`Simulation failed: ${res.statusText}`);
  return res.json();
}

export async function createTestOrder(params: {
  amount: number;
  contact: string;
  email?: string;
  customer_id?: string;
}): Promise<CreateOrderResult> {
  const res = await fetch(`${API_BASE}/api/create-order`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
  if (!res.ok) throw new Error(`Create order failed: ${res.statusText}`);
  return res.json();
}
