"""Core data types."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def allowed(roles: Optional[List[str]], principal: Any) -> bool:
    """Whether ``principal`` may see something guarded by ``roles``.

    One function for objects and for columns, deliberately. Two copies of a
    visibility rule drift, and a drifted ACL is the failure this library
    exists to prevent -- so there is one rule and both callers use it.

    No roles means everyone: absence of a restriction is not a restriction.
    Roles with no principal means nobody, which is the important half. An
    anonymous caller failing open would hand every restricted column to the
    first request that forgot to say who it was for.
    """
    if not roles:
        return True
    if principal is None:
        return False
    return principal.has_any_role(frozenset(roles))


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
    #: Roles that may see this column. None means everyone, the same as on
    #: ObjectDoc. A column-level restriction is for the case where the table
    #: is the answer and one column in it is not: salary on an employee table,
    #: a national insurance number on a patient.
    roles: Optional[List[str]] = None
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

    def render_parts(self) -> "tuple[str, str]":
        """``(declaration, note)`` -- kept apart so the caller can put the
        DDL's own comma between them.

        Joined into one string, the comma a column list needs lands *after*
        the comment, and `one of: 'Acme', 'Globex',` reads as a list that
        continues rather than one that ended. Every comment had the problem;
        value lists are just where it became obvious.
        """
        bits = [self.name, self.type]
        if self.pk:
            bits.append("PK")
        if not self.nullable:
            bits.append("NOT NULL")
        notes = []
        if self.values:
            notes.append("one of: " + ", ".join(repr(v) for v in self.values))
        comment = _one_line(self.comment)
        if comment:
            notes.append(comment)
        return " ".join(bits), "; ".join(notes)

    def render(self) -> str:
        decl, note = self.render_parts()
        return f"{decl}  -- {note}" if note else decl


@dataclass
class ForeignKey:
    columns: List[str]
    ref_table: str
    ref_columns: List[str] = field(default_factory=list)
    #: False when the database declared this constraint, True when it was read
    #: out of the naming convention instead. Kept apart so the DDL can say so:
    #: a model told `tenant_id -> tenants` as fact would write a join the
    #: database will not enforce, and sometimes that join is wrong.
    inferred: bool = False


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

    def visible_columns(self, principal: Any = None) -> List[Column]:
        """The columns this caller may see, in declaration order."""
        return [c for c in self.columns if allowed(c.roles, principal)]

    def render_ddl(self, max_columns: int = 40, principal: Any = None) -> str:
        """The DDL for this object as one caller may see it.

        A restricted column is absent -- not masked, not renamed, no REDACTED
        placeholder. The name is itself the disclosure: `ssn REDACTED` tells a
        model the table holds one, and a model that knows a column exists can
        ask about it, join on it, or mention it in an explanation. A name that
        was never in the prompt cannot be referenced.
        """
        head = f"{self.kind} {self.qname}"
        note = _one_line(self.hint or _sentence(self.description))
        lines = [f"-- {note}" if note else "", head + " ("]
        visible = self.visible_columns(principal)
        cols = visible[:max_columns]
        for c in cols:
            decl, note = c.render_parts()
            # Comma first, then the comment. The other order hands the list's
            # last value a trailing comma it does not own.
            lines.append(f"  {decl},  -- {note}" if note else f"  {decl},")
        if len(visible) > max_columns:
            lines.append(f"  -- ...{len(visible) - max_columns} more columns")
        # The last column carries no comma. With the comma now before the
        # comment it is no longer the last character, so trim it where it is.
        if lines[-1].endswith(","):
            lines[-1] = lines[-1][:-1]
        elif "  -- " in lines[-1]:
            decl, _, note = lines[-1].partition("  -- ")
            lines[-1] = f"{decl.rstrip().rstrip(',')}  -- {note}"
        lines.append(")")
        # A foreign-key line names its columns, so it puts back the exact
        # identifier the loop above just withheld. Drop any line that does.
        shown = {c.name for c in visible}
        for fk in self.foreign_keys:
            if all(c in shown for c in fk.columns):
                note = "  (inferred from the column name, not declared)" if fk.inferred else ""
                lines.append(
                    f"-- FK {self.name}({','.join(fk.columns)}) -> {fk.ref_table}{note}")
        return "\n".join(l for l in lines if l)


@dataclass
class Scored:
    doc: ObjectDoc
    score: float
    #: Why this object was selected. `hybrid` means both the vector and the
    #: lexical index ranked it, which is the strongest signal available and
    #: was missing from this list for long enough that `Selection.explain()`
    #: printed a value the type said could not occur.
    reason: str = "vector"     # hybrid | vector | lexical | fk | pinned


@dataclass
class Selection:
    """Result of Catalog.select()."""
    question: str
    hits: List[Scored]
    total_objects: int = 0
    #: Who this selection was made for. Carried on the result rather than
    #: passed to `prompt_fragment` by the caller, because the fragment and the
    #: audit record must describe the same caller -- a selection rendered for
    #: someone other than the principal it was scored for is a hole.
    principal: Any = None

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
        return "\n\n".join(d.render_ddl(max_columns, principal=self.principal)
                            for d in self.objects)

    def to_dict(self) -> Dict[str, Any]:
        """A record of what was shown to whom, and what was held back.

        The thing an auditor asks for after the fact, and the thing that is
        impossible to reconstruct later: the catalog changes, roles change,
        and the question is gone. `columns_withheld` is the count, never the
        names -- a log that lists the columns it withheld has disclosed them
        to everyone who can read the log.
        """
        who = self.principal
        return {
            "question": self.question,
            "principal": getattr(who, "subject", None),
            "roles": sorted(getattr(who, "roles", None) or []),
            "total_objects": self.total_objects,
            "hits": [{
                "object": h.doc.qname,
                "kind": h.doc.kind,
                "score": h.score,
                "reason": h.reason,
                "columns_shown": len(h.doc.visible_columns(who)),
                "columns_withheld": len(h.doc.columns) - len(h.doc.visible_columns(who)),
            } for h in self.hits],
        }

    def explain(self) -> str:
        return "\n".join(
            f"{h.score:6.3f}  {h.reason:8s}  {h.doc.qname}" for h in self.hits
        )

    def __len__(self) -> int:
        return len(self.hits)

    def __repr__(self) -> str:
        return f"<Selection {len(self.hits)}/{self.total_objects}: {self.table_names}>"
