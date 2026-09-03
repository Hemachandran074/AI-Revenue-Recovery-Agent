import React, { useState, useEffect } from 'react';
import { X, ShieldCheck, Eye, Scale, Copy } from 'lucide-react';
import { fetchEventAudit, type EventRow, type AuditTrailResponse } from '../services/api';

interface EventDetailModalProps {
  event: EventRow | null;
  onClose: () => void;
}

export const EventDetailModal: React.FC<EventDetailModalProps> = ({ event, onClose }) => {
  const [auditData, setAuditData] = useState<AuditTrailResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (event) {
      setIsLoading(true);
      fetchEventAudit(event.event_id)
        .then((data) => setAuditData(data))
        .catch((err) => console.error('Failed to load audit trail:', err))
        .finally(() => setIsLoading(false));
    } else {
      setAuditData(null);
    }
  }, [event]);

  if (!event) return null;

  const copyEventId = () => {
    navigator.clipboard.writeText(event.event_id);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm animate-fade-in">
      <div className="glass-card bg-slate-900 border border-slate-700 w-full max-w-4xl rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[90vh]">
        {/* Modal Header */}
        <div className="px-6 py-4 border-b border-slate-800 flex items-center justify-between bg-slate-900/95">
          <div className="flex items-center space-x-3">
            <div className="p-2 rounded-xl bg-cyan-500/10 text-cyan-400">
              <ShieldCheck className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h2 className="text-base font-bold text-slate-100">Event Audit Trail</h2>
                <span className="font-mono text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300">
                  {event.event_id}
                </span>
                <button
                  onClick={copyEventId}
                  className="p-1 rounded hover:bg-slate-800 text-slate-400 hover:text-slate-200 transition cursor-pointer"
                  title="Copy Event ID"
                >
                  <Copy className="h-3.5 w-3.5" />
                </button>
                {copied && <span className="text-[10px] text-emerald-400">Copied!</span>}
              </div>
              <p className="text-xs text-slate-400 mt-0.5">
                Full 4-stage pipeline execution and guardrail audit record
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition cursor-pointer"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Modal Body */}
        <div className="p-6 overflow-y-auto space-y-6 flex-1 text-xs">
          {/* Summary Banner */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-slate-950/60 p-4 rounded-xl border border-slate-800">
            <div>
              <span className="text-slate-500 block text-[11px]">Root Cause</span>
              <strong className="text-purple-300 text-sm font-semibold">{event.root_cause || '-'}</strong>
            </div>
            <div>
              <span className="text-slate-500 block text-[11px]">Action</span>
              <strong className="text-cyan-300 text-sm font-semibold">{event.action || '-'}</strong>
            </div>
            <div>
              <span className="text-slate-500 block text-[11px]">Amount</span>
              <strong className="text-slate-100 text-sm font-semibold">₹{(event.amount_minor / 100).toFixed(2)}</strong>
            </div>
            <div>
              <span className="text-slate-500 block text-[11px]">Outcome</span>
              <strong className="text-emerald-400 text-sm font-semibold uppercase">{event.disposition}</strong>
            </div>
          </div>

          {/* Guardrail Checks Section */}
          <div>
            <h3 className="text-xs font-bold text-slate-200 uppercase tracking-wider mb-2.5 flex items-center gap-1.5">
              <Scale className="h-4 w-4 text-blue-400" />
              <span>Guardrail Safety Checks</span>
            </h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              {event.guardrail_checks.map((check, idx) => (
                <div
                  key={idx}
                  className={`p-3 rounded-xl border ${
                    check.passed
                      ? 'bg-emerald-950/10 border-emerald-500/30'
                      : 'bg-rose-950/10 border-rose-500/30'
                  }`}
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="font-semibold text-slate-200 font-mono text-[11px]">
                      {check.name}
                    </span>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                      check.passed ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'
                    }`}>
                      {check.passed ? 'PASSED' : 'BLOCKED'}
                    </span>
                  </div>
                  <p className="text-[11px] text-slate-400">{check.detail}</p>
                </div>
              ))}
            </div>
          </div>

          {/* Detailed 4-Stage Audit Cards */}
          <div>
            <h3 className="text-xs font-bold text-slate-200 uppercase tracking-wider mb-2.5 flex items-center gap-1.5">
              <Eye className="h-4 w-4 text-cyan-400" />
              <span>Stage-by-Stage Structured Records</span>
            </h3>

            {isLoading ? (
              <div className="py-8 text-center text-slate-500">Loading audit records...</div>
            ) : auditData && auditData.stages ? (
              <div className="space-y-3">
                {auditData.stages.map((stg, i) => (
                  <div key={i} className="bg-slate-950/80 rounded-xl p-4 border border-slate-800 space-y-2">
                    <div className="flex items-center justify-between border-b border-slate-800/80 pb-2">
                      <div className="flex items-center space-x-2">
                        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 uppercase font-mono">
                          STAGE {i + 1}: {stg.stage}
                        </span>
                      </div>
                      <span className="font-mono text-[11px] text-slate-400">{stg.timestamp}</span>
                    </div>

                    {stg.notes && (
                      <p className="text-[11px] text-slate-300 italic bg-slate-900/60 p-2 rounded border border-slate-800/60">
                        {stg.notes}
                      </p>
                    )}

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-1">
                      {stg.input_summary && (
                        <div>
                          <span className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider block mb-1">
                            Input Summary
                          </span>
                          <pre className="bg-slate-900 p-2.5 rounded-lg border border-slate-800 text-[10px] text-slate-300 overflow-x-auto font-mono">
                            {JSON.stringify(stg.input_summary, null, 2)}
                          </pre>
                        </div>
                      )}
                      {stg.output_summary && (
                        <div>
                          <span className="text-[10px] text-slate-500 font-semibold uppercase tracking-wider block mb-1">
                            Output Summary
                          </span>
                          <pre className="bg-slate-900 p-2.5 rounded-lg border border-slate-800 text-[10px] text-slate-300 overflow-x-auto font-mono">
                            {JSON.stringify(stg.output_summary, null, 2)}
                          </pre>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="p-4 bg-slate-950 rounded-xl text-slate-400">
                Audit records stored in DB for event {event.event_id}.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
