"""Vibechek command-line interface.

Subcommands map 1:1 to the legacy scripts in `legacy/` while the underlying
modules are being refactored. End-users will eventually drive everything from
the GUI; the CLI stays as the scripting/automation surface.
"""

from __future__ import annotations

import click
from rich.console import Console

from vibechek import __version__

console = Console()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="vibechek")
def main() -> None:
    """Vibechek — ML-powered DJ library organizer."""


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=True, dir_okay=True))
@click.option("--workers", default=4, show_default=True, help="Parallel analysis processes.")
@click.option("--skip", default=0, show_default=True, help="Skip the first N tracks (for chunking).")
@click.option("--limit", default=0, show_default=True, help="Limit to N tracks (0 = all).")
@click.option("--output", "-o", default="analysis.json", show_default=True, help="Output JSON path.")
def analyze(path: str, workers: int, skip: int, limit: int, output: str) -> None:
    """Analyze tracks with Essentia ML models."""
    # TODO(phase-1): port legacy/analyze_dj_tracks_v2.py into vibechek.analyzer
    console.print(f"[yellow]Stub:[/] would analyze {path} → {output}")
    console.print(f"  workers={workers} skip={skip} limit={limit}")


@main.command()
@click.argument("analysis_json", type=click.Path(exists=True))
@click.option(
    "--confidence",
    default=0.85,
    show_default=True,
    type=click.FloatRange(0.0, 1.0),
    help="Minimum ML confidence to apply genre/subgenre tags.",
)
@click.option("--skip-bpm-key/--write-bpm-key", default=True, show_default=True,
              help="Skip BPM/key tag writes (Rekordbox is more reliable).")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing.")
def tag(analysis_json: str, confidence: float, skip_bpm_key: bool, dry_run: bool) -> None:
    """Apply ML-derived tags to files (with confidence threshold)."""
    # TODO(phase-1): port legacy/apply_tags_filtered.py into vibechek.tagger
    console.print(f"[yellow]Stub:[/] would tag from {analysis_json}")
    console.print(f"  confidence={confidence} skip_bpm_key={skip_bpm_key} dry_run={dry_run}")


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--output", "-o", default="tags_backup.json", show_default=True)
def backup_tags(path: str, output: str) -> None:
    """Back up ID3/Vorbis tags (including Rekordbox GEOB/PRIV frames)."""
    # TODO(phase-1): port legacy/backup_tags.py into vibechek.tagger
    console.print(f"[yellow]Stub:[/] would back up tags from {path} → {output}")


@main.command()
@click.argument("backup_json", type=click.Path(exists=True))
def restore_tags(backup_json: str) -> None:
    """Restore tags from a previous backup."""
    # TODO(phase-1): port legacy/backup_tags.py restore mode
    console.print(f"[yellow]Stub:[/] would restore tags from {backup_json}")


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--output", "-o", default="duplicates.json", show_default=True)
def dedupe(path: str, output: str) -> None:
    """Find duplicate tracks via MD5 hash + Chromaprint audio fingerprinting."""
    # TODO(phase-1): port legacy/find_duplicates.py into vibechek.duplicates
    console.print(f"[yellow]Stub:[/] would dedupe {path} → {output}")


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--no-subgenres", is_flag=True, help="Organize by genre only, not subgenre.")
@click.option("--min-genre-size", default=10, show_default=True,
              help="Genres with fewer tracks go into Other/.")
@click.option("--dry-run", is_flag=True, help="Show planned moves without executing.")
def organize(path: str, no_subgenres: bool, min_genre_size: int, dry_run: bool) -> None:
    """Move files into genre/subgenre folders."""
    # TODO(phase-1): port legacy/organize_by_genre.py into vibechek.organizer
    console.print(f"[yellow]Stub:[/] would organize {path}")
    console.print(f"  no_subgenres={no_subgenres} min_genre_size={min_genre_size} dry_run={dry_run}")


if __name__ == "__main__":
    main()
