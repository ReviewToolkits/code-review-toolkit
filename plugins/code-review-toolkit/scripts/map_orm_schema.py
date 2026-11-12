#!/usr/bin/env python3
"""Inventory ORM models statically and check them against annotations, migrations and lookups.

An ORM application's type design lives in its models, and three of its
defect classes are mechanical to find but tedious by hand: a column whose
nullability disagrees with its annotation, a constraint declared on a model
that no migration ever created, and a family of sibling lookups whose filter
columns do not match the model's unique keys. SQLAlchemy declarative models
(``DeclarativeBase``, ``declarative_base()``, ``Mapped[]``/``mapped_column``,
legacy ``Column``) and Django models (``models.Model``) are recognised;
anything else makes the sections say ``"status": "not_detected"`` with a
reason.

Sections:

- ``models``: each mapped class with its table, primary key, unique keys
  (``primary_key``, ``unique=True``, ``UniqueConstraint``, unique ``Index``
  including ``lower(col)`` expressions and partial indexes, Django
  ``unique_together``/``Meta.constraints``) and named constraints. Columns
  inherited from project bases and mixins are included.
- ``nullability``: ``column_mismatches`` (``nullable_but_annotation_not_optional``:
  the database accepts NULL but ``Mapped[X]`` promises a value;
  ``not_null_but_annotation_optional``), ``relationship_mismatches`` (a scalar
  ``Mapped[Target]`` relationship that can be None: a nullable foreign key, a
  ``secondary`` table, or the one-to-many side), ``implicitly_nullable_columns``
  (``Column``/``mapped_column`` with no annotation, ``nullable=`` or primary
  key: NULL by default) and ``unannotated_columns``. Annotated aliases
  (``str_or_none = Annotated[str | None, ...]``) are resolved.
- ``migrations``: named model constraints and indexes against the Alembic
  revision chain (``op.create_*``/``op.drop_*``, ``sa.*Constraint(name=)``
  in ``create_table``, ``op.f()``, and raw-SQL ``ADD/DROP/RENAME CONSTRAINT``
  and ``CREATE/DROP INDEX``) or Django migrations (``AddConstraint``,
  ``RemoveConstraint``, ``AddIndex``, ``RemoveIndex``, ``RenameIndex``),
  replayed in revision order. Leads: ``declared_never_created``,
  ``declared_but_dropped``, ``check_constraints_only_in_migrations``.
  Unnamed constraints cannot be matched statically and are counted.
- ``lookups``: query filters (``filter``/``where``/``filter_by``, Django
  ``filter``/``get`` keywords, ``and_``/``or_``, ``func.lower(col)``,
  ``__iexact``) per function, with the terminal call (``one``, ``first``,
  ``get``, ...) and whether the equality filters cover a unique key.
  ``sibling_groups`` puts same-named methods of related classes side by side
  with the model's unique keys (``filter_sets_differ`` is the lead);
  ``single_row_lookups_not_unique`` lists ``one()``/``one_or_none()``/
  ``scalar_one*()``/Django ``get()`` whose filters cover no unique key.

This is a static view. The plugin's ``docs/orm-schema-diff.md`` gives the
live check: compare the metadata against a database migrated to head, and
introspect primary keys, CHECK constraints and indexes directly, which
autogenerate comparisons do not report.

Usage:
    python map_orm_schema.py [path] [--max-files N]
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (
    build_envelope,
    collect_python_files,
    discover_python_files,
    emit,
    migration_kind,
    parse_common_args,
    parse_source,
    relative_to_root,
    resolve_target,
)

_CAP = 50

_SA_BASES = frozenset({"DeclarativeBase", "DeclarativeBaseNoMeta"})
_SA_BASE_FACTORIES = frozenset({"declarative_base", "generate_base"})
_SA_MAPPING_DECORATORS = frozenset({"as_declarative", "mapped", "mapped_as_dataclass"})
_SA_COLUMN_CALLS = frozenset({"Column", "mapped_column"})
_COLLECTION_ANNOTATIONS = frozenset(
    {"list", "List", "set", "Set", "Sequence", "dict", "Dict"}
)
_DYNAMIC_MAPPED = frozenset({"WriteOnlyMapped", "DynamicMapped"})
_DJANGO_RELATIONS = frozenset({"ForeignKey", "OneToOneField"})
_SINGLE_ROW_TERMINALS = frozenset(
    {"one", "one_or_none", "scalar_one", "scalar_one_or_none"}
)
_TERMINALS = _SINGLE_ROW_TERMINALS | {
    "first",
    "scalar",
    "exists",
    "count",
    "all",
    "scalars",
}
_FILTER_METHODS = frozenset({"filter", "where", "filter_by"})
_EXECUTE_METHODS = frozenset({"execute", "scalars", "scalar"})
_CASE_FOLD = frozenset({"lower", "upper", "casefold"})
_DJANGO_EQ_LOOKUPS = {"exact": None, "iexact": "lower"}

# Raw SQL in migrations.
_SQL_ADD_CONSTRAINT = re.compile(r"ADD\s+CONSTRAINT\s+\"?(\w+)", re.IGNORECASE)
_SQL_DROP_CONSTRAINT = re.compile(
    r"DROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?\"?(\w+)", re.IGNORECASE
)
_SQL_RENAME_CONSTRAINT = re.compile(
    r"RENAME\s+CONSTRAINT\s+\"?(\w+)\"?\s+TO\s+\"?(\w+)", re.IGNORECASE
)
_SQL_CREATE_INDEX = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\"?(\w+)",
    re.IGNORECASE,
)
_SQL_DROP_INDEX = re.compile(
    r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?\"?(\w+)", re.IGNORECASE
)
_SQL_RENAME_TABLE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?\"?(\w+)\"?\s+RENAME\s+TO\s+\"?(\w+)",
    re.IGNORECASE,
)
_SQL_RENAME_INDEX = re.compile(
    r"ALTER\s+INDEX\s+(?:IF\s+EXISTS\s+)?\"?(\w+)\"?\s+RENAME\s+TO\s+\"?(\w+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _terminal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _terminal(node.value)
    return None


def _call_name(node: ast.AST | None) -> str | None:
    return _terminal(node.func) if isinstance(node, ast.Call) else None


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _bool_kw(call: ast.Call, name: str) -> bool | None:
    value = _kw(call, name)
    if isinstance(value, ast.Constant) and isinstance(value.value, bool):
        return value.value
    return None


def _unwrap_op_f(node: ast.AST | None) -> ast.AST | None:
    """`op.f("name")` -> the string node."""
    if isinstance(node, ast.Call) and _call_name(node) == "f" and node.args:
        return node.args[0]
    return node


def _assigned(stmt: ast.AST) -> list[str]:
    if isinstance(stmt, ast.Assign):
        return [t.id for t in stmt.targets if isinstance(t, ast.Name)]
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id]
    return []


def _parse_annotation(node: ast.AST | None) -> ast.AST | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
    return node


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _optional(node: ast.AST | None, aliases: dict[str, dict]) -> bool | None:
    """Whether a type expression admits None; None when it cannot be told."""
    node = _parse_annotation(node)
    if node is None:
        return None
    if _is_none(node):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _optional(node.left, aliases), _optional(node.right, aliases)
        return bool(left) or bool(right)
    if isinstance(node, ast.Subscript):
        head = _terminal(node.value)
        if head == "Optional":
            return True
        if head == "Union":
            elts = (
                node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            )
            return any(_optional(e, aliases) for e in elts)
        if head == "Annotated":
            first = (
                node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice
            )
            return _optional(first, aliases)
        return False
    name = _terminal(node)
    if name in aliases:
        return aliases[name]["optional"]
    return False


def _mapped_inner(annotation: ast.AST | None) -> tuple[str | None, ast.AST | None]:
    """(`Mapped`/`WriteOnlyMapped`/..., inner type) for a Mapped annotation."""
    annotation = _parse_annotation(annotation)
    if isinstance(annotation, ast.Subscript):
        head = _terminal(annotation.value)
        if head in ("Mapped",) or head in _DYNAMIC_MAPPED:
            return head, _parse_annotation(annotation.slice)
    return None, None


def _column_refs(node: ast.AST) -> tuple[str | None, str | None]:
    """(wrapper, column name) for `"col"`, `Model.col`, `col`, `func.lower(Model.col)`."""
    if _str(node) is not None:
        return None, _str(node)
    if isinstance(node, ast.Attribute):
        return None, node.attr
    if isinstance(node, ast.Name):
        return None, node.id
    if (
        isinstance(node, ast.Call)
        and node.args
        and _call_name(node) in _CASE_FOLD | {"Lower", "Upper"}
    ):
        _inner_wrapper, col = _column_refs(node.args[0])
        return (_call_name(node) or "").lower(), col
    return None, None


# ---------------------------------------------------------------------------
# Per-file collection
# ---------------------------------------------------------------------------


class _File:
    def __init__(self, rel: str, tree: ast.Module) -> None:
        self.rel = rel
        self.tree = tree
        self.classes: dict[str, ast.ClassDef] = {}
        self.base_vars: set[str] = set()  # names bound to declarative_base()
        self.aliases: dict[str, dict] = {}
        self.imports: dict[str, str] = {}  # local name -> imported simple name
        self.import_modules: dict[str, str] = {}  # local name -> absolute module
        self.module = ".".join(
            p for p in Path(rel).with_suffix("").parts if p not in ("src", "__init__")
        )
        self.naming_convention = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = alias.name
                    self.import_modules[alias.asname or alias.name] = (
                        node.module or "" if not node.level else ""
                    )
            elif (
                isinstance(node, ast.Call)
                and _kw(node, "naming_convention") is not None
            ):
                self.naming_convention = True
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
            elif (
                isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
            ):
                names = _assigned(node)
                if _call_name(node.value) in _SA_BASE_FACTORIES:
                    self.base_vars.update(names)
                elif isinstance(node.value, (ast.Subscript, ast.BinOp)):
                    for name in names:
                        self.aliases[name] = {"node": node.value}


def _resolve_aliases(files: list[_File]) -> dict[str, dict]:
    """Project-wide type aliases: optional-ness and an embedded mapped_column."""
    raw = {name: info["node"] for f in files for name, info in f.aliases.items()}
    out: dict[str, dict] = {}
    for name, node in raw.items():
        embedded = None
        if isinstance(node, ast.Subscript) and _terminal(node.value) == "Annotated":
            elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else []
            for extra in elts[1:]:
                if _call_name(extra) in _SA_COLUMN_CALLS:
                    embedded = extra
        out[name] = {"optional": None, "column": embedded, "node": node}
    for _ in range(3):  # aliases of aliases
        for name, info in out.items():
            info["optional"] = _optional(info["node"], out)
    return out


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class _Model:
    def __init__(self, f: _File, cls: ast.ClassDef, kind: str) -> None:
        self.file = f
        self.cls = cls
        self.kind = kind  # "sqlalchemy" | "django"
        self.name = cls.name
        self.table: str | None = None
        self.abstract = False
        self.columns: dict[str, dict] = {}
        self.relationships: list[dict] = []
        self.unique_keys: list[dict] = []
        self.constraints: list[dict] = []
        self.unnamed_constraints = 0
        self.unnamed_checks: list[tuple[str, int]] = []  # (normalised SQL, line)
        self.ancestors: list[str] = [cls.name]  # simple names, self first
        self.relationship_fks: dict[str, list[str]] = {}  # relationship -> FK columns


class _ClassIndex:
    """Project classes keyed by `file::Name`, with bases resolved through imports
    (two classes may share a simple name in different modules)."""

    def __init__(self, files: list[_File]) -> None:
        self.by_key: dict[str, tuple[_File, ast.ClassDef]] = {}
        self.by_simple: dict[str, list[str]] = defaultdict(list)
        for f in files:
            for name, cls in f.classes.items():
                key = f"{f.rel}::{name}"
                self.by_key[key] = (f, cls)
                self.by_simple[name].append(key)

    def resolve(self, f: _File, base: ast.AST) -> str | None:
        name = _terminal(base)
        if not name:
            return None
        if isinstance(base, ast.Name) and name in f.classes:
            return f"{f.rel}::{name}"
        original = f.imports.get(name, name)
        candidates = self.by_simple.get(original, [])
        module = f.import_modules.get(name)
        if module:
            for key in candidates:
                if self.by_key[key][0].module == module:
                    return key
        same_dir = [
            k for k in candidates if str(Path(k).parent) == str(Path(f.rel).parent)
        ]
        if len(candidates) == 1:
            return candidates[0]
        return same_dir[0] if len(same_dir) == 1 else None

    def bases(self, key: str) -> list[str]:
        f, cls = self.by_key[key]
        return [
            b for b in (self.resolve(f, base) for base in cls.bases) if b and b != key
        ]


def _class_kind(
    key: str,
    index: _ClassIndex,
    base_vars: set[str],
    memo: dict,
) -> str | None:
    if key in memo:
        return memo[key]
    memo[key] = None
    entry = index.by_key.get(key)
    if entry is None:
        return None
    _f, cls = entry
    kind = None
    for dec in cls.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if _terminal(target) in _SA_MAPPING_DECORATORS:
            kind = "sqlalchemy"
    for base in cls.bases:
        bname = _terminal(base)
        if bname in _SA_BASES or bname in base_vars:
            kind = "sqlalchemy"
        elif isinstance(base, ast.Attribute) and bname == "Model":
            owner = _terminal(base.value)
            kind = (
                "django"
                if owner == "models"
                else "sqlalchemy"
                if owner == "db"
                else kind
            )
    for base_key in index.bases(key):
        kind = kind or _class_kind(base_key, index, base_vars, memo)
    memo[key] = kind
    return kind


def _column_from_call(
    call: ast.Call, attr: str, annotation: ast.AST | None, aliases: dict, kind: str
) -> dict:
    if kind == "django":
        nullable = _bool_kw(call, "null") is True
        pk = _bool_kw(call, "primary_key") is True
        return {
            "name": attr,
            "db_name": f"{attr}_id" if _call_name(call) in _DJANGO_RELATIONS else attr,
            "line": call.lineno,
            "annotation": None,
            "annotation_optional": None,
            "nullable_explicit": _bool_kw(call, "null"),
            "nullable": nullable and not pk,
            "primary_key": pk,
            "unique": _bool_kw(call, "unique") is True or pk,
            "index": _bool_kw(call, "db_index") is True,
            "foreign_key": _django_target(call),
            "server_default": _kw(call, "db_default") is not None,
            "via": _call_name(call),
        }
    _head, inner = _mapped_inner(annotation)
    alias_column = None
    if inner is not None and _terminal(inner) in aliases:
        alias_column = aliases[_terminal(inner)]["column"]
    explicit = _bool_kw(call, "nullable")
    if explicit is None and alias_column is not None:
        explicit = _bool_kw(alias_column, "nullable")
    pk = _bool_kw(call, "primary_key") is True
    optional = _optional(inner, aliases) if inner is not None else None
    if explicit is not None:
        nullable = explicit
    elif pk:
        nullable = False
    elif optional is not None and _call_name(call) == "mapped_column":
        nullable = optional
    else:
        nullable = True  # Column() and an unannotated mapped_column()
    fk = None
    for arg in call.args:
        if _call_name(arg) == "ForeignKey" and arg.args:
            fk = _str(arg.args[0]) or ast.unparse(arg.args[0])
    db_name = _str(call.args[0]) if call.args and _str(call.args[0]) else attr
    return {
        "name": attr,
        "db_name": db_name,
        "line": call.lineno,
        "annotation": ast.unparse(annotation) if annotation is not None else None,
        "annotation_optional": optional,
        "nullable_explicit": explicit,
        "nullable": nullable,
        "primary_key": pk,
        "unique": _bool_kw(call, "unique") is True,
        "index": _bool_kw(call, "index") is True,
        "foreign_key": fk,
        "server_default": _kw(call, "server_default") is not None
        or (
            alias_column is not None and _kw(alias_column, "server_default") is not None
        ),
        "via": _call_name(call),
    }


def _django_target(call: ast.Call) -> str | None:
    """Target model of a ForeignKey/OneToOneField (`"app.Model"` or a name)."""
    if _call_name(call) not in _DJANGO_RELATIONS or not call.args:
        return None
    return _str(call.args[0]) or _terminal(call.args[0])


def _own_members(
    cls: ast.ClassDef, aliases: dict, kind: str, model_names: set[str]
) -> tuple[dict, list, list]:
    columns: dict[str, dict] = {}
    relationships: list[dict] = []
    unannotated: list[str] = []
    for stmt in cls.body:
        names = _assigned(stmt)
        if not names:
            continue
        attr = names[0]
        value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
        annotation = stmt.annotation if isinstance(stmt, ast.AnnAssign) else None
        call_name = _call_name(value)
        if kind == "django":
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and (
                    (call_name or "").endswith("Field")
                    or call_name in _DJANGO_RELATIONS
                )
            ):
                columns[attr] = _column_from_call(value, attr, None, aliases, kind)
            continue
        if call_name in _SA_COLUMN_CALLS:
            columns[attr] = _column_from_call(value, attr, annotation, aliases, kind)
            if annotation is None:
                unannotated.append(attr)
        elif call_name == "relationship":
            head, inner = _mapped_inner(annotation)
            relationships.append(
                {
                    "name": attr,
                    "line": stmt.lineno,
                    "call": value,
                    "annotation": ast.unparse(annotation)
                    if annotation is not None
                    else None,
                    "inner": inner,
                    "mapped": head,
                }
            )
        elif value is None and annotation is not None:
            head, inner = _mapped_inner(annotation)
            if head == "Mapped" and _terminal(inner) not in model_names:
                fake = ast.Call(func=ast.Name("mapped_column"), args=[], keywords=[])
                fake.lineno = stmt.lineno
                columns[attr] = _column_from_call(fake, attr, annotation, aliases, kind)
    return columns, relationships, unannotated


def _table_args(cls: ast.ClassDef) -> list[ast.AST]:
    """Elements of `__table_args__` (a tuple, a dict, or returned by a declared_attr)."""
    for stmt in cls.body:
        value = None
        if "__table_args__" in _assigned(stmt):
            value = stmt.value
        elif (
            isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
            and stmt.name == "__table_args__"
        ):
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Return):
                    value = sub.value
        if isinstance(value, (ast.Tuple, ast.List)):
            return list(value.elts)
    return []


def _django_meta(cls: ast.ClassDef) -> ast.ClassDef | None:
    for stmt in cls.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta":
            return stmt
    return None


def _template_name(node: ast.AST | None, table: str | None) -> str | None:
    """`f"{cls.__tablename__}_valid"` with the model's table substituted."""
    if not isinstance(node, ast.JoinedStr) or table is None:
        return None
    out = ""
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            out += value.value
        elif isinstance(value, ast.FormattedValue) and ast.unparse(value.value) in (
            "cls.__tablename__",
            "self.__tablename__",
        ):
            out += table
        else:
            return None
    return out


def _constraint_record(
    call: ast.Call, kind: str, table: str | None = None
) -> dict | None:
    name_node = _unwrap_op_f(_kw(call, "name"))
    cname = _call_name(call)
    columns: list[tuple[str | None, str | None]] = []
    if kind == "django":
        fields = _kw(call, "fields")
        if isinstance(fields, (ast.List, ast.Tuple)):
            columns = [(None, _str(e)) for e in fields.elts]
        columns += [_column_refs(a) for a in call.args if isinstance(a, ast.Call)]
        partial = _kw(call, "condition") is not None
    else:
        args = list(call.args)
        if cname == "Index" and args and _str(args[0]) is not None:
            name_node = name_node or args[0]
            args = args[1:]
        columns = [
            _column_refs(a)
            for a in args
            if not isinstance(a, ast.Constant) or cname != "CheckConstraint"
        ]
        partial = any((kw.arg or "").endswith("_where") for kw in call.keywords)
    unique = cname in ("UniqueConstraint", "PrimaryKeyConstraint") or (
        cname == "Index" and _bool_kw(call, "unique") is True
    )
    if cname not in (
        "UniqueConstraint",
        "PrimaryKeyConstraint",
        "CheckConstraint",
        "Index",
    ):
        return None
    return {
        "kind": cname,
        "name": _str(name_node) or _template_name(name_node, table),
        "line": call.lineno,
        "columns": [c for c in columns if c[1]],
        "unique": unique,
        "partial": partial,
    }


def _build_models(files: list[_File], aliases: dict) -> tuple[list[_Model], dict]:
    index = _ClassIndex(files)
    classes = index.by_key
    base_vars = {v for f in files for v in f.base_vars}
    memo: dict[str, str | None] = {}
    kinds = {key: _class_kind(key, index, base_vars, memo) for key in classes}
    orm_keys = {k for k, kind in kinds.items() if kind}
    orm_names = {k.split("::")[1] for k in orm_keys}
    members: dict[str, tuple[dict, list, list]] = {}
    for key, (f, cls) in classes.items():
        kind = kinds.get(key) or "sqlalchemy"
        members[key] = _own_members(cls, aliases, kind, orm_names)

    def mro(key: str, seen: set[str]) -> list[str]:
        if key in seen or key not in classes:
            return []
        seen.add(key)
        out = [key]
        for base_key in index.bases(key):
            out += mro(base_key, seen)
        return out

    models: list[_Model] = []
    by_name: dict[str, _Model] = {}
    for key in sorted(orm_keys, key=lambda k: (k.split("::")[1], k)):
        name = key.split("::")[1]
        f, cls = classes[key]
        kind = kinds[key] or "sqlalchemy"
        model = _Model(f, cls, kind)
        for stmt in cls.body:
            if "__tablename__" in _assigned(stmt) and _str(stmt.value):
                model.table = _str(stmt.value)
            if "__abstract__" in _assigned(stmt) and isinstance(
                stmt.value, ast.Constant
            ):
                model.abstract = bool(stmt.value.value)
        meta = _django_meta(cls) if kind == "django" else None
        if meta is not None:
            for stmt in meta.body:
                if "abstract" in _assigned(stmt) and isinstance(
                    stmt.value, ast.Constant
                ):
                    model.abstract = bool(stmt.value.value)
                if "db_table" in _assigned(stmt):
                    model.table = _str(stmt.value)
        has_table = model.table is not None or kind == "django"
        chain = mro(key, set())
        # Single-table inheritance: a subclass with no table of its own maps to
        # its parent's table.
        if model.table is None and kind == "sqlalchemy":
            for ancestor in chain[1:]:
                anc_cls = classes[ancestor][1]
                for stmt in anc_cls.body:
                    if "__tablename__" in _assigned(stmt) and _str(stmt.value):
                        model.table = _str(stmt.value)
                        has_table = True
                        break
                if model.table:
                    break
        if not has_table or model.abstract:
            continue
        for ancestor in reversed(chain):
            cols, rels, _ = members[ancestor]
            for col_name, col in cols.items():
                model.columns[col_name] = dict(
                    col,
                    inherited_from=None if ancestor == key else ancestor.split("::")[1],
                )
            if ancestor == key:
                model.relationships = rels
        _collect_keys(model, chain, classes)
        model.ancestors = [k.split("::")[1] for k in chain]
        models.append(model)
        by_name.setdefault(name, model)
    for model in models:
        for rel in model.relationships:
            target = by_name.get(_relationship_target(rel["inner"]) or "")
            fks = [
                c["name"]
                for c in model.columns.values()
                if target is not None
                and target.table
                and (c["foreign_key"] or "").split(".")[0] == target.table
            ]
            if fks:
                model.relationship_fks[rel["name"]] = fks
    return models, by_name


def _collect_keys(model: _Model, chain: list[str], classes: dict) -> None:
    pk = [c["name"] for c in model.columns.values() if c["primary_key"]]
    if pk:
        model.unique_keys.append(
            {
                "kind": "primary_key",
                "columns": [[None, c] for c in pk],
                "partial": False,
                "name": None,
            }
        )
    elif model.kind == "django":
        model.unique_keys.append(
            {
                "kind": "primary_key",
                "columns": [[None, "id"]],
                "partial": False,
                "name": None,
            }
        )
    for col in model.columns.values():
        if col["unique"] and not col["primary_key"]:
            model.unique_keys.append(
                {
                    "kind": "unique_column",
                    "columns": [[None, col["name"]]],
                    "partial": False,
                    "name": None,
                }
            )
    calls: list[ast.Call] = []
    for ancestor in chain:
        cls = classes[ancestor][1]
        calls += [e for e in _table_args(cls) if isinstance(e, ast.Call)]
        meta = _django_meta(cls) if model.kind == "django" else None
        if meta is None:
            continue
        for stmt in meta.body:
            names = _assigned(stmt)
            value = getattr(stmt, "value", None)
            if "constraints" in names or "indexes" in names:
                calls += [
                    e for e in getattr(value, "elts", []) if isinstance(e, ast.Call)
                ]
            if "unique_together" in names and isinstance(value, (ast.List, ast.Tuple)):
                groups = (
                    value.elts
                    if value.elts and isinstance(value.elts[0], (ast.List, ast.Tuple))
                    else [value]
                )
                for group in groups:
                    cols = [
                        [None, _str(e)] for e in getattr(group, "elts", []) if _str(e)
                    ]
                    if cols:
                        model.unique_keys.append(
                            {
                                "kind": "unique_together",
                                "columns": cols,
                                "partial": False,
                                "name": None,
                            }
                        )
    for call in calls:
        rec = _constraint_record(call, model.kind, model.table)
        if rec is None:
            continue
        if rec["name"]:
            model.constraints.append(rec)
        else:
            model.unnamed_constraints += 1
            if rec["kind"] == "CheckConstraint" and call.args and _str(call.args[0]):
                model.unnamed_checks.append(
                    (_norm_sql(_str(call.args[0])), call.lineno)
                )
        if rec["unique"] and rec["columns"]:
            model.unique_keys.append(
                {
                    "kind": rec["kind"],
                    "columns": [list(c) for c in rec["columns"]],
                    "partial": rec["partial"],
                    "name": rec["name"],
                }
            )


# ---------------------------------------------------------------------------
# Nullability
# ---------------------------------------------------------------------------


def _nullability(
    models: list[_Model], by_name: dict[str, _Model], aliases: dict
) -> dict:
    sa_models = [m for m in models if m.kind == "sqlalchemy"]
    if not sa_models:
        return {
            "status": "not_detected",
            "reason": "no SQLAlchemy declarative models (column annotations are a SQLAlchemy feature)",
        }
    mismatches, implicit, unannotated, rel_mismatches = [], [], [], []
    seen: set[tuple[str, str]] = set()
    for m in sa_models:
        for col in m.columns.values():
            owner = col.get("inherited_from") or m.name
            if (owner, col["name"]) in seen:
                continue
            seen.add((owner, col["name"]))
            where = (
                f"{m.file.rel}:{col['line']}"
                if owner == m.name
                else f"(inherited from {owner}) line {col['line']}"
            )
            if col["annotation"] is None:
                unannotated.append(f"{owner}.{col['name']}")
                if col["nullable_explicit"] is None and not col["primary_key"]:
                    implicit.append({"column": f"{owner}.{col['name']}", "at": where})
                continue
            optional = col["annotation_optional"]
            if optional is None:
                continue
            if col["nullable"] and not optional:
                kind = "nullable_but_annotation_not_optional"
            elif not col["nullable"] and optional and not col["primary_key"]:
                kind = "not_null_but_annotation_optional"
            else:
                continue
            mismatches.append(
                {
                    "kind": kind,
                    "column": f"{owner}.{col['name']}",
                    "at": where,
                    "annotation": col["annotation"],
                    "nullable_explicit": col["nullable_explicit"],
                    "via": col["via"],
                    "server_default": col["server_default"],
                }
            )
        for rel in m.relationships:
            if rel["mapped"] != "Mapped" or rel["inner"] is None:
                continue
            inner = rel["inner"]
            if _terminal(inner) in _COLLECTION_ANNOTATIONS:
                continue
            optional = _optional(inner, aliases)
            target_name = _terminal(inner) if not isinstance(inner, ast.BinOp) else None
            if isinstance(inner, ast.BinOp):
                # `Target | None`
                parts = [p for p in (inner.left, inner.right) if not _is_none(p)]
                target_name = _terminal(parts[0]) if parts else None
            elif (
                isinstance(inner, ast.Subscript)
                and _terminal(inner.value) == "Optional"
            ):
                target_name = _terminal(inner.slice)
            target = by_name.get(target_name or "")
            call: ast.Call = rel["call"]
            if _bool_kw(call, "uselist") is True:
                continue
            may_be_none, reason = _relationship_may_be_none(m, target, call)
            if may_be_none is None:
                continue
            if may_be_none and not optional:
                kind = "may_be_none_but_annotation_not_optional"
            elif not may_be_none and optional:
                kind = "never_none_but_annotation_optional"
            else:
                continue
            rel_mismatches.append(
                {
                    "kind": kind,
                    "relationship": f"{m.name}.{rel['name']}",
                    "at": f"{m.file.rel}:{rel['line']}",
                    "annotation": rel["annotation"],
                    "reason": reason,
                }
            )
    return {
        "status": "detected",
        "column_mismatches": mismatches,
        "relationship_mismatches": rel_mismatches,
        "implicitly_nullable_columns": implicit[:_CAP],
        "unannotated_columns": len(unannotated),
        "unannotated_column_names": unannotated[:_CAP],
    }


def _relationship_target(inner: ast.AST | None) -> str | None:
    """`Target`, `Target | None`, `Optional[Target]`, `"Target"` -> "Target"."""
    inner = _parse_annotation(inner)
    if isinstance(inner, ast.BinOp):
        parts = [p for p in (inner.left, inner.right) if not _is_none(p)]
        return _relationship_target(parts[0]) if parts else None
    if isinstance(inner, ast.Subscript) and _terminal(inner.value) == "Optional":
        return _relationship_target(inner.slice)
    return _terminal(inner)


def _relationship_may_be_none(
    m: _Model, target: _Model | None, call: ast.Call
) -> tuple[bool | None, str]:
    if _kw(call, "secondary") is not None:
        return True, "secondary table: the association row may be absent"
    if target is None or not target.table:
        return None, "target model not resolved"
    fk_names: list[str] = []
    explicit = _kw(call, "foreign_keys")
    for elt in getattr(explicit, "elts", [explicit] if explicit is not None else []):
        if elt is not None:
            fk_names.append(_terminal(elt) or "")
    local = [
        c
        for c in m.columns.values()
        if (c["name"] in fk_names)
        or (not fk_names and (c["foreign_key"] or "").split(".")[0] == target.table)
    ]
    if local:
        nullable = any(c["nullable"] for c in local)
        cols = ", ".join(c["name"] for c in local)
        return (
            nullable,
            f"many-to-one through {cols} ({'nullable' if nullable else 'NOT NULL'})",
        )
    if m.table and any(
        (c["foreign_key"] or "").split(".")[0] == m.table
        for c in target.columns.values()
    ):
        return True, "one-to-many side: the related row may not exist"
    return None, "join not resolved"


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def _migration_files(project_root: Path) -> tuple[list[Path], list[Path], list[str]]:
    """Alembic revisions, Django migrations and their directories.

    Uses scan_common's discovery and `migration_kind`, so this script and the
    migration-aware scanners agree on which files are migrations.
    """
    alembic, django, dirs = [], [], set()
    for path in sorted(discover_python_files(project_root)):
        if migration_kind(path) != "revision":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        (django if "Migration" in text and "class Migration" in text else alembic).append(
            path
        )
        dirs.add(relative_to_root(path.parent, project_root))
    return alembic, django, sorted(dirs)


def _alembic_order(paths: list[Path]) -> list[tuple[Path, ast.Module]]:
    info: dict[str, tuple[Path, ast.Module, list[str]]] = {}
    for path in paths:
        tree = parse_source(path)
        if tree is None:
            continue
        revision, down = None, []
        for node in tree.body:
            names = _assigned(node)
            value = getattr(node, "value", None)
            if "revision" in names:
                revision = _str(value)
            elif "down_revision" in names:
                if isinstance(value, (ast.Tuple, ast.List)):
                    down = [_str(e) for e in value.elts if _str(e)]
                elif _str(value):
                    down = [_str(value)]
        if revision:
            info[revision] = (path, tree, down)
    ordered: list[tuple[Path, ast.Module]] = []
    done: set[str] = set()

    def visit(rev: str, stack: set[str]) -> None:
        if rev in done or rev in stack or rev not in info:
            return
        stack.add(rev)
        for parent in info[rev][2]:
            visit(parent, stack)
        done.add(rev)
        ordered.append(info[rev][:2])

    for rev in sorted(info):
        visit(rev, set())
    return ordered


class _Event:
    """One schema change replayed from a migration."""

    __slots__ = ("kind", "name", "new", "op", "pattern", "sql", "table")

    def __init__(
        self,
        op: str,
        name: str,
        *,
        new: str | None = None,
        kind: str = "other",
        pattern: bool = False,
        sql: str | None = None,
        table: str | None = None,
    ) -> None:
        self.op = op  # "create" | "drop" | "rename" | "text"
        self.name = name  # a literal name, or a regex when `pattern`
        self.new = new
        self.kind = kind  # "check" | "unique" | "index" | "pk" | "fk" | "other"
        self.pattern = pattern
        self.sql = sql  # normalised CHECK text, for matching unnamed constraints
        self.table = table  # the table a constraint is created on, when known


_CREATE_OPS = {
    "create_check_constraint": "check",
    "create_unique_constraint": "unique",
    "create_primary_key": "pk",
    "create_foreign_key": "fk",
    "create_index": "index",
    "create_exclude_constraint": "other",
}
_CONSTRUCTORS = {
    "CheckConstraint": "check",
    "UniqueConstraint": "unique",
    "PrimaryKeyConstraint": "pk",
    "ForeignKeyConstraint": "fk",
    "Index": "index",
    "ExclusionConstraint": "other",
}


def _norm_sql(text: str) -> str:
    """Case-, whitespace- and parenthesis-insensitive form of a SQL condition."""
    return re.sub(r"[\s()]+", "", text.lower())


def _name_of(node: ast.AST | None) -> tuple[str | None, bool]:
    """(name, is_pattern): a literal, or a regex for an f-string name."""
    node = _unwrap_op_f(node)
    if _str(node) is not None:
        return _str(node), False
    if isinstance(node, ast.JoinedStr):
        regex = "".join(
            re.escape(v.value)
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
            else ".+"
            for v in node.values
        )
        return regex, True
    return None, False


def _check_text(call: ast.Call, index: int) -> str | None:
    """The condition of `op.create_check_constraint(name, table, condition)`
    (index 2) or `CheckConstraint(sqltext, name=...)` (index 0)."""
    node = call.args[index] if len(call.args) > index else None
    text = _str(node) or _str(_kw(call, "condition")) or _str(_kw(call, "sqltext"))
    return _norm_sql(text) if text is not None else None


def _upgrade_events(tree: ast.Module) -> list[_Event]:
    """Schema events in an Alembic upgrade(), in source order."""
    upgrade = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
        ),
        None,
    )
    if upgrade is None:
        return []
    found: list[tuple[int, int, _Event]] = []
    table_of: dict[int, str] = {}  # constructor node -> its create_table's table
    for node in ast.walk(upgrade):
        if (
            isinstance(node, ast.Call)
            and _call_name(node) == "create_table"
            and node.args
        ):
            for sub in ast.walk(node):
                if _str(node.args[0]):
                    table_of[id(sub)] = _str(node.args[0])
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node)
        pos = (node.lineno, node.col_offset)
        if call in ("drop_table", "rename_table") and node.args and _str(node.args[0]):
            new = _str(node.args[1]) if len(node.args) > 1 else None
            op = "drop_table" if call == "drop_table" else "rename_table"
            found.append((*pos, _Event(op, _str(node.args[0]), new=new)))
            continue
        if call in _CREATE_OPS or call in ("drop_constraint", "drop_index"):
            name, pattern = _name_of(
                node.args[0]
                if node.args
                else _kw(node, "constraint_name") or _kw(node, "index_name")
            )
            if call in _CREATE_OPS:
                kind = _CREATE_OPS[call]
                sql = _check_text(node, 2) if kind == "check" else None
                table = _str(node.args[1]) if len(node.args) > 1 else None
                table = table or _str(
                    _kw(node, "table_name") or _kw(node, "source_table")
                )
                if name is not None:
                    event = _Event(
                        "create", name, kind=kind, pattern=pattern, sql=sql, table=table
                    )
                elif sql:
                    event = _Event("text", "", kind=kind, sql=sql)  # unnamed
                else:
                    continue
                found.append((*pos, event))
            elif name is not None:
                found.append((*pos, _Event("drop", name, pattern=pattern)))
        elif call in _CONSTRUCTORS:
            kind = _CONSTRUCTORS[call]
            name, pattern = _name_of(_kw(node, "name"))
            if name is None and call == "Index" and node.args:
                name, pattern = _name_of(node.args[0])
            sql = _check_text(node, 0) if kind == "check" else None
            if name is not None or sql:
                found.append(
                    (
                        *pos,
                        _Event(
                            "create" if name else "text",
                            name or "",
                            kind=kind,
                            pattern=pattern,
                            sql=sql,
                            table=table_of.get(id(node)),
                        ),
                    )
                )
        elif call == "execute" and node.args:
            arg = node.args[0]
            sql = _str(arg) or (
                _str(arg.args[0]) if isinstance(arg, ast.Call) and arg.args else None
            )
            if sql:
                found += [(*pos, e) for e in _sql_events(sql)]
    # ast.walk is breadth-first; replay in source order.
    return [e for *_pos, e in sorted(found, key=lambda t: (t[0], t[1]))]


def _sql_events(sql: str) -> list[_Event]:
    found: list[tuple[int, _Event]] = []
    for m in _SQL_ADD_CONSTRAINT.finditer(sql):
        rest = sql[m.end() : m.end() + 20].upper()
        kind = "check" if "CHECK" in rest else "unique" if "UNIQUE" in rest else "other"
        found.append((m.start(), _Event("create", m.group(1), kind=kind)))
    found += [
        (m.start(), _Event("create", m.group(1), kind="index"))
        for m in _SQL_CREATE_INDEX.finditer(sql)
    ]
    found += [
        (m.start(), _Event("drop", m.group(1)))
        for rx in (_SQL_DROP_CONSTRAINT, _SQL_DROP_INDEX)
        for m in rx.finditer(sql)
    ]
    found += [
        (m.start(), _Event("rename", m.group(1), new=m.group(2)))
        for rx in (_SQL_RENAME_CONSTRAINT, _SQL_RENAME_INDEX)
        for m in rx.finditer(sql)
    ]
    found += [
        (m.start(), _Event("rename_table", m.group(1), new=m.group(2)))
        for m in _SQL_RENAME_TABLE.finditer(sql)
    ]
    if "CHECK" in sql.upper():
        found.append((len(sql), _Event("text", "", kind="check", sql=_norm_sql(sql))))
    return [e for _pos, e in sorted(found, key=lambda t: t[0])]


def _django_events(tree: ast.Module) -> list[_Event]:
    found: list[tuple[int, int, _Event]] = []
    removing: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node)
        pos = (node.lineno, node.col_offset)
        if call in ("RemoveConstraint", "RemoveIndex"):
            name = _str(_kw(node, "name"))
            if name:
                found.append((*pos, _Event("drop", name)))
            removing.update(id(sub) for sub in ast.walk(node))
        elif call == "RenameIndex":
            old, new = _str(_kw(node, "old_name")), _str(_kw(node, "new_name"))
            if old and new:
                found.append((*pos, _Event("rename", old, new=new, kind="index")))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and id(node) not in removing
            and _call_name(node) in _CONSTRUCTORS
        ):
            name = _str(_kw(node, "name"))
            if name:
                found.append(
                    (
                        (node.lineno, node.col_offset)[0],
                        node.col_offset,
                        _Event("create", name, kind=_CONSTRUCTORS[_call_name(node)]),
                    )
                )
    return [e for *_pos, e in sorted(found, key=lambda t: (t[0], t[1]))]


def _migrations(models: list[_Model], files: list[_File], project_root: Path) -> dict:
    if not models:
        return {"status": "not_detected", "reason": "no ORM models found"}
    alembic, django, dirs = _migration_files(project_root)
    if not alembic and not django:
        return {
            "status": "not_detected",
            "reason": "no Alembic `versions/` or Django `migrations/` directory under the project root",
        }
    sequences: list[tuple[str, list[_Event]]] = []
    for path, tree in _alembic_order(alembic):
        sequences.append((relative_to_root(path, project_root), _upgrade_events(tree)))
    for path in sorted(django):
        tree = parse_source(path)
        if tree is not None:
            sequences.append(
                (relative_to_root(path, project_root), _django_events(tree))
            )
    state: dict[str, str] = {}
    kinds: dict[str, str] = {}
    created_in: dict[str, str] = {}
    history: dict[str, list[str]] = defaultdict(list)
    patterns: list[tuple[re.Pattern, str]] = []
    check_texts: list[str] = []
    check_sql: dict[str, str] = {}
    table_of: dict[str, str] = {}
    for rel, events in sequences:
        for e in events:
            if e.sql:
                check_texts.append(e.sql)
            if e.op == "drop_table":
                for name, table in table_of.items():
                    if table == e.name and state.get(name) == "present":
                        state[name] = "dropped"
                        history[name].append(f"dropped with table {table} in {rel}")
            elif e.op == "rename_table" and e.new:
                for name, table in list(table_of.items()):
                    if table == e.name:
                        table_of[name] = e.new
            elif e.op == "create" and e.pattern:
                patterns.append((re.compile(e.name), rel))
            elif e.op == "create":
                state[e.name] = "present"
                kinds[e.name] = e.kind
                if e.table:
                    table_of[e.name] = e.table
                if e.sql:
                    check_sql[e.name] = e.sql
                created_in.setdefault(e.name, rel)
                history[e.name].append(f"created in {rel}")
            elif e.op == "drop" and not e.pattern:
                state[e.name] = "dropped"
                history[e.name].append(f"dropped in {rel}")
            elif e.op == "rename" and e.new:
                state[e.name] = "renamed"
                state[e.new] = "present"
                kinds[e.new] = kinds.get(e.name, "other")
                if e.name in table_of:
                    table_of[e.new] = table_of[e.name]
                if e.name in check_sql:
                    check_sql[e.new] = check_sql[e.name]
                history[e.name].append(f"renamed to {e.new} in {rel}")
                history[e.new].append(f"renamed from {e.name} in {rel}")
    never, dropped, by_pattern, unnamed_checks = [], [], [], []
    declared_names: set[str] = set()
    for m in models:
        for c in m.constraints:
            declared_names.add(c["name"])
            entry = {
                "model": m.name,
                "table": m.table,
                "constraint": c["name"],
                "kind": c["kind"],
                "at": f"{m.file.rel}:{c['line']}",
            }
            st = state.get(c["name"])
            if st == "present":
                continue
            matched = [rel for rx, rel in patterns if rx.fullmatch(c["name"])]
            if matched:
                by_pattern.append(dict(entry, created_by_pattern_in=matched[:3]))
            elif st is None:
                never.append(entry)
            else:
                dropped.append(dict(entry, history=history[c["name"]][-3:]))
        for text, line in m.unnamed_checks:
            if not any(text in t for t in check_texts):
                unnamed_checks.append(
                    {
                        "model": m.name,
                        "table": m.table,
                        "check": text,
                        "at": f"{m.file.rel}:{line}",
                    }
                )
    # A named migration check that matches an unnamed model check by its
    # condition is the same constraint.
    model_check_texts = {text for m in models for text, _line in m.unnamed_checks}
    only_in_migrations = [
        {
            "constraint": name,
            "created_in": created_in.get(name),
            "table": table_of.get(name),
        }
        for name in sorted(state)
        if state[name] == "present"
        and kinds.get(name) == "check"
        and name not in declared_names
        and check_sql.get(name) not in model_check_texts
    ]
    return {
        "status": "detected",
        "migration_dirs": dirs,
        "migration_files": len(sequences),
        "named_model_constraints": len(declared_names),
        "unnamed_model_constraints": sum(m.unnamed_constraints for m in models),
        "naming_convention": any(f.naming_convention for f in files),
        "declared_never_created": never,
        "declared_but_dropped": dropped,
        "created_by_dynamic_name": by_pattern,
        "unnamed_checks_not_in_migrations": unnamed_checks,
        "check_constraints_only_in_migrations": only_in_migrations[:_CAP],
    }


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


class _LookupContext:
    """Names a query can use for a model inside one function."""

    def __init__(
        self, models: dict[str, list[_Model]], owner: str | None, fn: ast.AST
    ) -> None:
        self.models = models  # local name -> candidate models
        self.owner = owner
        self.assignments: dict[str, ast.AST] = {}
        for sub in ast.walk(fn):
            if isinstance(sub, (ast.Assign, ast.AnnAssign)) and sub.value is not None:
                for name in _assigned(sub):
                    self.assignments[name] = sub.value

    def model_ref(self, node: ast.AST) -> str | None:
        """Model name for `Model`, `cls`, `self.__class__` or `type(self)`."""
        if isinstance(node, ast.Name) and node.id == "cls":
            return self.owner
        if isinstance(node, ast.Attribute) and node.attr == "__class__":
            return self.owner
        if isinstance(node, ast.Call) and _call_name(node) == "type":
            return self.owner
        name = _terminal(node)
        return name if name in self.models else None


def _filters_from_condition(
    cond: ast.AST, ctx: _LookupContext, out: list[dict], flags: dict
) -> None:
    if isinstance(cond, ast.Call) and _call_name(cond) == "and_":
        for arg in cond.args:
            _filters_from_condition(arg, ctx, out, flags)
        return
    if isinstance(cond, ast.Call) and _call_name(cond) == "or_":
        flags["has_or"] = True
        return
    if isinstance(cond, ast.BoolOp):
        if isinstance(cond.op, ast.Or):
            flags["has_or"] = True
            return
        for value in cond.values:
            _filters_from_condition(value, ctx, out, flags)
        return
    if isinstance(cond, ast.Compare) and len(cond.ops) == 1:
        op = cond.ops[0]
        for side in (cond.left, cond.comparators[0]):
            ref = _model_column(side, ctx)
            if ref is not None:
                out.append(
                    dict(
                        ref,
                        op="eq"
                        if isinstance(op, ast.Eq)
                        else type(op).__name__.lower(),
                    )
                )
                return
        return
    if isinstance(cond, ast.Call) and isinstance(cond.func, ast.Attribute):
        ref = _model_column(cond.func.value, ctx)
        if ref is not None:
            method = cond.func.attr
            out.append(
                dict(
                    ref,
                    op="ieq" if method == "ilike" else method,
                    wrapper="lower" if method == "ilike" else ref["wrapper"],
                )
            )


def _model_column(node: ast.AST, ctx: _LookupContext) -> dict | None:
    wrapper = None
    if isinstance(node, ast.Call) and _call_name(node) in _CASE_FOLD and node.args:
        wrapper = _call_name(node)
        node = node.args[0]
    if isinstance(node, ast.Attribute):
        model = ctx.model_ref(node.value)
        if model is not None:
            return {"model": model, "column": node.attr, "wrapper": wrapper}
    return None


def _query_base(
    node: ast.AST, ctx: _LookupContext
) -> tuple[str | None, ast.AST | None]:
    """(model, nested query) for `query(M)`, `Query(M)`, `select(M)`, `exists()...`."""
    if not node.args:  # type: ignore[attr-defined]
        return None, None
    first = node.args[0]  # type: ignore[attr-defined]
    model = ctx.model_ref(first)
    if model is not None:
        return model, None
    if isinstance(first, ast.Call):
        return None, first  # `query(exists().where(...))`, `query(select(M)...)`
    return None, None


def _chain_lookup(terminal: ast.Call, ctx: _LookupContext) -> dict | None:
    """Walk a query chain down from its terminal call, through local variables
    (`q = Query(cls).filter_by(...)`; `q.all()`) and nested queries."""
    filters: list[dict] = []
    flags: dict = {"has_or": False}
    base_model: str | None = None
    kw_filters: list[tuple[str, ast.AST]] = []
    terminal_name = _call_name(terminal)
    node: ast.AST | None = (
        terminal.func.value if isinstance(terminal.func, ast.Attribute) else None
    )
    django_get = terminal_name == "get"
    if django_get:
        kw_filters += [(kw.arg, kw.value) for kw in terminal.keywords if kw.arg]
    followed: set[str] = set()
    for _depth in range(30):
        if node is None:
            break
        if (
            isinstance(node, ast.Name)
            and node.id in ctx.assignments
            and node.id not in followed
        ):
            followed.add(node.id)
            node = ctx.assignments[node.id]
            continue
        if not isinstance(node, (ast.Call, ast.Attribute)):
            break
        if isinstance(node, ast.Attribute):
            if node.attr == "objects" and ctx.model_ref(node.value):
                base_model = ctx.model_ref(node.value)
                break
            node = node.value
            continue
        call = _call_name(node)
        if isinstance(node.func, ast.Attribute) and call in _FILTER_METHODS:
            if call == "filter_by" or (
                call == "filter" and node.keywords and not node.args
            ):
                kw_filters += [(kw.arg, kw.value) for kw in node.keywords if kw.arg]
            for cond in node.args:
                _filters_from_condition(cond, ctx, filters, flags)
            node = node.func.value
            continue
        if call in ("query", "Query", "select", "exists"):
            model, nested = _query_base(node, ctx)
            if model is not None:
                base_model = model
                break
            if nested is not None:
                node = nested
                continue
            break
        if call in _EXECUTE_METHODS and node.args:
            node = node.args[0]
            continue
        node = node.func.value if isinstance(node.func, ast.Attribute) else None
    if django_get and base_model is None:
        return None
    for key, _value in kw_filters:
        parts = key.split("__")
        last = parts[-1] if len(parts) > 1 else "exact"
        if len(parts) > 2 or (
            len(parts) == 2
            and last not in _DJANGO_EQ_LOOKUPS
            and last not in ("in", "isnull")
        ):
            continue  # a related lookup (`owner__name`): another model's column
        filters.append(
            {
                "model": base_model,
                "column": parts[0],
                "op": "eq" if last in _DJANGO_EQ_LOOKUPS else last,
                "wrapper": _DJANGO_EQ_LOOKUPS.get(last),
            }
        )
    if not filters:
        return None
    model = base_model or filters[0]["model"]
    return {
        "model": model,
        "filters": [
            dict(f, model=model) for f in filters if f["model"] in (model, None)
        ],
        "terminal": terminal_name,
        "has_or": flags["has_or"],
        "line": terminal.lineno,
    }


def _covers_one(lookup: dict, model: _Model) -> tuple[bool, str | None]:
    if lookup["has_or"]:
        return False, None
    eq: set[tuple[str, str | None]] = set()
    for f in lookup["filters"]:
        if f["op"] != "eq":
            continue
        # A relationship compared to an object constrains its foreign key(s).
        for column in [f["column"], *model.relationship_fks.get(f["column"], [])]:
            eq.add((column, f["wrapper"]))
            col = model.columns.get(column)
            if col is not None:
                eq.add((col["db_name"], f["wrapper"]))
    cols = {c for c, _w in eq}
    for key in model.unique_keys:
        if key["partial"]:
            continue
        ok = True
        for wrapper, column in key["columns"]:
            col = model.columns.get(column or "")
            names = {column, col["db_name"] if col else column}
            if wrapper is None:
                ok = ok and any((n, None) in eq for n in names)
            else:
                ok = ok and bool(names & cols)
        if ok:
            return True, key["name"] or key["kind"]
    return False, None


def _covers(lookup: dict, models: list[_Model]) -> tuple[bool | None, str | None]:
    """Covered for every candidate model (a mixin's lookup runs on each)."""
    if not models:
        return None, None
    results = [_covers_one(lookup, m) for m in models]
    if all(ok for ok, _ in results):
        return True, results[0][1]
    return False, None


def _format_key(key: dict) -> str:
    cols = ", ".join(f"{w}({c})" if w else str(c) for w, c in key["columns"])
    label = key["name"] or key["kind"]
    return f"{label}: ({cols})" + (" partial" if key["partial"] else "")


def _lookups(files: list[_File], by_name: dict[str, _Model]) -> dict:
    if not by_name:
        return {"status": "not_detected", "reason": "no ORM models found"}
    per_function: list[dict] = []
    classes: dict[str, tuple[_File, ast.ClassDef]] = {}
    # A mixin (or abstract base) stands for every concrete model built on it.
    candidates: dict[str, list[_Model]] = defaultdict(list)
    for model in by_name.values():
        for ancestor in model.ancestors:
            candidates[ancestor].append(model)
    for f in files:
        local_models = {name: list(ms) for name, ms in candidates.items()}
        for local, imported in f.imports.items():
            if imported in candidates:
                local_models[local] = candidates[imported]
        for node in f.tree.body:
            owners: list[tuple[str | None, ast.AST]] = []
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, (f, node))
                owners += [
                    (node.name, c)
                    for c in node.body
                    if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owners.append((None, node))
            for owner, fn in owners:
                found = []
                ctx = _LookupContext(local_models, owner, fn)
                for sub in ast.walk(fn):
                    if not (
                        isinstance(sub, ast.Call)
                        and _call_name(sub) in _TERMINALS | {"get"}
                    ):
                        continue
                    lookup = _chain_lookup(sub, ctx)
                    if lookup is None:
                        continue
                    targets = local_models.get(lookup["model"] or "", [])
                    covered, key = _covers(lookup, targets)
                    found.append(
                        dict(
                            lookup,
                            models=sorted(m.name for m in targets),
                            covers_unique_key=covered,
                            covering_key=key,
                        )
                    )
                if found:
                    per_function.append(
                        {
                            "class": owner,
                            "function": fn.name,  # type: ignore[attr-defined]
                            "file": f.rel,
                            "line": fn.lineno,  # type: ignore[attr-defined]
                            "lookups": found,
                        }
                    )

    by_method: dict[str, list[dict]] = defaultdict(list)
    for entry in per_function:
        if entry["class"]:
            by_method[entry["function"]].append(entry)
    groups = []
    # Same-named methods with lookups on two or more classes: per-provider or
    # per-model implementations of one concern (models share a declarative
    # base, so "related" adds nothing over the shared name).
    for method, related in sorted(by_method.items()):
        if len({e["class"] for e in related}) < 2:
            continue
        filter_sets: dict[str, set[frozenset]] = defaultdict(set)
        members = []
        key_mismatch = False
        for e in related:
            for lk in e["lookups"]:
                key_mismatch = key_mismatch or any(
                    d["missing"] or d["extra"] for d in _key_diffs(lk, by_name)
                )
                filter_sets[lk["model"] or "?"].add(
                    frozenset(
                        f"{f['wrapper']}({f['column']})"
                        if f["wrapper"]
                        else f["column"]
                        for f in lk["filters"]
                    )
                )
            members.append(
                {
                    "class": e["class"],
                    "at": f"{e['file']}:{e['line']}",
                    "lookups": [
                        {
                            "model": lk["model"],
                            "applies_to": lk["models"],
                            "filters": [_format_filter(f) for f in lk["filters"]],
                            "terminal": lk["terminal"],
                            "covers_unique_key": lk["covers_unique_key"],
                            "closest_unique_key": _key_diffs(lk, by_name),
                        }
                        for lk in e["lookups"]
                    ],
                }
            )
        models_used = sorted(
            {m for e in related for lk in e["lookups"] for m in lk["models"]}
        )
        groups.append(
            {
                "method": method,
                "members": members,
                "unique_keys": {
                    k: [_format_key(u) for u in by_name[k].unique_keys]
                    for k in models_used
                    if k in by_name
                },
                "filter_sets_differ": any(len(v) > 1 for v in filter_sets.values()),
                "key_mismatch": key_mismatch,
            }
        )
    groups.sort(
        key=lambda g: (not (g["filter_sets_differ"] or g["key_mismatch"]), g["method"])
    )
    not_unique = []
    for entry in per_function:
        for lk in entry["lookups"]:
            single = lk["terminal"] in _SINGLE_ROW_TERMINALS or lk["terminal"] == "get"
            has_eq = any(f["op"] == "eq" for f in lk["filters"])
            if single and has_eq and lk["covers_unique_key"] is False:
                model = by_name.get(lk["models"][0]) if lk["models"] else None
                not_unique.append(
                    {
                        "function": f"{entry['class']}.{entry['function']}"
                        if entry["class"]
                        else entry["function"],
                        "at": f"{entry['file']}:{lk['line']}",
                        "model": lk["model"],
                        "filters": [_format_filter(f) for f in lk["filters"]],
                        "terminal": lk["terminal"],
                        "has_or": lk["has_or"],
                        "unique_keys": [_format_key(u) for u in model.unique_keys]
                        if model
                        else [],
                    }
                )
    return {
        "status": "detected",
        "functions_with_lookups": len(per_function),
        "sibling_groups": groups[:_CAP],
        "single_row_lookups_not_unique": not_unique[:_CAP],
    }


def _key_diffs(lookup: dict, by_name: dict[str, _Model]) -> list[dict]:
    """Per candidate model: the unique key (other than the primary key) closest
    to the filter, with the key columns not filtered and the filtered columns
    outside the key. Empty when the model has no such key."""
    out = []
    filtered = {f["column"] for f in lookup["filters"] if f["op"] == "eq"}
    for name in lookup["models"]:
        model = by_name.get(name)
        if model is None:
            continue
        expanded = set(filtered)
        for column in filtered:
            expanded.update(model.relationship_fks.get(column, []))
        best = None
        for key in model.unique_keys:
            if key["kind"] == "primary_key":
                continue
            cols = {c for _w, c in key["columns"]}
            overlap = len(cols & expanded)
            if best is None or overlap > best[0]:
                best = (overlap, key, cols)
        if best is None:
            continue
        _overlap, key, cols = best
        fk_of = {fk: rel for rel, fks in model.relationship_fks.items() for fk in fks}
        out.append(
            {
                "model": name,
                "key": key["name"] or key["kind"],
                "missing": sorted(c for c in cols - expanded),
                "extra": sorted(
                    c
                    for c in filtered
                    if c not in cols
                    and not set(model.relationship_fks.get(c, [])) & cols
                    and fk_of.get(c) not in filtered
                ),
            }
        )
    return out


def _format_filter(f: dict) -> str:
    col = f"{f['wrapper']}({f['column']})" if f["wrapper"] else f["column"]
    return col if f["op"] == "eq" else f"{col} [{f['op']}]"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _model_summary(m: _Model) -> dict:
    return {
        "model": m.name,
        "kind": m.kind,
        "table": m.table,
        "at": f"{m.file.rel}:{m.cls.lineno}",
        "columns": len(m.columns),
        "primary_key": [c["name"] for c in m.columns.values() if c["primary_key"]],
        "unique_keys": [_format_key(k) for k in m.unique_keys],
        "named_constraints": [f"{c['kind']} {c['name']}" for c in m.constraints],
    }


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Inventory ORM models and check nullability, migrations and lookups."""
    resolved, project_root, scan_root = resolve_target(target)
    paths, files_total = collect_python_files(resolved, max_files)
    files: list[_File] = []
    unparsed: list[str] = []
    for path in paths:
        rel = relative_to_root(path, project_root)
        parts = Path(rel).parts
        if "versions" in parts or "migrations" in parts:
            continue  # migration files are read separately
        tree = parse_source(path)
        if tree is None:
            unparsed.append(rel)
            continue
        files.append(_File(rel, tree))
    aliases = _resolve_aliases(files)
    models, by_name = _build_models(files, aliases)
    result = build_envelope(project_root, scan_root, files_total, len(paths))
    result["unparsed_files"] = unparsed
    kinds = sorted({m.kind for m in models})
    result["orm"] = kinds or None
    if not models:
        reason = (
            "no ORM models recognised (DeclarativeBase / declarative_base() "
            "subclasses with __tablename__, or models.Model subclasses)"
        )
        for section in ("nullability", "migrations", "lookups"):
            result[section] = {"status": "not_detected", "reason": reason}
        result["models"] = []
        result["summary"] = {"models": 0}
        return result
    result["models"] = [_model_summary(m) for m in models]
    result["nullability"] = _nullability(models, by_name, aliases)
    result["migrations"] = _migrations(models, files, project_root)
    result["lookups"] = _lookups(files, by_name)
    summary: dict[str, object] = {"models": len(models)}
    for section in ("nullability", "migrations", "lookups"):
        data = result[section]
        summary[section] = (
            {
                k: len(v)
                for k, v in data.items()
                if isinstance(v, list)
                and k not in ("migration_dirs", "unannotated_column_names")
            }
            if data["status"] == "detected"
            else "not_detected"
        )
    result["summary"] = summary
    return result


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    emit(analyze(target, max_files=max_files))


if __name__ == "__main__":
    main()
