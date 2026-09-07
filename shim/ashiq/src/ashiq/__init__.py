"""ashiq was renamed to schemagate. This package only forwards to it."""
import warnings as _w

from schemagate import *  # noqa: F401,F403
from schemagate import __version__ as _v

_w.warn("'ashiq' was renamed to 'schemagate'; pip install schemagate and "
        "change your imports.", DeprecationWarning, stacklevel=2)
__version__ = _v
