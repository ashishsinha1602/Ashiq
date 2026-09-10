"""The guard on generated SQL, and the shape of the answer step.

Every case here is something a model actually does: fences round the code,
a trailing semicolon, a chatty preamble, or -- the one that matters -- a
second statement hidden after a comment.
"""
import pytest

from schemagate.answer import (UnsafeSQL, check_read_only, format_rows,
                               generate_sql, sql_prompt)


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select a, b from t where x = 1",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "```sql\nSELECT * FROM t\n```",
    "SELECT * FROM t;",
    "  \n SELECT * FROM t \n ",
])
def test_a_single_read_is_allowed(sql):
    assert check_read_only(sql).lower().startswith(("select", "with"))


@pytest.mark.parametrize("sql,why", [
    ("DELETE FROM patient", "a write"),
    ("DROP TABLE patient", "a write"),
    ("UPDATE t SET a = 1", "a write"),
    ("SELECT 1; DROP TABLE t", "two statements"),
    ("SELECT 1 -- \n; DROP TABLE t", "a statement hidden after a comment"),
    ("SELECT 1 /* x */; DELETE FROM t", "a statement hidden in a block comment"),
    ("PRAGMA table_info(t)", "not a read of data"),
    ("Sure! Here is the SQL you asked for.", "prose"),
    ("", "nothing at all"),
])
def test_anything_that_is_not_a_single_read_is_refused(sql, why):
    with pytest.raises(UnsafeSQL):
        check_read_only(sql)


def test_a_column_named_like_a_keyword_is_not_mistaken_for_one():
    """The first version of this guard matched substrings and rejected
    `SELECT updated_at`, which is a perfectly ordinary column. Word
    boundaries, not substrings."""
    for sql in ("SELECT updated_at FROM t",
                "SELECT deleted_flag, created_on FROM audit_event_001",
                "SELECT c.insertion_point FROM c"):
        assert check_read_only(sql)


def test_a_keyword_inside_a_string_literal_is_data_not_sql():
    """`WHERE denial_reason = 'duplicate claim'` is a real query against the
    demo schema. A guard that reads inside quotes refuses it."""
    assert check_read_only(
        "SELECT * FROM claim WHERE denial_reason = 'delete this duplicate'")


def test_insufficient_is_reported_as_a_selection_problem():
    """When the model says the tables cannot answer the question, the useful
    message is about selection -- top_k, or a hint -- not about the model."""
    class P:
        def complete(self, system, prompt, max_tokens=500):
            return "INSUFFICIENT"
    with pytest.raises(UnsafeSQL) as e:
        generate_sql(P(), "q", "TABLE t ()")
    assert "top-k" in str(e.value) or "hint" in str(e.value)


def test_the_prompt_carries_only_the_selected_tables():
    """The point of the whole library: a table the caller may not see is not
    in the prompt, so the model cannot write SQL against it."""
    frag = "TABLE med.lab_result (\n  analyte VARCHAR(120)\n)\n"
    p = sql_prompt("which results are abnormal", frag, dialect="sqlite")
    assert "lab_result" in p and "staff_salary" not in p
    assert "sqlite" in p


def test_generated_sql_is_checked_before_it_is_returned():
    class Malicious:
        def complete(self, system, prompt, max_tokens=500):
            return "SELECT 1; DROP TABLE patient"
    with pytest.raises(UnsafeSQL):
        generate_sql(Malicious(), "q", "TABLE t ()")


def test_rows_render_without_a_table_library():
    out = format_rows(["analyte", "value"], [("potassium", 6.4), ("sodium", None)])
    assert "analyte" in out and "potassium" in out
    assert "None" not in out          # NULL renders as blank, not the word


def test_no_rows_says_so_rather_than_printing_nothing():
    assert "(no rows)" in format_rows(["a"], [])


def test_provider_none_is_the_no_key_path_not_an_error(tmp_path, capsys):
    """The published docs tell people to run `--provider none` to get the
    paste prompt without an API key. It used to exit asking for a `--model`
    it would never use, so the documented no-key path was the one that did
    not work."""
    import sqlite3

    from schemagate.cli import main

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE customer (id INTEGER PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()

    rc = main(["describe", "--url", f"sqlite:///{db}", "--provider", "none"])
    assert rc == 0
    assert "customer" in capsys.readouterr().out
