#!/usr/bin/env python3
"""
Apply ML tags with confidence threshold.

Only applies genre/subgenre tags when confidence exceeds threshold.
Other tags (energy, mood, timeslot, etc.) are always applied.

Usage:
    # Preview what would be applied at 85% confidence
    python apply_tags_filtered.py /mnt/d/Music/analysis.json --confidence 0.85 --dry-run
    
    # Apply tags
    python apply_tags_filtered.py /mnt/d/Music/analysis.json --confidence 0.85
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
from mutagen.id3 import ID3, TCON, TIT1, TBPM, TKEY, TXXX, GEOB, PRIV


def apply_tags_filtered(analysis_file, min_confidence=0.85, dry_run=False, genre_only=False, skip_bpm_key=False):
    """Apply tags with confidence filtering."""
    
    with open(analysis_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    tracks = data['tracks']
    total = len(tracks)
    
    stats = {
        'genre_applied': 0,
        'genre_skipped_low_conf': 0,
        'genre_skipped_no_change': 0,
        'other_tags_applied': 0,
        'errors': [],
    }
    
    print(f"Processing {total} tracks (min confidence: {min_confidence*100:.0f}%)...", file=sys.stderr)
    if skip_bpm_key:
        print("Skipping BPM and Key (keeping Rekordbox values)", file=sys.stderr)
    
    for i, track in enumerate(tracks):
        if (i + 1) % 500 == 0 or (i + 1) == total:
            print(f"\rProgress: {i+1}/{total}", end='', file=sys.stderr)
        
        filepath = Path(track['path'])
        if not filepath.exists():
            stats['errors'].append(f"Not found: {filepath}")
            continue
        
        ml = track.get('ml_analysis', {})
        genre_conf = ml.get('ml_genre_confidence') or 0
        
        # Determine what to apply
        apply_genre = genre_conf >= min_confidence
        subgenre = ml.get('ml_subgenre', '')
        
        if not apply_genre:
            stats['genre_skipped_low_conf'] += 1
        else:
            stats['genre_applied'] += 1
        
        if dry_run:
            if apply_genre and i < 20:
                print(f"\n  [{genre_conf*100:.0f}%] {filepath.name[:45]} → {subgenre}", file=sys.stderr)
            continue
        
        # Actually apply tags
        try:
            ext = filepath.suffix.lower()
            
            if ext == '.mp3':
                audio = MP3(filepath)
                if audio.tags is None:
                    audio.add_tags()
                
                # Preserve GEOB/PRIV frames
                geob_frames = [(k, audio.tags[k]) for k in list(audio.tags.keys()) if k.startswith('GEOB:')]
                priv_frames = [(k, audio.tags[k]) for k in list(audio.tags.keys()) if k.startswith('PRIV:')]
                
                # Apply genre (subgenre as main genre) if confident
                if apply_genre and subgenre:
                    audio.tags.delall('TCON')
                    audio.tags.add(TCON(encoding=3, text=[subgenre]))
                    
                    # Also set subgenre tag
                    audio.tags.delall('TIT1')
                    audio.tags.add(TIT1(encoding=3, text=[subgenre]))
                
                # Apply other tags (always)
                if not genre_only:
                    if not skip_bpm_key:
                        if ml.get('ml_bpm'):
                            audio.tags.delall('TBPM')
                            audio.tags.add(TBPM(encoding=3, text=[str(int(round(ml['ml_bpm'])))]))
                        
                        if ml.get('ml_key'):
                            audio.tags.delall('TKEY')
                            audio.tags.add(TKEY(encoding=3, text=[ml['ml_key']]))
                    
                    for tag_name in ['ENERGY', 'MOOD', 'TIMESLOT', 'DIRECTION', 'VOCAL']:
                        val = ml.get(f'ml_{tag_name.lower()}')
                        if val is not None:
                            audio.tags.delall(f'TXXX:{tag_name}')
                            audio.tags.add(TXXX(encoding=3, desc=tag_name, text=[str(val)]))
                    
                    stats['other_tags_applied'] += 1
                
                # Restore GEOB/PRIV frames
                for key, frame in geob_frames:
                    if key not in audio.tags:
                        audio.tags.add(frame)
                for key, frame in priv_frames:
                    if key not in audio.tags:
                        audio.tags.add(frame)
                
                audio.save()
                
            elif ext == '.flac':
                audio = FLAC(filepath)
                
                if apply_genre and subgenre:
                    audio['GENRE'] = subgenre
                    audio['CONTENTGROUP'] = subgenre
                
                if not genre_only:
                    if not skip_bpm_key:
                        if ml.get('ml_bpm'):
                            audio['BPM'] = str(int(round(ml['ml_bpm'])))
                        if ml.get('ml_key'):
                            audio['INITIALKEY'] = ml['ml_key']
                    
                    for tag_name in ['ENERGY', 'MOOD', 'TIMESLOT', 'DIRECTION', 'VOCAL']:
                        val = ml.get(f'ml_{tag_name.lower()}')
                        if val is not None:
                            audio[tag_name] = str(val)
                    
                    stats['other_tags_applied'] += 1
                
                audio.save()
                
        except Exception as e:
            stats['errors'].append(f"{filepath.name}: {e}")
    
    print(file=sys.stderr)
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"{'DRY RUN - ' if dry_run else ''}COMPLETE", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Genre applied (conf >= {min_confidence*100:.0f}%): {stats['genre_applied']}", file=sys.stderr)
    print(f"Genre skipped (low confidence): {stats['genre_skipped_low_conf']}", file=sys.stderr)
    if not genre_only:
        if skip_bpm_key:
            print(f"Other tags applied (energy/mood/timeslot/etc, NO BPM/Key): {stats['other_tags_applied']}", file=sys.stderr)
        else:
            print(f"Other tags applied (BPM/key/energy/mood/etc): {stats['other_tags_applied']}", file=sys.stderr)
    print(f"Errors: {len(stats['errors'])}", file=sys.stderr)
    
    if stats['errors'][:5]:
        print("\nFirst few errors:", file=sys.stderr)
        for e in stats['errors'][:5]:
            print(f"  {e}", file=sys.stderr)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description='Apply ML tags with confidence threshold',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Preview at 85% confidence:
    python apply_tags_filtered.py analysis.json --confidence 0.85 --dry-run
  
  Apply all tags (genre at 85%, others always):
    python apply_tags_filtered.py analysis.json --confidence 0.85
  
  Apply only genre tags (skip BPM/key/energy/mood):
    python apply_tags_filtered.py analysis.json --confidence 0.85 --genre-only
        """
    )
    
    parser.add_argument('analysis_file', help='Path to analysis.json')
    parser.add_argument('--confidence', type=float, default=0.85, 
                        help='Minimum confidence for genre tags (0.0-1.0, default: 0.85)')
    parser.add_argument('--dry-run', action='store_true', help='Preview without applying')
    parser.add_argument('--genre-only', action='store_true', 
                        help='Only apply genre tags, skip BPM/key/energy/mood/etc')
    parser.add_argument('--skip-bpm-key', action='store_true',
                        help='Skip BPM and Key tags (keep Rekordbox values)')
    
    args = parser.parse_args()
    
    if not Path(args.analysis_file).exists():
        print(f"Error: File not found: {args.analysis_file}", file=sys.stderr)
        sys.exit(1)
    
    apply_tags_filtered(
        args.analysis_file,
        min_confidence=args.confidence,
        dry_run=args.dry_run,
        genre_only=args.genre_only,
        skip_bpm_key=args.skip_bpm_key
    )
