"""Tests for map_orm_schema.py."""

import json
import subprocess
import sys
import textwrap
import unittest

from helpers import _SCRIPTS_DIR, TempProject, import_script

mod = import_script("map_orm_schema")


def _map(files: dict[str, str], target: str = "app") -> dict:
    files = {name: textwrap.dedent(src) for name, src in files.items()}
    with TempProject(files) as root:
        return mod.analyze(str(root / target))


BASE = """
    from typing import Annotated, Optional
    from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, String, UniqueConstraint, func
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

    str_or_none = Annotated[Optional[str], mapped_column(String)]

    class Base(DeclarativeBase):
        pass

    class Model(Base):
        __abstract__ = True
        id: Mapped[int] = mapped_column(primary_key=True)
"""

MODELS = {
    "app/__init__.py": "",
    "app/db.py": BASE,
    "app/models.py": """
        from app.db import *

        class Owner(Model):
            __tablename__ = "owners"
            name: Mapped[str] = mapped_column(unique=True)
            nickname: Mapped[str] = mapped_column(nullable=True)          # mismatch
            motto: Mapped[str | None] = mapped_column(nullable=False)     # mismatch
            bio: Mapped[str_or_none]                                       # fine (alias)
            legacy = Column(String)                                        # implicit NULL
            pets: Mapped[list["Pet"]] = relationship(back_populates="owner")

        class Pet(Model):
            __tablename__ = "pets"
            __table_args__ = (
                UniqueConstraint("owner_id", "name", name="pets_owner_name_uc"),
                CheckConstraint("length(name) > 0", name="pets_name_not_empty"),
                CheckConstraint("weight >= 0"),
                Index("pets_lower_tag_idx", func.lower("tag"), unique=True),
            )
            owner_id: Mapped[int | None] = mapped_column(ForeignKey("owners.id"))
            name: Mapped[str]
            tag: Mapped[str]
            weight: Mapped[int]
            owner: Mapped["Owner"] = relationship(back_populates="pets")  # FK nullable

        class Collar(Model):
            __tablename__ = "collars"
            pet_id: Mapped[int] = mapped_column(ForeignKey("pets.id"))
            pet: Mapped[Pet] = relationship()                               # NOT NULL FK: fine
    """,
}


class TestModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _map(MODELS)
        cls.models = {m["model"]: m for m in cls.out["models"]}

    def test_inventory_skips_abstract_and_bases(self):
        self.assertEqual(sorted(self.models), ["Collar", "Owner", "Pet"])
        self.assertEqual(self.out["orm"], ["sqlalchemy"])
        # The primary key comes from the abstract base.
        self.assertEqual(self.models["Pet"]["primary_key"], ["id"])

    def test_unique_keys(self):
        keys = self.models["Pet"]["unique_keys"]
        self.assertIn("pets_owner_name_uc: (owner_id, name)", keys)
        self.assertIn("pets_lower_tag_idx: (lower(tag))", keys)
        self.assertIn("unique_column: (name)", self.models["Owner"]["unique_keys"])

    def test_column_nullability_mismatches(self):
        found = {
            (m["column"], m["kind"])
            for m in self.out["nullability"]["column_mismatches"]
        }
        self.assertEqual(
            found,
            {
                ("Owner.nickname", "nullable_but_annotation_not_optional"),
                ("Owner.motto", "not_null_but_annotation_optional"),
            },
        )
        implicit = [
            c["column"] for c in self.out["nullability"]["implicitly_nullable_columns"]
        ]
        self.assertEqual(implicit, ["Owner.legacy"])

    def test_relationship_mismatch_from_nullable_foreign_key(self):
        rels = self.out["nullability"]["relationship_mismatches"]
        self.assertEqual([r["relationship"] for r in rels], ["Pet.owner"])
        self.assertIn("owner_id", rels[0]["reason"])


MIGRATED = dict(MODELS)
MIGRATED.update(
    {
        "migrations/versions/001_initial.py": """
            import sqlalchemy as sa
            from alembic import op

            revision = "001"
            down_revision = None

            def upgrade():
                op.create_table(
                    "pets",
                    sa.Column("id", sa.Integer()),
                    sa.CheckConstraint("weight >= 0"),
                    sa.CheckConstraint("legacy <> ''", name="pets_legacy_check"),
                )
                op.create_unique_constraint("pets_owner_name_uc", "pets", ["owner_id", "name"])
                op.create_index("pets_lower_tag_idx", "pets", [sa.text("lower(tag)")], unique=True)
                op.create_check_constraint("old_tables_check", "old_table", "x > 0")

            def downgrade():
                op.drop_constraint("pets_owner_name_uc", "pets")
        """,
        "migrations/versions/002_more.py": """
            from alembic import op

            revision = "002"
            down_revision = "001"

            def upgrade():
                op.drop_index("pets_lower_tag_idx", table_name="pets")
                op.drop_table("old_table")
                op.execute("ALTER TABLE pets RENAME CONSTRAINT pets_legacy_check TO pets_legacy_ck")
        """,
    }
)


class TestMigrations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mig = _map(MIGRATED)["migrations"]

    def test_declared_never_created(self):
        self.assertEqual(
            [c["constraint"] for c in self.mig["declared_never_created"]],
            ["pets_name_not_empty"],
        )

    def test_dropped_in_a_later_revision(self):
        dropped = self.mig["declared_but_dropped"]
        self.assertEqual([c["constraint"] for c in dropped], ["pets_lower_tag_idx"])
        # downgrade() is ignored: the unique constraint is still present.
        self.assertNotIn("pets_owner_name_uc", [c["constraint"] for c in dropped])

    def test_check_constraints_only_in_migrations(self):
        # Created, renamed, still present, declared by no model; the one on a
        # dropped table is gone.
        self.assertEqual(
            [c["constraint"] for c in self.mig["check_constraints_only_in_migrations"]],
            ["pets_legacy_ck"],
        )

    def test_unnamed_check_matched_by_its_condition(self):
        self.assertEqual(self.mig["unnamed_checks_not_in_migrations"], [])

    def test_unnamed_check_missing_from_migrations(self):
        files = dict(MIGRATED)
        files["migrations/versions/001_initial.py"] = files[
            "migrations/versions/001_initial.py"
        ].replace('sa.CheckConstraint("weight >= 0"),', "")
        mig = _map(files)["migrations"]
        self.assertEqual(
            [c["check"] for c in mig["unnamed_checks_not_in_migrations"]], ["weight>=0"]
        )

    def test_dynamic_constraint_name_in_a_migration(self):
        files = dict(MIGRATED)
        files["migrations/versions/003_loop.py"] = textwrap.dedent(
            """
            from alembic import op

            revision = "003"
            down_revision = "002"

            def upgrade():
                for table in ("pets",):
                    op.create_check_constraint(f"{table}_name_not_empty", table, "length(name) > 0")
            """
        )
        mig = _map(files)["migrations"]
        self.assertEqual(mig["declared_never_created"], [])
        self.assertEqual(
            [c["constraint"] for c in mig["created_by_dynamic_name"]],
            ["pets_name_not_empty"],
        )


LOOKUPS = dict(MODELS)
LOOKUPS.update(
    {
        "app/providers.py": """
            from sqlalchemy import select
            from app.models import Owner, Pet

            class Provider:
                pass

            class AlphaProvider(Provider):
                def find(self, session, owner, name):
                    return session.query(Pet).filter(Pet.owner == owner, Pet.name == name).one()

            class BetaProvider(Provider):
                def find(self, session, name):
                    return session.execute(select(Pet).where(Pet.name == name)).scalar_one()

            class GammaProvider(Provider):
                def find(self, session, tag):
                    q = session.query(Pet).filter(func.lower(Pet.tag) == tag.lower())
                    return q.one_or_none()

            def by_owner_name(session, name):
                return session.query(Owner).filter_by(name=name).one()
        """,
    }
)


class TestMigrationDetectionIsShared(unittest.TestCase):
    """Migrations are what scan_common.migration_kind says, as in every scanner."""

    def test_a_helper_in_versions_is_not_a_revision(self):
        files = {
            # Mentions "revision" and defines upgrade(), but has no
            # module-level revision/down_revision: not an Alembic revision.
            "app/migrations/versions/helpers.py": (
                "# shared by every revision\ndef upgrade():\n    pass\n"
            ),
            "app/migrations/versions/abc123_add.py": (
                "revision = 'abc123'\ndown_revision = None\n"
                "def upgrade():\n    pass\n"
            ),
        }
        with TempProject(files) as root:
            alembic, django, dirs = mod._migration_files(root)
        self.assertEqual([p.name for p in alembic], ["abc123_add.py"])
        self.assertEqual(django, [])
        self.assertEqual(dirs, ["app/migrations/versions"])


class TestLookups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lookups = _map(LOOKUPS)["lookups"]

    def test_sibling_group_with_unique_keys(self):
        groups = {g["method"]: g for g in self.lookups["sibling_groups"]}
        group = groups["find"]
        self.assertTrue(group["filter_sets_differ"])
        covers = {
            m["class"]: m["lookups"][0]["covers_unique_key"] for m in group["members"]
        }
        # The relationship filter `Pet.owner == owner` covers the owner_id key.
        self.assertEqual(
            covers,
            {"AlphaProvider": True, "BetaProvider": False, "GammaProvider": True},
        )
        self.assertIn(
            "pets_owner_name_uc: (owner_id, name)", group["unique_keys"]["Pet"]
        )
        beta = next(m for m in group["members"] if m["class"] == "BetaProvider")
        diff = beta["lookups"][0]["closest_unique_key"][0]
        self.assertEqual(diff["missing"], ["owner_id"])

    def test_single_row_lookup_not_unique(self):
        flagged = [x["function"] for x in self.lookups["single_row_lookups_not_unique"]]
        self.assertEqual(flagged, ["BetaProvider.find"])

    def test_case_folded_unique_index_is_covered_by_folded_filter(self):
        groups = {g["method"]: g for g in self.lookups["sibling_groups"]}
        gamma = next(
            m for m in groups["find"]["members"] if m["class"] == "GammaProvider"
        )
        self.assertEqual(gamma["lookups"][0]["filters"], ["lower(tag)"])


class TestDjangoModels(unittest.TestCase):
    def test_django_models_unique_together_and_get(self):
        out = _map(
            {
                "app/__init__.py": "",
                "app/models.py": """
                    from django.db import models

                    class Shelf(models.Model):
                        code = models.CharField(max_length=5, unique=True)
                        room = models.CharField(max_length=5)
                        slot = models.IntegerField(null=True)

                        class Meta:
                            unique_together = [("room", "slot")]

                    def find(room):
                        return Shelf.objects.get(room=room)

                    def find_code(code):
                        return Shelf.objects.get(code__iexact=code)
                """,
            }
        )
        self.assertEqual(out["orm"], ["django"])
        keys = out["models"][0]["unique_keys"]
        self.assertIn("unique_together: (room, slot)", keys)
        flagged = [
            x["function"] for x in out["lookups"]["single_row_lookups_not_unique"]
        ]
        # A case-insensitive match on a case-sensitive unique column can
        # return several rows ("AB", "ab"): flagged.
        self.assertEqual(flagged, ["find", "find_code"])
        self.assertEqual(out["nullability"]["status"], "not_detected")


class TestNotDetected(unittest.TestCase):
    def test_no_models(self):
        out = _map({"app/__init__.py": "", "app/a.py": "class Thing:\n    size = 1\n"})
        self.assertIsNone(out["orm"])
        for section in ("nullability", "migrations", "lookups"):
            self.assertEqual(out[section]["status"], "not_detected")
            self.assertTrue(out[section]["reason"])

    def test_models_without_migrations(self):
        out = _map(MODELS)
        self.assertEqual(out["migrations"]["status"], "not_detected")
        self.assertIn("versions", out["migrations"]["reason"])

    def test_class_name_collision_resolved_through_imports(self):
        out = _map(
            {
                "app/__init__.py": "",
                "app/db.py": BASE,
                "app/mail.py": "class Message:\n    pass\n",
                "app/models.py": """
                    from app.db import Model, Mapped, mapped_column

                    class Message(Model):
                        __tablename__ = "messages"
                        body: Mapped[str]
                """,
                "app/sub/__init__.py": "",
                "app/sub/models.py": """
                    from app.models import Message

                    class Reply(Message):
                        __tablename__ = "replies"
                        quote: Mapped[str]
                """,
            }
        )
        names = sorted(m["model"] for m in out["models"])
        self.assertEqual(names, ["Message", "Reply"])


class TestCli(unittest.TestCase):
    def test_runs_and_prints_json(self):
        with TempProject({"app/__init__.py": "", "app/a.py": "x = 1\n"}) as root:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(_SCRIPTS_DIR / "map_orm_schema.py"),
                    str(root / "app"),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        data = json.loads(proc.stdout)
        self.assertEqual(data["summary"], {"models": 0})

    def test_help(self):
        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS_DIR / "map_orm_schema.py"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Usage:", proc.stdout)


if __name__ == "__main__":
    unittest.main()
