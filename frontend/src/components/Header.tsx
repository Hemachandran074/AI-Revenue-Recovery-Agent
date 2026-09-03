import React from 'react';
import { Activity, ShieldCheck, RefreshCw, Sparkles, Terminal, CreditCard } from 'lucide-react';

interface HeaderProps {
  autoRefresh: boolean;
  onToggleAutoRefresh: () => void;
  onRefresh: () => void;
  isRefreshing: boolean;
  onOpenSimulator: () => void;
  onOpenCheckout: () => void;
  onOpenReadiness: () => void;
}

export const Header: React.FC<HeaderProps> = ({
  autoRefresh,
  onToggleAutoRefresh,
  onRefresh,
  isRefreshing,
  onOpenSimulator,
  onOpenCheckout,
  onOpenReadiness,
}) => {
  return (
    <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur-md sticky top-0 z-40">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3.5 flex flex-wrap items-center justify-between gap-4">
        {/* Brand & Logo */}
        <div className="flex items-center space-x-3.5">
          <div className="h-10 w-10 rounded-xl bg-gradient-to-tr from-cyan-500 to-blue-600 flex items-center justify-center shadow-lg shadow-cyan-500/20">
            <Sparkles className="h-5 w-5 text-white animate-pulse" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <h1 className="text-lg font-bold text-slate-100 tracking-tight">
                AI Revenue Recovery Agent
              </h1>
              <span className="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 flex items-center gap-1">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-ping" />
                Live Poller Active
              </span>
            </div>
            <p className="text-xs text-slate-400">
              Autonomous 4-stage pipeline powered by Gemini LLM & Razorpay
            </p>
          </div>
        </div>

        {/* Quick Actions & Status */}
        <div className="flex items-center space-x-2.5">
          {/* 1-Click Simulation Button */}
          <button
            onClick={onOpenSimulator}
            className="flex items-center space-x-1.5 px-3.5 py-1.5 rounded-lg bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 text-white text-xs font-semibold shadow-md shadow-blue-500/20 transition-all hover:scale-[1.02] cursor-pointer"
          >
            <Terminal className="h-3.5 w-3.5" />
            <span>Simulate Failure</span>
          </button>

          {/* Test Browser Checkout Button */}
          <button
            onClick={onOpenCheckout}
            className="flex items-center space-x-1.5 px-3.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-cyan-500/30 text-xs font-semibold transition-all hover:border-cyan-500/60 cursor-pointer"
          >
            <CreditCard className="h-3.5 w-3.5" />
            <span>Test Checkout</span>
          </button>

          {/* Readiness / Health Status */}
          <button
            onClick={onOpenReadiness}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 text-xs font-medium transition cursor-pointer"
            title="System Capabilities & Configuration"
          >
            <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
            <span className="hidden sm:inline">Health</span>
          </button>

          {/* Auto Refresh Toggle */}
          <button
            onClick={onToggleAutoRefresh}
            className={`flex items-center space-x-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition cursor-pointer ${
              autoRefresh
                ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'
                : 'bg-slate-800 border-slate-700 text-slate-400'
            }`}
            title="Auto-refresh every 3s"
          >
            <Activity className={`h-3.5 w-3.5 ${autoRefresh ? 'text-emerald-400' : 'text-slate-400'}`} />
            <span className="hidden md:inline">{autoRefresh ? 'Auto 3s' : 'Paused'}</span>
          </button>

          {/* Manual Refresh Button */}
          <button
            onClick={onRefresh}
            disabled={isRefreshing}
            className="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition disabled:opacity-50 cursor-pointer"
            title="Refresh now"
          >
            <RefreshCw className={`h-4 w-4 ${isRefreshing ? 'animate-spin text-cyan-400' : ''}`} />
          </button>
        </div>
      </div>
    </header>
  );
};
