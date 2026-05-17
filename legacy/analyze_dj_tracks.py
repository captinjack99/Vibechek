#!/usr/bin/env python3
"""
DJ Track Collection Analyzer
Run this script locally on your Windows machine.

Requirements:
    pip install mutagen

For audio fingerprinting (optional but recommended for accurate duplicate detection):
    1. Download fpcalc from: https://acoustid.org/chromaprint
    2. Extract and add to PATH, or place fpcalc.exe in the same folder as this script

Usage:
    python analyze_dj_tracks.py "D:\Music\Tracks" --output report.json
"""

import os
import sys
import json
import subprocess
import hashlib
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import re

try:
    from mutagen import File as MutagenFile
    from mutagen.easyid3 import EasyID3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False
    print("Warning: mutagen not installed. Run: pip install mutagen", file=sys.stderr)

SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.aiff', '.ogg', '.aac', '.wma'}


def find_fpcalc():
    """Find fpcalc executable."""
    # Check if in PATH
    for cmd in ['fpcalc', 'fpcalc.exe']:
        try:
            result = subprocess.run([cmd, '-version'], capture_output=True, timeout=5)
            if result.returncode == 0:
                return cmd
        except:
            pass
    
    # Check current directory
    script_dir = Path(__file__).parent
    for name in ['fpcalc.exe', 'fpcalc']:
        fpcalc_path = script_dir / name
        if fpcalc_path.exists():
            return str(fpcalc_path)
    
    return None


def extract_from_filename(filename):
    """Extract BPM, key, and mix type from filename patterns."""
    info = {'filename_bpm': None, 'filename_key': None, 'filename_mix': None}
    
    # BPM patterns: trailing numbers like "Artist - Title 128.mp3"
    bpm_match = re.search(r'[\s_\-](\d{2,3})(?:\s*bpm)?(?:\.[a-z0-9]+)?$', filename, re.I)
    if bpm_match:
        bpm = int(bpm_match.group(1))
        if 60 <= bpm <= 200:
            info['filename_bpm'] = bpm
    
    # Camelot key patterns: "1A", "12B"
    key_match = re.search(r'[\s_\-\[\(]([1-9]|1[0-2])[AB][\s_\-\]\)\.]', filename, re.I)
    if key_match:
        info['filename_key'] = key_match.group(0).strip(' _-[]().').upper()
    
    # Standard key patterns: "Am", "F#m", "Bb"
    std_key_match = re.search(r'[\s_\-\[\(]([A-G][#b]?m?)[\s_\-\]\)]', filename)
    if std_key_match and not info['filename_key']:
        info['filename_key'] = std_key_match.group(1)
    
    # Mix type patterns
    mix_patterns = [
        (r'[\(\[]?\s*original\s*mix\s*[\)\]]?', 'Original Mix'),
        (r'[\(\[]?\s*extended\s*mix\s*[\)\]]?', 'Extended Mix'),
        (r'[\(\[]?\s*radio\s*edit\s*[\)\]]?', 'Radio Edit'),
        (r'[\(\[]?\s*club\s*mix\s*[\)\]]?', 'Club Mix'),
        (r'[\(\[]?\s*dub\s*mix\s*[\)\]]?', 'Dub Mix'),
        (r'[\(\[]?\s*vip\s*mix\s*[\)\]]?', 'VIP Mix'),
        (r'[\(\[]?\s*instrumental\s*(mix)?\s*[\)\]]?', 'Instrumental'),
        (r'[\(\[]?\s*acapella\s*[\)\]]?', 'Acapella'),
        (r'[\(\[]?\s*bootleg\s*[\)\]]?', 'Bootleg'),
        (r'\s*-\s*[^-]+\s+remix', 'Remix'),
        (r'[\(\[]\s*[^)\]]+\s+remix\s*[\)\]]', 'Remix'),
    ]
    for pattern, mix_type in mix_patterns:
        if re.search(pattern, filename, re.I):
            info['filename_mix'] = mix_type
            break
    
    # Try to extract artist and title from filename
    # Common pattern: "Artist - Title (Mix).ext"
    name_without_ext = Path(filename).stem
    
    # Remove leading track numbers like "001 - " or "01. "
    name_clean = re.sub(r'^[\d]+[\s\-\.]+', '', name_without_ext)
    
    # Try to split on " - "
    if ' - ' in name_clean:
        parts = name_clean.split(' - ', 1)
        info['filename_artist'] = parts[0].strip()
        # Remove mix info from title for cleaner matching
        title = parts[1] if len(parts) > 1 else ''
        title = re.sub(r'\s*[\(\[].*?(?:mix|edit|remix|version).*?[\)\]]', '', title, flags=re.I)
        title = re.sub(r'\s+\d{2,3}$', '', title)  # Remove trailing BPM
        info['filename_title'] = title.strip()
    
    return info


def get_metadata(filepath):
    """Extract metadata from audio file."""
    metadata = {
        'path': str(filepath),
        'filename': filepath.name,
        'extension': filepath.suffix.lower(),
        'size_mb': round(filepath.stat().st_size / (1024 * 1024), 2),
        'artist': None,
        'title': None,
        'album': None,
        'genre': None,
        'bpm': None,
        'key': None,
        'year': None,
        'label': None,
    }
    
    # Extract from filename first
    fn_info = extract_from_filename(filepath.name)
    metadata['filename_artist'] = fn_info.get('filename_artist')
    metadata['filename_title'] = fn_info.get('filename_title')
    metadata['filename_bpm'] = fn_info.get('filename_bpm')
    metadata['filename_key'] = fn_info.get('filename_key')
    metadata['filename_mix'] = fn_info.get('filename_mix')
    
    if not MUTAGEN_AVAILABLE:
        return metadata
    
    try:
        audio = MutagenFile(filepath, easy=True)
        if audio is None:
            # Try non-easy mode for some formats
            audio = MutagenFile(filepath)
        
        if audio is None:
            return metadata
        
        tags = None
        if hasattr(audio, 'tags') and audio.tags:
            tags = audio.tags
        elif isinstance(audio, dict):
            tags = audio
        
        if tags:
            # Standard fields mapping
            field_mappings = {
                'artist': ['artist', 'albumartist', '\xa9ART', 'aART', 'TPE1', 'TPE2'],
                'title': ['title', '\xa9nam', 'TIT2'],
                'album': ['album', '\xa9alb', 'TALB'],
                'genre': ['genre', '\xa9gen', 'TCON'],
                'year': ['date', 'year', '\xa9day', 'TDRC', 'TYER'],
                'label': ['label', 'publisher', 'TPUB'],
            }
            
            for field, keys in field_mappings.items():
                for key in keys:
                    val = tags.get(key)
                    if val:
                        if isinstance(val, list):
                            metadata[field] = str(val[0])
                        else:
                            metadata[field] = str(val)
                        break
            
            # BPM - various tag names
            for key in ['bpm', 'TBPM', 'tmpo', '\xa9bpm', 'TXXX:BPM']:
                val = tags.get(key)
                if val:
                    try:
                        bpm_val = val[0] if isinstance(val, list) else val
                        metadata['bpm'] = int(float(str(bpm_val).split('/')[0]))
                    except (ValueError, TypeError):
                        pass
                    break
            
            # Initial key
            for key in ['initialkey', 'key', 'TKEY', 'TXXX:INITIAL KEY', 'TXXX:KEY']:
                val = tags.get(key)
                if val:
                    metadata['key'] = str(val[0]) if isinstance(val, list) else str(val)
                    break
                    
    except Exception as e:
        metadata['metadata_error'] = str(e)
    
    # Use filename values as fallback
    if not metadata['bpm'] and metadata.get('filename_bpm'):
        metadata['bpm'] = metadata['filename_bpm']
    if not metadata['key'] and metadata.get('filename_key'):
        metadata['key'] = metadata['filename_key']
    if not metadata['artist'] and metadata.get('filename_artist'):
        metadata['artist'] = metadata['filename_artist']
    if not metadata['title'] and metadata.get('filename_title'):
        metadata['title'] = metadata['filename_title']
    
    return metadata


def get_fingerprint(filepath, fpcalc_cmd):
    """Generate audio fingerprint using fpcalc."""
    if not fpcalc_cmd:
        return None
    
    try:
        result = subprocess.run(
            [fpcalc_cmd, '-raw', '-length', '120', str(filepath)],
            capture_output=True, text=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line.startswith('FINGERPRINT='):
                    fp = line.split('=', 1)[1]
                    # Return hash for easier comparison
                    return hashlib.md5(fp.encode()).hexdigest()
    except subprocess.TimeoutExpired:
        return 'TIMEOUT'
    except Exception as e:
        return None
    return None


def analyze_file(filepath, fpcalc_cmd, do_fingerprint=True):
    """Analyze a single file - metadata + optional fingerprint."""
    metadata = get_metadata(filepath)
    if do_fingerprint:
        metadata['fingerprint'] = get_fingerprint(filepath, fpcalc_cmd)
    else:
        metadata['fingerprint'] = None
    return metadata


def find_duplicates_by_fingerprint(results):
    """Group files by fingerprint to find duplicates."""
    fingerprints = defaultdict(list)
    for r in results:
        fp = r.get('fingerprint')
        if fp and fp != 'TIMEOUT':
            fingerprints[fp].append(r['path'])
    
    return {fp: paths for fp, paths in fingerprints.items() if len(paths) > 1}


def find_potential_duplicates_by_name(results):
    """Find potential duplicates by normalized artist+title (for review)."""
    def normalize(s):
        if not s:
            return ''
        s = s.lower()
        s = re.sub(r'[\(\[].*?[\)\]]', '', s)  # Remove parentheticals
        s = re.sub(r'[^a-z0-9]', '', s)  # Keep only alphanumeric
        return s
    
    by_name = defaultdict(list)
    for r in results:
        artist = r.get('artist') or r.get('filename_artist') or ''
        title = r.get('title') or r.get('filename_title') or ''
        if artist and title:
            key = f"{normalize(artist)}_{normalize(title)}"
            if key and len(key) > 3:
                by_name[key].append({
                    'path': r['path'],
                    'mix': r.get('filename_mix'),
                    'fingerprint': r.get('fingerprint')
                })
    
    # Only include groups with multiple files AND same fingerprint (true dupes)
    # or flag as "potential" if different fingerprints (different mixes)
    potential_dupes = {}
    for key, files in by_name.items():
        if len(files) > 1:
            fps = set(f['fingerprint'] for f in files if f['fingerprint'])
            if len(fps) == 1:
                # Same fingerprint = true duplicate
                potential_dupes[key] = {'status': 'TRUE_DUPLICATE', 'files': files}
            else:
                # Different fingerprints = likely different mixes (KEEP)
                potential_dupes[key] = {'status': 'DIFFERENT_MIXES', 'files': files}
    
    return potential_dupes


def main():
    parser = argparse.ArgumentParser(description='Analyze DJ track collection')
    parser.add_argument('directory', help='Path to music directory')
    parser.add_argument('--output', '-o', default='dj_tracks_analysis.json', help='Output JSON file')
    parser.add_argument('--no-fingerprint', action='store_true', help='Skip audio fingerprinting (faster)')
    parser.add_argument('--workers', type=int, default=4, help='Number of parallel workers')
    args = parser.parse_args()
    
    music_path = Path(args.directory)
    if not music_path.exists():
        print(f"Error: Directory not found: {music_path}", file=sys.stderr)
        sys.exit(1)
    
    # Find fpcalc
    fpcalc_cmd = None if args.no_fingerprint else find_fpcalc()
    if not args.no_fingerprint and not fpcalc_cmd:
        print("Warning: fpcalc not found. Fingerprinting disabled.", file=sys.stderr)
        print("Download from: https://acoustid.org/chromaprint", file=sys.stderr)
    
    # Find all audio files
    print(f"Scanning {music_path}...", file=sys.stderr)
    audio_files = []
    for ext in SUPPORTED_EXTENSIONS:
        audio_files.extend(music_path.rglob(f'*{ext}'))
        audio_files.extend(music_path.rglob(f'*{ext.upper()}'))
    
    audio_files = list(set(audio_files))
    total = len(audio_files)
    print(f"Found {total} audio files", file=sys.stderr)
    
    if total == 0:
        print("No audio files found!", file=sys.stderr)
        sys.exit(1)
    
    # Analyze files
    print(f"Analyzing... (fingerprinting: {'ON' if fpcalc_cmd else 'OFF'})", file=sys.stderr)
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(analyze_file, f, fpcalc_cmd, bool(fpcalc_cmd)): f 
            for f in audio_files
        }
        
        for future in as_completed(futures):
            completed += 1
            if completed % 100 == 0 or completed == total:
                pct = 100 * completed // total
                print(f"\rProgress: {completed}/{total} ({pct}%)", end='', file=sys.stderr)
            
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                filepath = futures[future]
                results.append({'path': str(filepath), 'error': str(e)})
    
    print(file=sys.stderr)  # Newline after progress
    
    # Find duplicates
    print("Finding duplicates...", file=sys.stderr)
    fp_duplicates = find_duplicates_by_fingerprint(results) if fpcalc_cmd else {}
    name_analysis = find_potential_duplicates_by_name(results)
    
    # Separate true duplicates from different mixes
    true_duplicates = {k: v for k, v in name_analysis.items() if v['status'] == 'TRUE_DUPLICATE'}
    different_mixes = {k: v for k, v in name_analysis.items() if v['status'] == 'DIFFERENT_MIXES'}
    
    # Genre/BPM statistics
    genres = defaultdict(int)
    bpm_ranges = defaultdict(int)
    keys = defaultdict(int)
    
    for r in results:
        g = r.get('genre') or 'Unknown'
        genres[g] += 1
        
        bpm = r.get('bpm')
        if bpm:
            range_start = (bpm // 5) * 5
            bpm_ranges[f"{range_start}-{range_start+4}"] += 1
        
        key = r.get('key')
        if key:
            keys[key] += 1
    
    # Build report
    report = {
        'summary': {
            'total_files': total,
            'files_analyzed': len(results),
            'files_with_fingerprints': sum(1 for r in results if r.get('fingerprint') and r['fingerprint'] != 'TIMEOUT'),
            'fingerprinting_enabled': bool(fpcalc_cmd),
            'true_duplicate_groups': len(true_duplicates),
            'true_duplicate_files': sum(len(v['files']) for v in true_duplicates.values()),
            'different_mix_groups': len(different_mixes),
            'space_recoverable_mb': sum(
                min(r['size_mb'] for r in results if r['path'] in paths) * (len(paths) - 1)
                for paths in fp_duplicates.values()
            ) if fp_duplicates else 0,
        },
        'genres': dict(sorted(genres.items(), key=lambda x: -x[1])[:30]),
        'bpm_distribution': dict(sorted(bpm_ranges.items(), key=lambda x: int(x[0].split('-')[0]))),
        'key_distribution': dict(sorted(keys.items(), key=lambda x: -x[1])),
        'true_duplicates': {
            k: [f['path'] for f in v['files']] 
            for k, v in list(true_duplicates.items())[:50]  # Limit output
        },
        'different_mixes_kept': {
            k: [{'path': f['path'], 'mix': f['mix']} for f in v['files']]
            for k, v in list(different_mixes.items())[:30]
        },
        'fingerprint_duplicates': dict(list(fp_duplicates.items())[:50]),
        'tracks': results,
    }
    
    # Save report
    output_path = Path(args.output)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"ANALYSIS COMPLETE", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Total files: {total}", file=sys.stderr)
    print(f"True duplicates found: {report['summary']['true_duplicate_groups']} groups ({report['summary']['true_duplicate_files']} files)", file=sys.stderr)
    print(f"Different mixes (kept): {report['summary']['different_mix_groups']} groups", file=sys.stderr)
    print(f"Space recoverable: ~{report['summary']['space_recoverable_mb']:.1f} MB", file=sys.stderr)
    print(f"\nTop genres:", file=sys.stderr)
    for genre, count in list(report['genres'].items())[:10]:
        print(f"  {genre}: {count}", file=sys.stderr)
    print(f"\nReport saved to: {output_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
