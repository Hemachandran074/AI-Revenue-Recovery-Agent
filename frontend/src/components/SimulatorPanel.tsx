import React, { useState } from 'react';
import { X, Play, CreditCard, Sparkles, CheckCircle2, AlertCircle, ExternalLink, RefreshCw } from 'lucide-react';
import { triggerSimulation, createTestOrder, type SimulationResult, type CreateOrderResult } from '../services/api';

interface SimulatorPanelProps {
  isOpen: boolean;
  onClose: () => void;
  onSimulationSuccess: () => void;
}

export const SimulatorPanel: React.FC<SimulatorPanelProps> = ({
  isOpen,
  onClose,
  onSimulationSuccess,
}) => {
  const [activeTab, setActiveTab] = useState<'simulate' | 'checkout'>('simulate');
  const [cause, setCause] = useState<string>('sca');
  const [amount, setAmount] = useState<number>(499.0);
  const [contact, setContact] = useState<string>('+919566687795');
  const [email, setEmail] = useState<string>('recovery.demo@example.com');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [simulationResult, setSimulationResult] = useState<SimulationResult | null>(null);
  const [orderResult, setOrderResult] = useState<CreateOrderResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSimulate = async () => {
    setIsLoading(true);
    setError(null);
    setSimulationResult(null);
    try {
      const res = await triggerSimulation({
        cause,
        amount,
        contact,
      });
      setSimulationResult(res);
      onSimulationSuccess();
    } catch (err: any) {
      setError(err.message || 'Simulation request failed');
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreateOrder = async () => {
    setIsLoading(true);
    setError(null);
    setOrderResult(null);
    try {
      const res = await createTestOrder({
        amount,
        contact,
        email,
      });
      setOrderResult(res);
    } catch (err: any) {
      setError(err.message || 'Order creation failed');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm animate-fade-in">
      <div className="glass-card bg-slate-900 border border-slate-700 w-full max-w-2xl rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[90vh]">
        {/* Modal Header */}
        <div className="px-6 py-4 border-b border-slate-800 flex items-center justify-between bg-slate-900/90">
          <div className="flex items-center space-x-2.5">
            <div className="p-2 rounded-lg bg-blue-500/10 text-blue-400">
              <Sparkles className="h-5 w-5" />
            </div>
            <div>
              <h2 className="text-base font-bold text-slate-100">Live Recovery Simulator</h2>
              <p className="text-xs text-slate-400">Test AI diagnosis and WhatsApp recovery in real time</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition cursor-pointer"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Tab Navigation */}
        <div className="flex border-b border-slate-800 px-6 bg-slate-950/50 text-xs font-semibold">
          <button
            onClick={() => { setActiveTab('simulate'); setError(null); }}
            className={`py-3 px-4 border-b-2 transition cursor-pointer ${
              activeTab === 'simulate'
                ? 'border-cyan-500 text-cyan-400'
                : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
          >
            1-Click Pipeline Simulator
          </button>
          <button
            onClick={() => { setActiveTab('checkout'); setError(null); }}
            className={`py-3 px-4 border-b-2 transition cursor-pointer ${
              activeTab === 'checkout'
                ? 'border-cyan-500 text-cyan-400'
                : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
          >
            Manual Gateway Checkout Test
          </button>
        </div>

        {/* Modal Body */}
        <div className="p-6 overflow-y-auto space-y-5 flex-1">
          {error && (
            <div className="p-3.5 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs flex items-center gap-2">
              <AlertCircle className="h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {activeTab === 'simulate' ? (
            <>
              {/* Cause Selection */}
              <div>
                <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">
                  Select Failure Cause to Test
                </label>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                  {[
                    { id: 'sca', label: '3DS Auth Drop-off', desc: 'SCA Abandoned -> Sends Fresh 3DS Link' },
                    { id: 'card', label: 'Expired Card', desc: 'Card Expired -> Sends Update Link' },
                    { id: 'friction', label: 'Checkout Friction', desc: 'Modal Abandonment -> Sends 1-Click Reminder' },
                    { id: 'funds', label: 'Insufficient Funds', desc: 'Payday-aware retry -> Silent Retry' },
                  ].map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => setCause(item.id)}
                      className={`p-3 rounded-xl border text-left transition cursor-pointer ${
                        cause === item.id
                          ? 'border-cyan-500 bg-cyan-950/20 shadow-md shadow-cyan-500/10'
                          : 'border-slate-800 bg-slate-800/40 hover:border-slate-700'
                      }`}
                    >
                      <div className="mb-1">
                        <span className="text-xs font-bold text-slate-100">{item.label}</span>
                      </div>
                      <p className="text-[11px] text-slate-400">{item.desc}</p>
                    </button>
                  ))}
                </div>
              </div>

              {/* Amount & Contact Number */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1.5">
                    Amount (₹ INR)
                  </label>
                  <input
                    type="number"
                    value={amount}
                    onChange={(e) => setAmount(parseFloat(e.target.value) || 100)}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-cyan-500"
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1.5">
                    WhatsApp Recipient
                  </label>
                  <input
                    type="text"
                    value={contact}
                    onChange={(e) => setContact(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-cyan-500"
                  />
                </div>
              </div>

              {/* Trigger Button */}
              <button
                onClick={handleSimulate}
                disabled={isLoading}
                className="w-full py-2.5 rounded-xl bg-gradient-to-r from-blue-600 to-cyan-600 hover:from-blue-500 hover:to-cyan-500 text-white font-semibold text-sm shadow-lg shadow-blue-500/20 transition-all flex items-center justify-center space-x-2 cursor-pointer disabled:opacity-50"
              >
                {isLoading ? (
                  <>
                    <RefreshCw className="h-4 w-4 animate-spin" />
                    <span>Running 4-Stage Pipeline...</span>
                  </>
                ) : (
                  <>
                    <Play className="h-4 w-4" />
                    <span>Simulate & Execute Recovery</span>
                  </>
                )}
              </button>

              {/* Simulation Result Box */}
              {simulationResult && (
                <div className="p-4 rounded-xl bg-slate-950/80 border border-slate-800 space-y-2.5 animate-fade-in text-xs">
                  <div className="flex items-center justify-between border-b border-slate-800 pb-2">
                    <span className="font-bold text-slate-200 flex items-center gap-1.5">
                      <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                      Pipeline Result: {simulationResult.delivery_status === 'sent' ? 'Message Delivered' : simulationResult.delivery_status}
                    </span>
                    <span className="text-slate-400 font-mono text-[11px]">
                      Latency: {simulationResult.decision_latency_ms?.toFixed(0)}ms
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-[11px] text-slate-300">
                    <div><span className="text-slate-500">Root Cause:</span> <strong className="text-purple-400">{simulationResult.root_cause}</strong></div>
                    <div><span className="text-slate-500">Action:</span> <strong className="text-cyan-400">{simulationResult.action}</strong></div>
                    {simulationResult.provider_message_id && (
                      <div className="col-span-2"><span className="text-slate-500">Twilio SID:</span> <code className="text-slate-300">{simulationResult.provider_message_id}</code></div>
                    )}
                    {simulationResult.recovery_link_url && (
                      <div className="col-span-2 flex items-center justify-between pt-1">
                        <span className="text-slate-500">Recovery Link:</span>
                        <a
                          href={simulationResult.recovery_link_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-cyan-400 hover:underline flex items-center gap-1 font-mono text-[11px]"
                        >
                          <span>Open Link</span>
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </>
          ) : (
            <>
              {/* Tab 2: Interactive Browser Checkout Generator */}
              <div className="space-y-4">
                <p className="text-xs text-slate-400 leading-relaxed">
                  Generate a real Razorpay Gateway checkout session. Open the link to test failing cards, netbanking, wallets, or UPI in the real browser interface.
                </p>

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div>
                    <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1.5">
                      Amount (₹ INR)
                    </label>
                    <input
                      type="number"
                      value={amount}
                      onChange={(e) => setAmount(parseFloat(e.target.value) || 100)}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-cyan-500"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1.5">
                      Contact
                    </label>
                    <input
                      type="text"
                      value={contact}
                      onChange={(e) => setContact(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-cyan-500"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1.5">
                      Email
                    </label>
                    <input
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-cyan-500"
                    />
                  </div>
                </div>

                <button
                  onClick={handleCreateOrder}
                  disabled={isLoading}
                  className="w-full py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-cyan-500/40 font-semibold text-sm transition flex items-center justify-center space-x-2 cursor-pointer disabled:opacity-50"
                >
                  <CreditCard className="h-4 w-4" />
                  <span>Generate Test Gateway Session</span>
                </button>

                {orderResult && (
                  <div className="p-4 rounded-xl bg-slate-950/80 border border-slate-800 space-y-3 animate-fade-in">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-bold text-emerald-400 flex items-center gap-1.5">
                        <CheckCircle2 className="h-4 w-4" />
                        Test Gateway Order Ready
                      </span>
                      <span className="text-[11px] font-mono text-slate-400">{orderResult.order_id}</span>
                    </div>
                    <a
                      href={orderResult.checkout_url}
                      target="_blank"
                      rel="noreferrer"
                      className="w-full py-2.5 px-4 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white font-semibold text-xs transition flex items-center justify-center space-x-1.5 text-center cursor-pointer shadow-md shadow-cyan-500/20"
                    >
                      <span>Open Razorpay Checkout in Browser</span>
                      <ExternalLink className="h-3.5 w-3.5" />
                    </a>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
