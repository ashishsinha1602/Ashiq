#!/usr/bin/env python3
"""Certify schemagate against a real database, whatever the dialect.

    python scripts/certify_dialect.py 'postgresql+psycopg://user:pw@host/db'

The implementation moved into the package, so `schemagate certify <url>` does
the same thing and works from a pip install. This wrapper stays because the
path is in the README and in a published post.
"""
import sys
import traceback

from schemagate.certify import main

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    try:
        sys.exit(main(sys.argv[1]))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
