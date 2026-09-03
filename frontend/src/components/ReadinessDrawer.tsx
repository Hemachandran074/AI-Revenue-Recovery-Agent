import React, { useEffect, useState } from 'react';
import { X, ShieldCheck, Key, Cpu, MessageSquare, Database } from 'lucide-react';
import { fetchReadiness, type ReadinessResponse } from '../services/api';

interface ReadinessDrawerProps {
  isOpen: boolean;
  onClose: () => void;
}

export const ReadinessDrawer: React.FC<ReadinessDrawerProps> = ({ isOpen, onClose }) => {
  const [readiness, setReadiness] = useState<ReadinessResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (isOpen) {
      setIsLoading(true);
      fetchReadiness()
        .then(setReadiness)
        .catch(console.error)
        .finally(() => setIsLoading(false));
    }
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm animate-fade-in">
      <div className="glass-card bg-slate-900 border border-slate-700 w-full max-w-lg rounded-2xl shadow-2xl overflow-hidden flex flex-col">
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-800 flex items-center justify-between bg-slate-900/90">
          <div className="flex items-center space-x-2.5">
            <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400">
              <ShieldCheck className="h-5 w-5" />
            </div>
            <div>
              <h2 className="text-base font-bold text-slate-100">System Capabilities & Health</h2>
              <p className="text-xs text-slate-400">Live configuration & credential status</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition cursor-pointer"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-5 text-xs">
          {isLoading ? (
            <div className="py-6 text-center text-slate-500">Checking system health...</div>
          ) : readiness ? (
            <>
              {/* Capability Badges */}
              <div className="space-y-2.5">
                <h3 className="text-xs font-bold text-slate-300 uppercase tracking-wider">
                  Provider Integrations
                </h3>
                <div className="grid grid-cols-1 gap-2">
                  {[
                    {
                      name: 'Gemini Flash Lite LLM',
                      provider: readiness?.providers?.diagnose_llm || 'Gemini 3.1 Flash Lite',
                      ok: readiness?.capabilities?.diagnose_llm_configured ?? false,
                      icon: Cpu,
                      desc: 'DIAGNOSE stage root-cause classifier',
                    },
                    {
                      name: 'Razorpay Test Gateway',
                      provider: readiness?.providers?.payment || 'Razorpay',
                      ok: readiness?.capabilities?.payment_provider_configured ?? false,
                      icon: Key,
                      desc: 'DETECT webhook & order links',
                    },
                    {
                      name: 'Twilio WhatsApp Sandbox',
                      provider: readiness?.providers?.messaging || 'Twilio WhatsApp',
                      ok: readiness?.capabilities?.messaging_configured ?? false,
                      icon: MessageSquare,
                      desc: 'EXECUTE WhatsApp recovery dispatches',
                    },
                    {
                      name: 'PostgreSQL Database',
                      provider: 'SQLAlchemy / Postgres',
                      ok: readiness?.capabilities?.postgres_configured ?? false,
                      icon: Database,
                      desc: '100% stage audit log storage',
                    },
                  ].map((item, idx) => {
                    const Icon = item.icon;
                    return (
                      <div
                        key={idx}
                        className="p-3 rounded-xl bg-slate-950/70 border border-slate-800 flex items-center justify-between"
                      >
                        <div className="flex items-center space-x-3">
                          <div className={`p-2 rounded-lg ${item.ok ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'}`}>
                            <Icon className="h-4 w-4" />
                          </div>
                          <div>
                            <span className="font-semibold text-slate-200 block">{item.name}</span>
                            <span className="text-[11px] text-slate-400">{item.desc}</span>
                          </div>
                        </div>
                        <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold border ${
                          item.ok
                            ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                            : 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                        }`}>
                          {item.ok ? 'READY' : 'OFFLINE'}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Guardrails Config Table */}
              <div className="pt-2">
                <h3 className="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">
                  Active Guardrail Thresholds
                </h3>
                <div className="bg-slate-950/70 rounded-xl p-3 border border-slate-800 space-y-1.5 text-[11px] font-mono">
                  <div className="flex justify-between text-slate-300">
                    <span className="text-slate-500">Max Recovery Attempts:</span>
                    <strong className="text-cyan-400">{readiness.guardrail_config.max_recovery_attempts}</strong>
                  </div>
                  <div className="flex justify-between text-slate-300">
                    <span className="text-slate-500">Min Hours Between Contacts:</span>
                    <strong className="text-cyan-400">{readiness.guardrail_config.min_hours_between_contacts}h</strong>
                  </div>
                  <div className="flex justify-between text-slate-300">
                    <span className="text-slate-500">Quiet Hours Window:</span>
                    <strong className="text-cyan-400">
                      {readiness.guardrail_config.quiet_hours_start_local}:00 - {readiness.guardrail_config.quiet_hours_end_local}:00
                    </strong>
                  </div>
                  <div className="flex justify-between text-slate-300">
                    <span className="text-slate-500">Hard Stop Window:</span>
                    <strong className="text-cyan-400">{readiness.guardrail_config.hard_stop_days} days</strong>
                  </div>
                </div>
              </div>
            </>
          ) : (
            <div className="text-slate-400">Unable to load readiness details.</div>
          )}
        </div>
      </div>
    </div>
  );
};
