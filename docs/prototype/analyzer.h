#pragma once

#include "types.h"
#include <memory>
#include <vector>
#include <string>
#include <future>
#include <atomic>

namespace vibechek {

// Forward declarations
class EssentiaEngine;
class FingerprintEngine;
class TagReader;
class TagWriter;

/**
 * @brief Main analyzer class for DJ track analysis
 * 
 * Thread-safe. Supports async operations with progress reporting.
 * 
 * Usage:
 *   Analyzer analyzer(config);
 *   analyzer.initialize();  // Loads ML models
 *   
 *   // Single track
 *   auto result = analyzer.analyze_track("/path/to/track.mp3");
 *   
 *   // Batch with progress
 *   analyzer.analyze_directory("/path/to/music", 
 *       [](const AnalysisProgress& p) { update_ui(p); },
 *       [](const string& path, const string& err) { log_error(path, err); }
 *   );
 */
class Analyzer {
public:
    explicit Analyzer(const AnalyzerConfig& config = AnalyzerConfig{});
    ~Analyzer();
    
    // Non-copyable, movable
    Analyzer(const Analyzer&) = delete;
    Analyzer& operator=(const Analyzer&) = delete;
    Analyzer(Analyzer&&) noexcept;
    Analyzer& operator=(Analyzer&&) noexcept;
    
    // ========================================================================
    // Initialization
    // ========================================================================
    
    /**
     * @brief Initialize the analyzer and load ML models
     * @return true if successful, false if models couldn't be loaded
     * 
     * This may take several seconds on first run as models are loaded.
     * Call once before any analysis operations.
     */
    bool initialize();
    
    /**
     * @brief Check if analyzer is initialized and ready
     */
    bool is_initialized() const;
    
    /**
     * @brief Download required ML models if not present
     * @param progress_callback Called with download progress
     * @return true if all models are available
     */
    bool ensure_models(ProgressCallback progress_callback = nullptr);
    
    // ========================================================================
    // Single Track Analysis
    // ========================================================================
    
    /**
     * @brief Analyze a single audio file
     * @param file_path Path to audio file
     * @return Analysis results, or nullopt if analysis failed
     */
    std::optional<TrackAnalysis> analyze_track(const std::string& file_path);
    
    /**
     * @brief Read metadata only (no ML analysis)
     * @param file_path Path to audio file
     * @return Metadata, or nullopt if file couldn't be read
     */
    std::optional<AudioMetadata> read_metadata(const std::string& file_path);
    
    /**
     * @brief Generate audio fingerprint only
     * @param file_path Path to audio file
     * @return Fingerprint string, or empty if failed
     */
    std::string generate_fingerprint(const std::string& file_path);
    
    // ========================================================================
    // Batch Analysis
    // ========================================================================
    
    /**
     * @brief Scan a directory for audio files
     * @param directory_path Path to directory
     * @param recursive Search subdirectories
     * @return List of audio file paths
     */
    std::vector<std::string> scan_directory(
        const std::string& directory_path, 
        bool recursive = true
    );
    
    /**
     * @brief Analyze all audio files in a directory
     * @param directory_path Path to directory
     * @param progress_callback Called after each file
     * @param error_callback Called on errors
     * @param recursive Search subdirectories
     * @return Vector of analysis results
     */
    std::vector<TrackAnalysis> analyze_directory(
        const std::string& directory_path,
        ProgressCallback progress_callback = nullptr,
        ErrorCallback error_callback = nullptr,
        bool recursive = true
    );
    
    /**
     * @brief Analyze specific files
     * @param file_paths List of file paths
     * @param progress_callback Called after each file
     * @param error_callback Called on errors
     * @return Vector of analysis results
     */
    std::vector<TrackAnalysis> analyze_files(
        const std::vector<std::string>& file_paths,
        ProgressCallback progress_callback = nullptr,
        ErrorCallback error_callback = nullptr
    );
    
    // ========================================================================
    // Async Operations
    // ========================================================================
    
    /**
     * @brief Start async analysis of a directory
     * @return Future that resolves to analysis results
     */
    std::future<std::vector<TrackAnalysis>> analyze_directory_async(
        const std::string& directory_path,
        ProgressCallback progress_callback = nullptr,
        ErrorCallback error_callback = nullptr,
        bool recursive = true
    );
    
    /**
     * @brief Cancel ongoing async operation
     */
    void cancel();
    
    /**
     * @brief Check if a cancel has been requested
     */
    bool is_cancelled() const;
    
    // ========================================================================
    // Duplicate Detection
    // ========================================================================
    
    /**
     * @brief Find duplicate tracks by audio fingerprint
     * @param analyses Previously analyzed tracks
     * @return Groups of duplicate tracks
     */
    std::vector<DuplicateGroup> find_duplicates(
        const std::vector<TrackAnalysis>& analyses
    );
    
    /**
     * @brief Find duplicates in a directory
     * @param directory_path Path to directory
     * @param progress_callback Called with progress
     * @return Groups of duplicate tracks
     */
    std::vector<DuplicateGroup> find_duplicates_in_directory(
        const std::string& directory_path,
        ProgressCallback progress_callback = nullptr
    );
    
    // ========================================================================
    // Tag Writing
    // ========================================================================
    
    /**
     * @brief Write analysis results to file tags
     * @param analysis Analysis with new values
     * @param fields Which fields to write (empty = all)
     * @return true if successful
     */
    bool write_tags(
        const TrackAnalysis& analysis,
        const std::vector<std::string>& fields = {}
    );
    
    /**
     * @brief Batch write tags
     * @param analyses Analyses to write
     * @param progress_callback Called after each file
     * @return Number of files successfully updated
     */
    size_t write_tags_batch(
        const std::vector<TrackAnalysis>& analyses,
        ProgressCallback progress_callback = nullptr
    );
    
    // ========================================================================
    // Export
    // ========================================================================
    
    /**
     * @brief Export analysis to JSON
     * @param analyses Analyses to export
     * @param output_path Output file path
     * @return true if successful
     */
    bool export_to_json(
        const std::vector<TrackAnalysis>& analyses,
        const std::string& output_path
    );
    
    /**
     * @brief Export to Rekordbox XML
     * @param analyses Analyses to export
     * @param output_path Output file path
     * @return true if successful
     */
    bool export_to_rekordbox_xml(
        const std::vector<TrackAnalysis>& analyses,
        const std::string& output_path
    );
    
    /**
     * @brief Export to CSV
     * @param analyses Analyses to export
     * @param output_path Output file path
     * @return true if successful
     */
    bool export_to_csv(
        const std::vector<TrackAnalysis>& analyses,
        const std::string& output_path
    );
    
    // ========================================================================
    // Configuration
    // ========================================================================
    
    /**
     * @brief Get current configuration
     */
    const AnalyzerConfig& config() const;
    
    /**
     * @brief Update configuration
     * @note Some changes require re-initialization
     */
    void set_config(const AnalyzerConfig& config);
    
    /**
     * @brief Get analyzer version string
     */
    static std::string version();

private:
    struct Impl;
    std::unique_ptr<Impl> m_impl;
    
    std::atomic<bool> m_cancelled{false};
    AnalyzerConfig m_config;
    bool m_initialized{false};
};

} // namespace vibechek
