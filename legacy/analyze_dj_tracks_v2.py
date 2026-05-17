#!/usr/bin/env python3
"""
DJ Track Collection Analyzer v2 - Enhanced for Rekordbox
Full ML analysis with genre, energy, mood, timeslot, direction, and vocal detection.

Requirements:
    pip install essentia-tensorflow mutagen

Usage:
    # Phase 1: Analyze tracks and generate comparison report
    python analyze_dj_tracks_v2.py "/mnt/d/Music/Tracks" --output analysis.json

    # Phase 2: Review comparison report (generates readable diff)
    python analyze_dj_tracks_v2.py --review analysis.json --diff-output diff_report.txt

    # Phase 3: Apply tags after review
    python analyze_dj_tracks_v2.py --apply analysis.json

Models auto-download on first run (~500MB total).
"""

# === SUPPRESS WARNINGS BEFORE ANY IMPORTS ===
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # Suppress TensorFlow logging
os.environ['CUDA_VISIBLE_DEVICES'] = ''    # Disable GPU to avoid spam

import warnings
warnings.filterwarnings('ignore')

import sys
import json
import subprocess
import hashlib
import argparse
import urllib.request
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

# Check for essentia
try:
    # Suppress essentia warnings
    import essentia
    essentia.log.infoActive = False
    essentia.log.warningActive = False
    
    import numpy as np
    from essentia.standard import (
        MonoLoader, 
        TensorflowPredictEffnetDiscogs, 
        TensorflowPredict2D,
        TensorflowPredictVGGish,
        RhythmExtractor2013,
        KeyExtractor,
        Energy,
        Loudness,
        DynamicComplexity,
        ZeroCrossingRate,
        SpectralCentroidTime
    )
    ESSENTIA_AVAILABLE = True
except ImportError:
    ESSENTIA_AVAILABLE = False
    print("=" * 60)
    print("Essentia not found. Install with:")
    print("  pip install essentia-tensorflow")
    print("=" * 60)

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, TXXX, TKEY, TBPM, TCON, TIT1
    from mutagen.flac import FLAC
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False
    print("Warning: mutagen not installed. Run: pip install mutagen")

SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.aiff', '.ogg', '.aac'}

# ============================================================
# MODEL DEFINITIONS
# ============================================================

MODEL_BASE_URL = "https://essentia.upf.edu/models"

MODELS = {
    # Feature extractor
    'effnet': {
        'weights': f"{MODEL_BASE_URL}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
        'metadata': f"{MODEL_BASE_URL}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.json"
    },
    # Genre classification (400 classes)
    'genre_discogs400': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json"
    },
    # Danceability
    'danceability': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/danceability/danceability-discogs-effnet-1.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/danceability/danceability-discogs-effnet-1.json"
    },
    # Arousal (energy/intensity)
    'arousal': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/emomusic/emomusic-msd-musicnn-2.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/emomusic/emomusic-msd-musicnn-2.json"
    },
    # Valence (mood positive/negative)
    'valence': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/emomusic/emomusic-msd-musicnn-2.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/emomusic/emomusic-msd-musicnn-2.json"
    },
    # Voice/Instrumental
    'voice_instrumental': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.json"
    },
    # Aggressive
    'aggressive': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/mood_aggressive/mood_aggressive-discogs-effnet-1.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/mood_aggressive/mood_aggressive-discogs-effnet-1.json"
    },
    # Happy
    'happy': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/mood_happy/mood_happy-discogs-effnet-1.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/mood_happy/mood_happy-discogs-effnet-1.json"
    },
    # Relaxed
    'relaxed': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/mood_relaxed/mood_relaxed-discogs-effnet-1.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/mood_relaxed/mood_relaxed-discogs-effnet-1.json"
    },
    # Sad
    'sad': {
        'weights': f"{MODEL_BASE_URL}/classification-heads/mood_sad/mood_sad-discogs-effnet-1.pb",
        'metadata': f"{MODEL_BASE_URL}/classification-heads/mood_sad/mood_sad-discogs-effnet-1.json"
    },
    # MusicNN for arousal/valence
    'musicnn': {
        'weights': f"{MODEL_BASE_URL}/feature-extractors/musicnn/msd-musicnn-1.pb",
        'metadata': f"{MODEL_BASE_URL}/feature-extractors/musicnn/msd-musicnn-1.json"
    },
}

# ============================================================
# GENRE MAPPING AND HIERARCHY
# ============================================================

# Genre hierarchy for aggregation (parent -> subgenres)
GENRE_HIERARCHY = {
    'House': ['Deep House', 'Tech House', 'Electro House', 'Progressive House',
              'Acid House', 'Tribal House', 'Garage House', 'Euro House',
              'Italo House', 'Ghetto House', 'Tropical House', 'Hard House',
              'Hip-House', 'Minimal', 'Speed Garage', 'UK Garage'],
    'Techno': ['Deep Techno', 'Dub Techno', 'Hard Techno', 'Minimal Techno', 'Schranz'],
    'Trance': ['Progressive Trance', 'Psy-Trance', 'Goa Trance', 'Hard Trance',
               'Tech Trance', 'Hands Up'],
    'Drum n Bass': ['Jungle', 'Halftime'],
    'Hardcore': ['Happy Hardcore', 'Gabber', 'Hardstyle', 'Speedcore', 'Jumpstyle', 'Makina'],
    'Breaks': ['Breakbeat', 'Progressive Breaks', 'Big Beat', 'Breakcore'],
    'Dubstep': ['Bassline', 'Grime'],
    'Disco': ['Nu-Disco', 'Euro-Disco', 'Italo-Disco', 'Disco Polo'],
    'Ambient': ['Dark Ambient', 'Drone', 'New Age'],
    'Downtempo': ['Trip Hop', 'Chillwave'],
    'Industrial': ['EBM', 'Power Electronics', 'Rhythmic Noise', 'Noise', 'Neofolk'],
}

# Build reverse lookup: subgenre -> parent
SUBGENRE_TO_PARENT = {}
for parent, children in GENRE_HIERARCHY.items():
    for child in children:
        SUBGENRE_TO_PARENT[child] = parent

# DJ-friendly display names
DJ_GENRE_MAP = {
    # House variants
    'House': 'House', 'Deep House': 'Deep House', 'Tech House': 'Tech House',
    'Electro House': 'Electro House', 'Progressive House': 'Progressive House',
    'Tribal House': 'Tribal House', 'Garage House': 'UK Garage', 'UK Garage': 'UK Garage',
    'Acid House': 'Acid House', 'Euro House': 'Euro House', 'Italo House': 'Italo House',
    'Ghetto House': 'Ghetto House', 'Tropical House': 'Tropical House', 'Hard House': 'Hard House',
    'Hip-House': 'Hip-House', 'Speed Garage': 'Speed Garage', 'Minimal': 'Minimal',
    # Techno variants  
    'Techno': 'Techno', 'Minimal Techno': 'Minimal Techno',
    'Deep Techno': 'Deep Techno', 'Hard Techno': 'Hard Techno', 'Dub Techno': 'Dub Techno',
    'Schranz': 'Hard Techno',
    # Trance variants
    'Trance': 'Trance', 'Progressive Trance': 'Progressive Trance',
    'Psy-Trance': 'Psytrance', 'Goa Trance': 'Psytrance',
    'Hard Trance': 'Hard Trance', 'Tech Trance': 'Tech Trance', 'Hands Up': 'Hands Up',
    # Bass music
    'Drum n Bass': 'Drum & Bass', 'Jungle': 'Jungle', 'Halftime': 'Halftime',
    'Dubstep': 'Dubstep', 'Bassline': 'Bassline', 'Grime': 'Grime',
    'Breaks': 'Breaks', 'Breakbeat': 'Breaks', 'Big Beat': 'Big Beat',
    'Progressive Breaks': 'Progressive Breaks', 'Breakcore': 'Breakcore',
    # Hardcore variants
    'Hardcore': 'Hardcore', 'Happy Hardcore': 'Happy Hardcore', 'Gabber': 'Gabber',
    'Hardstyle': 'Hardstyle', 'Speedcore': 'Hardcore', 'Jumpstyle': 'Jumpstyle', 'Makina': 'Makina',
    # Other electronic
    'Electro': 'Electro', 'Electroclash': 'Electroclash',
    'IDM': 'IDM', 'Leftfield': 'Leftfield',
    'Ambient': 'Ambient', 'Dark Ambient': 'Dark Ambient', 'Drone': 'Drone',
    'Downtempo': 'Downtempo', 'Trip Hop': 'Trip Hop', 'Chillwave': 'Chillwave',
    'Synthwave': 'Synthwave', 'Synth-pop': 'Synth-pop', 'Vaporwave': 'Vaporwave',
    'EBM': 'EBM', 'Industrial': 'Industrial', 'Darkwave': 'Darkwave',
    'Disco': 'Disco', 'Nu-Disco': 'Nu-Disco', 'Euro-Disco': 'Euro-Disco', 'Italo-Disco': 'Italo-Disco',
    'Eurodance': 'Eurodance', 'Eurobeat': 'Eurobeat', 'Italodance': 'Italodance',
    'Dance-pop': 'Dance Pop', 'Hi NRG': 'Hi-NRG',
    'New Beat': 'New Beat', 'New Wave': 'New Wave',
    # Non-electronic (for mixed collections)
    'Hip Hop': 'Hip Hop', 'Trap': 'Trap', 'Bass Music': 'Bass Music', 'Miami Bass': 'Miami Bass',
    'Pop': 'Pop', 'Rock': 'Rock', 'R&B': 'R&B', 'Soul': 'Soul', 'Funk': 'Funk',
    'Reggae': 'Reggae', 'Dancehall': 'Dancehall', 'Dub': 'Dub',
    'Latin': 'Latin', 'Jazz': 'Jazz', 'Blues': 'Blues',
    'K-pop': 'K-Pop', 'J-pop': 'J-Pop',
}


def parse_discogs_genre(raw_genre):
    """Parse Discogs format 'Electronic---Tech House' into (parent, subgenre)."""
    if not raw_genre:
        return None, None
    
    parts = raw_genre.split('---')
    if len(parts) >= 2:
        # e.g., "Electronic---Tech House" -> ("Electronic", "Tech House")
        parent_category = parts[0]  # "Electronic", "Hip Hop", "Rock", etc.
        subgenre = parts[-1]  # Most specific level
        return parent_category, subgenre
    else:
        return raw_genre, raw_genre


def get_best_genre(predictions, genre_classes, min_confidence=0.05):
    """
    Improved genre detection with aggregation and hierarchy awareness.
    
    Returns: dict with genre, subgenre, confidence, all_predictions
    """
    if len(predictions) == 0:
        return {'genre': 'Unknown', 'subgenre': 'Unknown', 'confidence': 0, 'all_predictions': []}
    
    # Get top 15 predictions
    top_indices = np.argsort(predictions)[::-1][:15]
    
    # Build prediction list with parsed genres
    top_predictions = []
    for idx in top_indices:
        raw_genre = genre_classes[idx]
        confidence = float(predictions[idx])
        if confidence < min_confidence:
            continue
        
        parent_category, subgenre = parse_discogs_genre(raw_genre)
        dj_name = DJ_GENRE_MAP.get(subgenre, subgenre)
        
        top_predictions.append({
            'raw': raw_genre,
            'parent_category': parent_category,
            'subgenre': subgenre,
            'dj_name': dj_name,
            'confidence': confidence,
        })
    
    if not top_predictions:
        return {'genre': 'Unknown', 'subgenre': 'Unknown', 'confidence': 0, 'all_predictions': []}
    
    # Get the top prediction's info
    top_pred = top_predictions[0]
    top_subgenre = top_pred['subgenre']
    top_parent_category = top_pred['parent_category']
    
    # Determine the DJ-relevant parent genre
    if top_subgenre in SUBGENRE_TO_PARENT:
        top_dj_parent = SUBGENRE_TO_PARENT[top_subgenre]
    elif top_subgenre in GENRE_HIERARCHY:
        top_dj_parent = top_subgenre
    else:
        # For non-electronic genres, use subgenre as-is
        top_dj_parent = top_subgenre
    
    # Aggregate confidence for related genres
    family_confidence = 0.0
    best_specific_subgenre = top_subgenre
    best_specific_confidence = top_pred['confidence']
    
    # Define what's "related" to the top prediction
    related_subgenres = set()
    if top_dj_parent in GENRE_HIERARCHY:
        related_subgenres = set(GENRE_HIERARCHY[top_dj_parent])
        related_subgenres.add(top_dj_parent)
    else:
        related_subgenres = {top_subgenre}
    
    # Also consider same parent_category (Electronic, Hip Hop, etc.) for certain genres
    for pred in top_predictions:
        subgenre = pred['subgenre']
        conf = pred['confidence']
        
        # Check if this prediction is related to top prediction
        is_related = False
        
        # Same subgenre
        if subgenre == top_subgenre:
            is_related = True
        # Part of same DJ genre family
        elif subgenre in related_subgenres:
            is_related = True
        # Same parent category and subgenre is in our DJ hierarchy
        elif pred['parent_category'] == top_parent_category and subgenre in SUBGENRE_TO_PARENT:
            # Check if they share the same DJ parent
            pred_dj_parent = SUBGENRE_TO_PARENT.get(subgenre, subgenre)
            if pred_dj_parent == top_dj_parent:
                is_related = True
        
        if is_related:
            family_confidence += conf
            # Track most specific subgenre with good confidence
            if subgenre != top_dj_parent and subgenre in related_subgenres:
                if conf > best_specific_confidence * 0.5:  # At least 50% of top confidence
                    if conf > best_specific_confidence:
                        best_specific_subgenre = subgenre
                        best_specific_confidence = conf
    
    # If top prediction is already specific, keep it
    if top_subgenre in SUBGENRE_TO_PARENT:
        best_specific_subgenre = top_subgenre
    
    # Get DJ-friendly names
    dj_genre = DJ_GENRE_MAP.get(top_dj_parent, top_dj_parent)
    dj_subgenre = DJ_GENRE_MAP.get(best_specific_subgenre, best_specific_subgenre)
    
    # For non-electronic genres where subgenre == genre, don't duplicate
    if dj_genre == dj_subgenre and top_parent_category not in ['Electronic']:
        # Use the parent category for genre (e.g., "Funk / Soul" -> "Funk")
        if top_parent_category == 'Funk / Soul':
            dj_genre = 'Funk/Soul'
        elif top_parent_category == 'Hip Hop':
            dj_genre = 'Hip Hop'
        elif top_parent_category == 'Rock':
            dj_genre = 'Rock'
        elif top_parent_category == 'Pop':
            dj_genre = 'Pop'
        elif top_parent_category == 'Reggae':
            dj_genre = 'Reggae'
    
    # Calculate combined confidence (capped at 1.0)
    combined_confidence = min(family_confidence, 1.0)
    
    return {
        'genre': dj_genre,
        'subgenre': dj_subgenre,
        'confidence': round(combined_confidence, 3),
        'raw_confidence': round(top_pred['confidence'], 3),
        'all_predictions': top_predictions[:5],  # Top 5 for debugging
    }

# Standard key to Camelot mapping
KEY_TO_CAMELOT = {
    'C major': '8B', 'G major': '9B', 'D major': '10B', 'A major': '11B',
    'E major': '12B', 'B major': '1B', 'F# major': '2B', 'Gb major': '2B',
    'Db major': '3B', 'C# major': '3B', 'Ab major': '4B', 'G# major': '4B',
    'Eb major': '5B', 'D# major': '5B', 'Bb major': '6B', 'A# major': '6B',
    'F major': '7B',
    'A minor': '8A', 'E minor': '9A', 'B minor': '10A', 'F# minor': '11A',
    'Gb minor': '11A', 'C# minor': '12A', 'Db minor': '12A', 'G# minor': '1A',
    'Ab minor': '1A', 'D# minor': '2A', 'Eb minor': '2A', 'A# minor': '3A',
    'Bb minor': '3A', 'F minor': '4A', 'C minor': '5A', 'G minor': '6A',
    'D minor': '7A',
}

# Shorthand conversions
KEY_SHORTHAND = {
    'C': 'C major', 'Cm': 'C minor', 'C#': 'C# major', 'C#m': 'C# minor',
    'Db': 'Db major', 'Dbm': 'Db minor', 'D': 'D major', 'Dm': 'D minor',
    'D#': 'D# major', 'D#m': 'D# minor', 'Eb': 'Eb major', 'Ebm': 'Eb minor',
    'E': 'E major', 'Em': 'E minor', 'F': 'F major', 'Fm': 'F minor',
    'F#': 'F# major', 'F#m': 'F# minor', 'Gb': 'Gb major', 'Gbm': 'Gb minor',
    'G': 'G major', 'Gm': 'G minor', 'G#': 'G# major', 'G#m': 'G# minor',
    'Ab': 'Ab major', 'Abm': 'Ab minor', 'A': 'A major', 'Am': 'A minor',
    'A#': 'A# major', 'A#m': 'A# minor', 'Bb': 'Bb major', 'Bbm': 'Bb minor',
    'B': 'B major', 'Bm': 'B minor',
}

def key_to_camelot(key_str):
    """Convert any key format to Camelot notation."""
    if not key_str:
        return None
    
    key_str = str(key_str).strip()
    
    # Already Camelot?
    if re.match(r'^[1-9]|1[0-2][AB]$', key_str.upper()):
        return key_str.upper()
    
    # Try direct lookup
    if key_str in KEY_TO_CAMELOT:
        return KEY_TO_CAMELOT[key_str]
    
    # Try shorthand
    if key_str in KEY_SHORTHAND:
        return KEY_TO_CAMELOT.get(KEY_SHORTHAND[key_str])
    
    # Parse "C# min" / "Db maj" style
    match = re.match(r'^([A-Ga-g][#b]?)\s*(maj|min|major|minor|m)?', key_str, re.I)
    if match:
        note = match.group(1).capitalize()
        mode = match.group(2)
        if mode and mode.lower() in ('min', 'minor', 'm'):
            full_key = f"{note} minor"
        else:
            full_key = f"{note} major"
        return KEY_TO_CAMELOT.get(full_key)
    
    return None


# ============================================================
# MODEL DOWNLOAD AND LOADING
# ============================================================

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
            try:
                urllib.request.urlretrieve(urls['weights'], weights_path)
            except Exception as e:
                print(f"  Warning: Could not download {model_name}: {e}", file=sys.stderr)
                continue
        
        if not metadata_path.exists():
            print(f"Downloading {model_name} metadata...", file=sys.stderr)
            try:
                urllib.request.urlretrieve(urls['metadata'], metadata_path)
            except Exception as e:
                print(f"  Warning: Could not download {model_name} metadata: {e}", file=sys.stderr)
        
        models_downloaded[model_name] = {
            'weights': str(weights_path),
            'metadata': str(metadata_path)
        }
        
        # Load class labels from metadata
        if metadata_path.exists():
            with open(metadata_path) as f:
                meta = json.load(f)
                if 'classes' in meta:
                    models_downloaded[model_name]['classes'] = meta['classes']
    
    return models_downloaded


def load_models(model_dir):
    """Load all ML models for analysis."""
    models = download_models(model_dir)
    loaded = {}
    
    # Effnet embedding extractor
    if 'effnet' in models:
        loaded['effnet'] = TensorflowPredictEffnetDiscogs(
            graphFilename=models['effnet']['weights'],
            output="PartitionedCall:1"
        )
    
    # Genre classifier (uses serving_default input/output)
    if 'genre_discogs400' in models:
        loaded['genre'] = TensorflowPredict2D(
            graphFilename=models['genre_discogs400']['weights'],
            input="serving_default_model_Placeholder",
            output="PartitionedCall:0"
        )
        loaded['genre_classes'] = models['genre_discogs400'].get('classes', [])
    
    # Helper to try loading a model with different node name patterns
    def try_load_model(model_name, model_info):
        """Try different input/output node patterns for model loading."""
        weights = model_info['weights']
        
        # Common node name patterns used by Essentia models
        node_patterns = [
            ("serving_default_model_Placeholder", "PartitionedCall:0"),
            ("model/Placeholder", "model/Softmax"),
            ("model/Placeholder", "model/Sigmoid"),
            ("Placeholder", "Softmax"),
            ("Placeholder", "Sigmoid"),
        ]
        
        for input_node, output_node in node_patterns:
            try:
                model = TensorflowPredict2D(
                    graphFilename=weights,
                    input=input_node,
                    output=output_node
                )
                print(f"  Loaded {model_name} with {input_node} -> {output_node}", file=sys.stderr)
                return model
            except RuntimeError:
                continue
        
        print(f"  Warning: Could not load {model_name}", file=sys.stderr)
        return None
    
    # Danceability
    if 'danceability' in models:
        loaded['danceability'] = try_load_model('danceability', models['danceability'])
    
    # Voice/Instrumental
    if 'voice_instrumental' in models:
        loaded['voice_instrumental'] = try_load_model('voice_instrumental', models['voice_instrumental'])
    
    # Mood models
    for mood in ['aggressive', 'happy', 'relaxed', 'sad']:
        if mood in models:
            model = try_load_model(mood, models[mood])
            if model:
                loaded[mood] = model
    
    return loaded


# ============================================================
# AUDIO ANALYSIS
# ============================================================

def analyze_audio_features(filepath, models):
    """Comprehensive audio analysis using Essentia models."""
    results = {
        'ml_genre': None,
        'ml_subgenre': None,
        'ml_genre_confidence': None,
        'ml_genre_raw_confidence': None,
        'ml_genre_predictions': None,
        'ml_bpm': None,
        'ml_key': None,
        'ml_energy': None,
        'ml_mood': None,
        'ml_timeslot': None,
        'ml_direction': None,
        'ml_vocal': None,
        'ml_danceability': None,
    }
    
    try:
        # Load audio at 16kHz for ML models
        audio_16k = MonoLoader(filename=str(filepath), sampleRate=16000, resampleQuality=4)()
        
        # Load audio at 44.1kHz for rhythm/key analysis
        audio_44k = MonoLoader(filename=str(filepath), sampleRate=44100, resampleQuality=4)()
        
        # Get embeddings
        if 'effnet' not in models:
            return results
        
        embeddings = models['effnet'](audio_16k)
        
        # ----- Genre Classification (Improved) -----
        if 'genre' in models and 'genre_classes' in models:
            predictions = models['genre'](embeddings)
            avg_pred = np.mean(predictions, axis=0)
            
            # Use improved genre detection
            genre_result = get_best_genre(avg_pred, models['genre_classes'])
            
            results['ml_genre'] = genre_result['genre']
            results['ml_subgenre'] = genre_result['subgenre']
            results['ml_genre_confidence'] = genre_result['confidence']
            results['ml_genre_raw_confidence'] = genre_result.get('raw_confidence')
            results['ml_genre_predictions'] = genre_result.get('all_predictions')
        
        # ----- BPM Detection -----
        try:
            rhythm_extractor = RhythmExtractor2013(method="multifeature")
            bpm, beats, beats_confidence, _, _ = rhythm_extractor(audio_44k)
            results['ml_bpm'] = round(float(bpm), 1)
        except Exception as e:
            pass
        
        # ----- Key Detection -----
        try:
            key_extractor = KeyExtractor()
            key, scale, strength = key_extractor(audio_44k)
            full_key = f"{key} {scale}"
            results['ml_key'] = key_to_camelot(full_key)
        except Exception as e:
            pass
        
        # ----- Danceability -----
        if 'danceability' in models and models['danceability'] is not None:
            try:
                pred = models['danceability'](embeddings)
                # Average across frames, take positive class (index 0 = danceable)
                avg_pred = np.mean(pred, axis=0)
                danceability = float(avg_pred[0]) if len(avg_pred) > 1 else float(np.mean(avg_pred))
                results['ml_danceability'] = round(danceability, 3)
            except:
                pass
        
        # ----- Voice/Instrumental -----
        if 'voice_instrumental' in models and models['voice_instrumental'] is not None:
            try:
                pred = models['voice_instrumental'](embeddings)
                avg_pred = np.mean(pred, axis=0)
                # [instrumental, voice] typically
                voice_score = float(avg_pred[1]) if len(avg_pred) > 1 else float(avg_pred[0])
                
                if voice_score < 0.3:
                    results['ml_vocal'] = 'Instrumental'
                elif voice_score < 0.6:
                    results['ml_vocal'] = 'Light Vocal'
                else:
                    results['ml_vocal'] = 'Vocal'
            except:
                results['ml_vocal'] = 'Unknown'
        
        # ----- Mood Analysis -----
        mood_scores = {}
        
        # Class order differs by model! Check metadata:
        # aggressive: ['aggressive', 'not_aggressive'] -> index 0 = aggressive
        # happy: ['happy', 'non_happy'] -> index 0 = happy  
        # relaxed: ['non_relaxed', 'relaxed'] -> index 1 = relaxed
        # sad: ['non_sad', 'sad'] -> index 1 = sad
        
        mood_index_map = {
            'aggressive': 0,  # index 0 = aggressive
            'happy': 0,       # index 0 = happy
            'relaxed': 1,     # index 1 = relaxed (inverted!)
            'sad': 1,         # index 1 = sad (inverted!)
        }
        
        for mood in ['aggressive', 'happy', 'relaxed', 'sad']:
            if mood in models and models[mood] is not None:
                try:
                    pred = models[mood](embeddings)
                    # Average across frames
                    avg_pred = np.mean(pred, axis=0)
                    # Get the correct index for positive class
                    idx = mood_index_map[mood]
                    mood_scores[mood] = float(avg_pred[idx]) if len(avg_pred) > 1 else float(avg_pred)
                except Exception as e:
                    pass
        
        if mood_scores:
            # Get individual scores (now correctly oriented)
            aggressive = mood_scores.get('aggressive', 0.5)
            relaxed = mood_scores.get('relaxed', 0.5)
            happy = mood_scores.get('happy', 0.5)
            sad = mood_scores.get('sad', 0.5)
            
            # Calculate composite energy (0-5 scale)
            # High energy = aggressive, not relaxed, high danceability
            energy_raw = (
                aggressive * 0.35 +          # Aggressive contributes to energy
                (1 - relaxed) * 0.35 +       # Not relaxed = more energy
                happy * 0.15 +               # Happy adds some energy
                (1 - sad) * 0.15             # Not sad adds some energy
            )
            energy = int(round(energy_raw * 5))
            energy = max(0, min(5, energy))
            results['ml_energy'] = energy
            
            # Mood: Dark / Neutral / Bright based on emotional valence
            # Bright = happy, not sad, not aggressive
            # Dark = sad, aggressive, not happy
            brightness = (
                happy * 0.4 +
                (1 - sad) * 0.3 +
                (1 - aggressive) * 0.3
            )
            
            if brightness < 0.4:
                results['ml_mood'] = 'Dark'
            elif brightness > 0.6:
                results['ml_mood'] = 'Bright'
            else:
                results['ml_mood'] = 'Neutral'
            
            # Store raw scores for debugging
            results['ml_mood_scores'] = {
                'aggressive': round(aggressive, 3),
                'happy': round(happy, 3),
                'relaxed': round(relaxed, 3),
                'sad': round(sad, 3),
            }
        else:
            # Fallback based on genre if mood models didn't work
            genre = results.get('ml_genre', '')
            if genre in ['Techno', 'Hard Techno', 'Industrial', 'EBM', 'Hardcore', 'Gabber']:
                results['ml_energy'] = 4
                results['ml_mood'] = 'Dark'
            elif genre in ['Deep House', 'Ambient', 'Downtempo', 'Chillout']:
                results['ml_energy'] = 2
                results['ml_mood'] = 'Neutral'
            elif genre in ['Trance', 'Psytrance', 'Happy Hardcore', 'Eurodance']:
                results['ml_energy'] = 4
                results['ml_mood'] = 'Bright'
            else:
                results['ml_energy'] = 3
                results['ml_mood'] = 'Neutral'
        
        # ----- Time Slot Mapping -----
        # Based on energy, mood, BPM, and genre
        energy = results.get('ml_energy', 3)
        mood = results.get('ml_mood', 'Neutral')
        bpm = results.get('ml_bpm', 128)
        genre = results.get('ml_genre', '')
        
        # Genre-based hints
        if genre in ['Ambient', 'Downtempo', 'Chillout', 'Trip Hop']:
            results['ml_timeslot'] = 'Opener' if energy <= 2 else 'Afterhours'
        elif genre in ['Hardcore', 'Gabber', 'Hardstyle', 'Hard Techno']:
            results['ml_timeslot'] = 'Peak'
        elif energy <= 1:
            results['ml_timeslot'] = 'Afterhours' if mood == 'Dark' else 'Opener'
        elif energy <= 2:
            results['ml_timeslot'] = 'Opener'
        elif energy <= 3:
            results['ml_timeslot'] = 'Warm-Up'
        elif energy >= 4:
            results['ml_timeslot'] = 'Peak'
        else:
            results['ml_timeslot'] = 'Warm-Up'
        
        # Adjust for BPM extremes
        if bpm and bpm < 110:
            if results['ml_timeslot'] == 'Peak':
                results['ml_timeslot'] = 'Warm-Up'
        elif bpm and bpm > 145:
            if results['ml_timeslot'] in ['Opener', 'Warm-Up'] and energy >= 3:
                results['ml_timeslot'] = 'Peak'
        
        # ----- Direction (energy curve) -----
        try:
            # Split embeddings into thirds
            third = len(embeddings) // 3
            if third > 0 and 'aggressive' in models and models['aggressive'] is not None:
                pred_full = models['aggressive'](embeddings)
                
                start_energy = float(np.mean(pred_full[:third]))
                end_energy = float(np.mean(pred_full[-third:]))
                
                diff = end_energy - start_energy
                if diff > 0.08:
                    results['ml_direction'] = 'Up'
                elif diff < -0.08:
                    results['ml_direction'] = 'Down'
                else:
                    results['ml_direction'] = 'Steady'
            else:
                results['ml_direction'] = 'Steady'
        except:
            results['ml_direction'] = 'Steady'
        
    except Exception as e:
        results['ml_error'] = str(e)
    
    return results


# ============================================================
# METADATA EXTRACTION
# ============================================================

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


def get_existing_tags(filepath):
    """Extract all existing tags from audio file."""
    tags = {
        'artist': None,
        'title': None,
        'album': None,
        'genre': None,
        'bpm': None,
        'key': None,
        'energy': None,
        'mood': None,
        'timeslot': None,
        'direction': None,
        'vocal': None,
        'subgenre': None,
    }
    
    if not MUTAGEN_AVAILABLE:
        return tags
    
    try:
        audio = MutagenFile(filepath, easy=True)
        if audio and hasattr(audio, 'tags') and audio.tags:
            easy_tags = audio.tags
            
            # Standard tags
            for field, keys in [
                ('artist', ['artist', 'albumartist']),
                ('title', ['title']),
                ('album', ['album']),
                ('genre', ['genre']),
            ]:
                for key in keys:
                    val = easy_tags.get(key)
                    if val:
                        tags[field] = val[0] if isinstance(val, list) else str(val)
                        break
            
            # BPM
            for key in ['bpm', 'TBPM']:
                val = easy_tags.get(key)
                if val:
                    try:
                        tags['bpm'] = int(float(str(val[0] if isinstance(val, list) else val)))
                    except:
                        pass
                    break
            
            # Key
            for key in ['initialkey', 'key', 'TKEY']:
                val = easy_tags.get(key)
                if val:
                    tags['key'] = val[0] if isinstance(val, list) else str(val)
                    break
        
        # Also check ID3 TXXX frames for custom tags (MP3 only)
        if filepath.suffix.lower() == '.mp3':
            try:
                id3 = ID3(filepath)
                for txxx in id3.getall('TXXX'):
                    desc = txxx.desc.upper()
                    if desc == 'ENERGY':
                        try:
                            tags['energy'] = int(txxx.text[0])
                        except:
                            tags['energy'] = txxx.text[0]
                    elif desc == 'MOOD':
                        tags['mood'] = txxx.text[0]
                    elif desc == 'TIMESLOT':
                        tags['timeslot'] = txxx.text[0]
                    elif desc == 'DIRECTION':
                        tags['direction'] = txxx.text[0]
                    elif desc == 'VOCAL':
                        tags['vocal'] = txxx.text[0]
                
                # TIT1 for subgenre/content group
                tit1 = id3.get('TIT1')
                if tit1:
                    tags['subgenre'] = tit1.text[0]
            except:
                pass
        
        # FLAC custom tags
        elif filepath.suffix.lower() == '.flac':
            try:
                flac = FLAC(filepath)
                tags['energy'] = flac.get('ENERGY', [None])[0]
                tags['mood'] = flac.get('MOOD', [None])[0]
                tags['timeslot'] = flac.get('TIMESLOT', [None])[0]
                tags['direction'] = flac.get('DIRECTION', [None])[0]
                tags['vocal'] = flac.get('VOCAL', [None])[0]
                tags['subgenre'] = flac.get('CONTENTGROUP', [None])[0] or flac.get('GROUPING', [None])[0]
            except:
                pass
                
    except Exception as e:
        tags['_error'] = str(e)
    
    return tags


def get_metadata(filepath, models=None):
    """Extract metadata and perform ML analysis."""
    metadata = {
        'path': str(filepath),
        'filename': filepath.name,
        'extension': filepath.suffix.lower(),
        'size_mb': round(filepath.stat().st_size / (1024 * 1024), 2),
    }
    
    # Filename info
    fn_info = extract_from_filename(filepath.name)
    metadata.update(fn_info)
    
    # Existing tags
    existing = get_existing_tags(filepath)
    metadata['existing_tags'] = existing
    
    # ML analysis
    if models:
        ml_results = analyze_audio_features(filepath, models)
        metadata['ml_analysis'] = ml_results
    
    return metadata


# ============================================================
# COMPARISON AND DIFF GENERATION
# ============================================================

def generate_comparison(track_data):
    """Generate before/after comparison for a single track."""
    existing = track_data.get('existing_tags', {})
    ml = track_data.get('ml_analysis', {})
    
    comparison = {
        'path': track_data['path'],
        'filename': track_data['filename'],
        'changes': [],
        'new_values': {},
    }
    
    # Define tag mappings: (display_name, existing_key, ml_key)
    tag_mappings = [
        ('GENRE', 'genre', 'ml_genre'),
        ('SUBGENRE', 'subgenre', 'ml_subgenre'),
        ('BPM', 'bpm', 'ml_bpm'),
        ('KEY', 'key', 'ml_key'),
        ('ENERGY', 'energy', 'ml_energy'),
        ('MOOD', 'mood', 'ml_mood'),
        ('TIMESLOT', 'timeslot', 'ml_timeslot'),
        ('DIRECTION', 'direction', 'ml_direction'),
        ('VOCAL', 'vocal', 'ml_vocal'),
    ]
    
    for display_name, existing_key, ml_key in tag_mappings:
        old_val = existing.get(existing_key)
        new_val = ml.get(ml_key)
        
        if new_val is not None:
            comparison['new_values'][display_name] = new_val
            
            # Check if value changed
            old_str = str(old_val) if old_val is not None else '[empty]'
            new_str = str(new_val)
            
            if old_val != new_val:
                confidence = ''
                if ml_key == 'ml_genre':
                    conf = ml.get('ml_genre_confidence')
                    if conf:
                        confidence = f" ({int(conf*100)}% conf)"
                
                comparison['changes'].append({
                    'field': display_name,
                    'old': old_str,
                    'new': new_str,
                    'confidence': confidence,
                })
    
    return comparison


def generate_diff_report(analysis_data, output_path):
    """Generate human-readable diff report."""
    tracks = analysis_data.get('tracks', [])
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write("DJ TRACK ANALYSIS - COMPARISON REPORT\n")
        f.write("=" * 80 + "\n\n")
        
        f.write(f"Total tracks: {len(tracks)}\n")
        
        # Count changes
        tracks_with_changes = 0
        total_changes = 0
        
        for track in tracks:
            comp = generate_comparison(track)
            if comp['changes']:
                tracks_with_changes += 1
                total_changes += len(comp['changes'])
        
        f.write(f"Tracks with changes: {tracks_with_changes}\n")
        f.write(f"Total tag changes: {total_changes}\n\n")
        
        f.write("-" * 80 + "\n\n")
        
        # Detailed changes per track
        for track in tracks:
            comp = generate_comparison(track)
            
            if not comp['changes']:
                continue
            
            f.write(f"Track: {comp['filename']}\n")
            f.write("-" * 60 + "\n")
            
            for change in comp['changes']:
                f.write(f"  {change['field']:12} [{change['old']:20}] → [{change['new']:20}]{change['confidence']}\n")
            
            f.write("\n")
        
        f.write("=" * 80 + "\n")
        f.write("END OF REPORT\n")
        f.write("=" * 80 + "\n")
    
    return tracks_with_changes, total_changes


# ============================================================
# TAG WRITING
# ============================================================

def apply_tags(filepath, new_values):
    """Apply new tag values to audio file.
    
    IMPORTANT: Explicitly preserves GEOB (General Encapsulated Object) frames
    which Rekordbox uses to store cue points, beat grids, and other data.
    """
    if not MUTAGEN_AVAILABLE:
        return False, "Mutagen not available"
    
    filepath = Path(filepath)
    ext = filepath.suffix.lower()
    
    try:
        if ext == '.mp3':
            try:
                id3 = ID3(filepath)
            except:
                id3 = ID3()
                id3.save(filepath)
                id3 = ID3(filepath)
            
            # === PRESERVE GEOB FRAMES (Rekordbox data) ===
            # Capture all GEOB frames before any modifications
            geob_frames = []
            for key in list(id3.keys()):
                if key.startswith('GEOB:'):
                    geob_frames.append((key, id3[key]))
            
            # Also preserve PRIV frames (private data used by some software)
            priv_frames = []
            for key in list(id3.keys()):
                if key.startswith('PRIV:'):
                    priv_frames.append((key, id3[key]))
            
            # === APPLY NEW TAGS ===
            # Genre
            if 'GENRE' in new_values:
                id3.delall('TCON')
                id3.add(TCON(encoding=3, text=[new_values['GENRE']]))
            
            # Subgenre (Content Group)
            if 'SUBGENRE' in new_values:
                id3.delall('TIT1')
                id3.add(TIT1(encoding=3, text=[new_values['SUBGENRE']]))
            
            # BPM
            if 'BPM' in new_values:
                id3.delall('TBPM')
                id3.add(TBPM(encoding=3, text=[str(int(round(new_values['BPM'])))]))
            
            # Key
            if 'KEY' in new_values and new_values['KEY']:
                id3.delall('TKEY')
                id3.add(TKEY(encoding=3, text=[new_values['KEY']]))
            
            # Custom TXXX tags
            for tag_name in ['ENERGY', 'MOOD', 'TIMESLOT', 'DIRECTION', 'VOCAL']:
                if tag_name in new_values:
                    # Remove existing
                    id3.delall(f'TXXX:{tag_name}')
                    # Add new
                    id3.add(TXXX(encoding=3, desc=tag_name, text=[str(new_values[tag_name])]))
            
            # === VERIFY GEOB FRAMES ARE STILL PRESENT ===
            # Re-add any GEOB frames that may have been lost (shouldn't happen, but safety first)
            for key, frame in geob_frames:
                if key not in id3:
                    id3.add(frame)
            
            # Re-add any PRIV frames
            for key, frame in priv_frames:
                if key not in id3:
                    id3.add(frame)
            
            id3.save(filepath)
            return True, None
            
        elif ext == '.flac':
            flac = FLAC(filepath)
            
            if 'GENRE' in new_values:
                flac['GENRE'] = new_values['GENRE']
            if 'SUBGENRE' in new_values:
                flac['CONTENTGROUP'] = new_values['SUBGENRE']
            if 'BPM' in new_values:
                flac['BPM'] = str(int(round(new_values['BPM'])))
            if 'KEY' in new_values and new_values['KEY']:
                flac['INITIALKEY'] = new_values['KEY']
            
            for tag_name in ['ENERGY', 'MOOD', 'TIMESLOT', 'DIRECTION', 'VOCAL']:
                if tag_name in new_values:
                    flac[tag_name] = str(new_values[tag_name])
            
            flac.save()
            return True, None
            
        else:
            # For other formats, use easy tags where possible
            audio = MutagenFile(filepath, easy=True)
            if audio is None:
                return False, "Unsupported format"
            
            if 'GENRE' in new_values:
                audio['genre'] = new_values['GENRE']
            if 'BPM' in new_values:
                audio['bpm'] = str(int(round(new_values['BPM'])))
            
            audio.save()
            return True, "Partial (custom tags not supported for this format)"
            
    except Exception as e:
        return False, str(e)


# ============================================================
# PARALLEL PROCESSING WORKERS
# ============================================================

# Global variable for models in worker processes
_worker_models = None
_worker_model_dir = None

def _init_worker(model_dir):
    """Initialize models in worker process."""
    global _worker_models, _worker_model_dir
    _worker_model_dir = model_dir
    _worker_models = load_models(model_dir)

def _process_file(filepath_str):
    """Process a single file in worker process."""
    global _worker_models
    filepath = Path(filepath_str)
    try:
        result = get_metadata(filepath, _worker_models)
        return result
    except Exception as e:
        return {
            'path': filepath_str,
            'filename': filepath.name,
            'error': str(e)
        }


def _save_partial_report(output_path, results, total, in_progress):
    """Save partial report to JSON file."""
    ml_genres = defaultdict(int)
    energy_dist = defaultdict(int)
    timeslot_dist = defaultdict(int)
    mood_dist = defaultdict(int)
    
    for r in results:
        ml = r.get('ml_analysis', {})
        if ml.get('ml_genre'):
            ml_genres[ml['ml_genre']] += 1
        if ml.get('ml_energy') is not None:
            energy_dist[ml['ml_energy']] += 1
        if ml.get('ml_timeslot'):
            timeslot_dist[ml['ml_timeslot']] += 1
        if ml.get('ml_mood'):
            mood_dist[ml['ml_mood']] += 1
    
    partial_report = {
        'status': 'in_progress' if in_progress else 'complete',
        'summary': {
            'total_files': total,
            'analyzed': len([r for r in results if 'ml_analysis' in r]),
            'errors': len([r for r in results if 'error' in r]),
        },
        'statistics': {
            'genres': dict(sorted(ml_genres.items(), key=lambda x: -x[1])),
            'energy_distribution': dict(sorted(energy_dist.items())),
            'timeslot_distribution': dict(timeslot_dist),
            'mood_distribution': dict(mood_dist),
        },
        'tracks': results,
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(partial_report, f, indent=2, ensure_ascii=False)


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='DJ Track Analyzer v2 - Enhanced for Rekordbox',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Analyze tracks:
    python analyze_dj_tracks_v2.py "/mnt/d/Music/Tracks" -o analysis.json

  Generate diff report:
    python analyze_dj_tracks_v2.py --review analysis.json --diff-output diff.txt

  Apply tags after review:
    python analyze_dj_tracks_v2.py --apply analysis.json
        """
    )
    
    parser.add_argument('directory', nargs='?', help='Path to music directory')
    parser.add_argument('--output', '-o', default='dj_analysis.json', help='Output JSON file')
    parser.add_argument('--review', metavar='JSON', help='Review existing analysis file')
    parser.add_argument('--diff-output', metavar='FILE', help='Output diff report to file')
    parser.add_argument('--apply', metavar='JSON', help='Apply tags from analysis file')
    parser.add_argument('--workers', type=int, default=1, help='Number of parallel workers (default: 1)')
    parser.add_argument('--model-dir', default='./essentia_models', help='Directory for ML models')
    parser.add_argument('--limit', type=int, help='Limit number of files to process (for testing)')
    parser.add_argument('--skip', type=int, default=0, help='Skip first N files (for parallel runs)')
    
    args = parser.parse_args()
    
    # Mode: Review existing analysis
    if args.review:
        print(f"Loading analysis from {args.review}...", file=sys.stderr)
        with open(args.review, 'r', encoding='utf-8') as f:
            analysis = json.load(f)
        
        diff_output = args.diff_output or args.review.replace('.json', '_diff.txt')
        tracks_changed, total_changes = generate_diff_report(analysis, diff_output)
        
        print(f"\nDiff report saved to: {diff_output}", file=sys.stderr)
        print(f"Tracks with changes: {tracks_changed}", file=sys.stderr)
        print(f"Total tag changes: {total_changes}", file=sys.stderr)
        return
    
    # Mode: Apply tags
    if args.apply:
        print(f"Loading analysis from {args.apply}...", file=sys.stderr)
        with open(args.apply, 'r', encoding='utf-8') as f:
            analysis = json.load(f)
        
        tracks = analysis.get('tracks', [])
        success_count = 0
        error_count = 0
        
        for i, track in enumerate(tracks):
            comp = generate_comparison(track)
            if not comp['new_values']:
                continue
            
            success, error = apply_tags(track['path'], comp['new_values'])
            if success:
                success_count += 1
            else:
                error_count += 1
                print(f"Error on {track['filename']}: {error}", file=sys.stderr)
            
            if (i + 1) % 100 == 0:
                print(f"Progress: {i+1}/{len(tracks)}", file=sys.stderr)
        
        print(f"\nTags applied successfully: {success_count}", file=sys.stderr)
        print(f"Errors: {error_count}", file=sys.stderr)
        return
    
    # Mode: Analyze
    if not args.directory:
        parser.print_help()
        sys.exit(1)
    
    if not ESSENTIA_AVAILABLE:
        print("Essentia not available. Install with: pip install essentia-tensorflow")
        sys.exit(1)
    
    music_path = Path(args.directory)
    if not music_path.exists():
        print(f"Error: Directory not found: {music_path}", file=sys.stderr)
        sys.exit(1)
    
    # Find all audio files
    print(f"Scanning {music_path}...", file=sys.stderr)
    audio_files = []
    for ext in SUPPORTED_EXTENSIONS:
        audio_files.extend(music_path.rglob(f'*{ext}'))
        audio_files.extend(music_path.rglob(f'*{ext.upper()}'))
    
    audio_files = sorted(set(audio_files), key=lambda x: str(x))  # Sort for consistent ordering
    
    # Apply skip first, then limit
    if args.skip:
        audio_files = audio_files[args.skip:]
    if args.limit:
        audio_files = audio_files[:args.limit]
    
    total = len(audio_files)
    print(f"Found {total} audio files to process", file=sys.stderr)
    
    if total == 0:
        print("No audio files found!", file=sys.stderr)
        sys.exit(1)
    
    # Analyze files with incremental saving
    output_path = Path(args.output)
    results = []
    
    # Create initial empty JSON file
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({'status': 'in_progress', 'tracks': []}, f)
    
    num_workers = args.workers
    
    # Load models once (shared across threads)
    print("Loading ML models (this may take a moment on first run)...", file=sys.stderr)
    models = load_models(args.model_dir)
    print(f"Loaded {len(models)} models", file=sys.stderr)
    
    if num_workers > 1:
        # Parallel processing mode with threads
        print(f"Analyzing tracks with {num_workers} threads...", file=sys.stderr)
        sys.stderr.flush()
        
        import threading
        results_lock = threading.Lock()
        completed = [0]  # Use list for mutable counter in closure
        
        def process_file(filepath):
            """Process a single file."""
            try:
                result = get_metadata(filepath, models)
                return result
            except Exception as e:
                return {
                    'path': str(filepath),
                    'filename': filepath.name,
                    'error': str(e)
                }
        
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            # Submit all tasks
            future_to_path = {executor.submit(process_file, fp): fp for fp in audio_files}
            
            for future in as_completed(future_to_path):
                result = future.result()
                
                with results_lock:
                    results.append(result)
                    completed[0] += 1
                    count = completed[0]
                
                # Progress indicator
                pct = 100 * count // total
                fname = Path(result.get('path', '')).name[:40] if result.get('path') else '?'
                print(f"\rProgress: {count}/{total} ({pct}%) - {fname}...", end='', file=sys.stderr)
                sys.stderr.flush()
                
                # Save incrementally every 50 tracks
                if count % 50 == 0 or count == total:
                    with results_lock:
                        _save_partial_report(output_path, results, total, count < total)
    else:
        # Single-threaded mode (original behavior)
        print("Analyzing tracks...", file=sys.stderr)
        sys.stderr.flush()
        
        for i, filepath in enumerate(audio_files):
            # Progress indicator every file, flush to ensure visibility
            pct = 100 * (i + 1) // total
            print(f"\rProgress: {i+1}/{total} ({pct}%) - {filepath.name[:40]}...", end='', file=sys.stderr)
            sys.stderr.flush()
            
            try:
                result = get_metadata(filepath, models)
                results.append(result)
            except Exception as e:
                results.append({
                    'path': str(filepath),
                    'filename': filepath.name,
                    'error': str(e)
                })
            
            # Save incrementally every 10 tracks
            if (i + 1) % 10 == 0 or (i + 1) == total:
                _save_partial_report(output_path, results, total, (i + 1) < total)
    
    print(file=sys.stderr)
    
    # Generate final statistics
    ml_genres = defaultdict(int)
    energy_dist = defaultdict(int)
    timeslot_dist = defaultdict(int)
    mood_dist = defaultdict(int)
    
    for r in results:
        ml = r.get('ml_analysis', {})
        if ml.get('ml_genre'):
            ml_genres[ml['ml_genre']] += 1
        if ml.get('ml_energy') is not None:
            energy_dist[ml['ml_energy']] += 1
        if ml.get('ml_timeslot'):
            timeslot_dist[ml['ml_timeslot']] += 1
        if ml.get('ml_mood'):
            mood_dist[ml['ml_mood']] += 1
    
    # Build final report
    report = {
        'status': 'complete',
        'summary': {
            'total_files': total,
            'analyzed': len([r for r in results if 'ml_analysis' in r]),
            'errors': len([r for r in results if 'error' in r]),
        },
        'statistics': {
            'genres': dict(sorted(ml_genres.items(), key=lambda x: -x[1])),
            'energy_distribution': dict(sorted(energy_dist.items())),
            'timeslot_distribution': dict(timeslot_dist),
            'mood_distribution': dict(mood_dist),
        },
        'tracks': results,
    }
    
    # Save final report
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*60}", file=sys.stderr)
    print("ANALYSIS COMPLETE", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Total files: {total}", file=sys.stderr)
    print(f"Successfully analyzed: {report['summary']['analyzed']}", file=sys.stderr)
    print(f"Errors: {report['summary']['errors']}", file=sys.stderr)
    print(f"\nTop genres detected:", file=sys.stderr)
    for genre, count in list(report['statistics']['genres'].items())[:10]:
        print(f"  {genre}: {count}", file=sys.stderr)
    print(f"\nEnergy distribution:", file=sys.stderr)
    for energy, count in sorted(report['statistics']['energy_distribution'].items()):
        print(f"  Level {energy}: {count}", file=sys.stderr)
    print(f"\nTimeslot distribution:", file=sys.stderr)
    for slot, count in report['statistics']['timeslot_distribution'].items():
        print(f"  {slot}: {count}", file=sys.stderr)
    print(f"\nReport saved to: {output_path}", file=sys.stderr)
    print(f"\nNext steps:", file=sys.stderr)
    print(f"  1. Review: python {sys.argv[0]} --review {output_path}", file=sys.stderr)
    print(f"  2. Apply:  python {sys.argv[0]} --apply {output_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
