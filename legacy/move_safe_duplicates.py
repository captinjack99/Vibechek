#!/usr/bin/env python3
"""
Move safe duplicates to a review folder.
Uses the safe_duplicates.json report.

Usage:
    python move_safe_duplicates.py safe_duplicates.json --move-to "/mnt/d/Music/Duplicates"
"""

import json
import argparse
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Move safe duplicates to review folder')
    parser.add_argument('report', help='Path to safe_duplicates.json')
    parser.add_argument('--move-to', required=True, help='Destination folder for duplicates')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be moved without moving')
    
    args = parser.parse_args()
    
    # Load report
    with open(args.report, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    move_dir = Path(args.move_to)
    
    # Collect all duplicates to move
    all_dupes = []
    for group in data['exact_duplicates']:
        all_dupes.extend(group['duplicates'])
    for group in data['audio_duplicates']:
        all_dupes.extend(group['duplicates'])
    
    print(f"Found {len(all_dupes)} safe duplicates to move")
    space = data['summary'].get('space_recoverable_mb') or data['summary'].get('space_mb', 0)
    print(f"Space to recover: {space:.1f} MB")
    print(f"Destination: {move_dir}")
    print()
    
    if args.dry_run:
        print("DRY RUN - No files will be moved")
        print("-" * 60)
        for dupe in all_dupes[:20]:
            print(f"  Would move: {dupe['filename']}")
        if len(all_dupes) > 20:
            print(f"  ... and {len(all_dupes) - 20} more")
        return
    
    # Create destination directory
    move_dir.mkdir(parents=True, exist_ok=True)
    
    # Move files
    moved = 0
    errors = []
    
    for i, dupe in enumerate(all_dupes):
        src = Path(dupe['path'])
        
        if not src.exists():
            errors.append(f"Not found: {src}")
            continue
        
        dst = move_dir / src.name
        
        # Handle name conflicts
        counter = 1
        while dst.exists():
            dst = move_dir / f"{src.stem}_{counter}{src.suffix}"
            counter += 1
        
        try:
            shutil.move(str(src), str(dst))
            moved += 1
            if (i + 1) % 50 == 0:
                print(f"Progress: {i + 1}/{len(all_dupes)} moved")
        except Exception as e:
            errors.append(f"Error moving {src.name}: {e}")
    
    print()
    print("=" * 60)
    print(f"COMPLETE: Moved {moved} files to {move_dir}")
    if errors:
        print(f"Errors: {len(errors)}")
        for err in errors[:10]:
            print(f"  {err}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more errors")


if __name__ == '__main__':
    main()
