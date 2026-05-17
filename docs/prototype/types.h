#pragma once

#include <string>
#include <vector>
#include <optional>
#include <map>
#include <chrono>
#include <functional>

namespace vibechek {

// ============================================================================
// Audio File Metadata
// ============================================================================

struct AudioMetadata {
    std::string path;
    std::string filename;
    std::string format;          // mp3, flac, m4a, wav, etc.
    
    // Existing tags (read from file)
    std::optional<std::string> title;
    std::optional<std::string> artist;
    std::optional<std::string> album;
    std::optional<std::string> genre;
    std::optional<float> bpm;
    std::optional<std::string> key;
    std::optional<int> year;
    
    // File info
    size_t file_size = 0;
    double duration_seconds = 0.0;
    int sample_rate = 0;
    int channels = 0;
    int bitrate = 0;
};

// ============================================================================
// ML Analysis Results
// ============================================================================

struct GenreResult {
    std::string genre;
    float confidence;           // 0.0 - 1.0
};

struct KeyResult {
    std::string key;            // e.g., "Am", "C#m", "F"
    std::string camelot;        // e.g., "8A", "5B"
    std::string open_key;       // e.g., "1m", "6d"
    float confidence;
};

struct BpmResult {
    float bpm;
    float confidence;
    std::vector<float> bpm_candidates;  // Alternative tempos
};

// Energy level 1-10 scale
struct EnergyResult {
    int level;                  // 1-10
    float raw_value;            // 0.0 - 1.0 from model
    float confidence;
};

// Mood/vibe classification
enum class Mood {
    Unknown,
    Happy,
    Sad,
    Energetic,
    Chill,
    Dark,
    Uplifting,
    Aggressive,
    Melancholic
};

struct MoodResult {
    Mood primary_mood;
    float confidence;
    std::map<Mood, float> all_moods;  // All mood scores
};

// DJ timeslot recommendation
enum class Timeslot {
    Unknown,
    Opener,         // Low energy, warm-up
    EarlyNight,     // Building energy
    PeakTime,       // High energy bangers
    LateNight,      // Deeper, hypnotic
    Closing         // Winding down
};

struct TimeslotResult {
    Timeslot recommended;
    float confidence;
};

// Vocal presence detection
enum class VocalType {
    Unknown,
    Instrumental,
    VocalChops,     // Chopped/processed vocals
    FemaleLead,
    MaleLead,
    MixedVocals
};

struct VocalResult {
    VocalType type;
    float vocal_presence;       // 0.0 (instrumental) - 1.0 (full vocals)
    float confidence;
};

// ============================================================================
// Complete Track Analysis
// ============================================================================

struct TrackAnalysis {
    AudioMetadata metadata;
    
    // ML-detected values
    std::vector<GenreResult> genres;        // Top N genres with confidence
    BpmResult bpm;
    KeyResult key;
    EnergyResult energy;
    MoodResult mood;
    TimeslotResult timeslot;
    VocalResult vocals;
    
    // Audio fingerprint for duplicate detection
    std::string fingerprint;
    uint32_t fingerprint_hash;
    
    // Analysis metadata
    std::chrono::system_clock::time_point analyzed_at;
    std::string analyzer_version;
    double analysis_duration_ms;
    
    // Comparison helpers
    bool has_genre_change() const;
    bool has_bpm_change() const;
    bool has_key_change() const;
    bool has_any_change() const;
};

// ============================================================================
// Duplicate Detection
// ============================================================================

struct DuplicateGroup {
    std::vector<std::string> file_paths;
    std::string fingerprint;
    
    // The "best" file to keep (highest quality, best tags, etc.)
    std::string recommended_keep;
    std::vector<std::string> recommended_remove;
    
    enum class Reason {
        ExactHash,          // Byte-for-byte identical
        AudioFingerprint,   // Same audio content
        SimilarFingerprint  // Very similar (potential remix/edit)
    };
    Reason reason;
    float similarity;       // 0.0 - 1.0 for fingerprint matches
};

// ============================================================================
// Progress Reporting
// ============================================================================

struct AnalysisProgress {
    size_t total_files;
    size_t processed_files;
    size_t skipped_files;
    size_t error_files;
    
    std::string current_file;
    std::string current_phase;  // "scanning", "fingerprinting", "analyzing", "writing"
    
    double elapsed_seconds;
    double estimated_remaining_seconds;
    
    float percent_complete() const {
        return total_files > 0 ? 
            (static_cast<float>(processed_files) / total_files * 100.0f) : 0.0f;
    }
};

// Callback types
using ProgressCallback = std::function<void(const AnalysisProgress&)>;
using ErrorCallback = std::function<void(const std::string& path, const std::string& error)>;

// ============================================================================
// Configuration
// ============================================================================

struct AnalyzerConfig {
    // Paths
    std::string model_directory = "./models";
    
    // Processing options
    int num_threads = 4;
    bool extract_fingerprint = true;
    bool detect_genre = true;
    bool detect_bpm = true;
    bool detect_key = true;
    bool detect_energy = true;
    bool detect_mood = true;
    bool detect_timeslot = true;
    bool detect_vocals = true;
    
    // Thresholds
    float genre_confidence_threshold = 0.5f;
    float duplicate_similarity_threshold = 0.85f;
    
    // Tag writing options
    bool backup_before_write = true;
    bool write_to_file_tags = true;
    bool write_to_sidecar_json = false;
    
    // Genre mapping (ML genre -> user-preferred genre name)
    std::map<std::string, std::string> genre_mapping;
};

// ============================================================================
// Utility functions
// ============================================================================

std::string mood_to_string(Mood mood);
Mood string_to_mood(const std::string& str);

std::string timeslot_to_string(Timeslot slot);
Timeslot string_to_timeslot(const std::string& str);

std::string vocal_type_to_string(VocalType type);
VocalType string_to_vocal_type(const std::string& str);

// Camelot wheel conversions
std::string key_to_camelot(const std::string& key);
std::string key_to_open_key(const std::string& key);
std::string camelot_to_key(const std::string& camelot);

} // namespace vibechek
