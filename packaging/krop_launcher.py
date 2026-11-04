# PyInstaller entry point for krop (frozen builds).
import multiprocessing
from krop.application import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
