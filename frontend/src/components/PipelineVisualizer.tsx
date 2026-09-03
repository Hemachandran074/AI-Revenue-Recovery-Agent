import React from 'react';
import { Eye, Brain, Scale, Send, CheckCircle2, AlertCircle, ExternalLink } from 'lucide-react';
import type { EventRow } from '../services/api';

interface PipelineVisualizerProps {
  latestEvent: EventRow | null;
  onSelectEvent: (event: EventRow) => void;
}

export const PipelineVisualizer: React.FC<PipelineVisualizerProps> = ({ latestEvent, onSelectEvent }) => {
  if (!latestEvent) {
    return (
      <div className="glass-card rounded-xl p-6 text-center text-slate-400">
        <p className="text-sm">No events detected yet. Click "Simulate Failure" or "Test Checkout" above.</p>
      </div>
    );
  }

  const isContacted = latestEvent.delivery_status === 'sent';
  const isSkipped = latestEvent.delivery_status === 'skipped';
  const isFailed = latestEvent.delivery_status === 'failed';

  return (
    <div className="glass-card rounded-xl p-5 border border-slate-800">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-4 pb-3 border-b border-slate-800">
        <div className="flex items-center space-x-2">
          <span className="h-2.5 w-2.5 rounded-full bg-cyan-400 animate-ping" />
          <h2 className="text-sm font-bold text-slate-200 uppercase tracking-wider">
            Live 4-Stage Pipeline Execution Trace
          </h2>
          <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">
            Event: {latestEvent.event_id.slice(0, 8)}
          </span>
        </div>

        <button
          onClick={() => onSelectEvent(latestEvent)}
          className="text-xs text-cyan-400 hover:text-cyan-300 font-medium flex items-center gap-1 cursor-pointer transition"
        >
          <span>View Full Audit Details</span>
          <ExternalLink className="h-3 w-3" />
        </button>
      </div>

      {/* 4 Pipeline Stages Flow */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 relative">
        {/* Stage 1: DETECT */}
        <div className="bg-slate-900/90 rounded-xl p-3.5 border border-slate-800 hover:border-slate-700 transition">
          <div className="flex items-center justify-between mb-2">
            <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
              STAGE 1
            </span>
            <CheckCircle2 className="h-4 w-4 text-emerald-400" />
          </div>
          <div className="flex items-center space-x-2 mb-2">
            <Eye className="h-4 w-4 text-cyan-400" />
            <h3 className="text-xs font-bold text-slate-100 uppercase tracking-wide">DETECT</h3>
          </div>
          <p className="text-[11px] text-slate-400 leading-relaxed mb-2">
            Signature validated, PAN data dropped, customer history joined.
          </p>
          <div className="bg-slate-950/80 rounded p-2 text-[10px] font-mono text-slate-300 space-y-1">
            <div className="truncate"><span className="text-slate-500">Method:</span> {latestEvent.event_type}</div>
            <div className="truncate"><span className="text-slate-500">Amount:</span> ₹{(latestEvent.amount_minor / 100).toFixed(2)}</div>
            <div className="truncate"><span className="text-slate-500">Cust:</span> {latestEvent.customer_id}</div>
          </div>
        </div>

        {/* Stage 2: DIAGNOSE (Gemini LLM) */}
        <div className="bg-slate-900/90 rounded-xl p-3.5 border border-slate-800 hover:border-slate-700 transition">
          <div className="flex items-center justify-between mb-2">
            <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-purple-500/10 text-purple-400 border border-purple-500/20">
              STAGE 2
            </span>
            <CheckCircle2 className="h-4 w-4 text-emerald-400" />
          </div>
          <div className="flex items-center space-x-2 mb-2">
            <Brain className="h-4 w-4 text-purple-400" />
            <h3 className="text-xs font-bold text-slate-100 uppercase tracking-wide">DIAGNOSE (Gemini)</h3>
          </div>
          <p className="text-[11px] text-slate-400 leading-relaxed mb-2">
            Classified root cause with {latestEvent.confidence ? `${(latestEvent.confidence * 100).toFixed(0)}%` : '100%'} confidence.
          </p>
          <div className="bg-slate-950/80 rounded p-2 text-[10px] font-mono text-slate-300 space-y-1">
            <div className="truncate text-purple-300 font-bold"><span className="text-slate-500">Cause:</span> {latestEvent.root_cause || 'unknown'}</div>
            <div className="text-slate-400 text-[10px] line-clamp-2 italic font-sans mt-1">
              "{latestEvent.reasoning || 'Diagnostic complete.'}"
            </div>
          </div>
        </div>

        {/* Stage 3: DECIDE (Rules & Guardrails) */}
        <div className="bg-slate-900/90 rounded-xl p-3.5 border border-slate-800 hover:border-slate-700 transition">
          <div className="flex items-center justify-between mb-2">
            <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-blue-500/10 text-blue-400 border border-blue-500/20">
              STAGE 3
            </span>
            {latestEvent.blocked_reason ? (
              <AlertCircle className="h-4 w-4 text-amber-400" />
            ) : (
              <CheckCircle2 className="h-4 w-4 text-emerald-400" />
            )}
          </div>
          <div className="flex items-center space-x-2 mb-2">
            <Scale className="h-4 w-4 text-blue-400" />
            <h3 className="text-xs font-bold text-slate-100 uppercase tracking-wide">DECIDE (Rules)</h3>
          </div>
          <p className="text-[11px] text-slate-400 leading-relaxed mb-2">
            {latestEvent.blocked_reason ? 'Withheld by safety rule' : 'All 4 guardrail checks passed.'}
          </p>
          <div className="bg-slate-950/80 rounded p-2 text-[10px] font-mono text-slate-300 space-y-1">
            <div className="truncate text-blue-300 font-bold"><span className="text-slate-500">Action:</span> {latestEvent.action || '-'}</div>
            <div className="truncate"><span className="text-slate-500">Channel:</span> {latestEvent.channel || 'none'}</div>
            <div className="truncate text-emerald-400 text-[10px]">
              {latestEvent.blocked_reason ? 'Safety Stop Triggered' : '✓ 4/4 Guardrails Passed'}
            </div>
          </div>
        </div>

        {/* Stage 4: EXECUTE */}
        <div className={`rounded-xl p-3.5 border transition ${
          isContacted
            ? 'bg-emerald-950/20 border-emerald-500/40'
            : isFailed
            ? 'bg-rose-950/20 border-rose-500/40'
            : 'bg-slate-900/90 border-slate-800'
        }`}>
          <div className="flex items-center justify-between mb-2">
            <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
              isContacted
                ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                : 'bg-slate-800 text-slate-400 border border-slate-700'
            }`}>
              STAGE 4
            </span>
            <span className={`px-2 py-0.5 text-[10px] font-semibold rounded-full uppercase ${
              isContacted
                ? 'bg-emerald-500/20 text-emerald-300'
                : isSkipped
                ? 'bg-amber-500/20 text-amber-300'
                : 'bg-slate-800 text-slate-400'
            }`}>
              {latestEvent.disposition}
            </span>
          </div>
          <div className="flex items-center space-x-2 mb-2">
            <Send className={`h-4 w-4 ${isContacted ? 'text-emerald-400' : 'text-slate-400'}`} />
            <h3 className="text-xs font-bold text-slate-100 uppercase tracking-wide">EXECUTE</h3>
          </div>
          <p className="text-[11px] text-slate-400 leading-relaxed mb-2">
            {isContacted
              ? 'WhatsApp recovery link dispatched via Twilio!'
              : latestEvent.skip_reason || 'Internal escalation recorded.'}
          </p>
          <div className="bg-slate-950/80 rounded p-2 text-[10px] font-mono text-slate-300 space-y-1">
            <div className="truncate"><span className="text-slate-500">Latency:</span> {latestEvent.decision_latency_ms ? `${latestEvent.decision_latency_ms.toFixed(0)} ms` : '-'}</div>
            <div className="truncate text-emerald-400 font-semibold"><span className="text-slate-500">Status:</span> {latestEvent.delivery_status || 'recorded'}</div>
          </div>
        </div>
      </div>
    </div>
  );
};
