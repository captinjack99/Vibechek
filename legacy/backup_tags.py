#!/usr/bin/env python3
"""
DJ Track Tag Backup & Restore

Backup current tags before applying ML analysis, restore if needed.

Usage:
    # Backup current tags
    python backup_tags.py backup "/mnt/d/Music/Tracks" -o tags_backup.json

    # Restore tags from backup
    python backup_tags.py restore tags_backup.json
"""

import os
import sys
import json
import argparse
from pathlib import Path

# Suppress warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import warnings
warnings.filterwarnings('ignore')

try:
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, TBPM, TKEY, TIT1, TXXX, GEOB, PRIV
except ImportError:
    print("Error: mutagen not installed. Run: pip install mutagen", file=sys.stderr)
    sys.exit(1)

SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.aiff', '.ogg'}


def get_all_tags(filepath):
    """Extract all relevant tags from a file, including GEOB frames for Rekordbox."""
    tags = {}
    ext = filepath.suffix.lower()
    
    try:
        if ext == '.mp3':
            audio = MP3(filepath)
            if audio.tags:
                # Standard tags
                if 'TIT2' in audio.tags:
                    tags['title'] = str(audio.tags['TIT2'])
                if 'TPE1' in audio.tags:
                    tags['artist'] = str(audio.tags['TPE1'])
                if 'TALB' in audio.tags:
                    tags['album'] = str(audio.tags['TALB'])
                if 'TCON' in audio.tags:
                    tags['genre'] = str(audio.tags['TCON'])
                if 'TBPM' in audio.tags:
                    tags['bpm'] = str(audio.tags['TBPM'])
                if 'TKEY' in audio.tags:
                    tags['key'] = str(audio.tags['TKEY'])
                if 'TIT1' in audio.tags:
                    tags['subgenre'] = str(audio.tags['TIT1'])
                
                # Custom TXXX tags
                for key in audio.tags:
                    if key.startswith('TXXX:'):
                        tag_name = key.split(':', 1)[1]
                        tags[f'txxx_{tag_name.lower()}'] = str(audio.tags[key])
                
                # GEOB frames (Rekordbox cue points, beat grids, etc.)
                # Store as base64 for safe JSON serialization
                import base64
                for key in audio.tags:
                    if key.startswith('GEOB:'):
                        frame = audio.tags[key]
                        tags[f'geob_{key}'] = {
                            'encoding': frame.encoding,
                            'mime': frame.mime,
                            'filename': frame.filename,
                            'desc': frame.desc,
                            'data': base64.b64encode(frame.data).decode('ascii')
                        }
                
                # PRIV frames (private data)
                for key in audio.tags:
                    if key.startswith('PRIV:'):
                        frame = audio.tags[key]
                        tags[f'priv_{key}'] = {
                            'owner': frame.owner,
                            'data': base64.b64encode(frame.data).decode('ascii')
                        }
        
        elif ext == '.flac':
            audio = FLAC(filepath)
            if audio.tags:
                tags['title'] = audio.tags.get('title', [None])[0]
                tags['artist'] = audio.tags.get('artist', [None])[0]
                tags['album'] = audio.tags.get('album', [None])[0]
                tags['genre'] = audio.tags.get('genre', [None])[0]
                tags['bpm'] = audio.tags.get('bpm', [None])[0]
                tags['key'] = audio.tags.get('key', [None])[0]
                tags['subgenre'] = audio.tags.get('grouping', [None])[0]
                
                # Custom tags
                for custom in ['energy', 'mood', 'timeslot', 'direction', 'vocal']:
                    val = audio.tags.get(custom, [None])[0]
                    if val:
                        tags[f'txxx_{custom}'] = val
        
        elif ext == '.m4a':
            audio = MP4(filepath)
            if audio.tags:
                tags['title'] = audio.tags.get('\xa9nam', [None])[0]
                tags['artist'] = audio.tags.get('\xa9ART', [None])[0]
                tags['album'] = audio.tags.get('\xa9alb', [None])[0]
                tags['genre'] = audio.tags.get('\xa9gen', [None])[0]
                # M4A BPM is stored differently
                if 'tmpo' in audio.tags:
                    tags['bpm'] = str(audio.tags['tmpo'][0])
        
        # Remove None values
        tags = {k: v for k, v in tags.items() if v is not None}
        
    except Exception as e:
        tags['_error'] = str(e)
    
    return tags


def set_all_tags(filepath, tags):
    """Restore all tags to a file, including GEOB frames for Rekordbox."""
    ext = filepath.suffix.lower()
    
    try:
        if ext == '.mp3':
            try:
                audio = MP3(filepath)
                if audio.tags is None:
                    audio.add_tags()
            except:
                audio = MP3(filepath)
                audio.add_tags()
            
            # Standard tags
            if 'title' in tags:
                audio.tags['TIT2'] = TIT2(encoding=3, text=tags['title'])
            if 'artist' in tags:
                audio.tags['TPE1'] = TPE1(encoding=3, text=tags['artist'])
            if 'album' in tags:
                audio.tags['TALB'] = TALB(encoding=3, text=tags['album'])
            if 'genre' in tags:
                audio.tags['TCON'] = TCON(encoding=3, text=tags['genre'])
            if 'bpm' in tags:
                audio.tags['TBPM'] = TBPM(encoding=3, text=str(tags['bpm']))
            if 'key' in tags:
                audio.tags['TKEY'] = TKEY(encoding=3, text=tags['key'])
            if 'subgenre' in tags:
                audio.tags['TIT1'] = TIT1(encoding=3, text=tags['subgenre'])
            
            # Custom TXXX tags
            for key, val in tags.items():
                if key.startswith('txxx_'):
                    tag_name = key[5:].upper()
                    audio.tags.add(TXXX(encoding=3, desc=tag_name, text=str(val)))
            
            # Restore GEOB frames (Rekordbox data)
            import base64
            from mutagen.id3 import GEOB, PRIV
            for key, val in tags.items():
                if key.startswith('geob_'):
                    original_key = key[5:]  # Remove 'geob_' prefix
                    frame = GEOB(
                        encoding=val['encoding'],
                        mime=val['mime'],
                        filename=val['filename'],
                        desc=val['desc'],
                        data=base64.b64decode(val['data'])
                    )
                    audio.tags.add(frame)
            
            # Restore PRIV frames
            for key, val in tags.items():
                if key.startswith('priv_'):
                    frame = PRIV(
                        owner=val['owner'],
                        data=base64.b64decode(val['data'])
                    )
                    audio.tags.add(frame)
            
            audio.save()
            return True, None
        
        elif ext == '.flac':
            audio = FLAC(filepath)
            
            if 'title' in tags:
                audio['title'] = tags['title']
            if 'artist' in tags:
                audio['artist'] = tags['artist']
            if 'album' in tags:
                audio['album'] = tags['album']
            if 'genre' in tags:
                audio['genre'] = tags['genre']
            if 'bpm' in tags:
                audio['bpm'] = str(tags['bpm'])
            if 'key' in tags:
                audio['key'] = tags['key']
            if 'subgenre' in tags:
                audio['grouping'] = tags['subgenre']
            
            # Custom tags
            for key, val in tags.items():
                if key.startswith('txxx_'):
                    tag_name = key[5:]
                    audio[tag_name] = str(val)
            
            audio.save()
            return True, None
        
        elif ext == '.m4a':
            audio = MP4(filepath)
            
            if 'title' in tags:
                audio['\xa9nam'] = [tags['title']]
            if 'artist' in tags:
                audio['\xa9ART'] = [tags['artist']]
            if 'album' in tags:
                audio['\xa9alb'] = [tags['album']]
            if 'genre' in tags:
                audio['\xa9gen'] = [tags['genre']]
            if 'bpm' in tags:
                audio['tmpo'] = [int(float(tags['bpm']))]
            
            audio.save()
            return True, None
        
        else:
            return False, f"Unsupported format: {ext}"
    
    except Exception as e:
        return False, str(e)


def backup_tags(directory, output_file):
    """Backup all tags from directory to JSON file."""
    music_path = Path(directory)
    
    if not music_path.exists():
        print(f"Error: Directory not found: {music_path}", file=sys.stderr)
        sys.exit(1)
    
    # Find all audio files
    print(f"Scanning {music_path}...", file=sys.stderr)
    audio_files = []
    for ext in SUPPORTED_EXTENSIONS:
        audio_files.extend(music_path.rglob(f'*{ext}'))
        audio_files.extend(music_path.rglob(f'*{ext.upper()}'))
    
    audio_files = sorted(set(audio_files), key=lambda x: str(x))
    total = len(audio_files)
    print(f"Found {total} audio files", file=sys.stderr)
    
    # Backup tags
    backup_data = {
        'version': '1.0',
        'source_directory': str(music_path),
        'total_files': total,
        'files': {}
    }
    
    for i, filepath in enumerate(audio_files):
        if (i + 1) % 100 == 0 or (i + 1) == total:
            pct = 100 * (i + 1) // total
            print(f"\rBacking up: {i+1}/{total} ({pct}%)", end='', file=sys.stderr)
        
        tags = get_all_tags(filepath)
        backup_data['files'][str(filepath)] = tags
    
    print(file=sys.stderr)
    
    # Save backup
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(backup_data, f, indent=2, ensure_ascii=False)
    
    print(f"\nBackup complete!", file=sys.stderr)
    print(f"  Files backed up: {total}", file=sys.stderr)
    print(f"  Backup saved to: {output_file}", file=sys.stderr)


def restore_tags(backup_file):
    """Restore tags from backup JSON file."""
    with open(backup_file, 'r', encoding='utf-8') as f:
        backup_data = json.load(f)
    
    files = backup_data['files']
    total = len(files)
    
    print(f"Restoring {total} files from backup...", file=sys.stderr)
    
    restored = 0
    errors = 0
    
    for i, (filepath, tags) in enumerate(files.items()):
        if (i + 1) % 100 == 0 or (i + 1) == total:
            pct = 100 * (i + 1) // total
            print(f"\rRestoring: {i+1}/{total} ({pct}%)", end='', file=sys.stderr)
        
        path = Path(filepath)
        if not path.exists():
            errors += 1
            continue
        
        if '_error' in tags:
            continue  # Skip files that had errors during backup
        
        success, error = set_all_tags(path, tags)
        if success:
            restored += 1
        else:
            errors += 1
    
    print(file=sys.stderr)
    print(f"\nRestore complete!", file=sys.stderr)
    print(f"  Files restored: {restored}", file=sys.stderr)
    print(f"  Errors: {errors}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description='Backup and restore audio file tags',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Backup tags:
    python backup_tags.py backup "/mnt/d/Music/Tracks" -o tags_backup.json

  Restore tags:
    python backup_tags.py restore tags_backup.json
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # Backup command
    backup_parser = subparsers.add_parser('backup', help='Backup tags from directory')
    backup_parser.add_argument('directory', help='Path to music directory')
    backup_parser.add_argument('--output', '-o', default='tags_backup.json', help='Output backup file')
    
    # Restore command
    restore_parser = subparsers.add_parser('restore', help='Restore tags from backup')
    restore_parser.add_argument('backup_file', help='Path to backup JSON file')
    
    args = parser.parse_args()
    
    if args.command == 'backup':
        backup_tags(args.directory, args.output)
    elif args.command == 'restore':
        restore_tags(args.backup_file)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
