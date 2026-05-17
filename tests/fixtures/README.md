# Test fixtures

Drop small (under ~1 MB) audio files here to enable the tagger integration tests
that round-trip tag backup → restore.

Acceptable formats: `.mp3`, `.flac`, `.m4a`.

Use royalty-free content — these files get checked into the repo.

A few good sources for tiny test clips:
- https://opengameart.org/art-search-advanced?field_art_type_tid%5B0%5D=12 (CC0)
- https://freesound.org/ (filter for CC0)
- Or generate your own 1-second silent file with `ffmpeg`:
  ```
  ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 1 silent.flac
  ```

Without fixtures the integration test in `tests/test_tagger.py` is skipped.
Everything else runs regardless.
