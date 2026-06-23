"""PyInstaller entry shim.

Lives at the repo root because PyInstaller resolves spec-relative paths from
the directory it's invoked from. Keeping the shim separate from the package
lets us drop frozen-build-only setup here without polluting the import path
of normal `pip install` users.
"""

import multiprocessing

from vibechek.cli import main

if __name__ == "__main__":
    # The Windows native engine runs analyze IN-PROCESS in this frozen sidecar
    # (unlike the WSL / managed-venv paths, which delegate to a real Python).
    # `analyzer` uses multiprocessing.get_context("spawn"), and spawn re-execs
    # this binary for each worker — without freeze_support() the bootloader
    # would re-run `main()` in every worker instead of acting as a worker,
    # fork-bombing the analyze. Harmless no-op for the non-frozen/CLI path and
    # on the engines that don't analyze in-process. MUST be the first call.
    multiprocessing.freeze_support()
    main()
