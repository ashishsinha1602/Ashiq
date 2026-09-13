import sqlite3, sys, os, tempfile, pytest

# The suite pins the hashed embedder, whatever this machine happens to have
# installed. Catalog() now picks a sentence model when one is available, which
# is right for a user and wrong for these tests: the expected rankings are the
# hashed embedder's, the JS twin has no sentence model so parity could never
# hold, and CI installs only [dev] -- so without this, a developer with
# schemagate[huggingface] runs a different suite from the one that gates the
# merge. That exact gap (hypothesis absent locally, present in CI) already hid
# a real bug for a whole session.
os.environ.setdefault("SCHEMAGATE_AUTO_EMBEDDER", "0")
sys.path.insert(0, os.path.dirname(__file__))
from schema_fixture import DDL, HINTS
from schemagate import Catalog

@pytest.fixture(scope="session")
def db_url():
    p = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(p); c.executescript(DDL); c.commit(); c.close()
    return f"sqlite:///{p}"

@pytest.fixture
def cat(db_url):
    c = Catalog().bootstrap(db_url)
    for t, h in HINTS.items():
        c.hint(t, h)
    return c.index()
