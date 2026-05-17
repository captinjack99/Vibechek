"""PyInstaller entry shim.

Lives at the repo root because PyInstaller resolves spec-relative paths from
the directory it's invoked from. Keeping the shim separate from the package
lets us drop frozen-build-only setup here without polluting the import path
of normal `pip install` users.
"""

from vibechek.cli import main

if __name__ == "__main__":
    main()
