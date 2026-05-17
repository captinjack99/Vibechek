#!/usr/bin/env python3
"""
Apply subgenre as main genre tag for Rekordbox filtering.

Changes: "House" (genre) + "Deep House" (subgenre) → "Deep House" (genre)
"""

import os
import sys
import json
from pathlib import Path

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import warnings
warnings.filterwarnings('ignore')

from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TCON

def update_genre_to_subgenre(analysis_file, dry_run=False):
    with open(analysis_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    tracks = data['tracks']
    total = len(tracks)
    updated = 0
    skipped = 0
    errors = []
    
    print(f"Processing {total} tracks...", file=sys.stderr)
    
    for i, track in enumerate(tracks):
        if (i + 1) % 500 == 0 or (i + 1) == total:
            print(f"\rProgress: {i+1}/{total}", end='', file=sys.stderr)
        
        ml = track.get('ml_analysis', {})
        genre = ml.get('ml_genre', '')
        subgenre = ml.get('ml_subgenre', '')
        
        # Only update if subgenre exists and is different from genre
        if not subgenre or subgenre == genre:
            skipped += 1
            continue
        
        filepath = Path(track['path'])
        if not filepath.exists():
            errors.append(f"Not found: {filepath}")
            continue
        
        if dry_run:
            print(f"\n  {filepath.name[:50]}: {genre} → {subgenre}", file=sys.stderr)
            updated += 1
            continue
        
        try:
            ext = filepath.suffix.lower()
            if ext == '.mp3':
                audio = MP3(filepath)
                if audio.tags is None:
                    audio.add_tags()
                audio.tags.delall('TCON')
                audio.tags.add(TCON(encoding=3, text=[subgenre]))
                audio.save()
                updated += 1
            elif ext == '.flac':
                audio = FLAC(filepath)
                audio['GENRE'] = subgenre
                audio.save()
                updated += 1
            else:
                skipped += 1
        except Exception as e:
            errors.append(f"{filepath.name}: {e}")
    
    print(file=sys.stderr)
    print(f"\n{'='*50}", file=sys.stderr)
    print(f"{'DRY RUN - ' if dry_run else ''}COMPLETE", file=sys.stderr)
    print(f"{'='*50}", file=sys.stderr)
    print(f"Updated: {updated}", file=sys.stderr)
    print(f"Skipped (no subgenre or same): {skipped}", file=sys.stderr)
    print(f"Errors: {len(errors)}", file=sys.stderr)
    
    if errors[:5]:
        print("\nFirst few errors:", file=sys.stderr)
        for e in errors[:5]:
            print(f"  {e}", file=sys.stderr)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Apply subgenre as main genre tag')
    parser.add_argument('analysis_file', help='Path to analysis.json')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes')
    args = parser.parse_args()
    
    update_genre_to_subgenre(args.analysis_file, args.dry_run)
