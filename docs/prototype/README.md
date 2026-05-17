# Vibechek Core

C++ audio analysis library for DJ track management. Provides ML-based genre/mood/energy classification, BPM/key detection, and audio fingerprinting for duplicate detection.

## Features

- **ML Genre Classification**: Detects genre using Essentia's Discogs400 model
- **BPM Detection**: Accurate tempo detection with confidence scores
- **Key Detection**: Musical key with Camelot wheel notation
- **Energy Analysis**: 1-10 energy level for DJ mixing
- **Mood Classification**: Happy, sad, energetic, chill, dark, etc.
- **Timeslot Recommendation**: Opener, peak time, late night, closing
- **Vocal Detection**: Instrumental, vocal chops, male/female lead
- **Audio Fingerprinting**: Chromaprint-based duplicate detection
- **Tag Read/Write**: TagLib integration for MP3, FLAC, M4A
- **Export**: JSON, Rekordbox XML, CSV, M3U

## Dependencies

- **Essentia** (2.1+): ML audio analysis
- **Chromaprint** (1.5+): Audio fingerprinting
- **TagLib** (1.12+): Audio metadata
- **FFmpeg** (5.0+): Audio decoding (libavcodec, libavformat, libswresample)
- **nlohmann_json** (3.9+): JSON serialization (fetched automatically)

## Building

### Linux (Ubuntu/Debian)

```bash
# Install dependencies
sudo apt update
sudo apt install -y \
    build-essential cmake pkg-config \
    libessentia-dev \
    libchromaprint-dev \
    libtag1-dev \
    libavcodec-dev libavformat-dev libavutil-dev libswresample-dev

# Clone and build
git clone https://github.com/yourusername/vibechek.git
cd vibechek/core
mkdir build && cd build
cmake ..
make -j$(nproc)

# Run tests
./vibechek-cli --help
```

### macOS

```bash
# Install dependencies via Homebrew
brew install cmake essentia chromaprint taglib ffmpeg nlohmann-json

# Build
cd vibechek/core
mkdir build && cd build
cmake ..
make -j$(sysctl -n hw.ncpu)
```

### Windows (vcpkg)

```powershell
# Install vcpkg if not already installed
git clone https://github.com/Microsoft/vcpkg.git
cd vcpkg
.\bootstrap-vcpkg.bat
.\vcpkg integrate install

# Install dependencies
.\vcpkg install essentia:x64-windows chromaprint:x64-windows taglib:x64-windows ffmpeg:x64-windows nlohmann-json:x64-windows

# Build
cd vibechek\core
mkdir build && cd build
cmake .. -DCMAKE_TOOLCHAIN_FILE=C:/path/to/vcpkg/scripts/buildsystems/vcpkg.cmake
cmake --build . --config Release
```

## ML Models

Vibechek uses Essentia's pre-trained TensorFlow models. Download from:
https://essentia.upf.edu/models.html

Required models (~800MB total):
- `discogs-effnet-bs64-1.pb` (embedding extractor)
- `genre_discogs400-discogs-effnet-1.pb` (genre classification)
- `danceability-discogs-effnet-1.pb` (energy/danceability)
- `mood_happy-discogs-effnet-1.pb`
- `mood_aggressive-discogs-effnet-1.pb`
- `mood_relaxed-discogs-effnet-1.pb`
- `voice_instrumental-discogs-effnet-1.pb`
- `gender-discogs-effnet-1.pb`

Place models in `./models/` directory or specify path via config.

## CLI Usage

```bash
# Analyze a single track
./vibechek-cli analyze track.mp3

# Analyze a directory
./vibechek-cli analyze /path/to/music -r -o analysis.json

# Find duplicates
./vibechek-cli duplicates /path/to/music

# Generate fingerprint
./vibechek-cli fingerprint track.mp3

# Export to Rekordbox
./vibechek-cli analyze /path/to/music -o rekordbox.xml
```

### Options

| Option | Description |
|--------|-------------|
| `-o, --output <file>` | Output file (json, xml, csv) |
| `-t, --threads <n>` | Number of worker threads |
| `-m, --models <dir>` | Model directory path |
| `-r, --recursive` | Scan subdirectories |
| `--no-fingerprint` | Skip audio fingerprinting |
| `--no-genre` | Skip genre detection |
| `--no-bpm` | Skip BPM detection |
| `-v, --verbose` | Verbose output |

## C++ API

```cpp
#include <vibechek/analyzer.h>

using namespace vibechek;

// Configure analyzer
AnalyzerConfig config;
config.model_directory = "./models";
config.num_threads = 4;

// Create and initialize
Analyzer analyzer(config);
analyzer.initialize();

// Analyze single track
auto result = analyzer.analyze_track("/path/to/track.mp3");
if (result) {
    std::cout << "Genre: " << result->genres[0].genre << "\n";
    std::cout << "BPM: " << result->bpm.bpm << "\n";
    std::cout << "Key: " << result->key.camelot << "\n";
    std::cout << "Energy: " << result->energy.level << "/10\n";
}

// Batch analysis with progress
auto results = analyzer.analyze_directory("/path/to/music",
    [](const AnalysisProgress& p) {
        std::cout << p.percent_complete() << "% complete\n";
    },
    [](const std::string& path, const std::string& err) {
        std::cerr << "Error: " << path << ": " << err << "\n";
    }
);

// Export to Rekordbox
analyzer.export_to_rekordbox_xml(results, "library.xml");

// Find duplicates
auto duplicates = analyzer.find_duplicates(results);
for (const auto& group : duplicates) {
    std::cout << "Duplicate: " << group.file_paths[0] 
              << " <-> " << group.file_paths[1] << "\n";
}
```

## FFI (C Interface)

For Tauri/Rust integration:

```c
#include <vibechek_ffi.h>

// Create analyzer
VibechekConfig config = {
    .model_directory = "./models",
    .num_threads = 4,
    .detect_genre = 1,
    .detect_bpm = 1,
    // ...
};

VibechekAnalyzer analyzer = vibechek_analyzer_create(&config);
vibechek_analyzer_initialize(analyzer);

// Analyze track
VibechekTrackResult result;
if (vibechek_analyze_track(analyzer, "/path/to/track.mp3", &result)) {
    printf("Genre: %s (%.2f)\n", result.genres[0].genre, result.genres[0].confidence);
    printf("BPM: %.1f\n", result.bpm.bpm);
    printf("Key: %s\n", result.key.camelot);
}

// Cleanup
vibechek_analyzer_destroy(analyzer);
```

## Project Structure

```
core/
├── CMakeLists.txt
├── include/vibechek/
│   ├── types.h          # Data structures
│   ├── analyzer.h       # Main API
│   └── fingerprint.h    # Fingerprinting
├── src/
│   ├── analyzer.cpp     # Main analyzer implementation
│   ├── essentia_engine.cpp  # ML model wrapper
│   ├── fingerprint.cpp  # Chromaprint wrapper
│   ├── tag_reader.cpp   # TagLib reading
│   ├── tag_writer.cpp   # TagLib writing
│   ├── export.cpp       # JSON/XML/CSV export
│   ├── utils.cpp        # Utilities
│   ├── ffi.cpp          # C interface for Tauri
│   ├── types.cpp        # Type implementations
│   └── main.cpp         # CLI
└── tests/               # Unit tests
```

## Performance

On a modern CPU (i9-13900H):
- Single track analysis: ~200-500ms
- Full library (10,000 tracks): ~1-1.5 hours single-threaded
- With 8 threads: ~15-20 minutes

## License

MIT License - see LICENSE file.

## Acknowledgments

- [Essentia](https://essentia.upf.edu/) - Audio analysis algorithms
- [Chromaprint](https://acoustid.org/chromaprint) - Audio fingerprinting
- [TagLib](https://taglib.org/) - Audio metadata
