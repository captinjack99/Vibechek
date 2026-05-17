# Vibechek Project Summary

**Last Updated:** May 2026  
**Status:** Prototype Complete - Ready for Integration Testing

---

## Project Overview

**Vibechek** is an AI-powered DJ library management application that automatically analyzes and organizes music collections using machine learning. It solves the challenge of managing large DJ libraries (12,000+ tracks) by detecting genres, subgenres, energy levels, moods, and other DJ-relevant metadata while preserving critical performance data from DJ software like Rekordbox.

### Core Value Proposition
- Automatic ML-based genre/mood/energy classification
- BPM and key detection with Camelot wheel notation
- Audio fingerprinting for duplicate detection
- Tag diff preview before writing (non-destructive)
- Rekordbox/Traktor/Serato export compatibility
- Preserves existing cue points, beat grids, and performance annotations

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   React Frontend (src/)                  │
│   LibraryBrowser | AnalysisProgress | TrackDetails      │
├─────────────────────────────────────────────────────────┤
│                  Tauri Shell (src-tauri/)                │
│                  IPC Commands + State                    │
├─────────────────────────────────────────────────────────┤
│               Rust FFI (tauri-bindings/)                 │
│              Safe wrappers around C API                  │
├─────────────────────────────────────────────────────────┤
│                  C++ Core (core/)                        │
│   Essentia ML | Chromaprint | TagLib | FFmpeg           │
└─────────────────────────────────────────────────────────┘
```

### Technology Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| Frontend | React 18 + TypeScript | UI components |
| Styling | Tailwind CSS | Dark DJ theme |
| State | Zustand | Library/analysis/UI state |
| Animation | Framer Motion | Progress, transitions |
| Virtualization | react-virtuoso | 10k+ track performance |
| Desktop | Tauri 1.6 | Native app shell |
| FFI | Rust bindings | Safe C++ interop |
| Analysis | C++17 + Essentia | ML audio analysis |
| Fingerprinting | Chromaprint | Duplicate detection |
| Metadata | TagLib | MP3/FLAC/M4A tags |
| Decoding | FFmpeg | Audio format support |

---

## Project Structure

```
/home/claude/vibechek/
├── core/                      # C++ analysis library
│   ├── include/vibechek/
│   │   ├── types.h            # Data structures (Track, Genre, Key, etc.)
│   │   ├── analyzer.h         # Main API (analyze, export, duplicates)
│   │   └── fingerprint.h      # Chromaprint wrapper
│   ├── src/
│   │   ├── analyzer.cpp       # Main analyzer orchestration
│   │   ├── essentia_engine.cpp # ML model wrapper
│   │   ├── fingerprint.cpp    # Audio fingerprinting
│   │   ├── tag_reader.cpp     # TagLib metadata reading
│   │   ├── tag_writer.cpp     # TagLib metadata writing
│   │   ├── export.cpp         # JSON/XML/CSV export
│   │   ├── types.cpp          # Key/Camelot conversions
│   │   ├── utils.cpp          # Utility functions
│   │   ├── ffi.cpp            # C interface for Rust FFI
│   │   └── main.cpp           # CLI tool
│   ├── CMakeLists.txt         # Build configuration
│   ├── vcpkg.json             # Windows dependencies
│   └── README.md              # Build instructions
│
├── tauri-bindings/            # Rust FFI bindings
│   ├── src/
│   │   ├── lib.rs             # Safe Rust wrappers
│   │   └── commands.rs        # Tauri command examples
│   ├── Cargo.toml
│   └── build.rs
│
├── src-tauri/                 # Tauri application
│   ├── src/main.rs            # IPC handlers (with mock data)
│   ├── tauri.conf.json        # App configuration
│   ├── Cargo.toml
│   └── build.rs
│
├── src/                       # React frontend
│   ├── components/
│   │   ├── TitleBar.tsx       # Custom window controls
│   │   ├── Sidebar.tsx        # Navigation with badges
│   │   ├── LibraryBrowser.tsx # Main track list (virtualized)
│   │   ├── TrackDetails.tsx   # Side panel for track info
│   │   ├── AnalysisProgress.tsx # Progress overlay
│   │   ├── Settings.tsx       # Configuration panel
│   │   ├── TagBadge.tsx       # Energy/mood/key badges
│   │   └── index.ts           # Exports
│   ├── hooks/
│   │   ├── useTauri.ts        # Tauri IPC wrappers
│   │   └── index.ts
│   ├── stores/
│   │   └── index.ts           # Zustand state management
│   ├── types/
│   │   └── index.ts           # TypeScript definitions
│   ├── styles/
│   │   └── globals.css        # Tailwind + custom styles
│   ├── App.tsx                # Main app component
│   └── main.tsx               # Entry point
│
├── public/
│   └── vite.svg               # App icon
│
├── models/                    # ML models (download separately)
│
├── build/                     # C++ build output
│   └── libvibechek-core.a     # Static library (built)
│
├── package.json               # Node dependencies
├── vite.config.ts             # Vite configuration
├── tailwind.config.js         # Tailwind theme
├── tsconfig.json              # TypeScript config
├── postcss.config.js
├── README.md                  # Main documentation
├── build.sh                   # Linux/macOS build script
├── build.bat                  # Windows build script
└── .gitignore
```

---

## C++ Core API

### Key Types (types.h)

```cpp
struct TrackAnalysis {
    AudioMetadata metadata;       // Path, format, duration, existing tags
    std::vector<GenreResult> genres;  // Top 5 genres with confidence
    BpmResult bpm;                // BPM + candidates + confidence
    KeyResult key;                // Musical key, Camelot, Open Key
    EnergyResult energy;          // 1-10 level
    MoodResult mood;              // Primary mood classification
    TimeslotResult timeslot;      // Opener/Peak/Closing recommendation
    VocalResult vocals;           // Instrumental/Vocal type
    std::string fingerprint;      // Chromaprint audio fingerprint
};

enum class Mood { Happy, Sad, Energetic, Chill, Dark, Uplifting, Aggressive, Melancholic };
enum class Timeslot { Opener, EarlyNight, PeakTime, LateNight, Closing };
enum class VocalType { Instrumental, VocalChops, FemaleVocal, MaleVocal, MixedVocals };
```

### Main API (analyzer.h)

```cpp
class Analyzer {
public:
    bool initialize();
    std::optional<TrackAnalysis> analyze_track(const std::string& path);
    std::vector<TrackAnalysis> analyze_directory(path, progress_cb, error_cb);
    std::future<std::vector<TrackAnalysis>> analyze_directory_async(...);
    std::vector<DuplicateGroup> find_duplicates(analyses, threshold);
    bool write_tags(analysis, options);
    bool export_to_json/rekordbox_xml/csv(analyses, output_path);
    void cancel();
};
```

### FFI Interface (ffi.cpp)

C-compatible interface for Rust/Tauri:
- `vibechek_analyzer_create/destroy/initialize`
- `vibechek_analyze_track`
- `vibechek_analyze_directory` (with callbacks)
- `vibechek_fingerprint`
- `vibechek_export_*`
- Thread-safe with `thread_local` string returns

---

## Frontend Components

### State Management (Zustand)

```typescript
// Library Store
useLibraryStore: tracks[], selectedIds, setTracks, updateTrack, selectAll...

// Analysis Store  
useAnalysisStore: isAnalyzing, progress, duplicates, tagChanges...

// UI Store
useUIStore: viewMode, sortField, filters, sidebarCollapsed, selectedTrackId...

// Config Store (persisted)
useConfigStore: modelDirectory, numThreads, detectGenre, detectBpm...
```

### Key Components

| Component | Features |
|-----------|----------|
| **LibraryBrowser** | Virtual scrolling, sortable columns, multi-select, search filter |
| **TrackDetails** | Genre/BPM/Key display with diff arrows, energy bar, confidence % |
| **AnalysisProgress** | Animated waveform bars, progress %, ETA, cancel button |
| **Sidebar** | View switching, badge counts (total/analyzed/changes) |
| **Settings** | Model path, thread count, detection toggles, confidence threshold |

### Design System

- **Colors:** Dark surface (#11111b), accent purple (#a855f7), cyan (#06b6d4)
- **Fonts:** Geist (body), Outfit (display), Geist Mono (data)
- **Energy colors:** Green→Yellow→Orange→Red gradient (1-10)
- **Mood colors:** Unique per mood (Happy=yellow, Dark=indigo, etc.)

---

## Tauri IPC Commands

Defined in `src-tauri/src/main.rs`:

```rust
#[tauri::command] async fn get_version() -> String;
#[tauri::command] async fn scan_directory(path, recursive) -> Vec<TrackInfo>;
#[tauri::command] async fn analyze_tracks(paths) -> Vec<TrackInfo>;
#[tauri::command] async fn cancel_analysis();
#[tauri::command] async fn write_tags(tracks) -> usize;
#[tauri::command] async fn find_duplicates(tracks) -> Vec<DuplicateGroup>;
#[tauri::command] async fn export_rekordbox(tracks, output_path);
#[tauri::command] async fn export_json(tracks, output_path);
#[tauri::command] async fn get_config() -> AppConfig;
#[tauri::command] async fn save_config(config);
#[tauri::command] fn key_to_camelot(key) -> Option<String>;
```

Events emitted: `analysis-progress`, `analysis-complete`

**Note:** Current implementation uses mock data. Replace with actual `vibechek` crate calls when C++ core is connected.

---

## Build Instructions

### Frontend Only (Development)

```bash
cd vibechek
npm install
npm run dev
# Opens http://localhost:5173 with mock data
```

### C++ Core

```bash
# Ubuntu/Debian
sudo apt install cmake build-essential pkg-config \
    libessentia-dev libchromaprint-dev libtag1-dev \
    libavcodec-dev libavformat-dev libswresample-dev

# Build
cd core && mkdir build && cd build
cmake .. && make -j$(nproc)

# Test CLI
./vibechek-cli analyze /path/to/track.mp3
```

### Full Tauri App

```bash
# After C++ core is built
npm run tauri dev
```

### Windows (vcpkg)

```powershell
vcpkg install essentia chromaprint taglib ffmpeg nlohmann-json
cmake .. -DCMAKE_TOOLCHAIN_FILE=C:/vcpkg/scripts/buildsystems/vcpkg.cmake
```

---

## ML Models

Download from https://essentia.upf.edu/models.html (~800MB):

```
models/
├── discogs-effnet-bs64-1.pb           # Embedding extractor
├── genre_discogs400-discogs-effnet-1.pb
├── danceability-discogs-effnet-1.pb
├── mood_happy-discogs-effnet-1.pb
├── mood_aggressive-discogs-effnet-1.pb
├── mood_relaxed-discogs-effnet-1.pb
├── voice_instrumental-discogs-effnet-1.pb
└── gender-discogs-effnet-1.pb
```

---

## Key Design Decisions

1. **Tag Preservation:** Never overwrites Rekordbox GEOB frames containing cue points/beat grids
2. **Confidence Thresholds:** 85% default for genre acceptance
3. **Subgenres Over Genres:** More useful for DJ filtering (e.g., "Tech House" vs "House")
4. **Backup Before Write:** Always creates backups with base64-encoded binary data
5. **Camelot System:** Full bidirectional key↔Camelot↔OpenKey conversion
6. **Virtual Scrolling:** Required for 10k+ track libraries
7. **Async-First:** All analysis operations return futures, support cancellation

---

## Current State

### ✅ Complete
- C++ core library with all headers and implementations
- CMake build system (static library builds successfully)
- FFI C interface for Tauri integration
- Rust FFI bindings with safe wrappers
- Tauri shell with mock IPC handlers
- React frontend with all major components
- Zustand state management
- Tailwind theme with DJ aesthetic
- Virtual scrolling for large libraries

### 🔄 In Progress / Next Steps
1. **Test C++ core** on Windows with real audio files
2. **Download ML models** and verify Essentia integration
3. **Connect Tauri to C++ core** (replace mocks with real calls)
4. **Tag writing workflow** - diff preview → selective apply
5. **Duplicate detection UI** - show groups, select keeper
6. **Export functionality** - Rekordbox XML generation
7. **Error handling** - graceful failures, retry logic

---

## File Locations for Key Code

| Purpose | File |
|---------|------|
| C++ types/structs | `/home/claude/vibechek/core/include/vibechek/types.h` |
| C++ main API | `/home/claude/vibechek/core/include/vibechek/analyzer.h` |
| C++ ML wrapper | `/home/claude/vibechek/core/src/essentia_engine.cpp` |
| C++ FFI | `/home/claude/vibechek/core/src/ffi.cpp` |
| Rust FFI | `/home/claude/vibechek/tauri-bindings/src/lib.rs` |
| Tauri IPC | `/home/claude/vibechek/src-tauri/src/main.rs` |
| React app | `/home/claude/vibechek/src/App.tsx` |
| Track list | `/home/claude/vibechek/src/components/LibraryBrowser.tsx` |
| State stores | `/home/claude/vibechek/src/stores/index.ts` |
| Tauri hooks | `/home/claude/vibechek/src/hooks/useTauri.ts` |
| Types | `/home/claude/vibechek/src/types/index.ts` |
| Theme | `/home/claude/vibechek/tailwind.config.js` |

---

## Dependencies

### C++ (via apt/brew/vcpkg)
- essentia (2.1+)
- chromaprint (1.5+)
- taglib (1.12+)
- ffmpeg (5.0+) - libavcodec, libavformat, libswresample
- nlohmann-json (auto-fetched by CMake)

### Node (package.json)
- react, react-dom (18.2)
- @tauri-apps/api (1.5)
- zustand (4.5)
- framer-motion (11.0)
- react-virtuoso (4.6)
- lucide-react (0.312)
- clsx (2.1)
- tailwindcss (3.4)
- typescript (5.3)
- vite (5.0)

### Rust (Cargo.toml)
- tauri (1.6)
- serde (1.0)
- tokio (1.35)
- uuid (1.6)
- walkdir (2.4)

---

## Transcript Location

Full conversation history available at:
`/mnt/transcripts/2026-01-30-22-43-35-vibechek-cpp-core-prototype.txt`

Previous sessions covered Python analysis scripts for the 12,400 track library.

---

## Quick Resume Commands

```bash
# Check project structure
ls -la /home/claude/vibechek/

# View C++ headers
cat /home/claude/vibechek/core/include/vibechek/analyzer.h

# View React components
ls /home/claude/vibechek/src/components/

# View Tauri commands
cat /home/claude/vibechek/src-tauri/src/main.rs

# Start frontend dev
cd /home/claude/vibechek && npm install && npm run dev
```
