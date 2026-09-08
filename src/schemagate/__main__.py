"""``python -m schemagate`` -- same as the ``schemagate`` console script."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
