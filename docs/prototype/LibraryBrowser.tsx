import { useMemo, useCallback } from 'react';
import { Virtuoso } from 'react-virtuoso';
import { motion, AnimatePresence } from 'framer-motion';
import { clsx } from 'clsx';
import {
  FolderOpen,
  Sparkles,
  CheckSquare,
  Square,
  ChevronUp,
  ChevronDown,
  Search,
  X,
  Music,
  Clock,
  Disc3
} from 'lucide-react';
import { useLibraryStore, useUIStore, useAnalysisStore } from '../stores';
import { useOpenFolder, useAnalyze } from '../hooks/useTauri';
import { EnergyBar } from './EnergyBar';
import { TagBadge } from './TagBadge';
import type { Track, SortField } from '../types';

// ============================================================================
// Track Row Component
// ============================================================================

interface TrackRowProps {
  track: Track;
  isSelected: boolean;
  onSelect: () => void;
  onClick: () => void;
}

function TrackRow({ track, isSelected, onSelect, onClick }: TrackRowProps) {
  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className={clsx(
        'track-row group',
        isSelected && 'selected'
      )}
      onClick={onClick}
    >
      {/* Checkbox */}
      <button
        className="p-1 mr-2 text-white/30 hover:text-white transition-colors"
        onClick={(e) => {
          e.stopPropagation();
          onSelect();
        }}
      >
        {isSelected ? (
          <CheckSquare className="w-4 h-4 text-accent-400" />
        ) : (
          <Square className="w-4 h-4" />
        )}
      </button>
      
      {/* Track icon / waveform placeholder */}
      <div className="w-10 h-10 rounded-lg bg-surface-100 flex items-center justify-center mr-3 shrink-0">
        {track.analyzed ? (
          <div className="flex items-end gap-0.5 h-5">
            {[0.3, 0.7, 1, 0.5, 0.8].map((h, i) => (
              <div
                key={i}
                className="w-1 bg-accent-500 rounded-full"
                style={{ height: `${h * 100}%` }}
              />
            ))}
          </div>
        ) : (
          <Music className="w-4 h-4 text-white/30" />
        )}
      </div>
      
      {/* Title & Artist */}
      <div className="flex-1 min-w-0 mr-4">
        <div className="flex items-center gap-2">
          <span className="font-medium truncate">
            {track.title || track.filename}
          </span>
          {track.hasChanges && (
            <span className="w-2 h-2 rounded-full bg-amber-400 shrink-0" />
          )}
        </div>
        {track.artist && (
          <div className="text-sm text-white/50 truncate">
            {track.artist}
          </div>
        )}
      </div>
      
      {/* Genre */}
      <div className="w-28 shrink-0 mr-4">
        {track.detectedGenre ? (
          <TagBadge variant="genre">{track.detectedGenre}</TagBadge>
        ) : (
          <span className="text-sm text-white/30">—</span>
        )}
      </div>
      
      {/* BPM */}
      <div className="w-16 shrink-0 mr-4 text-right">
        {track.detectedBpm ? (
          <span className="text-sm font-mono">
            {track.detectedBpm.toFixed(1)}
          </span>
        ) : (
          <span className="text-sm text-white/30">—</span>
        )}
      </div>
      
      {/* Key */}
      <div className="w-14 shrink-0 mr-4">
        {track.keyCamelot ? (
          <TagBadge variant="key">{track.keyCamelot}</TagBadge>
        ) : (
          <span className="text-sm text-white/30">—</span>
        )}
      </div>
      
      {/* Energy */}
      <div className="w-20 shrink-0 mr-4">
        {track.energy ? (
          <EnergyBar level={track.energy} />
        ) : (
          <div className="h-1.5 bg-white/10 rounded-full" />
        )}
      </div>
      
      {/* Duration */}
      <div className="w-14 shrink-0 text-right text-sm text-white/50 font-mono">
        {track.duration > 0 ? formatDuration(track.duration) : '—'}
      </div>
    </motion.div>
  );
}

// ============================================================================
// Table Header
// ============================================================================

function TableHeader() {
  const sortField = useUIStore((s) => s.sortField);
  const sortDirection = useUIStore((s) => s.sortDirection);
  const setSort = useUIStore((s) => s.setSort);
  const selectAll = useLibraryStore((s) => s.selectAll);
  const deselectAll = useLibraryStore((s) => s.deselectAll);
  const selectedIds = useLibraryStore((s) => s.selectedIds);
  const trackCount = useLibraryStore((s) => s.tracks.length);
  
  const allSelected = selectedIds.size === trackCount && trackCount > 0;
  
  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null;
    return sortDirection === 'asc' ? (
      <ChevronUp className="w-3 h-3" />
    ) : (
      <ChevronDown className="w-3 h-3" />
    );
  };
  
  const HeaderCell = ({ field, children, className }: { 
    field: SortField; 
    children: React.ReactNode;
    className?: string;
  }) => (
    <button
      className={clsx(
        'flex items-center gap-1 text-xs font-medium text-white/50 hover:text-white transition-colors',
        className
      )}
      onClick={() => setSort(field)}
    >
      {children}
      <SortIcon field={field} />
    </button>
  );

  return (
    <div className="flex items-center px-4 py-2 border-b border-white/10 bg-surface-200/50 sticky top-0 z-10">
      <button
        className="p-1 mr-2 text-white/30 hover:text-white transition-colors"
        onClick={() => allSelected ? deselectAll() : selectAll()}
      >
        {allSelected ? (
          <CheckSquare className="w-4 h-4 text-accent-400" />
        ) : (
          <Square className="w-4 h-4" />
        )}
      </button>
      
      <div className="w-10 mr-3" /> {/* Album art space */}
      
      <HeaderCell field="filename" className="flex-1 min-w-0">
        Title
      </HeaderCell>
      
      <HeaderCell field="genre" className="w-28 shrink-0 mr-4">
        Genre
      </HeaderCell>
      
      <HeaderCell field="bpm" className="w-16 shrink-0 mr-4 justify-end">
        BPM
      </HeaderCell>
      
      <HeaderCell field="key" className="w-14 shrink-0 mr-4">
        Key
      </HeaderCell>
      
      <HeaderCell field="energy" className="w-20 shrink-0 mr-4">
        Energy
      </HeaderCell>
      
      <HeaderCell field="duration" className="w-14 shrink-0 justify-end">
        <Clock className="w-3 h-3" />
      </HeaderCell>
    </div>
  );
}

// ============================================================================
// Empty State
// ============================================================================

function EmptyState({ onOpenFolder }: { onOpenFolder: () => void }) {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center max-w-md">
        <div className="w-20 h-20 mx-auto mb-6 rounded-2xl bg-gradient-to-br from-accent-500/20 to-cyan-500/20 flex items-center justify-center">
          <Disc3 className="w-10 h-10 text-accent-400" />
        </div>
        <h2 className="text-xl font-display font-semibold mb-2">
          No tracks loaded
        </h2>
        <p className="text-white/50 mb-6">
          Open a folder to scan your DJ library and start analyzing tracks.
        </p>
        <button
          onClick={onOpenFolder}
          className="btn btn-primary"
        >
          <FolderOpen className="w-4 h-4" />
          Open Folder
        </button>
      </div>
    </div>
  );
}

// ============================================================================
// Main Library Browser
// ============================================================================

export function LibraryBrowser() {
  const tracks = useLibraryStore((s) => s.tracks);
  const selectedIds = useLibraryStore((s) => s.selectedIds);
  const toggleTrackSelection = useLibraryStore((s) => s.toggleTrackSelection);
  const setSelectedTrack = useUIStore((s) => s.setSelectedTrack);
  const filters = useUIStore((s) => s.filters);
  const sortField = useUIStore((s) => s.sortField);
  const sortDirection = useUIStore((s) => s.sortDirection);
  const setFilters = useUIStore((s) => s.setFilters);
  
  const isAnalyzing = useAnalysisStore((s) => s.isAnalyzing);
  
  const openFolder = useOpenFolder();
  const analyze = useAnalyze();
  
  // Filter and sort tracks
  const filteredTracks = useMemo(() => {
    let result = [...tracks];
    
    // Search filter
    if (filters.search) {
      const search = filters.search.toLowerCase();
      result = result.filter((t) =>
        t.filename.toLowerCase().includes(search) ||
        t.title?.toLowerCase().includes(search) ||
        t.artist?.toLowerCase().includes(search) ||
        t.detectedGenre?.toLowerCase().includes(search)
      );
    }
    
    // Genre filter
    if (filters.genres.length > 0) {
      result = result.filter((t) =>
        t.detectedGenre && filters.genres.includes(t.detectedGenre)
      );
    }
    
    // Has changes filter
    if (filters.hasChanges !== null) {
      result = result.filter((t) => t.hasChanges === filters.hasChanges);
    }
    
    // Analyzed filter
    if (filters.analyzed !== null) {
      result = result.filter((t) => t.analyzed === filters.analyzed);
    }
    
    // Energy range filter
    result = result.filter((t) =>
      !t.energy || (t.energy >= filters.energyRange[0] && t.energy <= filters.energyRange[1])
    );
    
    // BPM range filter
    result = result.filter((t) =>
      !t.detectedBpm || (t.detectedBpm >= filters.bpmRange[0] && t.detectedBpm <= filters.bpmRange[1])
    );
    
    // Sort
    result.sort((a, b) => {
      let aVal: any, bVal: any;
      
      switch (sortField) {
        case 'filename':
          aVal = a.title || a.filename;
          bVal = b.title || b.filename;
          break;
        case 'artist':
          aVal = a.artist || '';
          bVal = b.artist || '';
          break;
        case 'bpm':
          aVal = a.detectedBpm || 0;
          bVal = b.detectedBpm || 0;
          break;
        case 'key':
          aVal = a.keyCamelot || '';
          bVal = b.keyCamelot || '';
          break;
        case 'energy':
          aVal = a.energy || 0;
          bVal = b.energy || 0;
          break;
        case 'genre':
          aVal = a.detectedGenre || '';
          bVal = b.detectedGenre || '';
          break;
        case 'duration':
          aVal = a.duration || 0;
          bVal = b.duration || 0;
          break;
        default:
          aVal = a.filename;
          bVal = b.filename;
      }
      
      if (typeof aVal === 'string') {
        const cmp = aVal.localeCompare(bVal);
        return sortDirection === 'asc' ? cmp : -cmp;
      }
      
      return sortDirection === 'asc' ? aVal - bVal : bVal - aVal;
    });
    
    return result;
  }, [tracks, filters, sortField, sortDirection]);

  const handleRowClick = useCallback((track: Track) => {
    setSelectedTrack(track.id);
  }, [setSelectedTrack]);

  if (tracks.length === 0) {
    return <EmptyState onOpenFolder={openFolder} />;
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Toolbar */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-white/5">
        {/* Search */}
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-white/30" />
          <input
            type="text"
            placeholder="Search tracks..."
            value={filters.search}
            onChange={(e) => setFilters({ search: e.target.value })}
            className="input pl-10"
          />
          {filters.search && (
            <button
              className="absolute right-3 top-1/2 -translate-y-1/2 text-white/30 hover:text-white"
              onClick={() => setFilters({ search: '' })}
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
        
        {/* Actions */}
        <div className="flex items-center gap-2">
          <button
            onClick={openFolder}
            className="btn btn-secondary"
          >
            <FolderOpen className="w-4 h-4" />
            Open
          </button>
          
          <button
            onClick={analyze}
            disabled={isAnalyzing || tracks.length === 0}
            className="btn btn-primary"
          >
            <Sparkles className="w-4 h-4" />
            {isAnalyzing ? 'Analyzing...' : 'Analyze'}
            {selectedIds.size > 0 && ` (${selectedIds.size})`}
          </button>
        </div>
      </div>
      
      {/* Results count */}
      <div className="px-4 py-2 text-xs text-white/40">
        {filteredTracks.length} of {tracks.length} tracks
        {selectedIds.size > 0 && ` • ${selectedIds.size} selected`}
      </div>
      
      {/* Table */}
      <div className="flex-1 min-h-0">
        <TableHeader />
        
        <Virtuoso
          data={filteredTracks}
          itemContent={(index, track) => (
            <TrackRow
              key={track.id}
              track={track}
              isSelected={selectedIds.has(track.id)}
              onSelect={() => toggleTrackSelection(track.id)}
              onClick={() => handleRowClick(track)}
            />
          )}
          className="scrollbar-hide"
        />
      </div>
    </div>
  );
}
