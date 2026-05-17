# Vibechek - DJ Library ML Analysis Project Summary

## What We Built
An ML-powered DJ library organizer that analyzes audio files using Essentia ML models and automatically classifies genre, subgenre, energy, mood, timeslot, direction, and vocal content.

## What Was Accomplished

### 1. Duplicate Removal
- Scanned 12,674 files using MD5 hash + Chromaprint audio fingerprinting
- Found and removed 202 duplicates (2.64 GB recovered)
- Moved duplicates to `/mnt/d/Music/Duplicates` for review
- Final library: ~12,466 unique tracks

### 2. ML Analysis
- Analyzed all 12,466 tracks using Essentia-TensorFlow ML models
- Models detect: genre (400 classes), subgenre, BPM, key, energy (0-5), mood (Dark/Neutral/Bright), timeslot (Opener/Warm-Up/Peak/Afterhours), direction (Up/Steady/Down), vocal (Instrumental/Light Vocal/Vocal)
- Analysis was parallelized across 4 separate processes (--skip/--limit flags) to work around Python's GIL
- Results stored in analysis_1.json through analysis_8.json, merged into analysis.json
- One corrupted file removed during analysis: "Samim - Heater (Tube & Berger Remix).mp3"

### 3. Tag Application
- Backed up all existing tags (including Rekordbox GEOB/PRIV frames) to tags_backup.json (~2GB due to base64-encoded binary data)
- Applied ML tags with confidence filtering:
  - Genre/Subgenre: only applied when confidence >= 85% (~6,623 of 12,466 tracks)
  - Subgenre is written as the main genre tag (TCON) so Rekordbox can filter by it
  - Energy, Mood, Timeslot, Direction, Vocal: always applied
  - BPM and Key: SKIPPED (Rekordbox detection is more reliable)
- All tag writes explicitly preserve Rekordbox GEOB and PRIV frames

### 4. Folder Organization
- Organized tracks into genre/subgenre folder structure under D:\Music\Tracks\
- Genres with <10 tracks consolidated into Other/GenreName/ folders
- Structure: Tracks/House/Deep House/, Tracks/Techno/Minimal Techno/, Tracks/Other/Vaporwave/, etc.
- Rekordbox files relinked via Relocate → "Search in subfolders" pointing to D:\Music\Tracks

### 5. New Tracks Workflow
- Created script to copy manually-tagged new tracks from "New Tracks" folder to appropriate genre folders in "Tracks"
- Supports MP3, FLAC, M4A, AIFF/AIF formats

### 6. Flat Backup
- Copied all tracks (flat structure) to E:\Tracks as backup

## Library Stats
- **Total tracks**: ~12,466
- **Top genres**: House (48.6%), Trance (15.7%), Techno (9.7%), Hip Hop (5.8%), Dubstep (3.6%)
- **Energy distribution**: Mostly levels 2-3 (80%)
- **Mood**: 50% Bright, 47% Neutral, 3% Dark
- **Timeslots**: 47% Opener, 42% Warm-Up, 10% Peak

## File Locations

### Scripts (all on D:\Music\)
| Script | Purpose |
|--------|---------|
| `analyze_dj_tracks_v2.py` | Main ML analysis script. Supports --workers, --skip, --limit, --apply |
| `backup_tags.py` | Backup/restore all tags including GEOB frames. Commands: backup, restore |
| `apply_tags_filtered.py` | Apply ML tags with confidence threshold. Flags: --confidence, --skip-bpm-key, --genre-only |
| `organize_by_genre.py` | Move files into genre/subgenre folders. Flags: --no-subgenres, --min-genre-size |
| `copy_to_genre_folders.py` | Copy new tracks to genre folders based on existing tags |
| `find_duplicates.py` | Find exact + audio duplicates via MD5 + Chromaprint |
| `move_safe_duplicates.py` | Move identified duplicates to review folder |

### Data Files (all on D:\Music\)
| File | Purpose |
|------|---------|
| `analysis.json` | Merged ML analysis results for all tracks |
| `analysis_1.json` - `analysis_8.json` | Individual analysis chunks |
| `tags_backup.json` | Pre-ML tag backup (~2GB, includes GEOB/PRIV frames) |
| `safe_duplicates.json` | 193 safe-to-remove duplicates |
| `suspect_duplicates.json` | 9 version-different duplicates |

### Directories
| Directory | Contents |
|-----------|----------|
| `D:\Music\Tracks\` | Main library, organized in genre/subgenre folders |
| `D:\Music\New Tracks\` | Manually tagged new tracks (staging area) |
| `D:\Music\Duplicates\` | Removed duplicates (can be deleted) |
| `E:\Tracks\` | Flat backup of all tracks |

## Environment Setup
- **WSL Ubuntu 24** running on Windows (hostname: JACK-PC4)
- **Python venv**: `/root/djenv/` (activate with `source /root/djenv/bin/activate`)
- **Key packages**: essentia-tensorflow, mutagen, chromaprint (fpcalc)
- **ML models**: ~/essentia_models/ (downloaded automatically on first run)
- **Hardware**: i9-13900H, RTX 4070 Laptop (GPU not used - CUDA 11/12 incompatibility with Essentia's bundled TensorFlow)
- **Music accessed via**: /mnt/d/Music/ (Windows D:\ drive)

## Key Technical Decisions
1. **CPU-only analysis**: GPU acceleration abandoned due to CUDA version mismatch with Essentia's bundled TensorFlow
2. **Multi-process parallelism**: Python GIL prevents true threading parallelism for CPU-bound ML inference. Solution: 4 separate processes via --skip/--limit flags (~35 tracks/min vs 10 tracks/min single-process)
3. **Confidence threshold at 85%**: Only ~53% of tracks get ML genre tags applied; rest keep original tags
4. **BPM/Key preserved**: Rekordbox's own detection is more reliable than ML models
5. **Subgenre as main genre**: Rekordbox can only sort/filter by the main genre field, so subgenres (e.g., "Deep House" not "House") are written to TCON
6. **GEOB/PRIV preservation**: All tag writes explicitly capture and restore Rekordbox binary frames

## Future Plans: Vibechek App
- **Stack**: Tauri + React + Python sidecar
- **Features**: ML genre/subgenre detection, energy/mood/timeslot analysis, smart organization, Rekordbox/Serato integration
- **Estimated effort**: 10-14 weeks solo dev
- **Monetization**: $29-49 one-time or $5-10/mo subscription
- **Key challenge**: Bundling ~800MB Essentia ML models
