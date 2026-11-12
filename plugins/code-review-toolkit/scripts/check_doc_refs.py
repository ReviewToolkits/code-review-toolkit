#!/usr/bin/env python3
"""Check the references documentation makes: links, anchors, names, paths, make.

Backs the project-docs-auditor's reference validation. Offline and read-only;
each section is a candidate list to confirm, not a verdict:

- ``links``: relative links and anchors in the Markdown/reST sources (README,
  CONTRIBUTING, root ``*.md``/``*.rst``, ``docs/``/``doc/``) whose file or
  anchor does not exist. External URLs are counted, never fetched.
- ``site``: the same for a built HTML site (``--site DIR``): every relative
  ``href``/``src`` and ``#anchor`` against the generated files and their ``id``s.
- ``app_doc_links``: documentation URLs the application itself emits, from
  templates and Python code -- absolute URLs under a docs base (``site_url`` of
  an ``mkdocs*.yml``, or ``--docs-url BASE=DIR``) and calls to a docs-link
  helper (``--doc-helper NAME=DIR``, e.g. ``help_url('/guide/', anchor='x')``)
  -- resolved to a page and an anchor in the docs sources (or the built site).
- ``identifiers``: backticked names in the docs that look like code (a call, a
  dotted path into the project, a CamelCase class) and resolve to nothing the
  project defines, imports or mentions. URL fragments are not names.
- ``paths``: backticked relative paths (``pkg/settings.py``) that do not exist.
- ``make``: documented ``make X`` targets that the Makefile does not define
  (flagging the silent no-op when a file named ``X`` exists), and
  ``VAR=x make X`` overrides the Makefile ignores (it assigns ``VAR`` with
  ``=``/``:=`` rather than ``?=``) or never reads.

Usage:
    python check_doc_refs.py [path] [--max-files N] [--site DIR]
        [--docs-url BASE=DIR ...] [--doc-helper NAME=DIR ...]
"""

from __future__ import annotations

import html
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    _is_excluded,
    build_envelope,
    collect_python_files,
    emit,
    parse_common_args,
    parse_source,
    relative_to_root,
    resolve_target,
)
from survey_docstrings import ReferenceIndex  # noqa: E402

_DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt"})
_DOC_DIRS = ("docs", "doc")
_BUILD_DIRS = frozenset({"_build", "build", "site", "_site", "node_modules"})
_TEMPLATE_SUFFIXES = frozenset({".html", ".htm", ".jinja", ".jinja2", ".j2", ".txt"})
_MAX_BYTES = 2_000_000
_CAP = 200

# --------------------------------------------------------------------------
# Doc discovery and parsing
# --------------------------------------------------------------------------


def doc_files(project_root: Path) -> list[Path]:
    """Root-level docs plus every doc under docs/ or doc/, minus build output."""
    found = [
        p
        for p in sorted(project_root.iterdir())
        if p.is_file()
        and p.suffix.lower() in _DOC_SUFFIXES
        and not p.name.lower().startswith(("requirements", "constraints"))
    ]
    for name in _DOC_DIRS:
        base = project_root / name
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _DOC_SUFFIXES:
                continue
            parts = set(p.relative_to(base).parts[:-1])
            if parts & _BUILD_DIRS or any(x.endswith("-site") for x in parts):
                continue
            found.append(p)
    return found


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1")


def split_code(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """``(prose lines, code lines)``, each ``(line number, text)``. Code is a
    fenced block, a reST literal block (after ``::``) or a code-block
    directive; inline code spans are added to the code lines too."""
    prose: list[tuple[int, str]] = []
    code: list[tuple[int, str]] = []
    fence = None
    literal_indent: int | None = None
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            else:
                code.append((number, line))
            continue
        match = _FENCE.match(line)
        if match:
            fence = match.group(1)
            continue
        if literal_indent is not None:
            indent = len(line) - len(line.lstrip())
            if not stripped or indent > literal_indent:
                code.append((number, line))
                continue
            literal_indent = None
        if stripped.endswith("::") or stripped.startswith(
            (".. code-block::", ".. code::", ".. sourcecode::")
        ):
            literal_indent = len(line) - len(line.lstrip())
        prose.append((number, line))
        for span in _INLINE_CODE.finditer(line):
            code.append((number, span.group(2)))
    return prose, code


def _strip_markup(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [text](url) -> text
    text = text.replace("`", "")
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    # `_x_` is emphasis only at word edges: `snake_case_name` keeps its `_`.
    text = re.sub(r"(?<!\w)_{1,3}([^_]+)_{1,3}(?!\w)", r"\1", text)
    return re.sub(r"<[^>]+>", "", text)


def github_slug(text: str) -> str:
    """The anchor GitHub gives a heading when it renders Markdown: no
    stripping, so `## 📦 Install` becomes `-install`."""
    text = _strip_markup(text).lower()
    kept = "".join(
        ch
        for ch in text
        if ch.isalnum() or ch in "_- " or unicodedata.category(ch).startswith("M")
    )
    return kept.replace(" ", "-")


def slugify(text: str) -> str:
    """The anchor Python-Markdown's toc extension (and so MkDocs) gives a
    heading."""
    text = _strip_markup(text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_ATTR_ID = re.compile(r"\{[^}]*#([\w:.-]+)[^}]*\}")
_HTML_ID = re.compile(r"""\b(?:id|name)\s*=\s*["']([^"']+)["']""")
_RST_LABEL = re.compile(r"^\s*\.\.\s+_([^:]+):\s*$")
_TAB_LABEL = re.compile(r"""^\s*===\+?\s+["'](.+?)["']""")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+<>:._])\1{2,}\s*$")


def source_anchors(text: str) -> set[str]:
    """Anchors a Markdown/reST source defines: heading slugs (with the `_1`
    suffixes duplicates get), `{#id}` attributes, HTML ids, reST labels and
    tabbed-content labels."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    lines = text.splitlines()

    def heading(title: str) -> None:
        explicit = _ATTR_ID.search(title)
        if explicit:
            anchors.add(explicit.group(1))
            title = _ATTR_ID.sub("", title)
        anchors.add(github_slug(title))
        slug = slugify(title)
        if not slug:
            return
        n = counts.get(slug, 0)
        anchors.add(slug if n == 0 else f"{slug}_{n}")
        anchors.add(slug if n == 0 else f"{slug}-{n}")  # GitHub's suffix
        counts[slug] = n + 1

    for index, line in enumerate(lines):
        match = _MD_HEADING.match(line)
        if match:
            heading(match.group(1))
        elif (
            index + 1 < len(lines)
            and line.strip()
            and _RST_UNDERLINE.match(lines[index + 1])
            and len(lines[index + 1].strip()) >= len(line.strip())
        ):
            heading(line.strip())
        label = _RST_LABEL.match(line)
        if label:
            anchors.add(label.group(1).strip().lower().replace(" ", "-"))
        tab = _TAB_LABEL.match(line)
        if tab:
            anchors.add(slugify(tab.group(1)))
        anchors.update(_HTML_ID.findall(line))
        for attr in _ATTR_ID.finditer(line):
            anchors.add(attr.group(1))
    return anchors


_MD_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^)]*)?\)")
# A reference definition; `[^1]: text` is a footnote, not a link.
_MD_REF_DEF = re.compile(r"^\s{0,3}\[(?!\^)[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
_HREF = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""")
_RST_LINK = re.compile(r"`[^`<]*<([^>`]+)>`_")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def doc_links(text: str) -> list[tuple[int, str]]:
    """``(line, target)`` for every link a Markdown/reST/HTML text makes,
    outside code blocks."""
    prose, _code = split_code(text)
    out: list[tuple[int, str]] = []
    for number, line in prose:
        line = _INLINE_CODE.sub(" ", line)
        for pattern in (_MD_LINK, _HREF, _RST_LINK):
            out.extend((number, m.group(1)) for m in pattern.finditer(line))
        ref = _MD_REF_DEF.match(line)
        if ref:
            out.append((number, ref.group(1)))
    return out


# --------------------------------------------------------------------------
# Links in the doc sources
# --------------------------------------------------------------------------


def check_source_links(project_root: Path, docs: list[Path]) -> dict:
    anchors_cache: dict[Path, set[str]] = {}

    def anchors_of(path: Path) -> set[str] | None:
        if path not in anchors_cache:
            text = _read(path)
            anchors_cache[path] = source_anchors(text) if text is not None else set()
        return anchors_cache[path]

    broken: list[dict] = []
    checked = external = unchecked = 0
    for doc in docs:
        text = _read(doc)
        if text is None:
            continue
        rel = relative_to_root(doc, project_root)
        for number, target in doc_links(text):
            target = html.unescape(target.strip())
            if _SCHEME.match(target) or target.startswith("//"):
                external += 1
                continue
            if target.startswith("/") or "{" in target:
                unchecked += 1  # site-root relative, or a template expression
                continue
            path_part, _, anchor = target.partition("#")
            path_part = path_part.split("?", 1)[0]
            checked += 1
            dest = (doc.parent / path_part).resolve() if path_part else doc
            if path_part and not dest.exists():
                # MkDocs serves `page.md` as `page/`; a link to `page/` is fine.
                alt = dest.with_suffix(".md") if dest.suffix == "" else None
                if not (alt and alt.exists()):
                    broken.append(
                        {
                            "file": rel,
                            "line": number,
                            "target": target,
                            "kind": "missing_file",
                        }
                    )
                    continue
                dest = alt
            if (
                anchor
                and dest.is_file()
                and dest.suffix.lower() in _DOC_SUFFIXES
                and anchor not in (anchors_of(dest) or set())
            ):
                broken.append(
                    {
                        "file": rel,
                        "line": number,
                        "target": target,
                        "kind": "missing_anchor",
                    }
                )
    return {
        "checked": checked,
        "external_skipped": external,
        "unchecked": unchecked,
        "broken": broken[:_CAP],
        "broken_total": len(broken),
    }


# --------------------------------------------------------------------------
# The built site
# --------------------------------------------------------------------------


def _page_for(site: Path, url_path: str) -> Path | None:
    url_path = url_path.lstrip("/")
    candidates = [site / url_path] if url_path else []
    candidates += [site / url_path / "index.html", site / f"{url_path}.html"]
    for c in candidates:
        if c.is_file():
            return c
    return None


def check_site(site: Path) -> dict:
    """Relative links and anchors of every page of a built HTML site."""
    ids: dict[Path, set[str]] = {}

    def ids_of(page: Path) -> set[str]:
        if page not in ids:
            ids[page] = set(_HTML_ID.findall(_read(page) or ""))
        return ids[page]

    broken: list[dict] = []
    pages = sorted(site.rglob("*.html"))
    checked = 0
    for page in pages:
        text = _read(page) or ""
        text = re.sub(r"<(script|style|pre|code)\b.*?</\1>", "", text, flags=re.DOTALL)
        rel = str(page.relative_to(site))
        for target in _HREF.findall(text):
            target = html.unescape(target)
            if _SCHEME.match(target) or target.startswith(("//", "/")):
                continue
            path_part, _, anchor = target.partition("#")
            path_part = path_part.split("?", 1)[0]
            checked += 1
            dest = (page.parent / path_part).resolve() if path_part else page
            if dest.is_dir():
                dest = dest / "index.html"
            if path_part and not dest.exists():
                broken.append({"page": rel, "target": target, "kind": "missing_file"})
            elif anchor and dest.suffix == ".html" and anchor not in ids_of(dest):
                broken.append({"page": rel, "target": target, "kind": "missing_anchor"})
    return {
        "site": str(site),
        "pages": len(pages),
        "checked": checked,
        "broken": broken[:_CAP],
        "broken_total": len(broken),
    }


# --------------------------------------------------------------------------
# Documentation links the application emits
# --------------------------------------------------------------------------


def _mkdocs_bases(project_root: Path) -> dict[str, Path]:
    """``{site_url: docs dir}`` from every ``mkdocs*.yml`` near the root."""
    bases: dict[str, Path] = {}
    configs = sorted(project_root.glob("mkdocs*.y*ml")) + sorted(
        project_root.glob("docs/mkdocs*.y*ml")
    )
    for config in configs:
        text = _read(config) or ""
        url = re.search(r"^site_url:\s*['\"]?(\S+?)['\"]?\s*$", text, re.MULTILINE)
        docs_dir = re.search(r"^docs_dir:\s*['\"]?(\S+?)['\"]?\s*$", text, re.MULTILINE)
        if url:
            folder = config.parent / (docs_dir.group(1) if docs_dir else "docs")
            bases[url.group(1).rstrip("/") + "/"] = folder
    return bases


def resolve_doc_page(docs_dir: Path, url_path: str) -> Path | None:
    """The source page a docs URL path is built from (MkDocs or Sphinx
    layout): `/a/b/` -> a/b.md, a/b/index.md or a/b/README.md."""
    stem = url_path.strip("/")
    stem = stem.removesuffix(".html")
    if stem.endswith("/index") or stem == "index":
        stem = stem[: -len("index")].rstrip("/")
    base = docs_dir / stem if stem else docs_dir
    names = [base.with_suffix(s) for s in (".md", ".rst", ".txt")] if stem else []
    names += [base / f"index{s}" for s in (".md", ".rst")] + [base / "README.md"]
    return next((n for n in names if n.is_file()), None)


_ABS_URL = re.compile(r"""https?://[^\s"'<>()\[\]{}]+""")


def _helper_call(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"""\b{re.escape(name)}\(\s*["']([^"']*)["']"""
        rf"""(?:\s*,\s*anchor\s*=\s*["']([^"']*)["'])?"""
    )


def collect_app_doc_links(
    project_root: Path,
    scan_root: Path,
    py_files: list[Path],
    bases: dict[str, Path],
    helpers: dict[str, Path],
) -> list[dict]:
    """``{file, line, url, docs_dir, path, anchor, via}`` for each docs URL
    the templates and Python code under *scan_root* emit."""
    found: list[dict] = []
    sources = list(py_files)
    if scan_root.is_dir():
        sources += [
            p
            for p in sorted(scan_root.rglob("*"))
            if p.suffix.lower() in _TEMPLATE_SUFFIXES
            and p.is_file()
            and not _is_excluded(p, scan_root)
        ]
    patterns = {name: _helper_call(name) for name in helpers}
    unmapped = re.compile(r"""\b(\w*docs?_url)\(\s*["']([^"']*)["']""")
    for path in sources:
        text = _read(path)
        if text is None:
            continue
        rel = relative_to_root(path, project_root)
        for number, line in enumerate(text.splitlines(), 1):
            for url in _ABS_URL.findall(line):
                url = url.rstrip(".,;")
                for base, folder in bases.items():
                    if url.startswith(base) or url.rstrip("/") + "/" == base:
                        rest = url[len(base) :] if url.startswith(base) else ""
                        page, _, anchor = rest.partition("#")
                        found.append(
                            {
                                "file": rel,
                                "line": number,
                                "url": url,
                                "docs_dir": folder,
                                "path": "/" + page.split("?", 1)[0],
                                "anchor": anchor or None,
                                "via": "absolute_url",
                            }
                        )
                        break
            mapped = False
            for name, pattern in patterns.items():
                for match in pattern.finditer(line):
                    mapped = True
                    found.append(
                        {
                            "file": rel,
                            "line": number,
                            "url": match.group(0),
                            "docs_dir": helpers[name],
                            "path": match.group(1),
                            "anchor": match.group(2) or None,
                            "via": f"helper:{name}",
                        }
                    )
            if not mapped:
                for match in unmapped.finditer(line):
                    if match.group(1) not in helpers:
                        found.append(
                            {
                                "file": rel,
                                "line": number,
                                "url": match.group(0),
                                "docs_dir": None,
                                "path": match.group(2),
                                "anchor": None,
                                "via": f"helper:{match.group(1)}",
                            }
                        )
    return found


def check_app_doc_links(
    project_root: Path, links: list[dict], site: Path | None
) -> dict:
    anchors: dict[Path, set[str]] = {}
    results: list[dict] = []
    for link in links:
        folder = link.pop("docs_dir")
        entry = dict(link)
        if folder is None:
            entry["status"] = "unchecked"
            entry["reason"] = (
                "a docs-link helper with no mapping: pass --doc-helper NAME=DIR"
            )
            results.append(entry)
            continue
        page = resolve_doc_page(folder, link["path"])
        entry["page"] = relative_to_root(page, project_root) if page else None
        if page is None:
            entry["status"] = "missing_page"
        elif link["anchor"]:
            if page not in anchors:
                anchors[page] = source_anchors(_read(page) or "")
            entry["status"] = (
                "ok" if link["anchor"] in anchors[page] else "missing_anchor"
            )
        else:
            entry["status"] = "ok"
        if site is not None and entry["status"] == "missing_anchor":
            built = _page_for(site, link["path"])
            if built and link["anchor"] in set(_HTML_ID.findall(_read(built) or "")):
                entry["status"] = "ok"  # the built page has it (a generated id)
        results.append(entry)
    problems = [r for r in results if r["status"] not in ("ok",)]
    return {
        "checked": sum(1 for r in results if r["status"] != "unchecked"),
        "unchecked": sum(1 for r in results if r["status"] == "unchecked"),
        "problems": problems[:_CAP],
        "problems_total": len(problems),
        "ok": len(results) - len(problems),
    }


# --------------------------------------------------------------------------
# Names and paths cited in the docs
# --------------------------------------------------------------------------

_CODE_NAME = re.compile(r"^[~.]?([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)(\(\))?$")
_PATH = re.compile(r"^(?:\./)?[\w.-]+(?:/[\w.-]+)+/?$")
# `docs/RELEASE-vX.Y.Z.md`, `<name>/config.py`, `logs/YYYY-MM.txt`: templates.
_PLACEHOLDER = re.compile(r"X\.Y|\bvX\b|YYYY|[<>{}*]|\.\.\.")
_HISTORY_STEMS = ("changelog", "changes", "history", "news", "release")
_PLANNING_STEMS = ("roadmap", "todo", "backlog", "plan")
# `pkg.example.org`, `requirements.in`, `setup.py`: a host or a file name,
# not a dotted path into the code.
_NOT_A_PATH_TAIL = frozenset({
    "io", "org", "com", "net", "dev", "app", "ai", "co", "in", "txt", "md", "rst",
    "py", "pyi", "toml", "yml", "yaml", "json", "cfg", "ini", "lock", "html", "js",
    "css", "sh", "gz", "zip", "whl", "tar", "log", "csv", "pot", "po", "mo",
})  # fmt: skip


def check_names_and_paths(
    project_root: Path, docs: list[Path], index: ReferenceIndex
) -> tuple[dict, dict]:
    names: list[dict] = []
    paths: list[dict] = []
    names_checked = paths_checked = 0
    packages = index.packages
    package_dirs = [project_root / p for p in packages if (project_root / p).is_dir()]
    package_dirs += [project_root / "src" / p for p in packages]
    for doc in docs:
        if doc.name.lower().startswith(_HISTORY_STEMS + _PLANNING_STEMS):
            # A changelog names what used to exist, and a roadmap or design
            # plan what does not exist yet, by design.
            continue
        text = _read(doc)
        if text is None:
            continue
        rel = relative_to_root(doc, project_root)
        for number, line in split_code(text)[0]:
            for span in _INLINE_CODE.finditer(line):
                token = span.group(2).strip()
                if " " in token or "://" in token or token.startswith(("-", "$")):
                    continue
                if _PLACEHOLDER.search(token):
                    continue
                if _PATH.match(token) and not token.startswith("/"):
                    target = token.removeprefix("./").rstrip("/")
                    head = target.split("/", 1)[0]
                    bases = [project_root, doc.parent, *package_dirs]
                    # Judge only paths that start somewhere real: `org/repo`
                    # slugs and paths on the reader's machine are not ours.
                    if not any((b / head).exists() for b in bases):
                        continue
                    paths_checked += 1
                    if not any((b / target).exists() for b in bases):
                        paths.append({"file": rel, "line": number, "path": token})
                    continue
                match = _CODE_NAME.match(token)
                if not match:
                    continue
                name, call = match.group(1), match.group(2)
                dotted_project = (
                    "." in name
                    and name.split(".", 1)[0] in packages
                    and name.rsplit(".", 1)[-1].lower() not in _NOT_A_PATH_TAIL
                )
                # A call (`helper()`) or a dotted path into the project's own
                # packages. Bare words, CamelCase product names and
                # third-party paths are prose here, not references.
                if not (call or dotted_project):
                    continue
                if "." in name and not dotted_project:
                    continue
                names_checked += 1
                reason = index.resolve(name)
                if reason:
                    names.append(
                        {
                            "file": rel,
                            "line": number,
                            "name": token,
                            "confidence": "high" if dotted_project else "medium",
                            "reason": reason,
                        }
                    )
    return (
        {
            "checked": names_checked,
            "unresolved": names[:_CAP],
            "unresolved_total": len(names),
        },
        {
            "checked": paths_checked,
            "missing": paths[:_CAP],
            "missing_total": len(paths),
        },
    )


# --------------------------------------------------------------------------
# make
# --------------------------------------------------------------------------

_MAKE_ASSIGN = re.compile(
    r"^\s*(?:override\s+|export\s+)*([A-Za-z_][A-Za-z0-9_]*)\s*(\?=|::=|:=|\+=|!=|=)"
)
_MAKE_RULE = re.compile(r"^([^\s#:=][^:=#]*?)\s*::?(?!=)")
_MAKE_REF = re.compile(
    r"\$[({]([A-Za-z_][A-Za-z0-9_]*)[)}]|\$\$\{?([A-Za-z_][A-Za-z0-9_]*)"
)
_MAKE_CALL = re.compile(
    r"(?:^|[\s;&|(`$])((?:[A-Z_][A-Z0-9_]*=\S*\s+)*)make((?:\s+-\S+)*)"
    r"((?:\s+(?:[A-Za-z_][A-Za-z0-9_]*=\S*|[A-Za-z0-9][\w.%/-]*))*)"
)


def parse_makefile(path: Path) -> dict:
    targets: set[str] = set()
    phony: set[str] = set()
    assigned: dict[str, tuple[str, int]] = {}
    referenced: set[str] = set()
    text = _read(path) or ""
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("\t") or line.lstrip().startswith("#"):
            referenced |= {a or b for a, b in _MAKE_REF.findall(line)}
            continue
        assign = _MAKE_ASSIGN.match(line)
        if assign:
            assigned.setdefault(assign.group(1), (assign.group(2), number))
            referenced |= {a or b for a, b in _MAKE_REF.findall(line[assign.end() :])}
            continue
        rule = _MAKE_RULE.match(line)
        if rule:
            names = rule.group(1).split()
            if names and names[0] == ".PHONY":
                phony.update(line.split(":", 1)[1].split())
            else:
                targets.update(n for n in names if not n.startswith("."))
        referenced |= {a or b for a, b in _MAKE_REF.findall(line)}
    return {
        "targets": targets,
        "phony": phony,
        "assigned": assigned,
        "referenced": referenced,
    }


def _has_target(target: str, targets: set[str]) -> bool:
    if target in targets:
        return True
    return any(
        "%" in t and re.fullmatch(re.escape(t).replace("%", ".+"), target)
        for t in targets
    )


_ENV_READERS = frozenset(
    {".py", ".sh", ".bash", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".env"}
)


def _mentioned_in_project(project_root: Path, names: set[str]) -> set[str]:
    """Which *names* some script, config or compose file mentions: make
    exports a documented variable to its recipes' environment, so a recipe's
    subprocess may read it without the Makefile ever doing so."""
    found: set[str] = set()
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b")
    for path in project_root.rglob("*"):
        if found == names:
            break
        if not path.is_file() or _is_excluded(path, project_root):
            continue
        if path.suffix.lower() not in _ENV_READERS and not path.name.startswith(
            ("Dockerfile", "docker-compose", "compose", ".env")
        ):
            continue
        found |= set(pattern.findall(_read(path) or ""))
    return found


def check_make(project_root: Path, docs: list[Path]) -> dict | None:
    makefiles = [
        p for p in (project_root / n for n in ("GNUmakefile", "makefile", "Makefile"))
        if p.is_file()
    ]  # fmt: skip
    if not makefiles:
        return None
    parsed = [parse_makefile(p) for p in makefiles]
    targets = set().union(*(m["targets"] for m in parsed))
    phony = set().union(*(m["phony"] for m in parsed))
    assigned: dict[str, tuple[str, int]] = {}
    for m in parsed:
        for var, value in m["assigned"].items():
            assigned.setdefault(var, value)
    referenced = set().union(*(m["referenced"] for m in parsed))
    missing: list[dict] = []
    ignored: list[dict] = []
    unused: list[dict] = []
    collisions: list[dict] = []
    seen_collision: set[str] = set()
    calls = 0
    for doc in docs:
        text = _read(doc)
        if text is None:
            continue
        rel = relative_to_root(doc, project_root)
        for number, line in split_code(text)[1]:
            for match in _MAKE_CALL.finditer(line):
                if line[match.end() : match.end() + 1] in ("{", "<", "$"):
                    continue  # `make requirements/{file}.txt`: a placeholder
                env_vars = [v.split("=", 1)[0] for v in match.group(1).split()]
                words = match.group(3).split()
                cli_vars = [w.split("=", 1)[0] for w in words if "=" in w]
                goals = [w for w in words if "=" not in w]
                if not goals and not env_vars and not cli_vars:
                    continue
                calls += 1
                command = match.group(0).strip()[:160]
                for goal in goals[:3]:
                    if not _has_target(goal, targets):
                        missing.append(
                            {
                                "file": rel,
                                "line": number,
                                "target": goal,
                                "command": command,
                                # `make tests` with a tests/ directory and no
                                # such target prints "Nothing to be done" and
                                # exits 0: a silent no-op.
                                "path_exists": (project_root / goal).exists(),
                            }
                        )
                    elif (
                        (project_root / goal).exists()
                        and goal not in phony
                        and goal not in seen_collision
                    ):
                        seen_collision.add(goal)
                        collisions.append({"target": goal, "file": rel, "line": number})
                for var in env_vars:
                    if var in assigned and assigned[var][0] != "?=":
                        op, at = assigned[var]
                        ignored.append(
                            {
                                "file": rel,
                                "line": number,
                                "variable": var,
                                "command": command,
                                "makefile_assignment": f"{var} {op} (line {at})",
                            }
                        )
                    elif var not in assigned and var not in referenced:
                        unused.append(
                            {
                                "file": rel,
                                "line": number,
                                "variable": var,
                                "command": command,
                            }
                        )
                for var in cli_vars:
                    if var not in assigned and var not in referenced:
                        unused.append(
                            {
                                "file": rel,
                                "line": number,
                                "variable": var,
                                "command": command,
                            }
                        )
    if unused:
        elsewhere = _mentioned_in_project(project_root, {u["variable"] for u in unused})
        unused = [u for u in unused if u["variable"] not in elsewhere]
    return {
        "makefiles": [relative_to_root(p, project_root) for p in makefiles],
        "targets": len(targets),
        "documented_calls": calls,
        "missing_targets": missing[:_CAP],
        # A real target named like a file or directory and missing from
        # .PHONY: make treats it as up to date once that path exists.
        "not_phony_path_collisions": collisions[:_CAP],
        # `VAR=x make t` while the Makefile assigns VAR with = or := : the
        # environment value is overridden, so the documented override does
        # nothing. Only `?=` (or no assignment) lets it through.
        "env_overrides_ignored": ignored[:_CAP],
        # Variables a documented command sets that no Makefile line, script,
        # config or compose file mentions: the override does nothing.
        "variables_not_in_makefile": unused[:_CAP],
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _pairs(values: list[str], project_root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for value in values:
        key, sep, folder = value.rpartition("=")
        if sep and key:
            out[key] = (project_root / folder).resolve()
    return out


def analyze(
    target: str,
    *,
    max_files: int = 0,
    site: str | None = None,
    docs_urls: list[str] | None = None,
    doc_helpers: list[str] | None = None,
) -> dict:
    """Check the references the docs under *target*'s project make."""
    _resolved, project_root, scan_root = resolve_target(target)
    py_files, files_total = collect_python_files(scan_root, max_files)
    docs = doc_files(project_root)

    index = ReferenceIndex()
    unparseable: list[str] = []
    for path in collect_python_files(project_root)[0]:
        tree = parse_source(path)
        if tree is None:
            unparseable.append(relative_to_root(path, project_root))
            continue
        index.add(relative_to_root(path, project_root), tree)

    bases = _mkdocs_bases(project_root)
    bases.update(_pairs(docs_urls or [], project_root))
    helpers = _pairs(doc_helpers or [], project_root)
    site_path = Path(site).resolve() if site else None

    result = build_envelope(project_root, scan_root, files_total, len(py_files))
    result["doc_files"] = [relative_to_root(p, project_root) for p in docs]
    result["links"] = check_source_links(project_root, docs)
    if site_path is not None and site_path.is_dir():
        result["site"] = check_site(site_path)
    else:
        result["site"] = None
        result["site_reason"] = (
            f"--site {site} is not a directory"
            if site
            else "not requested: build the docs into a private directory and pass --site DIR"
        )
    links = collect_app_doc_links(project_root, scan_root, py_files, bases, helpers)
    result["docs_bases"] = {
        k: relative_to_root(v, project_root) for k, v in sorted(bases.items())
    }
    result["doc_helpers"] = {
        k: relative_to_root(v, project_root) for k, v in sorted(helpers.items())
    }
    result["app_doc_links"] = check_app_doc_links(project_root, links, site_path)
    result["identifiers"], result["paths"] = check_names_and_paths(
        project_root, docs, index
    )
    make = check_make(project_root, docs)
    result["make"] = make
    if make is None:
        result["make_reason"] = "no Makefile at the project root"
    result["summary"] = {
        "doc_files": len(docs),
        "broken_links": result["links"]["broken_total"],
        "broken_site_links": None
        if result["site"] is None
        else result["site"]["broken_total"],
        "app_doc_link_problems": result["app_doc_links"]["problems_total"],
        "app_doc_links_unchecked": result["app_doc_links"]["unchecked"],
        "unresolved_identifiers": result["identifiers"]["unresolved_total"],
        "missing_paths": result["paths"]["missing_total"],
        "missing_make_targets": None if make is None else len(make["missing_targets"]),
        "env_overrides_ignored": None
        if make is None
        else len(make["env_overrides_ignored"]),
    }
    result["skipped"] = {"unparseable": unparseable}
    return result


def _option_values(argv: list[str], flag: str) -> list[str]:
    values: list[str] = []
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            values.append(argv[i + 1])
        elif arg.startswith(flag + "="):
            values.append(arg.split("=", 1)[1])
    return values


def main() -> None:
    argv = sys.argv[1:]
    own = ("--site", "--docs-url", "--doc-helper")
    rest: list[str] = []
    skip = False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg in own:
            skip = i + 1 < len(argv)
            continue
        if not arg.startswith(tuple(f"{f}=" for f in own)):
            rest.append(arg)
    target, max_files = parse_common_args(rest, __doc__)
    site = _option_values(argv, "--site")
    emit(
        analyze(
            target,
            max_files=max_files,
            site=site[-1] if site else None,
            docs_urls=_option_values(argv, "--docs-url"),
            doc_helpers=_option_values(argv, "--doc-helper"),
        )
    )


if __name__ == "__main__":
    main()
