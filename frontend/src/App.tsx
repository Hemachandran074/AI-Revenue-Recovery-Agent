import { useEffect, useState, useCallback } from 'react';
import { Header } from './components/Header';
import { MetricsOverview } from './components/MetricsOverview';
import { EventsTable } from './components/EventsTable';
import { SimulatorPanel } from './components/SimulatorPanel';
import { EventDetailModal } from './components/EventDetailModal';
import { ReadinessDrawer } from './components/ReadinessDrawer';
import { fetchMetrics, fetchEvents, type BatchMetrics, type EventRow } from './services/api';

export function App() {
  const [metrics, setMetrics] = useState<BatchMetrics | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [selectedEvent, setSelectedEvent] = useState<EventRow | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);

  // Modals state
  const [isSimulatorOpen, setIsSimulatorOpen] = useState<boolean>(false);
  const [isReadinessOpen, setIsReadinessOpen] = useState<boolean>(false);

  const loadData = useCallback(async (isManual = false) => {
    if (isManual) setIsRefreshing(true);
    try {
      const [metricsData, eventsData] = await Promise.all([
        fetchMetrics(100),
        fetchEvents(100),
      ]);
      setMetrics(metricsData);
      setEvents(eventsData.events || []);
    } catch (err) {
      console.error('Failed to refresh dashboard data:', err);
    } finally {
      setIsLoading(false);
      if (isManual) setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Polling loop (every 3 seconds)
  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(() => {
      loadData();
    }, 3000);
    return () => clearInterval(interval);
  }, [autoRefresh, loadData]);

  return (
    <div className="min-h-screen bg-[#0b0f19] text-slate-100 flex flex-col antialiased selection:bg-cyan-500/30 selection:text-cyan-200">
      {/* Top Navbar Header */}
      <Header
        autoRefresh={autoRefresh}
        onToggleAutoRefresh={() => setAutoRefresh(!autoRefresh)}
        onRefresh={() => loadData(true)}
        isRefreshing={isRefreshing}
        onOpenSimulator={() => setIsSimulatorOpen(true)}
        onOpenCheckout={() => setIsSimulatorOpen(true)}
        onOpenReadiness={() => setIsReadinessOpen(true)}
      />

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">
        {/* Executive Metrics Overview */}
        <MetricsOverview metrics={metrics} isLoading={isLoading} />

        {/* Filterable Batch Events Stream Table */}
        <EventsTable
          events={events}
          onSelectEvent={(ev) => setSelectedEvent(ev)}
          isLoading={isLoading}
        />
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-800/80 py-4 text-center text-xs text-slate-500 bg-slate-950/40">
        <p>AI Revenue Recovery Agent • Built for Razorpay Buildathon • Gemini 3.1 Flash Lite & Twilio WhatsApp</p>
      </footer>

      {/* Simulator Modal */}
      <SimulatorPanel
        isOpen={isSimulatorOpen}
        onClose={() => setIsSimulatorOpen(false)}
        onSimulationSuccess={() => loadData(true)}
      />

      {/* Event Details & Audit Trail Modal */}
      <EventDetailModal
        event={selectedEvent}
        onClose={() => setSelectedEvent(null)}
      />

      {/* System Health & Readiness Modal */}
      <ReadinessDrawer
        isOpen={isReadinessOpen}
        onClose={() => setIsReadinessOpen(false)}
      />
    </div>
  );
}

export default App;
