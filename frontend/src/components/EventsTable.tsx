import React, { useState } from 'react';
import { Search, ChevronRight } from 'lucide-react';
import type { EventRow } from '../services/api';

interface EventsTableProps {
  events: EventRow[];
  onSelectEvent: (event: EventRow) => void;
  isLoading: boolean;
}

const DISPOSITION_BADGES: Record<string, { label: string; class: string }> = {
  contacted: { label: 'Contacted', class: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' },
  retry_scheduled: { label: 'Retry Scheduled', class: 'bg-blue-500/10 text-blue-400 border-blue-500/30' },
  deferred_to_allowed_window: { label: 'Deferred (Quiet Hours)', class: 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30' },
  send_refused_not_opted_in: { label: 'Send Refused', class: 'bg-amber-500/10 text-amber-400 border-amber-500/30' },
  withheld_by_guardrail: { label: 'Withheld by Guardrail', class: 'bg-slate-700/40 text-slate-300 border-slate-600' },
  escalated_to_human: { label: 'Escalated to Human', class: 'bg-purple-500/10 text-purple-400 border-purple-500/30' },
  classifier_unavailable: { label: 'Classifier Unavailable', class: 'bg-amber-500/10 text-amber-400 border-amber-500/30' },
  dispatch_failed: { label: 'Dispatch Failed', class: 'bg-rose-500/10 text-rose-400 border-rose-500/30' },
  not_processed: { label: 'Not Processed', class: 'bg-slate-800 text-slate-400 border-slate-700' },
};

export const EventsTable: React.FC<EventsTableProps> = ({ events, onSelectEvent, isLoading }) => {
  const [search, setSearch] = useState('');
  const [filterDisposition, setFilterDisposition] = useState('all');

  const filteredEvents = events.filter((ev) => {
    const matchesSearch =
      ev.event_id.toLowerCase().includes(search.toLowerCase()) ||
      ev.customer_id.toLowerCase().includes(search.toLowerCase()) ||
      (ev.root_cause && ev.root_cause.toLowerCase().includes(search.toLowerCase())) ||
      (ev.decline_code && ev.decline_code.toLowerCase().includes(search.toLowerCase())) ||
      (ev.action && ev.action.toLowerCase().includes(search.toLowerCase()));

    const matchesFilter =
      filterDisposition === 'all' ||
      (filterDisposition === 'contacted' && ev.disposition === 'contacted') ||
      (filterDisposition === 'escalated' && ev.disposition === 'escalated_to_human') ||
      (filterDisposition === 'retry' && ev.disposition === 'retry_scheduled') ||
      (filterDisposition === 'withheld' && ev.disposition === 'withheld_by_guardrail');

    return matchesSearch && matchesFilter;
  });

  const formatTimestamp = (isoStr: string | null) => {
    if (!isoStr) return '-';
    try {
      const dt = new Date(isoStr);
      return dt.toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata',
        hour12: false,
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
    } catch {
      return isoStr;
    }
  };

  return (
    <div className="glass-card rounded-2xl border border-slate-800 overflow-hidden shadow-xl">
      {/* Table Controls Bar */}
      <div className="p-4 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3 bg-slate-900/60">
        <div className="flex items-center space-x-2">
          <h2 className="text-sm font-bold text-slate-200 uppercase tracking-wider">
            Batch Events Stream
          </h2>
          <span className="px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 text-xs font-mono">
            {filteredEvents.length} events
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          {/* Search Input */}
          <div className="relative">
            <Search className="h-3.5 w-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              placeholder="Search event, customer, cause..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="bg-slate-950/80 border border-slate-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-cyan-500 w-56 sm:w-64"
            />
          </div>

          {/* Filter Pills */}
          <div className="flex items-center bg-slate-950/80 border border-slate-800 rounded-lg p-0.5 text-xs font-medium">
            {[
              { id: 'all', label: 'All' },
              { id: 'contacted', label: 'Contacted' },
              { id: 'escalated', label: 'Escalated' },
              { id: 'retry', label: 'Retry' },
              { id: 'withheld', label: 'Withheld' },
            ].map((tab) => (
              <button
                key={tab.id}
                onClick={() => setFilterDisposition(tab.id)}
                className={`px-2.5 py-1 rounded-md transition cursor-pointer ${
                  filterDisposition === tab.id
                    ? 'bg-slate-800 text-cyan-400 font-semibold shadow'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-slate-950/60 text-slate-400 uppercase font-semibold text-[11px] border-b border-slate-800">
            <tr>
              <th scope="col" className="py-3 px-4">Event ID</th>
              <th scope="col" className="py-3 px-4">Timestamp (IST)</th>
              <th scope="col" className="py-3 px-4">Root Cause</th>
              <th scope="col" className="py-3 px-4">Action</th>
              <th scope="col" className="py-3 px-4">Outcome</th>
              <th scope="col" className="py-3 px-4 text-right">Amount</th>
              <th scope="col" className="py-3 px-4 text-right">Latency</th>
              <th scope="col" className="py-3 px-4 text-center">Trail</th>
              <th scope="col" className="py-3 px-3 text-right"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/60">
            {isLoading && events.length === 0 ? (
              <tr>
                <td colSpan={9} className="py-8 text-center text-slate-500">
                  Loading events stream...
                </td>
              </tr>
            ) : filteredEvents.length === 0 ? (
              <tr>
                <td colSpan={9} className="py-8 text-center text-slate-500">
                  No matching events found.
                </td>
              </tr>
            ) : (
              filteredEvents.map((ev) => {
                const badge = DISPOSITION_BADGES[ev.disposition] || {
                  label: ev.disposition,
                  class: 'bg-slate-800 text-slate-400 border-slate-700',
                };
                const amount = (ev.amount_minor / 100).toFixed(2);
                const stageCount = ev.stages ? ev.stages.length : 0;
                const isComplete = stageCount >= 4;

                return (
                  <tr
                    key={ev.event_id}
                    onClick={() => onSelectEvent(ev)}
                    className="hover:bg-slate-800/40 transition cursor-pointer group"
                  >
                    <td className="py-3 px-4 font-mono font-medium text-slate-200">
                      <span className="group-hover:text-cyan-400 transition">
                        {ev.event_id.slice(0, 8)}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-slate-400 whitespace-nowrap font-mono text-[11px]">
                      {formatTimestamp(ev.received_at)}
                    </td>
                    <td className="py-3 px-4">
                      <span className="font-semibold text-purple-300">
                        {ev.root_cause || '-'}
                      </span>
                      {ev.confidence !== null && (
                        <span className="text-[10px] text-slate-500 ml-1">
                          ({(ev.confidence * 100).toFixed(0)}%)
                        </span>
                      )}
                    </td>
                    <td className="py-3 px-4 text-slate-300">
                      {ev.action || '-'}
                    </td>
                    <td className="py-3 px-4">
                      <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold border ${badge.class}`}>
                        {badge.label}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-right font-mono font-medium text-slate-200">
                      ₹{amount}
                    </td>
                    <td className="py-3 px-4 text-right font-mono text-slate-400 text-[11px]">
                      {ev.decision_latency_ms ? `${ev.decision_latency_ms.toFixed(0)}ms` : '-'}
                    </td>
                    <td className="py-3 px-4 text-center font-mono text-[11px]">
                      <span className={`px-1.5 py-0.5 rounded ${isComplete ? 'text-emerald-400 bg-emerald-500/10' : 'text-amber-400 bg-amber-500/10'}`}>
                        {isComplete ? '4/4' : `${stageCount}/4`}
                      </span>
                    </td>
                    <td className="py-3 px-3 text-right text-slate-500 group-hover:text-cyan-400 transition">
                      <ChevronRight className="h-4 w-4 inline" />
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};
