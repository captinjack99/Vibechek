"""Vibechek command-line interface.

Each subcommand maps to one module of the core package. Progress is rendered
with `rich.progress`; the underlying functions take a generic `on_progress`
callback so the future GUI can subscribe just as easily.
"""

from __future__ import annotations

import json
import sys
import time
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


def _resolve_default_engine() -> str:
    """The engine an --engine-less command should use.

    The saved config's engine (what the GUI runs with), falling back to the
    platform default (native on Windows, essentia_tf elsewhere). A hardcoded
    essentia_tf click-default used to send a stock Windows box — whose GUI
    default is the bundled native engine — down the WSL route that was never
    set up, breaking the documented "anything the GUI can do, the CLI can do"
    contract.
    """
    from vibechek.config import _DEFAULT_INFERENCE_ENGINE, VibechekConfig  # noqa: PLC0415

    try:
        return VibechekConfig.load().analysis.inference_engine
    except Exception:  # noqa: BLE001 — a broken config file must not kill the CLI
        return _DEFAULT_INFERENCE_ENGINE


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


def _load_analysis_json(path: Path) -> object:
    """Read + parse an analysis JSON file for tag / organize / export.

    `click.Path(exists=True)` guarantees the file is THERE, but not that it's
    valid JSON — a truncated/interrupted `analyze` write or a wrong file would
    raise a bare JSONDecodeError and dump a Python traceback at the user. Turn
    that into a clean Click error instead.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise click.ClickException(
            f"{path.name} is not a valid analysis JSON file — expected output "
            f"from `vibechek analyze` ({e})."
        ) from e


def _load_analysis_with_tracks(path: Path) -> dict:
    """Load analysis JSON and normalize to a dict with a `tracks` list.

    Accepts the standard ``{"tracks": [...]}`` object OR a bare ``[...]`` list
    of track objects. Any other top-level shape (a string, a number, a dict
    whose `tracks` isn't a list) raises a clean Click error instead of letting
    `apply_ml_tags`/`plan_organization` blow up with an AttributeError traceback
    on a hand-edited or wrong-format file.
    """
    data = _load_analysis_json(path)
    if isinstance(data, list):
        return {"tracks": data}
    if isinstance(data, dict):
        _tracks_from_analysis(data, path.name)  # shape check — raises on a non-list
        return data
    raise click.ClickException(
        f"{path.name} is not a valid analysis file — expected an object with a "
        f"'tracks' list (or a list of tracks) from `vibechek analyze`."
    )


def _tracks_from_analysis(data: object, name: str) -> list:
    """Pull the `tracks` list out of already-parsed analysis JSON, or fail loud.

    Same shape contract as `_load_analysis_with_tracks`, but over a parsed
    object so `export` can keep the raw data for its json passthrough while
    still rejecting e.g. `"tracks": "pending"` — which the loose loader let
    through and then iterated character by character, reporting each character
    as an exported track.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        tracks = data.get("tracks", [])
        if not isinstance(tracks, list):
            raise click.ClickException(
                f"{name}: the 'tracks' field must be a list — this doesn't "
                f"look like output from `vibechek analyze`."
            )
        return tracks
    raise click.ClickException(
        f"{name} is not a valid analysis file — expected an object with a "
        f"'tracks' list (or a list of tracks) from `vibechek analyze`."
    )


def _print_errors(errors: list, limit: int = 5) -> None:
    """Print the first `limit` errors and say how many were withheld.

    Truncating to 5 with no remainder count hid the scale of a failed batch —
    "errors: 500" followed by exactly five lines reads like five problems.
    """
    for err in errors[:limit]:
        console.print(f"  [red]✗[/] {err}")
    if len(errors) > limit:
        console.print(f"  [dim]… and {len(errors) - limit} more[/]")


def _exit_if_total_failure(succeeded: int, error_count: int, what: str) -> None:
    """Exit non-zero when a mutating batch achieved nothing and errored.

    These commands used to print a green "Done." and exit 0 no matter how many
    per-file operations failed, so `vibechek organize a.json && rm a.json`
    proceeded happily after a read-only share failed all 500 moves. The exit
    code is the only machine-readable signal a script has.
    """
    if error_count > 0 and succeeded == 0:
        console.print(
            f"[red]Nothing was {what} — all {error_count} operations failed.[/]"
        )
        raise click.exceptions.Exit(code=1)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="vibechek")
def main() -> None:
    """Vibechek — ML-powered DJ library organizer."""
    # The RPC sidecar configures logging on entry; the CLI never did, so every
    # log.info/debug from a CLI run went nowhere and `doctor`'s log tail could
    # only ever show GUI activity. Idempotent (guarded by `_configured`).
    from vibechek import logging_setup  # noqa: PLC0415

    try:
        logging_setup.configure()
    except OSError as e:
        # A read-only or permission-denied data dir must not take EVERY CLI
        # command down with a traceback — least of all `vibechek doctor`, which
        # is the command a user runs BECAUSE their install is broken. Logging is
        # a diagnostic, never a precondition: say so on stderr and carry on.
        # configure() drops the root handlers before it can fail, so re-attach a
        # console one or warnings from the run would vanish silently too.
        import logging  # noqa: PLC0415

        root = logging.getLogger()
        if not root.handlers:
            console_only = logging.StreamHandler(sys.stderr)
            console_only.setLevel(logging.WARNING)
            root.addHandler(console_only)
        click.echo(
            f"Warning: could not open the log file ({e}). This run will not be "
            "logged; everything else works normally.",
            err=True,
        )


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--workers", type=click.IntRange(min=0), default=0, show_default=False,
              help="Parallel analysis processes (default: auto = cpu_count - 1).")
@click.option("--gpu", type=click.Choice(["auto", "on", "off"]), default="auto", show_default=True,
              help="GPU usage: auto = use if available, on = force, off = CPU-only.")
@click.option("--skip", type=click.IntRange(min=0), default=0, show_default=True,
              help="Skip the first N tracks.")
@click.option("--limit", type=click.IntRange(min=0), default=0, show_default=True,
              help="Limit to N tracks (0 = all).")
@click.option("--output", "-o", type=click.Path(path_type=Path),
              default=Path("analysis.json"), show_default=True)
@click.option("--models-dir", type=click.Path(path_type=Path), default=None,
              help="Override the ML model directory (defaults to user data dir).")
@click.option("--hybrid/--no-hybrid", default=True, show_default=True,
              help="Run GPU + CPU workers together against a shared queue "
                   "(self-balancing). --no-hybrid uses a single device pool.")
@click.option("--engine", type=click.Choice(["essentia_tf", "onnx", "native"]),
              default=None,
              help="Inference engine: essentia_tf (bundled TensorFlow, NVIDIA-only "
                   "GPU), onnx (TF-free ONNX Runtime; NVIDIA GPU today, cross-vendor "
                   "planned), or native "
                   "(in-process ONNX + native essentia — the Windows GUI default; "
                   "needs essentia importable in this Python). Omitted → the saved "
                   "config's engine, then the platform default (native on Windows, "
                   "essentia_tf elsewhere) — same resolution as the GUI.")
@click.option("--skip-paths-file", type=click.Path(exists=True, dir_okay=False,
              path_type=Path), default=None,
              help="File with one absolute path per line to SKIP (already-analyzed "
                   "tracks). Used by the GUI's incremental 'Analyze new tracks "
                   "only' when it routes through WSL/the managed venv.")
@click.option("--genre-policy",
              type=click.Choice(["prefer_tag", "prefer_ml", "tag_only", "ml_only"]),
              default="prefer_tag", show_default=True,
              help="Reconcile an existing genre tag with the ML read: prefer_tag "
                   "(trust a specific existing tag, ML fills gaps + can override if "
                   "very confident), prefer_ml, tag_only, or ml_only (pure audio).")
@click.option("--genre-classifier", type=click.Choice(["discogs", "clap"]),
              default="discogs", show_default=True,
              help="Audio genre model: discogs (bundled Discogs-EffNet) or clap "
                   "(pure-audio CLAP+kNN student, ~2x better; needs CLAP setup).")
@click.option("--genre-web-lookup/--no-genre-web-lookup", default=False, show_default=True,
              help="Look the genre up online (reads the genre field off catalog "
                   "pages for artist+title) and layer it into reconciliation. "
                   "Needs network + the online-lookup setup.")
@click.option("--genre-llm-backend", type=click.Choice(["ollama"]),
              default="ollama", show_default=True,
              help="Deprecated, ignored: the online lookup uses no model.")
@click.option("--genre-override-confidence", type=click.FloatRange(0.0, 1.0),
              default=0.90, show_default=True,
              help="With --genre-policy prefer_tag: minimum ML confidence for "
                   "the model to override a disagreeing specific tag.")
def analyze(path: Path, workers: int, gpu: str, skip: int, limit: int,
            output: Path, models_dir: Path | None, hybrid: bool, engine: str | None,
            skip_paths_file: Path | None,
            genre_policy: str, genre_classifier: str, genre_web_lookup: bool,
            genre_llm_backend: str, genre_override_confidence: float) -> None:
    """Analyze every audio file under PATH with the ML models."""
    from vibechek.analyzer import analyze_directory

    engine = engine or _resolve_default_engine()
    config = AnalysisConfig(workers=workers, use_gpu=gpu, hybrid_cpu_gpu=hybrid,
                            inference_engine=engine, genre_source_policy=genre_policy,
                            genre_classifier=genre_classifier,
                            genre_web_lookup=genre_web_lookup,
                            genre_llm_backend=genre_llm_backend,
                            genre_ml_override_confidence=genre_override_confidence)
    if models_dir:
        config.models_dir = models_dir

    skip_paths: set[str] | None = None
    if skip_paths_file is not None:
        skip_paths = {
            line.strip()
            for line in skip_paths_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        } or None

    with _progress_bar("Analyzing") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        _t0 = time.monotonic()
        try:
            report = analyze_directory(
                path,
                config=config,
                on_progress=on_progress,
                output_path=output,
                skip=skip,
                limit=limit or None,
                skip_paths=skip_paths,
            )
        except RuntimeError as e:
            console.print(f"[red]Error:[/] {e}")
            raise click.Abort() from e

    # Same durable run history the GUI path writes — doctor's "last analyze
    # run" section must see CLI runs too, or it reports a stale picture.
    from vibechek import logging_setup  # noqa: PLC0415
    logging_setup.record_run_history(
        report,
        duration_sec=round(time.monotonic() - _t0, 1),
        fallback_engine=config.inference_engine,
        fallback_classifier=config.genre_classifier,
        fallback_workers=config.workers,
    )

    summary = report["summary"]
    if summary["total_files"] == 0:
        # Zero matching audio files (empty dir, or --skip/--limit filtered
        # everything out). Don't print a green "Done." that names an output
        # file — make the "did nothing" outcome visible instead of looking
        # like a successful refresh.
        console.print(
            f"\n[yellow]No audio files found under[/] [cyan]{path}[/] "
            f"— nothing analyzed, [cyan]{output}[/] not refreshed."
        )
    else:
        done = "[yellow]Done (with errors).[/]" if summary["errors"] else "[green]Done.[/]"
        console.print(
            f"\n{done} Analyzed {summary['analyzed']}/{summary['total_files']} "
            f"({summary['errors']} errors) → [cyan]{output}[/]"
        )
        # The report IS still written (500 error records are worth keeping),
        # but `analyze L -o a.json && tag a.json` must not proceed on a run
        # where every single file failed.
        _exit_if_total_failure(summary["analyzed"], summary["errors"], "analyzed")


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

    data = _load_analysis_with_tracks(analysis_json)
    # --skip-bpm-key (default) maps to write_bpm/write_key=False; the inverse
    # --write-bpm-key flag turns both on (per-field toggles superseded the old
    # single skip_bpm_and_key flag).
    config = TaggingConfig(
        genre_confidence_threshold=confidence,
        write_bpm=not skip_bpm_key,
        write_key=not skip_bpm_key,
        preserve_rekordbox_frames=not no_preserve_rekordbox,
    )

    with _progress_bar("Tagging") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        stats = apply_ml_tags(data, config, on_progress=on_progress, dry_run=dry_run)

    mode = "[yellow](dry-run)[/] " if dry_run else ""
    done = "[yellow]Done (with errors).[/]" if stats.errors else "[green]Done.[/]"
    # `genre_applied + genre_applied_parent_only + genre_skipped_*` is exactly
    # the track count, so every term has to be printed or the numbers don't add
    # up and tracks vanish from the summary. Parent-only is a SEPARATE bucket
    # from applied (it's the parent-genre fallback, not a subgenre write), so it
    # gets its own term rather than being folded into "Genre applied".
    write_off = (
        f" • [yellow]Genre writes off in config: "
        f"{stats.genre_skipped_write_disabled}[/]"
        if stats.genre_skipped_write_disabled
        else ""
    )
    console.print(
        f"\n{mode}{done} "
        f"Genre applied: {stats.genre_applied} • "
        f"parent-only: {stats.genre_applied_parent_only} • "
        f"skipped low-conf: {stats.genre_skipped_low_confidence}{write_off} • "
        f"Other tags: {stats.other_tags_applied} • Errors: {len(stats.errors)}"
    )
    _print_errors(stats.errors)
    # A parent-only genre IS a tag that landed on disk — leaving it out of the
    # success count made a run whose every genre came from the parent fallback
    # look like a total failure.
    _exit_if_total_failure(
        stats.genre_applied + stats.genre_applied_parent_only + stats.other_tags_applied,
        len(stats.errors),
        "tagged",
    )


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

    done = "[yellow]Done (with errors).[/]" if stats.errors else "[green]Done.[/]"
    console.print(
        f"\n{done} Backed up {stats.backed_up}/{stats.total} files → [cyan]{output}[/]"
    )
    _print_errors(stats.errors)
    _exit_if_total_failure(stats.backed_up, len(stats.errors), "backed up")


@main.command("restore-tags")
@click.argument("backup_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def restore_tags_cmd(backup_file: Path) -> None:
    """Restore tags from a backup produced by `backup-tags`."""
    from vibechek.tagger import restore_tags

    with _progress_bar("Restoring") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        try:
            stats = restore_tags(backup_file, on_progress=on_progress)
        except (ValueError, FileNotFoundError) as e:
            # _load_backup_files raises a friendly ValueError/FileNotFoundError
            # for empty / corrupt / wrong-shape backups. Render it as a clean
            # Click error rather than leaking the raw traceback.
            raise click.ClickException(str(e)) from e

    done = "[yellow]Done (with errors).[/]" if stats.errors else "[green]Done.[/]"
    console.print(
        f"\n{done} Restored {stats.restored}/{stats.total} • "
        f"missing: {stats.skipped_missing} • errors: {len(stats.errors)}"
    )
    _print_errors(stats.errors)
    _exit_if_total_failure(stats.restored, len(stats.errors), "restored")


# ---------------------------------------------------------------------------
# undo journals (revert organize / dedupe-move)
# ---------------------------------------------------------------------------


@main.command("journals")
def journals_cmd() -> None:
    """List recent organize/dedupe operation journals (for `revert`)."""
    import datetime as _dt

    from vibechek.journal import list_journals

    items = list_journals()
    if not items:
        console.print("No operation journals found.")
        return
    for j in items:
        when = (
            _dt.datetime.fromtimestamp(j["started_at"]).strftime("%Y-%m-%d %H:%M")
            if j.get("started_at") else "?"
        )
        trash_note = (
            f" • {j['trash_count']} trashed (not revertible)"
            if j["trash_count"] else ""
        )
        console.print(
            f"[cyan]{j['path']}[/]\n"
            f"  {j['kind']} • {when} • {j['move_count']} moves{trash_note}"
        )


@main.command()
@click.argument("journal_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def revert(journal_file: Path) -> None:
    """Undo an organize/dedupe-move by moving files back to their origins.

    Trash entries can't be auto-restored — restore those from the OS recycle
    bin manually. Run `vibechek journals` to list available journals.
    """
    from vibechek.journal import revert_journal

    with _progress_bar("Reverting") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        try:
            summary = revert_journal(journal_file, on_progress=on_progress)
        except (ValueError, FileNotFoundError) as e:
            # revert_journal rejects a non-journal / corrupt file with a clean
            # message — surface it as a Click error instead of a raw traceback,
            # and never silently "succeed" (Reverted 0) on the wrong file.
            raise click.ClickException(str(e)) from e

    done = "[yellow]Done (with errors).[/]" if summary["errors"] else "[green]Done.[/]"
    console.print(
        f"\n{done} Reverted {summary['reverted']} • "
        f"skipped: {summary['skipped']} • errors: {summary['errors']}"
    )
    if summary["trashed_not_reverted"]:
        console.print(
            f"  [yellow]⚠[/] {summary['trashed_not_reverted']} trashed files "
            f"can't be auto-restored — recover them from your OS recycle bin."
        )
    _print_errors(summary["error_messages"])
    _exit_if_total_failure(summary["reverted"], summary["errors"], "reverted")


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
@click.option("--across-versions", is_flag=True,
              help="Also dedupe ACROSS versions (collapse Extended/Radio/Remix into one). "
                   "Default keeps distinct versions and only collapses redundant encodings.")
@click.option("--keep-all-formats", is_flag=True,
              help="Within a version, keep the best file of EACH format (e.g. a FLAC AND an MP3) "
                   "instead of just the single best.")
def dedupe(path: Path, output: Path, no_chromaprint: bool, no_md5: bool,
           move_to: Path | None, trash: bool, across_versions: bool,
           keep_all_formats: bool) -> None:
    """Find duplicate tracks via MD5 + Chromaprint."""
    from vibechek.duplicates import (
        DuplicateAction,
        find_duplicates,
        handle_duplicates,
        save_report,
    )

    if move_to and trash:
        raise click.UsageError("--move-to and --trash are mutually exclusive")
    if no_md5 and no_chromaprint:
        # With both detection methods off, find_duplicates does no work and
        # returns an empty report — indistinguishable from a clean library.
        # Reject up front rather than report a misleading "0 groups".
        raise click.UsageError(
            "--no-md5 and --no-chromaprint cannot both be set — "
            "no detection method would run."
        )
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
        keep_distinct_versions=not across_versions,
        keep_all_formats=keep_all_formats,
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
        acted = summary["moved"] + summary["deleted"]
        done = "[yellow]Done (with errors).[/]" if summary["errors"] else "[green]Done.[/]"
        console.print(
            f"{done} moved: {summary['moved']} • deleted: {summary['deleted']} • "
            f"errors: {summary['errors']}"
        )
        _print_errors(summary["error_messages"])
        _exit_if_total_failure(
            acted, summary["errors"],
            "moved" if action is DuplicateAction.MOVE else "trashed",
        )


# ---------------------------------------------------------------------------
# organize
# ---------------------------------------------------------------------------


@main.command()
@click.argument("analysis_json", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--no-subgenres", is_flag=True, help="Organize by genre only.")
@click.option("--min-genre-size", default=10, show_default=True,
              help="Genres with fewer tracks go into Other/.")
@click.option("--target-root", type=click.Path(path_type=Path), default=None,
              help="Destination root for the genre tree (default: the common parent "
                   "folder of the analysed tracks — pass your library root if you "
                   "analysed a single genre folder).")
@click.option("--dry-run", is_flag=True, help="Preview the moves without executing.")
def organize(analysis_json: Path, no_subgenres: bool, min_genre_size: int,
             target_root: Path | None, dry_run: bool) -> None:
    """Move files into genre/subgenre folders based on analysis.json."""
    from vibechek.organizer import organize_from_analysis, plan_organization

    data = _load_analysis_with_tracks(analysis_json)
    config = OrganizationConfig(
        use_subgenres=not no_subgenres,
        min_genre_size=min_genre_size,
        target_root=target_root,
    )

    if dry_run:
        try:
            plan = plan_organization(data, config)
        except ValueError as e:
            # e.g. an empty analysis with no --target-root to anchor the tree.
            raise click.ClickException(str(e)) from e
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

        try:
            stats = organize_from_analysis(data, config, on_progress=on_progress, dry_run=False)
        except ValueError as e:
            raise click.ClickException(str(e)) from e

    done = "[yellow]Done (with errors).[/]" if stats.errors else "[green]Done.[/]"
    console.print(
        f"\n{done} Moved {stats.moved}/{stats.planned} • errors: {len(stats.errors)}"
    )
    # Sibling of the cdj-export case: organize_from_analysis records that the
    # undo journal is missing completed moves, and the CLI dropped the flag. A
    # later `vibechek revert` on that journal restores only part of the run and
    # still reports a clean success, so the one moment the user can be told is
    # here.
    if stats.journal_incomplete:
        console.print(
            "[yellow]Warning:[/] the undo journal is INCOMPLETE — some moves that "
            "happened were not recorded, so a revert will only restore part of "
            "this run. Note the moves above before undoing."
        )
    _print_errors(stats.errors)
    _exit_if_total_failure(stats.moved, len(stats.errors), "moved")


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
    done = "[yellow]Done (with errors).[/]" if summary["errors"] else "[green]Done.[/]"
    console.print(f"\n{mode}{done} {summary}")
    _exit_if_total_failure(summary["copied"], summary["errors"], "copied")


# ---------------------------------------------------------------------------
# download-models
# ---------------------------------------------------------------------------


@main.command("download-models")
@click.option("--models-dir", type=click.Path(path_type=Path), default=None,
              help="Where to put the models (defaults to user data dir).")
@click.option("--engine", type=click.Choice(["essentia_tf", "onnx", "native"]),
              default=None,
              help="Which model set: essentia_tf (.pb), or onnx/native (the shared "
                   "converted .onnx heads + backbone). Omitted → the saved config's "
                   "engine, then the platform default.")
def download_models_cmd(models_dir: Path | None, engine: str | None) -> None:
    """Download Essentia ML models (~800MB). Run once before first analyze.

    Models are downloaded to a per-user directory so they survive Vibechek
    reinstalls. Already-downloaded models are skipped. `--engine onnx` fetches
    the converted ONNX model set instead of the TensorFlow `.pb` set.
    """
    from vibechek.analyzer import download_models
    from vibechek.config import MODELS_DIR

    engine = engine or _resolve_default_engine()
    target = models_dir or MODELS_DIR
    target.mkdir(parents=True, exist_ok=True)
    console.print(f"Downloading {engine} models to [cyan]{target}[/]")

    with _progress_bar("Downloading") as progress:
        task = progress.add_task("starting", total=None)

        def on_progress(current: int, total: int, message: str) -> None:
            progress.update(task, completed=current, total=total, description=message[:40])

        descriptors = download_models(target, on_progress=on_progress, engine=engine)

    console.print(f"\n[green]Done.[/] {len(descriptors)} models available in [cyan]{target}[/]")


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


@main.command()
@click.option("--models-dir", type=click.Path(path_type=Path), default=None,
              help="Override the ML model directory (defaults to user data dir).")
@click.option("--quick/--full", default=False, show_default=True,
              help="Skip per-distro WSL probes (faster, less accurate).")
@click.option("--engine", type=click.Choice(["essentia_tf", "onnx", "native"]),
              default=None,
              help="Which engine's environment to check: essentia_tf checks the "
                   "TensorFlow venv + .pb models; onnx/native check the ONNX venv "
                   "+ .onnx models. Omitted → the saved config's engine, then the "
                   "platform default (native on Windows, essentia_tf elsewhere) — "
                   "same resolution as `analyze`. The old hardcoded essentia_tf "
                   "reported the wrong engine's readiness for a Windows GUI whose "
                   "default is native.")
def preflight(models_dir: Path | None, quick: bool, engine: str | None) -> None:
    """Verify Vibechek is ready to run `analyze` (essentia + model files).

    Does a full WSL distro probe by default so the output is accurate. Pass
    `--quick` to skip that probe (returns in <1 sec but won't tell you whether
    essentia is installed inside your WSL distros).
    """
    from vibechek.preflight import preflight as run_preflight
    from vibechek.preflight import summary_lines

    engine = engine or _resolve_default_engine()
    result = run_preflight(models_dir, quick_wsl=quick, engine=engine)
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
# selftest-native — frozen-build gate: prove the bundled native engine loads
# ---------------------------------------------------------------------------


@main.command("selftest-native", hidden=True)
@click.option("--gold-dir", type=click.Path(path_type=Path, exists=True,
              file_okay=False), default=None,
              help="Also run the gold-corpus ACCURACY gate: analyze the "
                   "openly-licensed fixture clips in this directory through "
                   "the native engine and assert the genre/BPM/key values "
                   "pinned in its manifest.json (tests/fixtures/gold).")
def selftest_native_cmd(gold_dir: Path | None) -> None:
    """Prove the bundled native (WSL-free) engine loads + runs in the frozen exe.

    Build-time gate only — packaging/build-windows.bat runs this against the
    frozen `dist/vibechek.exe` when the DSP-only essentia wheel was bundled.
    The PyInstaller spec swallows bundling errors (a missing DLL or a skipped
    bundle silently degrades to the lean CLI), so without an explicit in-process
    check a broken native bundle would ship on a GREEN build. This imports
    essentia inside the onefile, DECODES a synthesized clip through essentia's
    FFmpeg/libav path (the avcodec/avformat DLLs a load-only smoke never
    exercises — a missing one lets `import essentia` succeed but throws at the
    first decode), runs the analyze-path feature extractors on it, and checks
    onnxruntime + the bundled ONNX heads. Exits non-zero on any failure so the
    build fails loudly instead of silently shipping a native engine that can't
    decode audio.

    With --gold-dir it additionally runs the gold-corpus gate (vibechek.gold_gate):
    a REAL analyze of committed reference clips with the values the production
    pipeline is known to produce for them, so a silent accuracy regression (wrong
    genre/BPM/key, not a crash) also fails the build. Runs in-process (workers=1);
    models self-provision via load_models (bundled heads + hosted backbone).
    """
    try:
        import os
        import tempfile
        import wave

        import essentia
        import essentia.standard as es
        import numpy as np

        # 1) DECODE a real file through essentia's FFmpeg/libav path. Synthesize a
        #    5s WAV (stdlib, no encoder dep) and decode it; essentia routes EVERY
        #    format (WAV included) through libav, so this exercises the same
        #    avcodec/avformat DLLs MP3/FLAC decode needs — the one runtime path
        #    the load-only smoke could not reach.
        sr = 44100
        sig = (0.3 * np.sin(2.0 * np.pi * 440.0 * np.arange(sr * 5) / sr) * 32767).astype("<i2")
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            with wave.open(tmp.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(sig.tobytes())
            audio = es.MonoLoader(filename=tmp.name, sampleRate=sr)()
            if audio is None or len(audio) == 0:
                raise RuntimeError("essentia MonoLoader decoded zero samples")
            # 2) Run the analyze worker's feature extractors on the decoded audio —
            #    proves the compiled extension + FFTW actually RUN, not just load.
            es.RhythmExtractor2013(method="multifeature")(audio)
            es.KeyExtractor()(audio)
        finally:
            os.unlink(tmp.name)

        # 3) onnxruntime + the bundled (un-hosted) ONNX classification heads.
        import onnxruntime  # noqa: F401

        from vibechek.onnx_backend import bundled_onnx_assets_dir

        assets = bundled_onnx_assets_dir()
        if assets is None:
            raise RuntimeError("bundled ONNX assets dir not found in the frozen exe")
    except Exception as exc:  # noqa: BLE001 — build gate: surface ANY failure loudly
        console.print(f"[red]native self-test FAILED[/] — {type(exc).__name__}: {exc}")
        raise click.exceptions.Exit(code=1) from exc

    console.print(
        f"[green]native self-test OK[/] — essentia "
        f"{getattr(essentia, '__version__', '?')} decoded + analyzed a test clip; "
        f"onnxruntime + ONNX heads at {assets}"
    )

    if gold_dir is None:
        return
    try:
        from vibechek.gold_gate import run_gold_gate

        console.print(f"Running the gold-corpus accuracy gate on [cyan]{gold_dir}[/]…")
        failures = run_gold_gate(gold_dir, engine="native")
    except Exception as exc:  # noqa: BLE001 — build gate: surface ANY failure loudly
        console.print(f"[red]gold-corpus gate FAILED to run[/] — {type(exc).__name__}: {exc}")
        raise click.exceptions.Exit(code=1) from exc
    if failures:
        console.print(f"[red]gold-corpus gate FAILED[/] — {len(failures)} assertion(s):")
        for f in failures:
            console.print(f"  [red]✗[/] {f}")
        raise click.exceptions.Exit(code=1)
    console.print(
        "[green]gold-corpus gate OK[/] — reference clips analyzed with the "
        "expected genre/BPM/key"
    )


# ---------------------------------------------------------------------------
# doctor — paste-friendly diagnostic report
# ---------------------------------------------------------------------------


@main.command()
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="Write the report to a file (default: print to stdout).")
@click.option("--models-dir", type=click.Path(path_type=Path), default=None,
              help="Override the ML model directory (defaults to user data dir).")
def doctor(output: Path | None, models_dir: Path | None) -> None:
    """Print a paste-friendly diagnostic report.

    Use this when filing a bug — the markdown body is safe to copy into a
    GitHub issue (paths + sizes + versions only, no file contents or
    credentials). The default prints to stdout; pass `--output` to save
    to a file you can attach.
    """
    from vibechek.doctor import run as run_doctor

    md = run_doctor(output=output, models_dir=models_dir)
    if output:
        console.print(f"[green]Wrote diagnostic to[/] [cyan]{output}[/]")
    else:
        # Print raw so the markdown is paste-able from the terminal scrollback
        # without Rich's box-drawing junk.
        click.echo(md)


# ---------------------------------------------------------------------------
# verify-models — hash the on-disk models against the expected table
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Stream a file's SHA256 in 1 MiB chunks (the .pb weights are 100+ MB)."""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _optional_onnx_filenames() -> set[str]:
    """ONNX files `download-models` treats as BEST-EFFORT, so their absence is
    not a broken install.

    Authoritative source is model_download.py's own `required` expression in
    `_download_onnx_set` — a head is required unless it is the
    `genre_discogs400.onnx` weights (the backbone already emits genre), and a
    head's class-label `.json` is required only for `genre_discogs400` (without
    those 400 labels the engine loads "ready" and silently emits no genre).
    Verifying every file as required made a healthy install that never fetched
    e.g. `mood_happy.json` report MISSING and exit 1.

    `vibechek.rpc._optional_onnx_filenames` mirrors this; the two are pinned
    together by a test.
    """
    from vibechek.analyzer import _ONNX_HEAD_STEMS  # noqa: PLC0415

    optional = {"genre_discogs400.onnx"}
    optional.update(
        f"{stem}.json" for stem in _ONNX_HEAD_STEMS if stem != "genre_discogs400"
    )
    return optional


def _model_verification_targets(engine: str, models_dir: Path) -> list[tuple[str, Path, str | None]]:
    """(display name, path, pinned digest) for every file `engine` needs on disk.

    Mirrors the `verify_model_hashes` RPC. The ONNX family (`onnx`/`native`)
    stages its backbone + converted heads under `<models>/onnx` and never
    downloads the flat essentia `.pb` set, so checking the `.pb` files for
    those engines reported all 16 "MISSING" on the Windows default.
    """
    if engine in ("onnx", "native"):
        from vibechek.analyzer import (  # noqa: PLC0415
            _ONNX_HEAD_STEMS,
            _ONNX_SUBDIR,
            MODEL_SHA256_ONNX,
        )
        from vibechek.model_download import BACKBONE_ONNX_SHA256  # noqa: PLC0415
        from vibechek.onnx_backend import BACKBONE_ONNX_FILENAME  # noqa: PLC0415

        onnx_dir = models_dir / _ONNX_SUBDIR
        targets = [
            (
                BACKBONE_ONNX_FILENAME,
                onnx_dir / BACKBONE_ONNX_FILENAME,
                BACKBONE_ONNX_SHA256,
            )
        ]
        for stem in _ONNX_HEAD_STEMS:
            for fname in (f"{stem}.onnx", f"{stem}.json"):
                targets.append((fname, onnx_dir / fname, MODEL_SHA256_ONNX.get(fname)))
        return targets

    from vibechek.analyzer import MODEL_SHA256, MODELS  # noqa: PLC0415

    # MODEL_SHA256 is keyed by model NAME with an inner {"pb": ..., "json": ...}.
    # Looking it up by FILENAME (`effnet.pb`) always returned None, so every
    # comparison was a silent no-op and the command could only fail on a
    # missing file — the tamper check it exists for never ran.
    return [
        (f"{name}.{suffix}", models_dir / f"{name}.{suffix}",
         MODEL_SHA256.get(name, {}).get(suffix))
        for name in MODELS
        for suffix in ("pb", "json")
    ]


@main.command("verify-models")
@click.option("--models-dir", type=click.Path(path_type=Path), default=None,
              help="Override the ML model directory (defaults to user data dir).")
@click.option("--engine", type=click.Choice(["essentia_tf", "onnx", "native"]),
              default=None,
              help="Which model set to verify (default: the configured engine).")
def verify_models_cmd(models_dir: Path | None, engine: str | None) -> None:
    """Hash each model file on disk and check it against the pinned SHA256.

    Engine-aware, like the GUI's "Verify model integrity" button: `onnx` and
    `native` verify the `.onnx` backbone + heads under `<models>/onnx`,
    `essentia_tf` verifies the flat `.pb`/`.json` set.

    Exits non-zero if any file is missing, unreadable, or mismatched.
    """
    from vibechek.config import MODELS_DIR  # noqa: PLC0415

    engine = engine or _resolve_default_engine()
    target = models_dir or MODELS_DIR
    entries = _model_verification_targets(engine, target)

    console.print(f"Verifying [bold]{engine}[/] models in [cyan]{target}[/]")
    if not any(exp for _, _, exp in entries):
        console.print("[yellow]No pinned hashes for this set — printing computed hashes only.[/]")

    optional = _optional_onnx_filenames() if engine in ("onnx", "native") else set()

    failures = 0
    for fname, fp, exp in entries:
        if not fp.exists():
            if fname in optional:
                # download-models never treats these as an error, so neither can
                # the verifier — otherwise a healthy install reports a broken one.
                console.print(f"  [yellow]{fname}: optional-missing[/]")
                continue
            console.print(f"  [red]{fname}: MISSING[/]")
            failures += 1
            continue
        try:
            h = _sha256_file(fp)
        except OSError as e:
            console.print(f"  [red]{fname}: READ ERROR — {e}[/]")
            failures += 1
            continue
        if exp is None:
            console.print(f"  {fname}: sha256={h}")
        elif exp.lower() == h.lower():
            console.print(f"  [green]{fname}: OK[/] (sha256={h})")
        else:
            console.print(
                f"  [red]{fname}: MISMATCH[/] "
                f"(expected={exp}, got={h}) — redownload via "
                f"`vibechek download-models --engine {engine}`."
            )
            failures += 1

    if failures > 0:
        raise click.exceptions.Exit(code=1)


# ---------------------------------------------------------------------------
# export — flatten analysis.json into CSV / JSON / m3u8
# ---------------------------------------------------------------------------


# Columns for the flat CSV export. Order matters — it's the column order
# users see in Excel / Numbers / their CSV tool of choice. Kept stable so
# downstream scripts can rely on positional access.
_EXPORT_CSV_COLUMNS = [
    "path", "filename", "ext", "size_mb",
    "existing_genre", "existing_bpm", "existing_key",
    "ml_genre", "ml_subgenre", "ml_genre_confidence",
    "ml_bpm", "ml_key", "ml_energy", "ml_mood",
    "ml_timeslot", "ml_direction", "ml_vocal", "ml_danceability",
    "error",
]


def _track_to_csv_row(track: dict) -> dict:
    """Flatten one analysis.json record into the CSV's flat dict shape."""
    ml = track.get("ml_analysis") or {}
    existing = track.get("existing_tags") or {}
    error = track.get("error") or ml.get("ml_error") or ""
    return {
        "path": track.get("path", ""),
        "filename": track.get("filename", ""),
        "ext": track.get("extension", ""),
        "size_mb": track.get("size_mb", ""),
        "existing_genre": existing.get("genre", "") or "",
        "existing_bpm": existing.get("bpm", "") or "",
        "existing_key": existing.get("key", "") or "",
        "ml_genre": ml.get("ml_genre", "") or "",
        "ml_subgenre": ml.get("ml_subgenre", "") or "",
        "ml_genre_confidence": ml.get("ml_genre_confidence", "") or "",
        "ml_bpm": ml.get("ml_bpm", "") or "",
        "ml_key": ml.get("ml_key", "") or "",
        "ml_energy": ml.get("ml_energy", "") or "",
        "ml_mood": ml.get("ml_mood", "") or "",
        "ml_timeslot": ml.get("ml_timeslot", "") or "",
        "ml_direction": ml.get("ml_direction", "") or "",
        "ml_vocal": ml.get("ml_vocal", "") or "",
        "ml_danceability": ml.get("ml_danceability", "") or "",
        "error": error,
    }


@main.command("export")
@click.argument("analysis_json", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--format", "fmt", type=click.Choice(["csv", "json", "m3u8"]), default="csv",
              show_default=True, help="Output format.")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="Destination file. Defaults to <analysis>.<format>.")
def export_cmd(analysis_json: Path, fmt: str, output: Path | None) -> None:
    """Export an analysis.json to CSV, JSON, or m3u8.

    - `csv`: one row per track, columns documented in `_EXPORT_CSV_COLUMNS`.
    - `json`: passthrough of the analysis file (useful for testing tooling).
    - `m3u8`: simple playlist — one file path per line, no extended tags.
              Treat as a placeholder; future versions may emit `#EXTINF`.
    """
    import csv

    from vibechek.io import atomic_write_json

    data = _load_analysis_json(analysis_json)
    # Same strict shape check `tag`/`organize` get. The loose path let a
    # `"tracks": "pending"` string through, iterated it character by character
    # and reported "Exported 7 tracks" for an empty CSV.
    tracks = _tracks_from_analysis(data, analysis_json.name)

    if output is None:
        output = analysis_json.with_suffix(f".{fmt}")

    # `Path("analysis.json").with_suffix(".json")` IS the input, so the json
    # branch used to truncate-and-rewrite the report it was handed — 30+ min of
    # analysis destroyed if that write is interrupted. Refuse instead.
    if output.resolve() == analysis_json.resolve():
        raise click.ClickException(
            f"Refusing to write over the source analysis file "
            f"({analysis_json.name}) — pass -o/--output to name a destination."
        )

    written = 0
    if fmt == "csv":
        with output.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_EXPORT_CSV_COLUMNS)
            writer.writeheader()
            for track in tracks:
                # A malformed analysis.json (hand-edited, wrong shape) can have
                # non-dict entries; iterating a dict yields its string keys.
                # Skip anything that isn't a track object rather than crashing
                # with a Click traceback dump.
                if not isinstance(track, dict):
                    continue
                writer.writerow(_track_to_csv_row(track))
                written += 1
    elif fmt == "json":
        # Atomic, like analyzer.py's own report write: an interrupted export
        # must never leave a half-written JSON behind.
        atomic_write_json(output, data, indent=2)
        written = len(tracks)
    elif fmt == "m3u8":
        # Minimal — just paths. We deliberately *don't* prefix with `#EXTM3U`
        # yet because some DJ apps (rekordbox) get confused by m3u8 without
        # full EXTINF lines. Document as basic for now.
        # Mirror the CSV branch's guard: a hand-edited / wrong-shape file can
        # have non-dict entries; the isinstance check short-circuits before
        # .get so a bare string/int doesn't raise AttributeError.
        lines = [t.get("path", "") for t in tracks if isinstance(t, dict) and t.get("path")]
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written = len(lines)

    # Report what was WRITTEN, not the length of the input collection — both
    # writer branches drop entries the old count still included.
    skipped = len(tracks) - written
    note = f" [yellow]({skipped} unusable entries skipped)[/]" if skipped else ""
    console.print(
        f"[green]Exported {written} tracks[/] → [cyan]{output}[/] ({fmt}){note}"
    )


# ---------------------------------------------------------------------------
# profile — list + load built-in DJ profiles
# ---------------------------------------------------------------------------


@main.group()
def profile() -> None:
    """Manage built-in DJ profiles (house-dj, disco-dj, edm-festival, …)."""


@profile.command("list")
def profile_list_cmd() -> None:
    """List every built-in profile + the current config snapshot."""
    from vibechek.config import VibechekConfig
    from vibechek.profiles import list_profiles

    cfg = VibechekConfig.load()
    console.print("[bold]Built-in profiles[/]")
    for p in list_profiles():
        console.print(
            f"  [cyan]{p['name']:14}[/] {p['description']}\n"
            f"     conf={p['genre_confidence_threshold']} "
            f"min_genre={p['min_genre_size']} gpu={p['use_gpu']}"
        )
    console.print(
        f"\n[bold]Current config[/]\n"
        f"  genre_confidence_threshold = {cfg.tagging.genre_confidence_threshold}\n"
        f"  min_genre_size             = {cfg.organization.min_genre_size}\n"
        f"  use_gpu                    = {cfg.analysis.use_gpu}"
    )


@profile.command("load")
@click.argument("name")
def profile_load_cmd(name: str) -> None:
    """Apply a profile's overrides to the saved config (and write to disk)."""
    from vibechek.config import ConfigSaveRefused
    from vibechek.profiles import load_profile

    try:
        result = load_profile(name)
    except KeyError as e:
        raise click.UsageError(str(e)) from e
    except ConfigSaveRefused as e:
        # load_profile is a load→save round trip, so an unreadable config.json
        # would have it write profile-flavoured factory defaults over the
        # user's real settings. config.save() refuses; without this handler the
        # refusal reached the user as a raw traceback.
        raise click.UsageError(str(e)) from e
    console.print(
        f"[green]Loaded profile[/] [cyan]{result['loaded']}[/] → "
        f"[dim]{result['saved_to']}[/]"
    )
    applied = result["applied"]
    console.print(
        f"  genre_confidence_threshold = {applied['genre_confidence_threshold']}\n"
        f"  min_genre_size             = {applied['min_genre_size']}\n"
        f"  use_gpu                    = {applied['use_gpu']}"
    )


# ---------------------------------------------------------------------------
# cdj-export — transcode a FLAC library to AIFF + rewrite Rekordbox XML
# ---------------------------------------------------------------------------


@main.command("cdj-export")
@click.argument("rekordbox_xml", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False, path_type=Path),
              help="Directory for the AIFFs and the rewritten rekordbox_cdj.xml.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Plan the export (counts + intended files) without writing audio or XML.")
def cdj_export_cmd(rekordbox_xml: Path, out_dir: Path, dry_run: bool) -> None:
    """Transcode FLAC tracks to AIFF and rewrite a Rekordbox XML for old CDJs.

    Input is a Rekordbox collection XML (File → Export Collection in xml
    format). For every FLAC TRACK this transcodes the audio to a 16-bit AIFF
    under --out and writes <out>/rekordbox_cdj.xml repointed at those AIFFs,
    keeping every beatgrid (TEMPO) and cue (POSITION_MARK) intact. Import that
    XML back into Rekordbox and export to USB to play FLAC libraries on CDJs
    (e.g. CDJ-2000nexus) that can't read FLAC. Non-FLAC tracks pass through.
    """
    from vibechek.cdj_export import CdjExportError, export_for_cdj

    try:
        with _progress_bar("CDJ export") as progress:
            task = progress.add_task("transcoding…", total=None)

            def _on_progress(done: int, total: int, name: str) -> None:
                progress.update(task, total=total or None, completed=done, description=name)

            result = export_for_cdj(
                rekordbox_xml, out_dir, on_progress=_on_progress, dry_run=dry_run
            )
    except CdjExportError as e:
        raise click.ClickException(str(e)) from e

    verb = "Planned" if dry_run else "Converted"
    colour = "yellow" if result.errors else "green"
    console.print(
        f"[{colour}]{verb}[/] [bold]{result.flac_planned}[/] FLAC → AIFF • "
        f"[dim]{result.passthrough} passthrough • "
        f"{result.skipped} skipped • {result.errors} errors[/]"
    )
    if result.track_errors:
        console.print("[yellow]Per-track issues:[/]")
        for te in result.track_errors[:20]:
            console.print(f"  [red]{te.location}[/] — {te.message}")
        if len(result.track_errors) > 20:
            console.print(f"  [dim]… and {len(result.track_errors) - 20} more[/]")
    # Down-sampling is an IRREVERSIBLE quality reduction (hi-res source, or an
    # unprobeable one that got the CDJ-safe 44.1 kHz forced on it). The export
    # reported it and the CLI threw it away, so a DJ exporting a 96 kHz library
    # had no way to learn their AIFFs are not the masters.
    if result.resampled:
        console.print(
            f"[yellow]Down-sampled {len(result.resampled)} track(s)[/] "
            f"[dim]— the AIFF has a lower sample rate than the source[/]"
        )
        for src in result.resampled[:20]:
            console.print(f"  [yellow]{src}[/]")
        if len(result.resampled) > 20:
            console.print(f"  [dim]… and {len(result.resampled) - 20} more[/]")
    # Renames are benign but must be visible: the XML points at the new name,
    # so a user diffing --out against their collection needs to know why
    # "Track.aiff" came out as "Track_1.aiff" — and that the file already there
    # is untouched.
    if result.renamed:
        console.print(
            f"[dim]Renamed {len(result.renamed)} destination file(s) to avoid a "
            f"name already in {out_dir} — nothing was overwritten.[/]"
        )
        for name in result.renamed[:20]:
            console.print(f"  [dim]{name}[/]")
        if len(result.renamed) > 20:
            console.print(f"  [dim]… and {len(result.renamed) - 20} more[/]")
    if dry_run:
        console.print(
            f"[dim]Dry run — no audio or XML written. Output would go to[/] [cyan]{out_dir}[/]"
        )
    else:
        console.print(f"[green]Wrote[/] [cyan]{result.output_xml}[/]")
        console.print(
            "[dim]Next: import this XML into Rekordbox, then export to USB for the CDJ.[/]"
        )

    converted = result.flac_planned if dry_run else result.flac_converted
    _exit_if_total_failure(converted + result.passthrough, result.errors, "exported")


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
