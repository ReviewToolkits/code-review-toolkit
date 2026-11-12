# Diff the ORM models against the migrated schema

A model can declare a constraint that no migration ever created, a migration can create one no
model knows about, and a column can be nullable in the database while its annotation promises a
value. `map_orm_schema.py` finds the static cases: annotations against `nullable=`, named
constraints against the migration history, unnamed CHECK conditions against migration text. This
recipe is the live check: build the schema the migrations produce, build the schema the models
describe, and compare them in the database's own words.

---

## 1. Run the static pass

```bash
d=$(mktemp -d "${TMPDIR:-/tmp}/crt-orm.XXXXXX") && echo "$d" &&
python3 <plugin_root>/scripts/map_orm_schema.py <package> > "$d/orm.json" 2> "$d/orm.err"; echo "exit=$?"
```

Its `migrations` section lists `declared_never_created`, `declared_but_dropped`,
`unnamed_checks_not_in_migrations` and `check_constraints_only_in_migrations`. Each is a lead; the
live diff below settles it.

## 2. Autogenerate comparisons do not see everything

Alembic's `compare_metadata()` (what `alembic check` and `--autogenerate` run) and Django's
`makemigrations --check` are drift checks, not schema diffs. By default they do **not** report:

- **primary keys**: a missing, extra or reordered primary key;
- **CHECK constraints**: added, removed or changed;
- **duplicate indexes**: two indexes over the same columns (only names are compared);
- **server defaults**, unless `compare_server_default=True`;
- **constraint names** that differ while the definition is the same.

A clean `alembic check` is therefore not evidence that the schema matches the models. Say which
tool you ran, and do not report "no drift" from it alone.

## 3. Build both schemas in an export

Work in an exported copy with a scratch database (see `<plugin_root>/docs/reproduction-convention.md`);
use the project's own test fixture for the database URL, so the engine, extensions and search path
match what its tests use.

- **Migrated schema**: run the migrations to head into one schema (`alembic upgrade head`, or the
  fixture that already does it; Django: `migrate`).
- **Model schema**: `metadata.create_all()` into a second, empty schema (Django: the test runner's
  database with `--no-migrations`, or `sqlmigrate` for each migration and `inspectdb` for the
  result).

Then introspect both with the database's catalog, not with the ORM, so both sides are rendered the
same way. A PostgreSQL template:

```python
# Run inside the project's test fixture; SCHEMAS = ("migrated", "models").
from sqlalchemy import text

def catalog(conn, schema):
    q = lambda sql: conn.execute(text(sql), {"s": schema}).fetchall()
    return {
        "columns": {
            (t, c): (typ, notnull, default)
            for t, c, typ, notnull, default in q(
                """SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                          a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
                   FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                   WHERE n.nspname = :s AND c.relkind = 'r' AND a.attnum > 0
                     AND NOT a.attisdropped"""
            )
        },
        # Compare constraints and indexes by DEFINITION as well as by name.
        "constraints": sorted(
            q(
                """SELECT c.relname, k.contype, pg_get_constraintdef(k.oid), k.conname
                   FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :s"""
            )
        ),
        "indexes": sorted(
            q(
                """SELECT tablename, regexp_replace(indexdef, '^.* USING ', 'USING '), indexname
                   FROM pg_indexes WHERE schemaname = :s"""
            )
        ),
    }
```

Diff the two catalogs on `(table, type, definition)` and, separately, on `(table, name)`:

- a definition only in the model schema: **declared, never created** (a CHECK a migration forgot,
  a primary key that differs);
- a definition only in the migrated schema: created by a migration, unknown to the models (a
  constraint the ORM will not reproduce in a fresh test database, so tests miss it);
- the same definition twice in one schema: a **duplicate** constraint or index;
- the same definition under two names: a naming drift that breaks the next migration that drops
  the constraint by its model name.

For SQLite or MySQL, use the dialect's catalog the same way (`sqlite_master` and
`PRAGMA index_list`; `information_schema.TABLE_CONSTRAINTS` and `CHECK_CONSTRAINTS`). With only the
ORM available, `sqlalchemy.inspect(engine)` gives `get_pk_constraint`, `get_unique_constraints`,
`get_check_constraints` and `get_indexes` per table; compare definitions, not just names.

## 4. Report

Each difference is a finding only when it changes behaviour: a CHECK the database never enforces
lets invalid rows in; a missing unique constraint lets a lookup's `.one()` raise
`MultipleResultsFound`; a nullable column behind a non-optional annotation lets a `None` reach code
that dereferences it. Cite the model `file:line`, the migration that should have created it (or the
one that dropped it), and the two catalog rows.
