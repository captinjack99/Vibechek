#!/usr/bin/env python3
"""
DJ Track Collection Analyzer with ML Genre Classification
Uses Essentia's pre-trained models for accurate EDM subgenre detection.

Requirements:
    pip install essentia-tensorflow mutagen

For audio fingerprinting (optional):
    Download fpcalc from: https://acoustid.org/chromaprint

Usage:
    python analyze_dj_tracks_ml.py "D:\Music\Tracks" --output report.json

The script will automatically download required ML models on first run.
"""

import os
import sys
import json
import subprocess
import hashlib
import argparse
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import re
import warnings

warnings.filterwarnings('ignore')

# Check for essentia
try:
    import numpy as np
    from essentia.standard import MonoLoader, TensorflowPredictEffnetDiscogs, TensorflowPredict2D
    ESSENTIA_AVAILABLE = True
except ImportError:
    ESSENTIA_AVAILABLE = False
    print("=" * 60)
    print("Essentia not found. Install with:")
    print("  pip install essentia-tensorflow")
    print("=" * 60)

try:
    from mutagen import File as MutagenFile
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False
    print("Warning: mutagen not installed. Run: pip install mutagen")

SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.aiff', '.ogg', '.aac', '.wma'}

# Essentia model URLs
MODEL_BASE_URL = "https://essentia.upf.edu/models"
MODELS = {
    'effnet': {
        'weights': f"{MODEL_BASE_URL}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
        'metadata': f"{MODEL_BASE_URL}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.json"
    },
    'genre_discogs400': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json"
    }
}

# DJ-friendly genre mapping (consolidate similar subgenres)
DJ_GENRE_MAP = {
    # House variants
    'House': 'House',
    'Deep House': 'Deep House',
    'Tech House': 'Tech House',
    'Electro House': 'Electro House',
    'Progressive House': 'Progressive House',
    'Tribal House': 'Tribal House',
    'Garage House': 'UK Garage',
    'UK Garage': 'UK Garage',
    'Acid House': 'Acid House',
    'Euro House': 'Euro House',
    'Italo House': 'Italo House',
    'Ghetto House': 'Ghetto House',
    'Tropical House': 'Tropical House',
    'Hard House': 'Hard House',
    
    # Techno variants  
    'Techno': 'Techno',
    'Minimal Techno': 'Minimal Techno',
    'Deep Techno': 'Deep Techno',
    'Hard Techno': 'Hard Techno',
    'Dub Techno': 'Dub Techno',
    'Schranz': 'Hard Techno',
    
    # Trance variants
    'Trance': 'Trance',
    'Progressive Trance': 'Progressive Trance',
    'Psy-Trance': 'Psytrance',
    'Goa Trance': 'Psytrance',
    'Hard Trance': 'Hard Trance',
    'Tech Trance': 'Tech Trance',
    
    # Bass music
    'Drum n Bass': 'Drum & Bass',
    'Jungle': 'Drum & Bass',
    'Dubstep': 'Dubstep',
    'Bassline': 'Bass House',
    'Grime': 'Grime',
    'Breaks': 'Breaks',
    'Breakbeat': 'Breaks',
    'Big Beat': 'Breaks',
    
    # Hardcore variants
    'Hardcore': 'Hardcore',
    'Happy Hardcore': 'Happy Hardcore',
    'Gabber': 'Gabber',
    'Hardstyle': 'Hardstyle',
    'Speedcore': 'Hardcore',
    
    # Other electronic
    'Electro': 'Electro',
    'IDM': 'IDM',
    'Ambient': 'Ambient',
    'Downtempo': 'Downtempo',
    'Trip Hop': 'Trip Hop',
    'Chillout': 'Chillout',
    'Synthwave': 'Synthwave',
    'Synth-pop': 'Synth-pop',
    'EBM': 'EBM',
    'Industrial': 'Industrial',
    'Disco': 'Disco',
    'Nu-Disco': 'Nu-Disco',
    'Eurodance': 'Eurodance',
    'Dance-pop': 'Dance Pop',
    
    # Non-electronic (for mixed collections)
    'Hip Hop': 'Hip Hop',
    'Pop': 'Pop',
    'Rock': 'Rock',
    'R&B': 'R&B',
}


def download_models(model_dir):
    """Download required Essentia models if not present."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    
    models_downloaded = {}
    
    for model_name, urls in MODELS.items():
        weights_path = model_dir / f"{model_name}.pb"
        metadata_path = model_dir / f"{model_name}.json"
        
        if not weights_path.exists():
            print(f"Downloading {model_name} weights...", file=sys.stderr)
            urllib.request.urlretrieve(urls['weights'], weights_path)
        
        if not metadata_path.exists():
            print(f"Downloading {model_name} metadata...", file=sys.stderr)
            urllib.request.urlretrieve(urls['metadata'], metadata_path)
        
        models_downloaded[model_name] = {
            'weights': str(weights_path),
            'metadata': str(metadata_path)
        }
        
        # Load class labels from metadata
        with open(metadata_path) as f:
            meta = json.load(f)
            if 'classes' in meta:
                models_downloaded[model_name]['classes'] = meta['classes']
    
    return models_downloaded


def find_fpcalc():
    """Find fpcalc executable."""
    for cmd in ['fpcalc', 'fpcalc.exe']:
        try:
            result = subprocess.run([cmd, '-version'], capture_output=True, timeout=5)
            if result.returncode == 0:
                return cmd
        except:
            pass
    
    script_dir = Path(__file__).parent
    for name in ['fpcalc.exe', 'fpcalc']:
        fpcalc_path = script_dir / name
        if fpcalc_path.exists():
            return str(fpcalc_path)
    
    return None


def extract_from_filename(filename):
    """Extract BPM, key, and mix type from filename patterns."""
    info = {'filename_bpm': None, 'filename_key': None, 'filename_mix': None}
    
    # BPM patterns
    bpm_match = re.search(r'[\s_\-](\d{2,3})(?:\s*bpm)?(?:\.[a-z0-9]+)?$', filename, re.I)
    if bpm_match:
        bpm = int(bpm_match.group(1))
        if 60 <= bpm <= 200:
            info['filename_bpm'] = bpm
    
    # Camelot key patterns
    key_match = re.search(r'[\s_\-\[\(]([1-9]|1[0-2])[AB][\s_\-\]\)\.]', filename, re.I)
    if key_match:
        info['filename_key'] = key_match.group(0).strip(' _-[]().').upper()
    
    # Mix type patterns
    mix_patterns = [
        (r'[\(\[]?\s*original\s*mix\s*[\)\]]?', 'Original Mix'),
        (r'[\(\[]?\s*extended\s*mix\s*[\)\]]?', 'Extended Mix'),
        (r'[\(\[]?\s*radio\s*edit\s*[\)\]]?', 'Radio Edit'),
        (r'[\(\[]?\s*club\s*mix\s*[\)\]]?', 'Club Mix'),
        (r'[\(\[]?\s*dub\s*mix\s*[\)\]]?', 'Dub Mix'),
        (r'[\(\[]?\s*vip\s*mix\s*[\)\]]?', 'VIP Mix'),
        (r'\s*-\s*[^-]+\s+remix', 'Remix'),
        (r'[\(\[]\s*[^)\]]+\s+remix\s*[\)\]]', 'Remix'),
    ]
    for pattern, mix_type in mix_patterns:
        if re.search(pattern, filename, re.I):
            info['filename_mix'] = mix_type
            break
    
    # Extract artist and title
    name_without_ext = Path(filename).stem
    name_clean = re.sub(r'^[\d]+[\s\-\.]+', '', name_without_ext)
    
    if ' - ' in name_clean:
        parts = name_clean.split(' - ', 1)
        info['filename_artist'] = parts[0].strip()
        title = parts[1] if len(parts) > 1 else ''
        title = re.sub(r'\s*[\(\[].*?(?:mix|edit|remix|version).*?[\)\]]', '', title, flags=re.I)
        title = re.sub(r'\s+\d{2,3}$', '', title)
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
    }
    
    fn_info = extract_from_filename(filepath.name)
    metadata.update({f'filename_{k}': v for k, v in fn_info.items() if not k.startswith('filename_')})
    metadata.update(fn_info)
    
    if MUTAGEN_AVAILABLE:
        try:
            audio = MutagenFile(filepath, easy=True)
            if audio and hasattr(audio, 'tags') and audio.tags:
                tags = audio.tags
                
                for field, keys in [
                    ('artist', ['artist', 'albumartist']),
                    ('title', ['title']),
                    ('album', ['album']),
                    ('genre', ['genre']),
                ]:
                    for key in keys:
                        val = tags.get(key)
                        if val:
                            metadata[field] = val[0] if isinstance(val, list) else str(val)
                            break
                
                for key in ['bpm', 'TBPM']:
                    val = tags.get(key)
                    if val:
                        try:
                            metadata['bpm'] = int(float(str(val[0] if isinstance(val, list) else val)))
                        except:
                            pass
                        break
                
                for key in ['initialkey', 'key', 'TKEY']:
                    val = tags.get(key)
                    if val:
                        metadata['key'] = val[0] if isinstance(val, list) else str(val)
                        break
        except Exception as e:
            metadata['metadata_error'] = str(e)
    
    # Use filename values as fallback
    if not metadata['bpm'] and metadata.get('filename_bpm'):
        metadata['bpm'] = metadata['filename_bpm']
    if not metadata['key'] and metadata.get('filename_key'):
        metadata['key'] = metadata['filename_key']
    
    return metadata


def classify_genre_ml(filepath, embedding_model, genre_model, genre_classes):
    """Classify genre using Essentia ML models."""
    try:
        # Load audio at 16kHz (required by model)
        audio = MonoLoader(filename=str(filepath), sampleRate=16000, resampleQuality=4)()
        
        # Get embeddings
        embeddings = embedding_model(audio)
        
        # Get genre predictions
        predictions = genre_model(embeddings)
        
        # Average predictions over time
        avg_predictions = np.mean(predictions, axis=0)
        
        # Get top 3 predictions
        top_indices = np.argsort(avg_predictions)[::-1][:3]
        
        results = []
        for idx in top_indices:
            genre = genre_classes[idx]
            confidence = float(avg_predictions[idx])
            # Map to DJ-friendly genre if applicable
            dj_genre = DJ_GENRE_MAP.get(genre, genre)
            results.append({
                'genre': genre,
                'dj_genre': dj_genre,
                'confidence': round(confidence, 4)
            })
        
        return results
        
    except Exception as e:
        return [{'error': str(e)}]


def get_fingerprint(filepath, fpcalc_cmd):
    """Generate audio fingerprint using fpcalc."""
    if not fpcalc_cmd:
        return None
    
    try:
        kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        result = subprocess.run(
            [fpcalc_cmd, '-raw', '-length', '120', str(filepath)],
            capture_output=True, text=True, timeout=60, **kwargs
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line.startswith('FINGERPRINT='):
                    fp = line.split('=', 1)[1]
                    return hashlib.md5(fp.encode()).hexdigest()
    except:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description='Analyze DJ track collection with ML genre classification')
    parser.add_argument('directory', help='Path to music directory')
    parser.add_argument('--output', '-o', default='dj_tracks_ml_analysis.json', help='Output JSON file')
    parser.add_argument('--no-fingerprint', action='store_true', help='Skip audio fingerprinting')
    parser.add_argument('--no-genre-ml', action='store_true', help='Skip ML genre classification')
    parser.add_argument('--workers', type=int, default=4, help='Number of parallel workers')
    parser.add_argument('--model-dir', default='./essentia_models', help='Directory for ML models')
    args = parser.parse_args()
    
    if not ESSENTIA_AVAILABLE and not args.no_genre_ml:
        print("Essentia not available. Run with --no-genre-ml or install essentia-tensorflow")
        sys.exit(1)
    
    music_path = Path(args.directory)
    if not music_path.exists():
        print(f"Error: Directory not found: {music_path}", file=sys.stderr)
        sys.exit(1)
    
    # Download/load models
    embedding_model = None
    genre_model = None
    genre_classes = []
    
    if ESSENTIA_AVAILABLE and not args.no_genre_ml:
        print("Loading ML models...", file=sys.stderr)
        models = download_models(args.model_dir)
        
        embedding_model = TensorflowPredictEffnetDiscogs(
            graphFilename=models['effnet']['weights'],
            output="PartitionedCall:1"
        )
        genre_model = TensorflowPredict2D(
            graphFilename=models['genre_discogs400']['weights'],
            input="serving_default_model_Placeholder",
            output="PartitionedCall:0"
        )
        genre_classes = models['genre_discogs400']['classes']
        print(f"Loaded {len(genre_classes)} genre classes", file=sys.stderr)
    
    # Find fpcalc
    fpcalc_cmd = None if args.no_fingerprint else find_fpcalc()
    if not args.no_fingerprint and not fpcalc_cmd:
        print("Warning: fpcalc not found. Fingerprinting disabled.", file=sys.stderr)
    
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
    print(f"Analyzing... (ML genre: {'ON' if embedding_model else 'OFF'}, fingerprinting: {'ON' if fpcalc_cmd else 'OFF'})", file=sys.stderr)
    
    results = []
    completed = 0
    
    for filepath in audio_files:
        completed += 1
        if completed % 50 == 0 or completed == total:
            pct = 100 * completed // total
            print(f"\rProgress: {completed}/{total} ({pct}%)", end='', file=sys.stderr)
        
        try:
            # Get basic metadata
            result = get_metadata(filepath)
            
            # ML genre classification
            if embedding_model:
                ml_genres = classify_genre_ml(filepath, embedding_model, genre_model, genre_classes)
                result['ml_genres'] = ml_genres
                if ml_genres and 'error' not in ml_genres[0]:
                    result['ml_primary_genre'] = ml_genres[0]['dj_genre']
                    result['ml_confidence'] = ml_genres[0]['confidence']
            
            # Fingerprint
            if fpcalc_cmd:
                result['fingerprint'] = get_fingerprint(filepath, fpcalc_cmd)
            
            results.append(result)
            
        except Exception as e:
            results.append({'path': str(filepath), 'error': str(e)})
    
    print(file=sys.stderr)
    
    # Find duplicates by fingerprint
    fp_duplicates = {}
    if fpcalc_cmd:
        fingerprints = defaultdict(list)
        for r in results:
            fp = r.get('fingerprint')
            if fp:
                fingerprints[fp].append(r['path'])
        fp_duplicates = {fp: paths for fp, paths in fingerprints.items() if len(paths) > 1}
    
    # Genre statistics (ML-based)
    ml_genres = defaultdict(int)
    for r in results:
        g = r.get('ml_primary_genre', 'Unknown')
        ml_genres[g] += 1
    
    # BPM statistics
    bpm_ranges = defaultdict(int)
    for r in results:
        bpm = r.get('bpm')
        if bpm:
            range_start = (bpm // 5) * 5
            bpm_ranges[f"{range_start}-{range_start+4}"] += 1
    
    # Build report
    report = {
        'summary': {
            'total_files': total,
            'files_with_ml_genre': sum(1 for r in results if r.get('ml_primary_genre')),
            'files_with_fingerprints': sum(1 for r in results if r.get('fingerprint')),
            'duplicate_groups': len(fp_duplicates),
            'duplicate_files': sum(len(p) for p in fp_duplicates.values()),
        },
        'ml_genres': dict(sorted(ml_genres.items(), key=lambda x: -x[1])),
        'bpm_distribution': dict(sorted(bpm_ranges.items(), key=lambda x: int(x[0].split('-')[0]))),
        'duplicates': dict(list(fp_duplicates.items())[:100]),
        'tracks': results,
    }
    
    # Save report
    output_path = Path(args.output)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*60}", file=sys.stderr)
    print("ANALYSIS COMPLETE", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Total files: {total}", file=sys.stderr)
    print(f"ML genre classified: {report['summary']['files_with_ml_genre']}", file=sys.stderr)
    print(f"Duplicates found: {report['summary']['duplicate_groups']} groups", file=sys.stderr)
    print(f"\nTop ML-detected genres:", file=sys.stderr)
    for genre, count in list(report['ml_genres'].items())[:15]:
        print(f"  {genre}: {count}", file=sys.stderr)
    print(f"\nReport saved to: {output_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
