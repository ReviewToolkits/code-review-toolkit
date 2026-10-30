#!/usr/bin/env python3
"""Count type annotation coverage and catalog type design patterns.

Surveys: annotated vs. unannotated function signatures, Any usage,
type: ignore comments, data container types (dataclass, TypedDict,
NamedTuple, Protocol, Enum), and class attribute annotations.

Usage:
    python count_types.py [path]
"""

import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_common import (  # noqa: E402
    discover_python_files,
    find_project_root,
)


def _annotation_to_str(node: ast.AST | None) -> str | None:
    """Convert an annotation AST node to a string representation."""
    if node is None:
        return None
    return ast.unparse(node)


def _is_any(annotation: ast.AST | None) -> bool:
    """Check if an annotation is 'Any'."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name) and annotation.id == "Any":
        return True
    if isinstance(annotation, ast.Attribute) and annotation.attr == "Any":
        return True
    return False


def _contains_any(annotation: ast.AST | None) -> bool:
    """Check if an annotation contains Any anywhere (e.g. Dict[str, Any])."""
    if annotation is None:
        return False
    if _is_any(annotation):
        return True
    for child in ast.walk(annotation):
        if _is_any(child):
            return True
    return False


def _detect_container_type(class_node: ast.ClassDef) -> str | None:
    """Detect if a class is a dataclass, TypedDict, NamedTuple, Protocol, Enum, or ABC."""
    # Check decorators for @dataclass.
    for dec in class_node.decorator_list:
        dec_name = None
        if isinstance(dec, ast.Name):
            dec_name = dec.id
        elif isinstance(dec, ast.Attribute):
            dec_name = dec.attr
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                dec_name = dec.func.id
            elif isinstance(dec.func, ast.Attribute):
                dec_name = dec.func.attr
        if dec_name == "dataclass":
            return "dataclass"

    # Check base classes.
    for base in class_node.bases:
        base_name = None
        if isinstance(base, ast.Name):
            base_name = base.id
        elif isinstance(base, ast.Attribute):
            base_name = base.attr
        elif isinstance(base, ast.Subscript):
            if isinstance(base.value, ast.Name):
                base_name = base.value.id
            elif isinstance(base.value, ast.Attribute):
                base_name = base.value.attr

        if base_name == "TypedDict":
            return "TypedDict"
        elif base_name == "NamedTuple":
            return "NamedTuple"
        elif base_name == "Protocol":
            return "Protocol"
        elif base_name in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"):
            return "Enum"
        elif base_name == "ABC":
            return "ABC"

    return None


def _check_frozen_dataclass(class_node: ast.ClassDef) -> bool:
    """Check if a dataclass has frozen=True."""
    for dec in class_node.decorator_list:
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "frozen" and isinstance(kw.value, ast.Constant):
                    return bool(kw.value.value)
    return False


def _function_any_usage(rel: str, func_info: dict) -> dict:
    """One `any_usages[]` entry for a function or method.

    Every entry, whatever its `kind`, carries `file` and `line`; `location` is
    the same pair as one `file:line` string. `details` lists the parameters
    whose annotation contains Any and is empty when Any is in the return
    annotation only (then `in_return` is true).
    """
    return {
        "kind": "function",
        "file": rel,
        "line": func_info["line"],
        "location": f"{rel}:{func_info['line']}",
        "function": func_info["name"],
        "details": func_info["any_in_params"],
        "in_return": func_info["any_in_return"],
    }


def analyze_file(filepath: Path, project_root: Path) -> dict:
    """Analyze type annotations in a file."""
    rel = str(filepath.relative_to(project_root))
    result: dict = {
        "file": rel,
        "functions": [],
        "classes": [],
        "any_usages": [],
        "type_ignore_count": 0,
        "untyped_containers": [],
        "parse_error": None,
        # Filled in below; stays None when the file could not be parsed.
        "coverage": None,
    }

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        result["parse_error"] = f"SyntaxError: {exc.msg} (line {exc.lineno})"
        return result

    # Count type: ignore comments.
    for line in source.splitlines():
        if re.search(r"#\s*type:\s*ignore", line):
            result["type_ignore_count"] += 1

    # Analyze functions.
    def _analyze_func(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        class_name: str | None = None,
    ) -> dict:
        name = f"{class_name}.{node.name}" if class_name else node.name
        is_public = not node.name.startswith("_") or node.name in (
            "__init__", "__new__", "__call__", "__enter__", "__exit__",
            "__repr__", "__str__", "__eq__", "__hash__", "__len__",
            "__iter__", "__next__", "__getitem__", "__setitem__",
            "__contains__", "__bool__",
        )

        # Check parameter annotations.
        all_args = (
            node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        )
        # Exclude self/cls.
        params = [
            a for a in all_args
            if a.arg not in ("self", "cls")
        ]
        annotated_params = [a for a in params if a.annotation is not None]
        has_return = node.returns is not None

        # Check for Any in annotations.
        any_in_params = [
            {"param": a.arg, "annotation": _annotation_to_str(a.annotation)}
            for a in params
            if _contains_any(a.annotation)
        ]
        any_in_return = _contains_any(node.returns)

        total_params = len(params)
        annotated_count = len(annotated_params)
        fully_annotated = (
            annotated_count == total_params and has_return
        ) if total_params > 0 else has_return

        return {
            "name": name,
            "line": node.lineno,
            "is_public": is_public,
            "is_method": class_name is not None,
            "total_params": total_params,
            "annotated_params": annotated_count,
            "has_return_annotation": has_return,
            "return_annotation": _annotation_to_str(node.returns),
            "fully_annotated": fully_annotated,
            "any_in_params": any_in_params,
            "any_in_return": any_in_return,
        }

    # Analyze classes.
    def _analyze_class(node: ast.ClassDef) -> dict:
        container_type = _detect_container_type(node)
        is_public = not node.name.startswith("_")

        # Count annotated class attributes.
        annotated_attrs: list[dict] = []
        unannotated_attrs: list[str] = []
        for child in node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                annotated_attrs.append({
                    "name": child.target.id,
                    "line": child.lineno,
                    "annotation": _annotation_to_str(child.annotation),
                    "has_any": _contains_any(child.annotation),
                })
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        unannotated_attrs.append(target.id)

        # Analyze methods.
        methods = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(_analyze_func(child, node.name))

        info: dict = {
            "name": node.name,
            "line": node.lineno,
            "is_public": is_public,
            "container_type": container_type,
            "annotated_attributes": annotated_attrs,
            "unannotated_attributes": unannotated_attrs,
            "methods": methods,
        }

        if container_type == "dataclass":
            info["frozen"] = _check_frozen_dataclass(node)

        return info

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_info = _analyze_func(node)
            result["functions"].append(func_info)
            # Track Any usage.
            if func_info["any_in_params"] or func_info["any_in_return"]:
                result["any_usages"].append(_function_any_usage(rel, func_info))
        elif isinstance(node, ast.ClassDef):
            cls_info = _analyze_class(node)
            result["classes"].append(cls_info)
            # Track Any in class attributes.
            for attr in cls_info["annotated_attributes"]:
                if attr["has_any"]:
                    result["any_usages"].append({
                        "kind": "class_attribute",
                        "file": rel,
                        "line": attr["line"],
                        # `location` keeps the class line it always had, so
                        # diff_findings keys built on it stay stable.
                        "location": f"{rel}:{node.lineno}",
                        "class": cls_info["name"],
                        "attribute": attr["name"],
                        "annotation": attr["annotation"],
                    })
            # Track methods' Any usage.
            for method in cls_info["methods"]:
                if method["any_in_params"] or method["any_in_return"]:
                    result["any_usages"].append(_function_any_usage(rel, method))

    # Find untyped container usage (bare list, dict, set in annotations).
    _BARE_CONTAINER_RE = re.compile(
        r":\s*(?:list|dict|set|tuple)\s*(?:[=\n#,)]|$)", re.IGNORECASE
    )
    for i, line in enumerate(source.splitlines(), 1):
        # Only check lines that look like annotations (contain ':').
        if ":" in line and _BARE_CONTAINER_RE.search(line):
            result["untyped_containers"].append({
                "line": i,
                "content": line.strip(),
            })

    result["coverage"] = _file_coverage(_all_file_functions(result))
    return result


def _percent(part: int, whole: int) -> float | None:
    """`part/whole` as a percentage; None when there is nothing to measure."""
    return round(100 * part / whole, 1) if whole else None


def _file_coverage(funcs: list[dict]) -> dict:
    """Per-file annotation coverage over functions AND methods.

    `files[].functions` lists module-level functions only, while the summary
    counts methods too; coverage computed from `functions` alone is badly
    wrong for class-heavy modules (a module with 2 functions and 160
    methods reads as 2). A file with no functions reports null, not 0%.
    """
    public = [f for f in funcs if f["is_public"]]
    annotated = sum(1 for f in funcs if f["fully_annotated"])
    annotated_public = sum(1 for f in public if f["fully_annotated"])
    return {
        "total_functions": len(funcs),
        "public_functions": len(public),
        "fully_annotated_functions": annotated,
        "fully_annotated_public": annotated_public,
        "annotation_coverage_all": _percent(annotated, len(funcs)),
        "annotation_coverage_public": _percent(annotated_public, len(public)),
    }


def _all_file_functions(file_analysis: dict) -> list[dict]:
    """Module-level functions followed by class methods, in summary order."""
    funcs = list(file_analysis["functions"])
    for cls in file_analysis["classes"]:
        funcs.extend(cls["methods"])
    return funcs


def main() -> None:
    from scan_common import exit_on_help

    exit_on_help(sys.argv[1:], __doc__)
    max_files = 0  # 0 = no limit
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = Path(positional[0]) if positional else Path(".")
    target = target.resolve()

    project_root = find_project_root(target)
    scan_root = target if target.is_dir() else project_root
    all_files = sorted(discover_python_files(scan_root))
    files_total = len(all_files)
    if max_files > 0 and files_total > max_files:
        all_files = all_files[:max_files]

    file_analyses = [analyze_file(f, project_root) for f in all_files]

    # Aggregate statistics.
    all_functions: list[dict] = []
    all_classes: list[dict] = []
    all_any: list[dict] = []
    total_type_ignores = 0

    for fa in file_analyses:
        all_functions.extend(fa["functions"])
        all_classes.extend(fa["classes"])
        all_any.extend(fa["any_usages"])
        total_type_ignores += fa["type_ignore_count"]
        # Include methods from classes.
        for cls in fa["classes"]:
            all_functions.extend(cls["methods"])

    public_funcs = [f for f in all_functions if f["is_public"]]
    fully_annotated = [f for f in all_functions if f["fully_annotated"]]
    public_fully_annotated = [f for f in public_funcs if f["fully_annotated"]]

    # Container type inventory.
    container_types: dict[str, list[str]] = {}
    for cls in all_classes:
        ct = cls["container_type"]
        if ct:
            container_types.setdefault(ct, []).append(cls["name"])

    # Unannotated public functions. `file` is needed to tell apart same-named
    # functions in different modules (one `build` per plugin module).
    unannotated_public = [
        {"file": fa["file"], "name": f["name"], "line": f["line"]}
        for fa in file_analyses
        for f in _all_file_functions(fa)
        if f["is_public"] and not f["fully_annotated"]
    ]

    output = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_total": files_total,
        "files_analyzed": len(all_files),
        "files_capped": max_files > 0 and files_total > max_files,
        "summary": {
            "total_functions": len(all_functions),
            "public_functions": len(public_funcs),
            "fully_annotated_functions": len(fully_annotated),
            "fully_annotated_public": len(public_fully_annotated),
            "annotation_coverage_all": (
                round(100 * len(fully_annotated) / len(all_functions), 1)
                if all_functions else 0
            ),
            "annotation_coverage_public": (
                round(100 * len(public_fully_annotated) / len(public_funcs), 1)
                if public_funcs else 0
            ),
            "total_classes": len(all_classes),
            "total_any_usages": len(all_any),
            "total_type_ignores": total_type_ignores,
            "container_types": {k: len(v) for k, v in container_types.items()},
        },
        "container_inventory": {
            k: sorted(v) for k, v in container_types.items()
        },
        "any_usages": all_any,
        # Report the DENOMINATOR with the cap. A bare list of 50 reads as a
        # count of 50; on idlelib the true figure is 2079, and a reviewer who
        # quoted "50 unannotated public functions" would be off by 40x.
        "unannotated_public_functions_total": len(unannotated_public),
        "unannotated_public_functions_capped": len(unannotated_public) > 50,
        "unannotated_public_functions": unannotated_public[:50],
        "files": file_analyses,
    }

    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
