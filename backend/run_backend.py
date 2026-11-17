"""PyInstaller entry point for the `smartspace-backend` sidecar."""

import multiprocessing

from app.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
