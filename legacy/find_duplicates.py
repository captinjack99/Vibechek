#!/usr/bin/env python3
"""
DJ Track Duplicate Finder

Finds duplicates using:
1. Exact file hash (MD5) - identical files
2. Audio fingerprint (Chromaprint) - same audio, different encoding/tags

Usage:
    # Find duplicates (report only)
    python find_duplicates.py "/mnt/d/Music/Tracks" -o dupes_report.json

    # Find and move duplicates to a folder for review
    python find_duplicates.py "/mnt/d/Music/Tracks" -o dupes_report.json --move-to "/mnt/d/Music/Duplicates"

Requirements:
    pip install mutagen
    
For audio fingerprinting (optional but recommended):
    sudo apt install libchromaprint-tools
"""

import os
import sys
import json
import hashlib
import subprocess
import argparse
from pathlib import Path
from collections import defaultdict
import shutil

SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.aiff', '.ogg', '.aac', '.wma'}


def get_file_hash(filepath, chunk_size=8192):
    """Calculate MD5 hash of file."""
    hasher = hashlib.md5()
    try:
        with open(filepath, 'rb') as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        return None


def find_fpcalc():
    """Find fpcalc executable for audio fingerprinting."""
    for cmd in ['fpcalc', '/usr/bin/fpcalc']:
        try:
            result = subprocess.run([cmd, '-version'], capture_output=True, timeout=5)
            if result.returncode == 0:
                return cmd
        except:
            pass
    return None


def get_audio_fingerprint(filepath, fpcalc_cmd):
    """Generate audio fingerprint using Chromaprint."""
    if not fpcalc_cmd:
        return None
    
    try:
        result = subprocess.run(
            [fpcalc_cmd, '-raw', '-length', '120', str(filepath)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line.startswith('FINGERPRINT='):
                    fp = line.split('=', 1)[1]
                    # Hash the fingerprint for easier comparison
                    return hashlib.md5(fp.encode()).hexdigest()
    except Exception as e:
        pass
    return None


def get_file_info(filepath):
    """Get basic file info."""
    stat = filepath.stat()
    return {
        'path': str(filepath),
        'filename': filepath.name,
        'size_bytes': stat.st_size,
        'size_mb': round(stat.st_size / (1024 * 1024), 2),
    }


def choose_keeper(files):
    """
    Choose which file to keep from a group of duplicates.
    Preference: FLAC > WAV > M4A > MP3, then larger file, then shorter path
    """
    ext_priority = {'.flac': 1, '.wav': 2, '.aiff': 3, '.m4a': 4, '.mp3': 5, '.ogg': 6, '.aac': 7, '.wma': 8}
    
    def score(f):
        ext = Path(f['path']).suffix.lower()
        return (
            ext_priority.get(ext, 99),  # Format priority
            -f['size_bytes'],            # Larger is better (negative for sorting)
            len(f['path']),              # Shorter path is better
        )
    
    sorted_files = sorted(files, key=score)
    return sorted_files[0], sorted_files[1:]  # keeper, duplicates


def main():
    parser = argparse.ArgumentParser(description='Find duplicate audio files')
    parser.add_argument('directory', help='Path to music directory')
    parser.add_argument('--output', '-o', default='duplicates_report.json', help='Output JSON file')
    parser.add_argument('--move-to', metavar='DIR', help='Move duplicates to this directory (keeps originals)')
    parser.add_argument('--delete', action='store_true', help='Delete duplicates (DANGEROUS - use with caution)')
    parser.add_argument('--no-fingerprint', action='store_true', help='Skip audio fingerprinting (faster but less accurate)')
    parser.add_argument('--hash-only', action='store_true', help='Only find exact file duplicates (fastest)')
    
    args = parser.parse_args()
    
    music_path = Path(args.directory)
    if not music_path.exists():
        print(f"Error: Directory not found: {music_path}", file=sys.stderr)
        sys.exit(1)
    
    # Find fpcalc
    fpcalc_cmd = None
    if not args.no_fingerprint and not args.hash_only:
        fpcalc_cmd = find_fpcalc()
        if not fpcalc_cmd:
            print("Warning: fpcalc not found. Install with: sudo apt install libchromaprint-tools", file=sys.stderr)
            print("Falling back to hash-only mode.", file=sys.stderr)
    
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
    
    # Phase 1: Hash all files
    print("\nPhase 1: Calculating file hashes...", file=sys.stderr)
    file_hashes = defaultdict(list)
    file_info = {}
    
    for i, filepath in enumerate(audio_files):
        if (i + 1) % 500 == 0 or (i + 1) == total:
            print(f"\r  Progress: {i+1}/{total}", end='', file=sys.stderr)
        
        info = get_file_info(filepath)
        file_info[str(filepath)] = info
        
        file_hash = get_file_hash(filepath)
        if file_hash:
            info['file_hash'] = file_hash
            file_hashes[file_hash].append(info)
    
    print(file=sys.stderr)
    
    # Find exact duplicates
    exact_dupes = {h: files for h, files in file_hashes.items() if len(files) > 1}
    exact_dupe_count = sum(len(f) - 1 for f in exact_dupes.values())
    print(f"  Found {len(exact_dupes)} groups of exact duplicates ({exact_dupe_count} duplicate files)", file=sys.stderr)
    
    # Phase 2: Audio fingerprinting (if enabled)
    audio_dupes = {}
    if fpcalc_cmd and not args.hash_only:
        print("\nPhase 2: Audio fingerprinting (this takes a while)...", file=sys.stderr)
        
        # Only fingerprint files that aren't already exact dupes
        exact_dupe_paths = set()
        for files in exact_dupes.values():
            for f in files[1:]:  # Skip the first (keeper)
                exact_dupe_paths.add(f['path'])
        
        files_to_fingerprint = [f for f in audio_files if str(f) not in exact_dupe_paths]
        fp_total = len(files_to_fingerprint)
        
        fingerprints = defaultdict(list)
        for i, filepath in enumerate(files_to_fingerprint):
            if (i + 1) % 100 == 0 or (i + 1) == fp_total:
                print(f"\r  Progress: {i+1}/{fp_total}", end='', file=sys.stderr)
            
            fp = get_audio_fingerprint(filepath, fpcalc_cmd)
            if fp:
                info = file_info[str(filepath)]
                info['audio_fingerprint'] = fp
                fingerprints[fp].append(info)
        
        print(file=sys.stderr)
        
        # Find audio duplicates (same audio, different file)
        # Exclude groups that are already exact dupes
        for fp, files in fingerprints.items():
            if len(files) > 1:
                # Check if these are actually different files (not same hash)
                hashes = set(f.get('file_hash') for f in files)
                if len(hashes) > 1:  # Different file hashes = different encodings
                    audio_dupes[fp] = files
        
        audio_dupe_count = sum(len(f) - 1 for f in audio_dupes.values())
        print(f"  Found {len(audio_dupes)} groups of audio duplicates ({audio_dupe_count} duplicate files)", file=sys.stderr)
    
    # Build report
    report = {
        'summary': {
            'total_files': total,
            'exact_duplicate_groups': len(exact_dupes),
            'exact_duplicate_files': exact_dupe_count,
            'audio_duplicate_groups': len(audio_dupes),
            'audio_duplicate_files': sum(len(f) - 1 for f in audio_dupes.values()),
            'total_duplicates': exact_dupe_count + sum(len(f) - 1 for f in audio_dupes.values()),
            'space_recoverable_mb': 0,
        },
        'exact_duplicates': [],
        'audio_duplicates': [],
    }
    
    # Process exact duplicates
    total_recoverable = 0
    for hash_val, files in exact_dupes.items():
        keeper, dupes = choose_keeper(files)
        group = {
            'hash': hash_val,
            'keep': keeper,
            'duplicates': dupes,
            'recoverable_mb': sum(d['size_mb'] for d in dupes),
        }
        total_recoverable += group['recoverable_mb']
        report['exact_duplicates'].append(group)
    
    # Process audio duplicates
    for fp, files in audio_dupes.items():
        keeper, dupes = choose_keeper(files)
        group = {
            'fingerprint': fp,
            'keep': keeper,
            'duplicates': dupes,
            'recoverable_mb': sum(d['size_mb'] for d in dupes),
        }
        total_recoverable += group['recoverable_mb']
        report['audio_duplicates'].append(group)
    
    report['summary']['space_recoverable_mb'] = round(total_recoverable, 2)
    
    # Save report
    output_path = Path(args.output)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*60}", file=sys.stderr)
    print("DUPLICATE SCAN COMPLETE", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Total files scanned: {total}", file=sys.stderr)
    print(f"Exact duplicates: {exact_dupe_count} files in {len(exact_dupes)} groups", file=sys.stderr)
    print(f"Audio duplicates: {report['summary']['audio_duplicate_files']} files in {len(audio_dupes)} groups", file=sys.stderr)
    print(f"Total duplicates: {report['summary']['total_duplicates']}", file=sys.stderr)
    print(f"Space recoverable: {report['summary']['space_recoverable_mb']:.1f} MB", file=sys.stderr)
    print(f"\nReport saved to: {output_path}", file=sys.stderr)
    
    # Handle duplicates if requested
    all_dupes = []
    for group in report['exact_duplicates']:
        all_dupes.extend(group['duplicates'])
    for group in report['audio_duplicates']:
        all_dupes.extend(group['duplicates'])
    
    if args.move_to and all_dupes:
        move_dir = Path(args.move_to)
        move_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"\nMoving {len(all_dupes)} duplicates to {move_dir}...", file=sys.stderr)
        moved = 0
        for dupe in all_dupes:
            src = Path(dupe['path'])
            dst = move_dir / src.name
            
            # Handle name conflicts
            counter = 1
            while dst.exists():
                dst = move_dir / f"{src.stem}_{counter}{src.suffix}"
                counter += 1
            
            try:
                shutil.move(str(src), str(dst))
                moved += 1
            except Exception as e:
                print(f"  Error moving {src.name}: {e}", file=sys.stderr)
        
        print(f"Moved {moved} files to {move_dir}", file=sys.stderr)
    
    elif args.delete and all_dupes:
        print(f"\n⚠️  About to DELETE {len(all_dupes)} files!", file=sys.stderr)
        confirm = input("Type 'DELETE' to confirm: ")
        if confirm == 'DELETE':
            deleted = 0
            for dupe in all_dupes:
                try:
                    Path(dupe['path']).unlink()
                    deleted += 1
                except Exception as e:
                    print(f"  Error deleting {dupe['path']}: {e}", file=sys.stderr)
            print(f"Deleted {deleted} files", file=sys.stderr)
        else:
            print("Deletion cancelled.", file=sys.stderr)


if __name__ == '__main__':
    main()
