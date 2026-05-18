"""Vibechek command-line interface.

Each subcommand maps to one module of the core package. Progress is rendered
with `rich.progress`; the underlying functions take a generic `on_progress`
callback so the future GUI can subscribe just as easily.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console

# Windows consoles often run cp1252; force UTF-8 so Rich's box-drawing and
# arrow glyphs don't crash on output. No-op on platforms that already use UTF-8.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from vibechek import __version__
from vibechek.config import (
    AnalysisConfig,
    DuplicateConfig,
    OrganizationConfig,
    TaggingConfig,
)

console = Console()


def _progress_bar(description: str) -> Progress:
    """Construct the standard Vibechek progress display."""
    return Progress(
        TextColumn(f"[bold blue]{description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        TextColumn("• [dim]{task.description}"),
        console=console,
        transient=False,
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="vibechek")
def main() -> None:
    """Vibechek — ML-powered DJ library organizer."""


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--workers", default=0, show_default=False,
              help="Parallel analysis processes (default: auto = cpu_count - 1).")
@click.option("--gpu", type=click.Choice(["auto", "on", "off"]), default="auto", show_default=True,
              help="GPU usage: auto = use if available, on = force, off = CPU-only.")
@click.option("--skip", default=0, show_default=True, help="Skip the first N tracks.")
@click.option("--limit", default=0, show_default=True, help="Limit to N tracks (0 = all).")
@click.option("--output", "-o", type=click.Path(path_type=Path),
              default=Path("analysis.json"), show_default=True)
@click.option("--models-dir", type=click.Path(path_type=Path), default=None,
              help="Override the ML model directory (defaults to user data dir).")
def analyze(path: Path, workers: int, gpu: str, skip: int, limit: int,
            output: Path, models_dir: Path | None) -> None:
    """Analyze every audio file under PATH with the ML models."""
    from vibechek.analyzer import analyze_directory

    config = AnalysisConfig(workers=workers, use_gpu=gpu)
    if models_dir:
        config.models_dir = models_dir

    with _progress_bar("Analyzing") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        try:
            report = analyze_directory(
                path,
                config=config,
                on_progress=on_progress,
                output_path=output,
                skip=skip,
                limit=limit or None,
            )
        except RuntimeError as e:
            console.print(f"[red]Error:[/] {e}")
            raise click.Abort() from e

    summary = report["summary"]
    console.print(
        f"\n[green]Done.[/] Analyzed {summary['analyzed']}/{summary['total_files']} "
        f"({summary['errors']} errors) → [cyan]{output}[/]"
    )


# ---------------------------------------------------------------------------
# tag
# ---------------------------------------------------------------------------


@main.command()
@click.argument("analysis_json", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--confidence", default=0.85, show_default=True,
              type=click.FloatRange(0.0, 1.0),
              help="Minimum ML confidence to apply genre tags.")
@click.option("--skip-bpm-key/--write-bpm-key", default=True, show_default=True,
              help="Skip BPM/key writes (Rekordbox is more reliable).")
@click.option("--no-preserve-rekordbox", is_flag=True,
              help="Do NOT preserve Rekordbox GEOB/PRIV frames (dangerous).")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing.")
def tag(analysis_json: Path, confidence: float, skip_bpm_key: bool,
        no_preserve_rekordbox: bool, dry_run: bool) -> None:
    """Apply ML tags from an analysis.json to your files."""
    from vibechek.tagger import apply_ml_tags

    data = json.loads(analysis_json.read_text(encoding="utf-8"))
    config = TaggingConfig(
        genre_confidence_threshold=confidence,
        skip_bpm_and_key=skip_bpm_key,
        preserve_rekordbox_frames=not no_preserve_rekordbox,
    )

    with _progress_bar("Tagging") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        stats = apply_ml_tags(data, config, on_progress=on_progress, dry_run=dry_run)

    mode = "[yellow](dry-run)[/] " if dry_run else ""
    console.print(
        f"\n{mode}[green]Done.[/] "
        f"Genre applied: {stats.genre_applied} (skipped low-conf: {stats.genre_skipped_low_confidence}) • "
        f"Other tags: {stats.other_tags_applied} • Errors: {len(stats.errors)}"
    )
    for err in stats.errors[:5]:
        console.print(f"  [red]✗[/] {err}")


# ---------------------------------------------------------------------------
# backup-tags / restore-tags
# ---------------------------------------------------------------------------


@main.command("backup-tags")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path),
              default=Path("tags_backup.json"), show_default=True)
def backup_tags_cmd(path: Path, output: Path) -> None:
    """Back up every tag (incl. Rekordbox GEOB/PRIV) on every file under PATH."""
    from vibechek.tagger import backup_tags

    with _progress_bar("Backing up") as progress:
        task = progress.add_task("scanning", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        stats = backup_tags(path, output, on_progress=on_progress)

    console.print(
        f"\n[green]Done.[/] Backed up {stats.backed_up}/{stats.total} files → [cyan]{output}[/]"
    )
    for err in stats.errors[:5]:
        console.print(f"  [red]✗[/] {err}")


@main.command("restore-tags")
@click.argument("backup_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def restore_tags_cmd(backup_file: Path) -> None:
    """Restore tags from a backup produced by `backup-tags`."""
    from vibechek.tagger import restore_tags

    with _progress_bar("Restoring") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        stats = restore_tags(backup_file, on_progress=on_progress)

    console.print(
        f"\n[green]Done.[/] Restored {stats.restored}/{stats.total} • "
        f"missing: {stats.skipped_missing} • errors: {len(stats.errors)}"
    )
    for err in stats.errors[:5]:
        console.print(f"  [red]✗[/] {err}")


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path),
              default=Path("duplicates.json"), show_default=True)
@click.option("--no-chromaprint", is_flag=True, help="Skip audio fingerprinting (faster).")
@click.option("--no-md5", is_flag=True, help="Skip exact-byte hashing (slower).")
@click.option("--move-to", type=click.Path(path_type=Path), default=None,
              help="Move duplicates to this folder for review.")
@click.option("--trash", is_flag=True, help="Send duplicates to OS trash (needs send2trash).")
def dedupe(path: Path, output: Path, no_chromaprint: bool, no_md5: bool,
           move_to: Path | None, trash: bool) -> None:
    """Find duplicate tracks via MD5 + Chromaprint."""
    from vibechek.duplicates import (
        DuplicateAction,
        find_duplicates,
        handle_duplicates,
        save_report,
    )

    if move_to and trash:
        raise click.UsageError("--move-to and --trash are mutually exclusive")
    if trash:
        action = DuplicateAction.TRASH
    elif move_to:
        action = DuplicateAction.MOVE
    else:
        action = DuplicateAction.REPORT

    config = DuplicateConfig(
        use_md5=not no_md5,
        use_chromaprint=not no_chromaprint,
        action=action.value,
        review_folder=move_to,
    )

    with _progress_bar("Scanning") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        report = find_duplicates(path, config, on_progress=on_progress)

    save_report(report, output)
    console.print(
        f"\n[green]Scan done.[/] "
        f"Exact: {len(report.exact_duplicates)} groups • "
        f"Audio: {len(report.audio_duplicates)} groups • "
        f"Recoverable: {report.summary.space_recoverable_mb:.1f} MB → [cyan]{output}[/]"
    )

    if action is not DuplicateAction.REPORT:
        with _progress_bar(action.value.title()) as progress:
            task = progress.add_task("starting", total=None)

            def on_progress(current: int, total: int, message: str) -> None:
                progress.update(task, completed=current, total=total, description=message[:40])

            summary = handle_duplicates(report, config, on_progress=on_progress)
        console.print(f"[green]Done.[/] {summary}")


# ---------------------------------------------------------------------------
# organize
# ---------------------------------------------------------------------------


@main.command()
@click.argument("analysis_json", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--no-subgenres", is_flag=True, help="Organize by genre only.")
@click.option("--min-genre-size", default=10, show_default=True,
              help="Genres with fewer tracks go into Other/.")
@click.option("--target-root", type=click.Path(path_type=Path), default=None,
              help="Override the destination root (defaults to first track's parent).")
@click.option("--dry-run", is_flag=True, help="Preview the moves without executing.")
def organize(analysis_json: Path, no_subgenres: bool, min_genre_size: int,
             target_root: Path | None, dry_run: bool) -> None:
    """Move files into genre/subgenre folders based on analysis.json."""
    from vibechek.organizer import organize_from_analysis, plan_organization

    data = json.loads(analysis_json.read_text(encoding="utf-8"))
    config = OrganizationConfig(
        use_subgenres=not no_subgenres,
        min_genre_size=min_genre_size,
        target_root=target_root,
    )

    if dry_run:
        plan = plan_organization(data, config)
        console.print(f"\n[yellow](dry-run)[/] {len(plan.moves)} moves planned:")
        for move in plan.moves[:20]:
            rel = move.destination.relative_to(plan.base_dir)
            console.print(f"  [dim]{move.source.name[:40]:40}[/] → {rel}")
        if len(plan.moves) > 20:
            console.print(f"  [dim]... and {len(plan.moves) - 20} more[/]")
        return

    with _progress_bar("Organizing") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        stats = organize_from_analysis(data, config, on_progress=on_progress, dry_run=False)

    console.print(
        f"\n[green]Done.[/] Moved {stats.moved}/{stats.planned} • errors: {len(stats.errors)}"
    )
    for err in stats.errors[:5]:
        console.print(f"  [red]✗[/] {err}")


# ---------------------------------------------------------------------------
# route (copy_to_genre_folders equivalent)
# ---------------------------------------------------------------------------


@main.command()
@click.argument("staging", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("library_root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Preview without copying.")
def route(staging: Path, library_root: Path, dry_run: bool) -> None:
    """Copy tracks from STAGING into LIBRARY_ROOT/<Genre>/ based on existing tags."""
    from vibechek.organizer import route_new_tracks

    with _progress_bar("Routing") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        summary = route_new_tracks(staging, library_root, on_progress=on_progress, dry_run=dry_run)

    mode = "[yellow](dry-run)[/] " if dry_run else ""
    console.print(f"\n{mode}[green]Done.[/] {summary}")


# ---------------------------------------------------------------------------
# download-models
# ---------------------------------------------------------------------------


@main.command("download-models")
@click.option("--models-dir", type=click.Path(path_type=Path), default=None,
              help="Where to put the models (defaults to user data dir).")
def download_models_cmd(models_dir: Path | None) -> None:
    """Download Essentia ML models (~800MB). Run once before first analyze.

    Models are downloaded to a per-user directory so they survive Vibechek
    reinstalls. Already-downloaded models are skipped.
    """
    from vibechek.analyzer import download_models
    from vibechek.config import MODELS_DIR

    target = models_dir or MODELS_DIR
    target.mkdir(parents=True, exist_ok=True)
    console.print(f"Downloading models to [cyan]{target}[/]")

    with _progress_bar("Downloading") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        descriptors = download_models(target, on_progress=on_progress)

    console.print(f"\n[green]Done.[/] {len(descriptors)} models available in [cyan]{target}[/]")


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


@main.command()
@click.option("--models-dir", type=click.Path(path_type=Path), default=None,
              help="Override the ML model directory (defaults to user data dir).")
@click.option("--quick/--full", default=False, show_default=True,
              help="Skip per-distro WSL probes (faster, less accurate).")
def preflight(models_dir: Path | None, quick: bool) -> None:
    """Verify Vibechek is ready to run `analyze` (essentia + model files).

    Does a full WSL distro probe by default so the output is accurate. Pass
    `--quick` to skip that probe (returns in <1 sec but won't tell you whether
    essentia is installed inside your WSL distros).
    """
    from vibechek.preflight import check_essentia, check_models, summary_lines
    from vibechek.preflight import PreflightResult
    from vibechek.wsl import detect_wsl
    import platform as _platform

    # We inline what `preflight()` does so we can pass quick= to detect_wsl,
    # which preflight() hardcodes to True for sub-second GUI responsiveness.
    essentia = check_essentia()
    models = check_models(models_dir)
    wsl_status = detect_wsl(quick=quick)
    have_native = essentia.installed
    have_wsl = wsl_status.can_run_vibechek
    ready = (have_native or have_wsl) and not models.missing
    analyze_via = "native" if have_native else ("wsl" if have_wsl else None)

    result = PreflightResult(
        ready=ready,
        essentia=essentia,
        models=models,
        platform=_platform.platform(),
        wsl=wsl_status,
        analyze_via=analyze_via,
    )
    for line in summary_lines(result):
        console.print(line)

    if not result.ready:
        raise click.exceptions.Exit(code=1)


# ---------------------------------------------------------------------------
# system-info
# ---------------------------------------------------------------------------


@main.command("system-info")
def system_info_cmd() -> None:
    """Show what CPU, RAM, and GPU Vibechek detects on this machine.

    Use this to confirm GPU availability before launching a long analyze,
    or to figure out a sensible `--workers` value.
    """
    from vibechek.resources import detect, to_dict

    info = detect()
    console.print("[bold]System resources[/]")
    console.print(f"  Platform:   {info.platform}")
    console.print(f"  CPU cores:  {info.cpu_count} "
                  f"([dim]recommended workers: {info.recommended_workers}[/])")
    if info.memory_total_mb:
        avail = f"{info.memory_available_mb} MB free" if info.memory_available_mb else ""
        console.print(f"  Memory:     {info.memory_total_mb} MB total  {avail}")
    else:
        console.print("  Memory:     [dim]install `psutil` for memory detection[/]")

    if info.gpu_available:
        console.print(f"\n[green]GPU available[/] (driver {info.cuda_runtime or 'unknown'})")
        for g in info.gpu_devices:
            mem = f" ({g.memory_mb} MB)" if g.memory_mb else ""
            console.print(f"  • {g.name} [{g.backend}]{mem}")
        console.print("\n  Use [bold]--gpu on[/] (or set 'on' in Settings) to force GPU.")
    else:
        console.print("\n[yellow]No GPU detected[/] — analysis will run on CPU.")
        if info.cuda_runtime:
            console.print(f"  (NVIDIA driver {info.cuda_runtime} present, but TF can't see it — "
                          "check CUDA/cuDNN versions.)")

    # Also dump as JSON for scripting
    import json as _json
    console.print(f"\n[dim]{_json.dumps(to_dict(info), indent=2, default=str)}[/]")


# ---------------------------------------------------------------------------
# rpc — JSON-RPC server for the desktop sidecar
# ---------------------------------------------------------------------------


@main.command()
def rpc() -> None:
    """Run as a JSON-RPC sidecar (used by the Tauri desktop shell).

    Reads JSON-RPC 2.0 requests from stdin, writes responses + progress
    notifications to stdout. Not intended for direct human use; see
    `vibechek/rpc.py` for the protocol.
    """
    from vibechek.rpc import serve

    serve()


if __name__ == "__main__":
    main()
