import React from 'react';
import { IndianRupee, Zap, ShieldCheck, CheckCircle2, AlertTriangle, Clock } from 'lucide-react';
import type { BatchMetrics } from '../services/api';

interface MetricsOverviewProps {
  metrics: BatchMetrics | null;
  isLoading: boolean;
}

export const MetricsOverview: React.FC<MetricsOverviewProps> = ({ metrics, isLoading }) => {
  if (isLoading && !metrics) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 animate-pulse">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="h-28 bg-slate-800/50 rounded-xl border border-slate-800" />
        ))}
      </div>
    );
  }

  if (!metrics) return null;

  const money = metrics.money;
  const atRisk = money.at_risk_minor / 100;
  const recovered = money.recovered_minor / 100;
  const recoveryRate = money.recovery_rate ? (money.recovery_rate * 100).toFixed(1) : '0.0';
  const actionedRate = money.actioned_recovery_rate ? (money.actioned_recovery_rate * 100).toFixed(1) : '0.0';

  const meanLatency = metrics.decision_latency.mean_ms ? (metrics.decision_latency.mean_ms / 1000).toFixed(2) : '0.0';
  const overBudget = metrics.decision_latency.over_budget;
  const hasViolations = metrics.violation_count > 0;
  const auditPct = (metrics.audit.rate * 100).toFixed(1);

  return (
    <div className="space-y-4">
      {/* 4 Primary Hero Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Card 1: Revenue At Risk & Recovered */}
        <div className="glass-card rounded-xl p-4.5 relative overflow-hidden border-l-4 border-l-cyan-500">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              Recovered / At Risk
            </span>
            <div className="p-2 rounded-lg bg-cyan-500/10 text-cyan-400">
              <IndianRupee className="h-4 w-4" />
            </div>
          </div>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="text-2xl font-bold text-slate-100">
              ₹{recovered.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
            </span>
            <span className="text-xs text-slate-400">
              of ₹{atRisk.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
            </span>
          </div>
          {/* Progress Bar */}
          <div className="mt-3 w-full bg-slate-800 rounded-full h-1.5 overflow-hidden">
            <div
              className="bg-gradient-to-r from-cyan-500 to-blue-500 h-1.5 rounded-full transition-all duration-500"
              style={{ width: `${Math.min(100, parseFloat(recoveryRate) || (recovered > 0 ? 10 : 0))}%` }}
            />
          </div>
          <div className="mt-2 flex items-center justify-between text-[11px] text-slate-400">
            <span>{recoveryRate}% recovered overall</span>
            <span className="text-cyan-400">{actionedRate}% of actioned</span>
          </div>
        </div>

        {/* Card 2: Decision Latency (60s SLA) */}
        <div className="glass-card rounded-xl p-4.5 relative overflow-hidden border-l-4 border-l-blue-500">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              Decision Latency
            </span>
            <div className="p-2 rounded-lg bg-blue-500/10 text-blue-400">
              <Zap className="h-4 w-4" />
            </div>
          </div>
          <div className="mt-2 flex items-baseline justify-between">
            <div className="flex items-baseline space-x-1.5">
              <span className="text-2xl font-bold text-slate-100">{meanLatency}s</span>
              <span className="text-xs text-slate-400">avg</span>
            </div>
            <span className={`px-2 py-0.5 text-[11px] font-semibold rounded-full ${
              overBudget === 0
                ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30'
                : 'bg-rose-500/10 text-rose-400 border border-rose-500/30'
            }`}>
              {overBudget === 0 ? '✓ <60s SLA Met' : `${overBudget} Over SLA`}
            </span>
          </div>
          <p className="mt-3 text-[11px] text-slate-400 flex items-center gap-1">
            <Clock className="h-3 w-3 text-slate-500" />
            <span>Fast AI diagnosis via Gemini Flash Lite</span>
          </p>
        </div>

        {/* Card 3: Stopping-Rule Violations */}
        <div className={`glass-card rounded-xl p-4.5 relative overflow-hidden border-l-4 ${
          hasViolations ? 'border-l-rose-500 bg-rose-950/20' : 'border-l-emerald-500'
        }`}>
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              Guardrail Violations
            </span>
            <div className={`p-2 rounded-lg ${hasViolations ? 'bg-rose-500/10 text-rose-400' : 'bg-emerald-500/10 text-emerald-400'}`}>
              {hasViolations ? <AlertTriangle className="h-4 w-4" /> : <ShieldCheck className="h-4 w-4" />}
            </div>
          </div>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="text-2xl font-bold text-slate-100">
              {metrics.violation_count}
            </span>
            <span className={`px-2 py-0.5 text-[11px] font-semibold rounded-full ${
              hasViolations ? 'bg-rose-500/20 text-rose-300' : 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30'
            }`}>
              {hasViolations ? 'BREACH DETECTED' : '✓ 100% Compliant'}
            </span>
          </div>
          <p className="mt-3 text-[11px] text-slate-400">
            {hasViolations
              ? 'Safety rules breached! Check audit details below.'
              : 'Re-derived stopping rules pass on all events.'}
          </p>
        </div>

        {/* Card 4: Audit Coverage */}
        <div className="glass-card rounded-xl p-4.5 relative overflow-hidden border-l-4 border-l-indigo-500">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              Audit Coverage
            </span>
            <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400">
              <CheckCircle2 className="h-4 w-4" />
            </div>
          </div>
          <div className="mt-2 flex items-baseline justify-between">
            <span className="text-2xl font-bold text-slate-100">{auditPct}%</span>
            <span className="text-xs text-slate-400">
              {metrics.audit.fully_covered} of {metrics.audit.events}
            </span>
          </div>
          <p className="mt-3 text-[11px] text-slate-400">
            Full 4-stage audit trace for every detected event.
          </p>
        </div>
      </div>

      {/* Breakdown Badges Bar */}
      <div className="glass-card rounded-xl p-3.5 flex flex-wrap items-center justify-between gap-3 text-xs">
        <div className="flex items-center space-x-2 text-slate-400 font-medium">
          <span>Root Causes Detected:</span>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(metrics.by_root_cause).map(([cause, count]) => (
              <span key={cause} className="px-2 py-0.5 rounded-md bg-slate-800 text-slate-200 border border-slate-700">
                {cause}: <strong className="text-cyan-400">{count}</strong>
              </span>
            ))}
          </div>
        </div>

        <div className="flex items-center space-x-2 text-slate-400 font-medium">
          <span>Actions Dispatched:</span>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(metrics.by_action).map(([action, count]) => (
              <span key={action} className="px-2 py-0.5 rounded-md bg-slate-800 text-slate-200 border border-slate-700">
                {action}: <strong className="text-emerald-400">{count}</strong>
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};
