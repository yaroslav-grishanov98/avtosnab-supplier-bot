"""Split a large flat Python module into a package without changing behaviour.

Usage:
    python3 scripts/split_module.py scripts/split_configs/<name>.json [--dry-run]

The config names the module, the target submodules and regexes that pin public
top-level definitions (views, serializers, ...) to a submodule. Everything else
(private helpers, constants) follows its users: one user group -> that group,
several -> ``_shared``. Each submodule gets the original import block (unused
imports are pruned afterwards by ruff), the package ``__init__`` re-exports every
top-level name so existing ``from pkg.module import X`` keeps working, and
``mock.patch("pkg.module.NAME")`` strings in the given test paths are pointed at
the submodule that actually looks the name up.

Re-runnable: the same config applied to a newer version of the module produces
the same layout, so the split can be redone on fresh develop instead of rebased.
"""

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = "_shared"


def fail(msg):
    print(f"split_module: {msg}", file=sys.stderr)
    sys.exit(1)


def defined_names(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    names = set()
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def used_names(node):
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def is_import(node):
    return isinstance(node, (ast.Import, ast.ImportFrom))


def is_logger(node, src):
    return isinstance(node, ast.Assign) and "getLogger(__name__)" in ast.get_source_segment(src, node)


def imported_names(node):
    return {(alias.asname or alias.name).split(".")[0] for alias in node.names}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    if len(args) != 1:
        fail(__doc__)
    config = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    module_path = ROOT / config["module"]
    dotted = config["module"].removesuffix(".py").replace("/", ".")
    src = module_path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    body = list(tree.body)

    docstring = None
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        docstring = body.pop(0)

    header, logger, defs = [], None, []
    for node in body:
        if is_import(node):
            header.append(node)
        elif is_logger(node, src):
            logger = node
        else:
            defs.append(node)

    # Source text of each definition, including the comments right above it.
    def start_line(node):
        return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])

    ordered = sorted(([docstring] if docstring else []) + body, key=start_line)
    text_of = {}
    prev_end = 0
    for node in ordered:
        # Pull the comment lines directly above a definition along with it.
        chunk_start = start_line(node) - 1
        while chunk_start > prev_end and lines[chunk_start - 1].lstrip().startswith("#"):
            chunk_start -= 1
        text_of[id(node)] = "".join(lines[chunk_start : node.end_lineno])
        prev_end = node.end_lineno

    # A name defined twice keeps module semantics only if both copies stay in one
    # submodule, in their original order; that is checked once groups are known.
    top_names, duplicates = {}, {}
    for node in defs:
        for name in defined_names(node):
            if name in top_names:
                duplicates.setdefault(name, [top_names[name]]).append(node)
            top_names[name] = node

    rules = [(group, re.compile(pattern)) for group, patterns in config["groups"].items() for pattern in patterns]
    group_of = {}
    for node in defs:
        for name in defined_names(node):
            for group, rx in rules:
                # Private names are only pinned by rules written for them explicitly.
                if name.startswith("_") and not rx.pattern.startswith("_"):
                    continue
                if rx.fullmatch(name):
                    group_of[id(node)] = group
                    break
    pinned = {key for key in group_of}
    unmatched = []
    for node in defs:
        if id(node) in pinned:
            continue
        public = [n for n in defined_names(node) if not n.startswith("_") and not n.isupper()]
        if public and isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            unmatched.append(public[0])
    if unmatched:
        fail("public names match no group rule:\n  " + " ".join(unmatched))

    uses = {id(node): used_names(node) & set(top_names) for node in defs}
    users_of = {id(node): set() for node in defs}
    for node in defs:
        for name in uses[id(node)]:
            users_of[id(top_names[name])].add(id(node))

    # An unpinned helper serves every domain that reaches it through any chain of
    # helper calls. Grow those sets to a fixpoint (monotone, so it terminates);
    # a helper reaching exactly one domain lives there, anything else in _shared.
    reach = {id(node): ({group_of[id(node)]} if id(node) in pinned else set()) for node in defs}
    changed = True
    while changed:
        changed = False
        for node in defs:
            if id(node) in pinned:
                continue
            new = set().union(*(reach[user] for user in users_of[id(node)] if user != id(node)))
            if new != reach[id(node)]:
                reach[id(node)] = new
                changed = True
    for node in defs:
        if id(node) not in pinned:
            domains = reach[id(node)]
            group_of[id(node)] = next(iter(domains)) if len(domains) == 1 else SHARED
    for name, nodes in duplicates.items():
        if len({group_of[id(node)] for node in nodes}) != 1:
            fail(f"{name} is defined {len(nodes)} times and the copies land in different submodules")
        print(f"WARNING: {name} is defined {len(nodes)} times (lines {[n.lineno for n in nodes]}); kept together")

    module_names = {}
    for node in defs:
        module_names.setdefault(group_of[id(node)], []).append(node)

    deps = {group: set() for group in module_names}
    why = {}
    for node in defs:
        for name in uses[id(node)]:
            other = group_of[id(top_names[name])]
            if other != group_of[id(node)]:
                deps[group_of[id(node)]].add(other)
                user = next(iter(sorted(defined_names(node))), f"line {node.lineno}")
                why.setdefault((group_of[id(node)], other), set()).add(f"{user}->{name}")
    seen, stack = set(), []

    def visit(group):
        if group in stack:
            path = stack[stack.index(group) :] + [group]
            edges = "; ".join(f"{a}->{b} via {sorted(why[(a, b)])[:5]}" for a, b in zip(path, path[1:]))
            fail(f"import cycle between submodules: {edges}")
        if group in seen:
            return
        stack.append(group)
        for dep in deps[group]:
            visit(dep)
        stack.pop()
        seen.add(group)

    for group in deps:
        visit(group)

    header_text = "".join(text_of[id(node)] + "\n" for node in header)
    package_dir = module_path.with_suffix("")
    logger_text = ""
    if logger is not None:
        logger_text = "\n" + ast.get_source_segment(src, logger).replace("__name__", repr(dotted)) + "\n"

    files = {}
    for group, nodes in module_names.items():
        imports_needed = {}
        for node in nodes:
            for name in uses[id(node)]:
                other = group_of[id(top_names[name])]
                if other != group:
                    imports_needed.setdefault(other, set()).add(name)
        cross = "".join(
            f"from .{other} import {', '.join(sorted(names))}\n" for other, names in sorted(imports_needed.items())
        )
        doc = config.get("docstrings", {}).get(group, f"{dotted} — {group}.")
        parts = [f'"""{doc}"""\n\n', header_text, cross, logger_text, "\n\n"]
        parts.append("\n\n".join(text_of[id(node)].strip("\n") + "\n" for node in nodes))
        files[package_dir / f"{group}.py"] = "".join(parts)

    reexports = []
    for group in sorted(module_names):
        names = sorted({n for node in module_names[group] for n in defined_names(node)})
        reexports.append(f"from .{group} import (  # noqa: F401\n" + "".join(f"    {n},\n" for n in names) + ")\n")
    # Names other code imports from this module that it only imported itself
    # (e.g. a constant from another module) must stay importable from here.
    provided_by = {}
    for node in header:
        for alias in node.names:
            provided_by[alias.asname or alias.name] = (node, alias)
    external, external_lazy = set(), set()
    for path in ROOT.rglob("*.py"):
        if ".venv" in path.parts or path == module_path or path.is_relative_to(package_dir):
            continue
        try:
            other = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(other):
            if isinstance(node, ast.ImportFrom) and node.module == dotted:
                external.update(alias.name for alias in node.names)
        if path.name.startswith("test") or path.name == "tests.py":
            continue
        # Application code importing from the package inside a function looks the name up
        # on the package at call time, so patching the package still has to reach it.
        for func in ast.walk(other):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    if isinstance(node, ast.ImportFrom) and node.module == dotted:
                        external_lazy.update(alias.name for alias in node.names)
    passthrough = []
    for name in sorted(external - set(top_names)):
        if name not in provided_by:
            fail(f"{name} is imported from {dotted} but neither defined nor imported there")
        node, alias = provided_by[name]
        if isinstance(node, ast.ImportFrom):
            source = "." * node.level + (node.module or "")
            as_part = f" as {alias.asname}" if alias.asname else ""
            passthrough.append(f"from {source} import {alias.name}{as_part}  # noqa: F401\n")
        else:
            passthrough.append(f"import {alias.name}  # noqa: F401\n")
    reexports.extend(passthrough)

    init_doc = ast.get_source_segment(src, docstring) + "\n\n" if docstring else ""
    files[package_dir / "__init__.py"] = (
        init_doc
        + "# Split into submodules; names are re-exported so imports of this package keep working.\n"
        + "".join(reexports)
    )

    # mock.patch("pkg.module.NAME") must target the submodule that looks NAME up.
    lookups = {}
    for group, nodes in module_names.items():
        for node in nodes:
            for name in used_names(node):
                lookups.setdefault(name, set()).add(group)
    patch_rx = re.compile(re.escape(dotted) + r"\.([A-Za-z_][A-Za-z0-9_]*)")
    parent, _, leaf = dotted.rpartition(".")
    alias_rx = re.compile(
        rf"^\s*(?:from {re.escape(parent)} import {leaf}(?: as (\w+))?|import {re.escape(dotted)} as (\w+))\s*$", re.M
    )
    rewrites, manual, ambiguous = {}, set(), set()
    overrides = config.get("patch_targets", {})

    def target(name, path):
        """Submodule a patch of NAME must hit, or None to keep patching the package."""
        if name in overrides:
            return overrides[name] or None
        if name not in lookups:
            # Only looked up from outside (or not at all): the package attribute is what callers see.
            return None
        groups = lookups[name]
        if name in config.get("patch_manual", []):
            manual.add(f"{path.relative_to(ROOT)}: {name} -> {sorted(groups)}")
            return None
        if len(groups) != 1 or name in external_lazy:
            # Several submodules look it up, or other code also imports it from the package at
            # call time: one patch string cannot cover every lookup.
            where = sorted(groups) + (["<package>"] if name in external_lazy else [])
            ambiguous.add(f"{name}: {where} (e.g. {path.relative_to(ROOT)})")
            return None
        return next(iter(groups))

    for test_glob in config.get("tests", []):
        for path in sorted(ROOT.glob(test_glob)):
            if path.is_relative_to(package_dir) or path == module_path:
                continue
            text = path.read_text(encoding="utf-8")

            def repl(match):
                group = target(match.group(1), path)
                return match.group(0) if group is None else f"{dotted}.{group}.{match.group(1)}"

            new_text = patch_rx.sub(repl, text)
            # patch.object(<alias of the module>, "NAME") needs the same treatment.
            for found in alias_rx.finditer(new_text):
                alias = found.group(1) or found.group(2) or leaf
                obj_rx = re.compile(rf"patch\.object\(\s*{re.escape(alias)}\s*,\s*([\"'])(\w+)\1")

                def obj_repl(match, alias=alias):
                    group = target(match.group(2), path)
                    if group is None:
                        return match.group(0)
                    return match.group(0).replace(alias, f"{alias}.{group}", 1)

                new_text = obj_rx.sub(obj_repl, new_text)
            if new_text != text:
                rewrites[path] = new_text

    for group, nodes in sorted(module_names.items()):
        print(f"{group:28} {len(nodes):4} defs")
    print(f"patch rewrites in {len(rewrites)} file(s)")
    if ambiguous:
        fail("ambiguous patch targets, set patch_targets or patch_manual:\n  " + "\n  ".join(sorted(ambiguous)))
    for item in sorted(manual):
        print(f"MANUAL patch fix needed: {item}")
    if dry_run:
        return
    if package_dir.exists():
        leftovers = [p for p in package_dir.iterdir() if p.name != "__pycache__"]
        if leftovers:
            fail(f"{package_dir.relative_to(ROOT)} already exists and is not empty")
        shutil.rmtree(package_dir)
    package_dir.mkdir()
    for path, text in {**files, **rewrites}.items():
        path.write_text(text, encoding="utf-8")
    module_path.unlink()
    targets = [str(p) for p in files]
    subprocess.run(["ruff", "check", "--fix", "--select", "F401,I", "--quiet", *targets], check=False)
    subprocess.run(["ruff", "format", "--quiet", *targets, *map(str, rewrites)], check=True)
    leftover = subprocess.run(["ruff", "check", "--quiet", *targets], capture_output=True, text=True)
    if leftover.returncode:
        fail("ruff still reports problems:\n" + leftover.stdout)


if __name__ == "__main__":
    main()
