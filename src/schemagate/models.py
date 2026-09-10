"""Core data types."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


def _identifiers(sql: str, limit: int = 1200) -> str:
    """Distinct identifiers from view SQL, keywords stripped."""
    import re
    kw = {"select", "from", "where", "join", "left", "right", "inner", "outer",
          "on", "group", "by", "order", "as", "and", "or", "not", "null", "case",
          "when", "then", "else", "end", "sum", "count", "avg", "min", "max",
          "distinct", "union", "all", "having", "with", "create", "view", "is",
          "substr", "cast", "coalesce", "asc", "desc", "limit", "cross", "full",
          "exists", "between", "like", "over", "partition", "rows", "range",
          "fetch", "first", "only", "top", "offset", "in", "any", "some",
          # functions: Oracle, Postgres, SQL Server, MySQL. Noise, not concepts.
          "nvl", "nvl2", "decode", "trunc", "sysdate", "systimestamp", "rownum",
          "rowid", "dual", "to_char", "to_date", "to_number", "add_months",
          "months_between", "listagg", "instr", "lpad", "rpad", "regexp_like",
          "regexp_substr", "nullif", "greatest", "least", "round", "floor",
          "ceil", "abs", "mod", "power", "sqrt", "upper", "lower", "initcap",
          "length", "replace", "concat", "date_trunc", "extract", "now",
          "current_date", "current_timestamp", "getdate", "dateadd", "datediff",
          "datepart", "isnull", "ifnull", "convert", "len", "charindex",
          "julianday", "strftime", "lag", "lead", "row_number", "rank",
          "dense_rank", "ntile", "first_value", "last_value", "string_agg",
          "group_concat", "array_agg", "unnest", "json_value", "json_query"}
    seen, out = set(), []
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql):
        t = tok.lower()
        if t in kw or len(t) < 3 or t in seen:
            continue
        seen.add(t)
        out.append(tok.replace("_", " "))
        out.append(tok)
    return " ".join(out)[:limit]


def _sentence(text: Optional[str]) -> Optional[str]:
    """The human sentence of a description, without the retrieval words
    a describer may have appended after ' | '."""
    if not text:
        return text
    return text.split(" | ", 1)[0].strip()


def _one_line(text: Optional[str]) -> Optional[str]:
    """Comments and hints go into ``-- ...`` lines. A newline inside one
    would put uncommented text into the DDL the model receives, and
    multi-line table comments are common in Oracle and PostgreSQL."""
    if not text:
        return text
    return " ".join(str(text).split())


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    comment: Optional[str] = None
    pk: bool = False
    #: The distinct values this column actually holds, when there are few
    #: enough to be worth saying. Empty unless reflection was asked to look:
    #: it is the one thing here that reads data rather than the catalog.
    #:
    #: This exists because of a specific wrong answer. A model was given
    #: `status VARCHAR(30)` and wrote `WHERE status = 'DENIED'`. The rows say
    #: `denied`. The query was correct in every way a schema can express and
    #: returned nothing, which is the worst kind of wrong -- it looks like an
    #: empty result, not a mistake. No model can guess the casing of a value
    #: it has never seen, so the fix is to stop asking it to.
    values: Optional[List[str]] = None

    def render(self) -> str:
        bits = [self.name, self.type]
        if self.pk:
            bits.append("PK")
        if not self.nullable:
            bits.append("NOT NULL")
        s = " ".join(bits)
        notes = []
        if self.values:
            notes.append("one of: " + ", ".join(repr(v) for v in self.values))
        comment = _one_line(self.comment)
        if comment:
            notes.append(comment)
        return f"{s}  -- {'; '.join(notes)}" if notes else s


@dataclass
class ForeignKey:
    columns: List[str]
    ref_table: str
    ref_columns: List[str] = field(default_factory=list)


@dataclass
class ObjectDoc:
    """One catalog entry: a table, view, or API endpoint."""
    name: str
    schema: Optional[str] = None
    kind: str = "TABLE"                       # TABLE | VIEW | ENDPOINT
    description: Optional[str] = None         # generated
    hint: Optional[str] = None                # human, always wins
    columns: List[Column] = field(default_factory=list)
    foreign_keys: List[ForeignKey] = field(default_factory=list)
    row_estimate: Optional[int] = None
    definition: Optional[str] = None          # view SQL, if any
    roles: Optional[List[str]] = None         # None => visible to all
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def qname(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def embed_text(self) -> str:
        """The text that gets vectorised. Hint first: it carries the most signal."""
        parts = [self.name.replace("_", " "), self.name]
        if self.hint:
            parts.append(self.hint)
        if self.description:
            parts.append(self.description)
        parts.extend(c.name.replace("_", " ") for c in self.columns)
        parts.extend(c.comment for c in self.columns if c.comment)
        if self.definition:
            # A view's output columns hide the logic that makes it findable:
            # v_stock_shortfall exposes only 'shortfall', while 'reorder_point'
            # lives in the SELECT. Index the definition so the view is
            # retrievable by the concepts it computes over.
            parts.append(_identifiers(self.definition))
        return " \n".join(p for p in parts if p)

    def render_ddl(self, max_columns: int = 40) -> str:
        head = f"{self.kind} {self.qname}"
        note = _one_line(self.hint or _sentence(self.description))
        lines = [f"-- {note}" if note else "", head + " ("]
        cols = self.columns[:max_columns]
        lines += [f"  {c.render()}," for c in cols]
        if len(self.columns) > max_columns:
            lines.append(f"  -- ...{len(self.columns) - max_columns} more columns")
        if lines[-1].endswith(","):
            lines[-1] = lines[-1][:-1]
        lines.append(")")
        for fk in self.foreign_keys:
            lines.append(f"-- FK {self.name}({','.join(fk.columns)}) -> {fk.ref_table}")
        return "\n".join(l for l in lines if l)


@dataclass
class Scored:
    doc: ObjectDoc
    score: float
    reason: str = "vector"     # vector | lexical | fk | pinned


@dataclass
class Selection:
    """Result of Catalog.select()."""
    question: str
    hits: List[Scored]
    total_objects: int = 0

    @property
    def objects(self) -> List[ObjectDoc]:
        return [h.doc for h in self.hits]

    @property
    def object_list(self) -> List[Dict[str, str]]:
        """Shape accepted by Oracle Select AI SET_ATTRIBUTE object_list."""
        out = []
        for d in self.objects:
            e = {"name": d.name}
            if d.schema:
                e["owner"] = d.schema
            out.append(e)
        return out

    @property
    def table_names(self) -> List[str]:
        return [d.qname for d in self.objects]

    def prompt_fragment(self, max_columns: int = 40) -> str:
        return "\n\n".join(d.render_ddl(max_columns) for d in self.objects)

    def explain(self) -> str:
        return "\n".join(
            f"{h.score:6.3f}  {h.reason:8s}  {h.doc.qname}" for h in self.hits
        )

    def __len__(self) -> int:
        return len(self.hits)

    def __repr__(self) -> str:
        return f"<Selection {len(self.hits)}/{self.total_objects}: {self.table_names}>"
