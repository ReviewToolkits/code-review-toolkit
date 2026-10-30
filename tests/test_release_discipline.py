"""Release discipline -- improvement-plan item 0.6.

The incident this exists to prevent: `python-pitfall-scanner` and
`test-investigation-agent` were added to the repo but no release was cut, so the
installed plugin genuinely did not contain them and both were invisible to the
agent registry for an entire session. Nothing in a repository can detect that a
user has not updated; what it CAN do is make adding an agent impossible to do
silently.

`_EXPECTED` below is that tripwire. Adding or removing an agent, command, or
script fails these tests, and the fix is to update the count **and bump
`plugins/code-review-toolkit/.claude-plugin/plugin.json`** so the change reaches
an installed plugin. That is the whole mechanism -- deliberately annoying, in
proportion to a defect that cost a session.

The rest of the module checks the wiring that makes an agent reachable at all:
valid frontmatter, a name matching its filename, dispatch from a command, and
existence of everything the shape catalog points at.
"""

import json
import re
import unittest
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "code-review-toolkit"
_AGENTS = _PLUGIN / "agents"
_COMMANDS = _PLUGIN / "commands"
_SCRIPTS = _PLUGIN / "scripts"
_DATA = _PLUGIN / "data"
_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# Update these WITH a version bump in plugin.json. See the module docstring.
_EXPECTED = {"agents": 19, "commands": 8, "scripts": 23}


def _frontmatter(path: Path) -> dict[str, str]:
    """Parse the leading `---` YAML block. Flat `key: value` pairs only."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if line.startswith((" ", "\t")) or ":" not in line:
            continue  # a continuation of the previous value
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def _agent_names() -> list[str]:
    return sorted(p.stem for p in _AGENTS.glob("*.md"))


class TestInventoryTripwire(unittest.TestCase):
    """Changing the plugin's surface must be a deliberate, visible act."""

    def test_agent_count(self):
        self.assertEqual(
            len(list(_AGENTS.glob("*.md"))),
            _EXPECTED["agents"],
            "Agent added or removed. Update _EXPECTED AND bump plugin.json — an "
            "agent added without a release is invisible to the registry.",
        )

    def test_command_count(self):
        self.assertEqual(
            len(list(_COMMANDS.glob("*.md"))),
            _EXPECTED["commands"],
            "Command added or removed. Update _EXPECTED AND bump plugin.json.",
        )

    def test_script_count(self):
        self.assertEqual(
            len(list(_SCRIPTS.glob("*.py"))),
            _EXPECTED["scripts"],
            "Script added or removed. Update _EXPECTED AND bump plugin.json.",
        )


class TestAgentWiring(unittest.TestCase):
    """An agent that is not wired in is not an agent."""

    def test_every_agent_has_frontmatter_with_a_name_and_description(self):
        for name in _agent_names():
            with self.subTest(agent=name):
                fm = _frontmatter(_AGENTS / f"{name}.md")
                self.assertIn("name", fm, "missing YAML frontmatter or `name`")
                self.assertIn("description", fm)
                self.assertTrue(fm["description"].strip())

    def test_agent_name_matches_its_filename(self):
        # Claude Code dispatches on the frontmatter `name`; a mismatch means the
        # file exists and the agent is unreachable.
        for name in _agent_names():
            with self.subTest(agent=name):
                self.assertEqual(_frontmatter(_AGENTS / f"{name}.md").get("name"), name)

    def test_every_agent_is_dispatched_by_some_command(self):
        commands = "\n".join(
            p.read_text(encoding="utf-8") for p in _COMMANDS.glob("*.md")
        )
        for name in _agent_names():
            with self.subTest(agent=name):
                self.assertIn(
                    name,
                    commands,
                    f"{name} is defined but no command dispatches it — it will "
                    "never run.",
                )

    def test_every_script_backed_agent_has_run_instructions(self):
        # Without them an agent takes the 120 s Bash default, shares a fixed temp
        # path with agents running in parallel, and retries a timed-out script —
        # or reads a failed run's empty output as "no findings".
        invocation = re.compile(r"<plugin_root>/scripts/(\w+\.py)")
        for name in _agent_names():
            text = (_AGENTS / f"{name}.md").read_text(encoding="utf-8")
            script = invocation.search(text)
            if script is None:
                continue
            with self.subTest(agent=name):
                self.assertIn("\n## Running the script\n", text)
                self.assertIn("timeout: 300000", text)
                self.assertIn(f'mktemp -d "${{TMPDIR:-/tmp}}/crt-{name}.XXXXXX"', text)
                self.assertIn(f"scripts/{script.group(1)} [scope]", text)


class TestCatalogReferencesResolve(unittest.TestCase):
    """The shape catalog points at agents and scripts; both must exist."""

    def setUp(self):
        self.shapes = json.loads(
            (_DATA / "python_bug_shapes.json").read_text(encoding="utf-8")
        )["shapes"]

    def test_every_owning_agent_exists(self):
        known = set(_agent_names())
        for shape in self.shapes:
            with self.subTest(shape=shape["id"]):
                self.assertIn(
                    shape["agent"],
                    known,
                    f"shape {shape['id']} is owned by a nonexistent agent",
                )

    def test_every_detected_by_script_exists(self):
        for shape in self.shapes:
            script = shape.get("detected_by")
            if not script:
                continue
            with self.subTest(shape=shape["id"]):
                self.assertTrue(
                    (_SCRIPTS / script).is_file(),
                    f"shape {shape['id']} names a nonexistent script {script}",
                )

    def test_detectability_and_detected_by_agree(self):
        # An `implemented` shape must name the check that implements it, and an
        # `agent-only` shape must not claim one.
        for shape in self.shapes:
            with self.subTest(shape=shape["id"]):
                detectability = shape["detectability"]
                self.assertIn(
                    detectability, {"implemented", "implementable", "agent-only"}
                )
                if detectability == "implemented":
                    self.assertTrue(shape.get("detected_by"))
                else:
                    self.assertIsNone(shape.get("detected_by"))

    def test_aliases_do_not_collide_with_real_shape_ids(self):
        ids = {s["id"] for s in self.shapes}
        for shape in self.shapes:
            for alias in shape.get("aliases", []):
                with self.subTest(alias=alias):
                    self.assertNotIn(
                        alias, ids, "an alias must not also be a live shape id"
                    )


class TestPromptReferencesShip(unittest.TestCase):
    """Every plugin file a prompt cites must be inside the plugin.

    1.15.0 shipped six prompts citing `docs/reproduction-convention.md`, which
    lived at the repo root: the installed plugin had no such file, and agents
    reported the reference as broken.
    """

    # `<plugin_root>/x` anywhere, and bare `data/x` or `scripts/x` in backticks.
    # A bare `docs/x` usually names the reviewed project's docs (`docs/conf.py`),
    # so plugin docs must be cited with the `<plugin_root>/` prefix.
    _REF = re.compile(
        r"<plugin_root>/([A-Za-z0-9_./-]+)|`((?:data|scripts)/[A-Za-z0-9_./-]+)`"
    )

    def test_every_cited_plugin_path_exists(self):
        for prompt in sorted([*_AGENTS.glob("*.md"), *_COMMANDS.glob("*.md")]):
            text = prompt.read_text(encoding="utf-8")
            for match in self._REF.finditer(text):
                rel = (match.group(1) or match.group(2)).rstrip(".")
                with self.subTest(prompt=prompt.name, path=rel):
                    self.assertTrue(
                        (_PLUGIN / rel).exists(),
                        f"{prompt.name} cites {rel}, which the plugin does not ship",
                    )

    def test_toolkit_docs_are_cited_through_plugin_root(self):
        # A bare `docs/<name>` naming one of the toolkit's own docs is how 1.15.0
        # broke: it resolves in this repo and nowhere in an installed plugin.
        own = {p.name for p in (_PLUGIN / "docs").glob("*")}
        own |= {p.name for p in (_PLUGIN.parent.parent / "docs").glob("*")}
        bare = re.compile(r"(?<![/\w])docs/([A-Za-z0-9_.-]+)")
        for prompt in sorted([*_AGENTS.glob("*.md"), *_COMMANDS.glob("*.md")]):
            text = prompt.read_text(encoding="utf-8").replace("<plugin_root>/docs/", "")
            for match in bare.finditer(text):
                with self.subTest(prompt=prompt.name, path=match.group(0)):
                    self.assertNotIn(
                        match.group(1),
                        own,
                        f"{prompt.name} cites {match.group(0)} without <plugin_root>/",
                    )


class TestChangelog(unittest.TestCase):
    """The changelog is how a release gets cut; it has to be well-formed."""

    def setUp(self):
        self.text = _CHANGELOG.read_text(encoding="utf-8")
        self.headings = re.findall(r"^## \[([^\]]+)\]", self.text, re.MULTILINE)

    def test_at_most_one_unreleased_section(self):
        # There were two for the whole 1.4.0 and 1.5.0 cycles: the heading was
        # never renamed when 1.4.0 was cut, so released work sat under
        # "Unreleased" and the next release had nowhere unambiguous to land.
        self.assertLessEqual(
            self.headings.count("Unreleased"),
            1,
            "CHANGELOG.md must not have more than one [Unreleased] section",
        )

    def test_no_duplicate_version_headings(self):
        versions = [h for h in self.headings if h != "Unreleased"]
        self.assertEqual(
            len(versions), len(set(versions)), f"duplicate version headings: {versions}"
        )

    def test_plugin_version_is_a_released_heading_or_work_is_pending(self):
        version = json.loads(
            (_PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )["version"]
        unreleased_body = self.text.split("## [Unreleased]", 1)[-1].split("\n## [", 1)[
            0
        ]
        self.assertTrue(
            version in self.headings or unreleased_body.strip(),
            f"plugin version {version} has no changelog entry and nothing is "
            "pending under [Unreleased] — one of the two must be true.",
        )


if __name__ == "__main__":
    unittest.main()
