#!/usr/bin/env python3
"""
DJ Track Folder Organizer

Organizes tracks into genre-based folders based on ML analysis.

Usage:
    # Preview what will be moved (dry run)
    python organize_by_genre.py /mnt/d/Music/analysis.json --dry-run

    # Actually move files
    python organize_by_genre.py /mnt/d/Music/analysis.json

Structure created:
    /mnt/d/Music/Tracks/
    ├── House/
    │   ├── Deep House/
    │   ├── Tech House/
    │   └── ...
    ├── Techno/
    ├── Trance/
    └── ...
"""

import os
import sys
import json
import shutil
import argparse
from pathlib import Path
from collections import defaultdict


def sanitize_folder_name(name):
    """Remove/replace characters that are invalid in folder names."""
    if not name:
        return "Unknown"
    # Replace invalid characters
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    return name.strip()


def organize_tracks(analysis_file, dry_run=False, use_subgenres=True, min_genre_size=10):
    """Organize tracks into genre folders."""
    
    # Load analysis
    with open(analysis_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    tracks = data['tracks']
    print(f"Loaded {len(tracks)} tracks from analysis", file=sys.stderr)
    
    # Get base directory from first track
    first_path = Path(tracks[0]['path'])
    base_dir = first_path.parent
    print(f"Base directory: {base_dir}", file=sys.stderr)
    
    # First pass: count genres to identify small ones
    genre_counts = defaultdict(int)
    for track in tracks:
        ml = track.get('ml_analysis', {})
        genre = sanitize_folder_name(ml.get('ml_genre', 'Unknown'))
        genre_counts[genre] += 1
    
    # Identify small genres (below threshold)
    small_genres = {g for g, count in genre_counts.items() if count < min_genre_size}
    print(f"Genres with <{min_genre_size} tracks (moving to Other/): {len(small_genres)}", file=sys.stderr)
    
    # Reset counts for final summary
    final_genre_counts = defaultdict(int)
    moves = []
    errors = []
    
    for track in tracks:
        source = Path(track['path'])
        
        if not source.exists():
            errors.append(f"File not found: {source}")
            continue
        
        ml = track.get('ml_analysis', {})
        genre = ml.get('ml_genre', 'Unknown')
        subgenre = ml.get('ml_subgenre', '')
        
        genre = sanitize_folder_name(genre)
        subgenre = sanitize_folder_name(subgenre)
        
        # Build destination path
        if genre in small_genres:
            # Small genres go to Other/GenreName/
            dest_folder = base_dir / "Other" / genre
            final_genre_counts["Other"] += 1
        elif use_subgenres and subgenre and subgenre != genre:
            dest_folder = base_dir / genre / subgenre
            final_genre_counts[genre] += 1
        else:
            dest_folder = base_dir / genre
            final_genre_counts[genre] += 1
        
        dest = dest_folder / source.name
        
        # Skip if already in correct location
        if source == dest:
            continue
        
        # Handle filename conflicts
        if dest.exists() and source != dest:
            # Add number suffix
            counter = 1
            stem = dest.stem
            suffix = dest.suffix
            while dest.exists():
                dest = dest_folder / f"{stem}_{counter}{suffix}"
                counter += 1
        
        moves.append({
            'source': source,
            'dest': dest,
            'genre': genre,
            'subgenre': subgenre,
        })
    
    # Print summary
    print(f"\n{'='*60}", file=sys.stderr)
    print("ORGANIZATION SUMMARY", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Total tracks to move: {len(moves)}", file=sys.stderr)
    print(f"Errors (file not found): {len(errors)}", file=sys.stderr)
    print(f"\nGenre breakdown:", file=sys.stderr)
    for genre, count in sorted(final_genre_counts.items(), key=lambda x: -x[1]):
        print(f"  {genre:25} {count:5} tracks", file=sys.stderr)
    
    if small_genres:
        print(f"\nSmall genres consolidated into Other/:", file=sys.stderr)
        for g in sorted(small_genres):
            print(f"  Other/{g}/ ({genre_counts[g]} tracks)", file=sys.stderr)
    
    if dry_run:
        print(f"\n{'='*60}", file=sys.stderr)
        print("DRY RUN - No files will be moved", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        print("\nSample moves (first 20):", file=sys.stderr)
        for move in moves[:20]:
            rel_dest = move['dest'].relative_to(base_dir)
            print(f"  {move['source'].name[:40]:40} → {rel_dest}", file=sys.stderr)
        if len(moves) > 20:
            print(f"  ... and {len(moves) - 20} more", file=sys.stderr)
        return
    
    # Actually move files
    print(f"\n{'='*60}", file=sys.stderr)
    print("MOVING FILES", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    
    moved = 0
    move_errors = []
    
    for i, move in enumerate(moves):
        if (i + 1) % 100 == 0 or (i + 1) == len(moves):
            pct = 100 * (i + 1) // len(moves)
            print(f"\rProgress: {i+1}/{len(moves)} ({pct}%)", end='', file=sys.stderr)
        
        try:
            # Create destination folder
            move['dest'].parent.mkdir(parents=True, exist_ok=True)
            
            # Move file
            shutil.move(str(move['source']), str(move['dest']))
            moved += 1
        except Exception as e:
            move_errors.append(f"{move['source'].name}: {e}")
    
    print(file=sys.stderr)
    print(f"\n{'='*60}", file=sys.stderr)
    print("COMPLETE", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Files moved: {moved}", file=sys.stderr)
    print(f"Errors: {len(move_errors)}", file=sys.stderr)
    
    if move_errors:
        print("\nErrors:", file=sys.stderr)
        for err in move_errors[:10]:
            print(f"  {err}", file=sys.stderr)
        if len(move_errors) > 10:
            print(f"  ... and {len(move_errors) - 10} more", file=sys.stderr)
    
    print(f"\nNext steps:", file=sys.stderr)
    print(f"  1. Open Rekordbox", file=sys.stderr)
    print(f"  2. Select all missing files (red ! icons)", file=sys.stderr)
    print(f"  3. Right-click → Relocate", file=sys.stderr)
    print(f"  4. Point to {base_dir} with 'Search in subfolders' checked", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description='Organize DJ tracks into genre folders',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Preview changes:
    python organize_by_genre.py /mnt/d/Music/analysis.json --dry-run

  Move files:
    python organize_by_genre.py /mnt/d/Music/analysis.json

  Flat genre structure (no subfolders):
    python organize_by_genre.py /mnt/d/Music/analysis.json --no-subgenres
        """
    )
    
    parser.add_argument('analysis_file', help='Path to analysis.json')
    parser.add_argument('--dry-run', action='store_true', help='Preview without moving files')
    parser.add_argument('--no-subgenres', action='store_true', help='Only use main genres (no subfolders)')
    parser.add_argument('--min-genre-size', type=int, default=10, help='Genres with fewer tracks go to Other/ (default: 10)')
    
    args = parser.parse_args()
    
    if not Path(args.analysis_file).exists():
        print(f"Error: Analysis file not found: {args.analysis_file}", file=sys.stderr)
        sys.exit(1)
    
    organize_tracks(
        args.analysis_file,
        dry_run=args.dry_run,
        use_subgenres=not args.no_subgenres,
        min_genre_size=args.min_genre_size
    )


if __name__ == '__main__':
    main()
