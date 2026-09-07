import sqlite3, sys, os, tempfile, pytest
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
