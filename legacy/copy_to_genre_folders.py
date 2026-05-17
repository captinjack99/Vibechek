#!/usr/bin/env python3
"""
Copy tracks to genre folders based on their existing genre tags.
"""

import os
import sys
import shutil
from pathlib import Path

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import warnings
warnings.filterwarnings('ignore')

from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.aiff import AIFF


def get_genre(filepath):
    """Get genre tag from file."""
    ext = filepath.suffix.lower()
    try:
        if ext == '.mp3':
            audio = MP3(filepath)
            if audio.tags and 'TCON' in audio.tags:
                return str(audio.tags['TCON'])
        elif ext == '.flac':
            audio = FLAC(filepath)
            if audio.tags and 'genre' in audio.tags:
                return audio.tags['genre'][0]
        elif ext == '.m4a':
            audio = MP4(filepath)
            if audio.tags and '\xa9gen' in audio.tags:
                return audio.tags['\xa9gen'][0]
        elif ext in ['.aiff', '.aif']:
            audio = AIFF(filepath)
            if audio.tags and 'TCON' in audio.tags:
                return str(audio.tags['TCON'])
    except:
        pass
    return None


def sanitize_folder_name(name):
    """Remove invalid characters from folder name."""
    if not name:
        return "Unknown"
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    return name.strip()


def copy_to_genre_folders(source_dir, dest_dir, dry_run=False):
    """Copy tracks to genre folders."""
    source_path = Path(source_dir)
    dest_path = Path(dest_dir)
    
    if not source_path.exists():
        print(f"Error: Source directory not found: {source_path}", file=sys.stderr)
        sys.exit(1)
    
    # Find all audio files
    audio_files = []
    for ext in ['*.mp3', '*.flac', '*.m4a', '*.wav', '*.aiff', '*.aif']:
        audio_files.extend(source_path.rglob(ext))
        audio_files.extend(source_path.rglob(ext.upper()))
    
    audio_files = list(set(audio_files))
    print(f"Found {len(audio_files)} audio files in {source_path}", file=sys.stderr)
    
    copied = 0
    skipped = 0
    errors = []
    
    for f in audio_files:
        genre = get_genre(f)
        
        if not genre:
            skipped += 1
            errors.append(f"No genre: {f.name}")
            continue
        
        genre = sanitize_folder_name(genre)
        dest_folder = dest_path / genre
        dest_file = dest_folder / f.name
        
        # Handle conflicts
        if dest_file.exists():
            skipped += 1
            continue
        
        if dry_run:
            print(f"  {f.name[:50]} → {genre}/", file=sys.stderr)
            copied += 1
            continue
        
        try:
            dest_folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(f), str(dest_file))
            copied += 1
        except Exception as e:
            errors.append(f"{f.name}: {e}")
    
    print(f"\n{'DRY RUN - ' if dry_run else ''}COMPLETE", file=sys.stderr)
    print(f"Copied: {copied}", file=sys.stderr)
    print(f"Skipped: {skipped}", file=sys.stderr)
    print(f"Errors: {len(errors)}", file=sys.stderr)
    
    if errors[:5]:
        print("\nFirst few issues:", file=sys.stderr)
        for e in errors[:5]:
            print(f"  {e}", file=sys.stderr)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Copy tracks to genre folders based on tags')
    parser.add_argument('source', help='Source directory (e.g., /mnt/d/Music/New Tracks)')
    parser.add_argument('dest', help='Destination directory (e.g., /mnt/d/Music/Tracks)')
    parser.add_argument('--dry-run', action='store_true', help='Preview without copying')
    args = parser.parse_args()
    
    copy_to_genre_folders(args.source, args.dest, args.dry_run)
