import { useEffect } from 'react';
import { AnimatePresence } from 'framer-motion';
import { TitleBar } from './components/TitleBar';
import { Sidebar } from './components/Sidebar';
import { LibraryBrowser } from './components/LibraryBrowser';
import { TrackDetails } from './components/TrackDetails';
import { AnalysisProgress } from './components/AnalysisProgress';
import { Settings } from './components/Settings';
import { useUIStore } from './stores';
import { useAnalysisProgress } from './hooks/useTauri';

function App() {
  const viewMode = useUIStore((s) => s.viewMode);
  
  // Set up event listeners for analysis progress
  useAnalysisProgress();
  
  // Handle keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ctrl/Cmd + O - Open folder
      if ((e.ctrlKey || e.metaKey) && e.key === 'o') {
        e.preventDefault();
        // Trigger open folder
      }
      
      // Ctrl/Cmd + A - Select all
      if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
        e.preventDefault();
        // Select all tracks
      }
      
      // Escape - Deselect / close panel
      if (e.key === 'Escape') {
        useUIStore.getState().setSelectedTrack(null);
      }
    };
    
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  const renderContent = () => {
    switch (viewMode) {
      case 'library':
        return <LibraryBrowser />;
      case 'analysis':
        return <LibraryBrowser />; // Same view but filtered to show changes
      case 'duplicates':
        return <DuplicatesView />;
      case 'settings':
        return <Settings />;
      default:
        return <LibraryBrowser />;
    }
  };

  return (
    <div className="h-screen flex flex-col bg-surface-300 overflow-hidden">
      {/* Custom title bar (for frameless window) */}
      <TitleBar />
      
      {/* Main content */}
      <div className="flex-1 flex min-h-0">
        {/* Sidebar */}
        <Sidebar />
        
        {/* Main area */}
        <main className="flex-1 flex flex-col min-w-0">
          <AnimatePresence mode="wait">
            {renderContent()}
          </AnimatePresence>
        </main>
        
        {/* Details panel */}
        <TrackDetails />
      </div>
      
      {/* Analysis progress overlay */}
      <AnimatePresence>
        <AnalysisProgress />
      </AnimatePresence>
    </div>
  );
}

// ============================================================================
// Duplicates View (placeholder)
// ============================================================================

function DuplicatesView() {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center">
        <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-white/5 flex items-center justify-center">
          <svg className="w-8 h-8 text-white/30" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
          </svg>
        </div>
        <h2 className="text-xl font-display font-semibold mb-2">
          Duplicate Detection
        </h2>
        <p className="text-white/50 max-w-sm">
          Analyze your library to find duplicate tracks using audio fingerprinting.
        </p>
      </div>
    </div>
  );
}

export default App;
