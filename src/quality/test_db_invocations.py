"""Builder-invocation identity and disposition subsystem."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import posixpath
import re
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from quality.test_db_models import (
    BuilderIdentity,
    BuilderInvocation,
    Disposition,
    Evidence,
    InvocationConversion,
    ParityReceipt,
    SourceLocator,
    Taxonomy,
)

_OWNER_RE = re.compile(r"^[A-Z]{2,10}-[0-9]+$")
_RECEIPT_PREFIXES = (".tmp/", "tmp/", "evidence/", "parity/")
_MAX_REASON_LEN = 500
_LEAF_EVIDENCE: dict[str, Evidence] = {
    "downgrade": "call:downgrade",
    "upgrade": "call:upgrade",
    "stamp": "call:stamp",
    "create_all": "call:create_all",
    "executescript": "call:executescript",
    "migrated_db": "call:migrated_db",
}
_BUILDER_LEAVES = frozenset(
    {"upgrade", "downgrade", "stamp", "migrated_db", "executescript", "create_all"}
)
_IDENTITY_EVIDENCE: dict[str, Evidence] = {
    "alembic.command.upgrade": "call:upgrade",
    "alembic.command.stamp": "call:stamp",
    "alembic.command.downgrade": "call:downgrade",
    "migrated_db": "call:migrated_db",
    "connection.executescript": "call:executescript",
    "metadata.create_all": "call:create_all",
}
_IDENTITY_TAXONOMY: dict[str, Taxonomy] = {
    "alembic.command.upgrade": "custom-bootstrap",
    "alembic.command.stamp": "direct-historical",
    "alembic.command.downgrade": "direct-downgrade",
    "migrated_db": "cached-current-head",
    "connection.executescript": "custom-bootstrap",
    "metadata.create_all": "custom-bootstrap",
}


def _dotted_name(func: ast.expr) -> str | None:
    parts: list[str] = []
    current: ast.expr = func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        parts.reverse()
        return ".".join(parts)
    return None


def _import_maps(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    modules: dict[str, str] = {}
    direct: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "alembic.command":
                    if alias.asname is not None:
                        modules[alias.asname] = "alembic.command"
                    else:
                        modules["alembic"] = "alembic"
                elif alias.name == "alembic":
                    local = alias.asname if alias.asname is not None else "alembic"
                    modules[local] = "alembic"
        elif isinstance(node, ast.ImportFrom):
            if node.module == "alembic.command":
                for alias in node.names:
                    if alias.name in ("upgrade", "stamp", "downgrade"):
                        local = alias.asname if alias.asname is not None else alias.name
                        direct[local] = "alembic.command." + alias.name
            elif node.module == "alembic":
                for alias in node.names:
                    if alias.name == "command":
                        local = alias.asname if alias.asname is not None else "command"
                        modules[local] = "alembic.command"
    return modules, direct


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out: list[str] = []
        for elt in target.elts:
            out.extend(_target_names(elt))
        return out
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _binds_name(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.Assign):
        return any(name in _target_names(target) for target in node.targets)
    if isinstance(node, ast.AnnAssign):
        return name in _target_names(node.target)
    if isinstance(node, ast.AugAssign):
        return name in _target_names(node.target)
    if isinstance(node, ast.NamedExpr):
        return name in _target_names(node.target)
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return name in _target_names(node.target)
    if isinstance(node, ast.With):
        for item in node.items:
            if item.optional_vars is not None and name in _target_names(item.optional_vars):
                return True
        return False
    if isinstance(node, ast.ExceptHandler):
        return node.name == name
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.Import):
        for alias in node.names:
            local = alias.asname if alias.asname is not None else alias.name.split(".")[0]
            if local == name:
                return True
        return False
    if isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname if alias.asname is not None else alias.name
            if local == name:
                return True
        return False
    if isinstance(node, ast.comprehension):
        return name in _target_names(node.target)
    return False


def _scope_of(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    current: ast.AST | None = parents.get(id(node))
    while current is not None:
        if isinstance(
            current,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.Lambda,
                ast.ClassDef,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            return current
        current = parents.get(id(current))
    return None


def _func_arg_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    names: set[str] = set()
    names.update(a.arg for a in func.args.posonlyargs)
    names.update(a.arg for a in func.args.args)
    names.update(a.arg for a in func.args.kwonlyargs)
    if func.args.vararg is not None:
        names.add(func.args.vararg.arg)
    if func.args.kwarg is not None:
        names.add(func.args.kwarg.arg)
    return frozenset(names)


def _lambda_arg_names(func: ast.Lambda) -> frozenset[str]:
    names: set[str] = set()
    names.update(a.arg for a in func.args.posonlyargs)
    names.update(a.arg for a in func.args.args)
    names.update(a.arg for a in func.args.kwonlyargs)
    if func.args.vararg is not None:
        names.add(func.args.vararg.arg)
    if func.args.kwarg is not None:
        names.add(func.args.kwarg.arg)
    return frozenset(names)


def _comp_target_names(scope: ast.AST) -> frozenset[str]:
    names: set[str] = set()
    if isinstance(scope, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        for gen in scope.generators:
            names.update(_target_names(gen.target))
    return frozenset(names)


def _simple_aliases(
    tree: ast.AST, parents: dict[int, ast.AST]
) -> list[tuple[str, str, ast.AST | None, tuple[int, int]]]:
    out: list[tuple[str, str, ast.AST | None, tuple[int, int]]] = []
    for node in ast.walk(tree):
        target: str | None = None
        value: str | None = None
        position: tuple[int, int] | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                if isinstance(node.value, ast.Name):
                    target = node.targets[0].id
                    value = node.value.id
                elif isinstance(node.value, ast.Attribute):
                    dotted = _dotted_name(node.value)
                    if dotted is not None:
                        target = node.targets[0].id
                        value = dotted
                position = (node.lineno, node.col_offset)
                if target is None or value is None:
                    position = None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Name):
                target = node.target.id
                value = node.value.id
            elif isinstance(node.value, ast.Attribute):
                dotted = _dotted_name(node.value)
                if dotted is not None:
                    target = node.target.id
                    value = dotted
            position = (node.lineno, node.col_offset)
            if target is None or value is None:
                position = None
        if target is None or value is None or position is None:
            continue
        out.append((target, value, _scope_of(node, parents), position))
    return out


def _alias_root_leaf(
    name: str,
    edges: dict[str, set[str]],
    direct: dict[str, str],
) -> str | None:
    seen: set[str] = {name}
    queue: list[str] = [name]
    while queue:
        current = queue.pop(0)
        if current in direct:
            return direct[current].split(".")[-1]
        if current == "migrated_db":
            return "migrated_db"
        if current == "metadata.create_all":
            return "create_all"
        if "." in current and current.split(".")[-1] == "executescript":
            return "executescript"
        for nxt in edges.get(current, set()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return None


def _terminal_identity(
    terminal: str,
    call: ast.Call,
    func: ast.FunctionDef | ast.AsyncFunctionDef | None,
    tree: ast.AST,
    parents: dict[int, ast.AST],
) -> BuilderIdentity | None:
    if terminal == "migrated_db":
        if _arg_binding_valid("migrated_db", call, func, tree, parents):
            return "migrated_db"
        return None
    if terminal == "metadata.create_all":
        if _arg_binding_valid("metadata", call, func, tree, parents):
            return "metadata.create_all"
        return None
    if "." in terminal and terminal.split(".")[-1] == "executescript":
        recv = terminal.split(".")[0]
        if _arg_binding_valid(recv, call, func, tree, parents):
            return "connection.executescript"
        return None
    return None


def _visible_alias_values(
    name: str,
    call: ast.Call,
    func: ast.FunctionDef | ast.AsyncFunctionDef | None,
    call_scope: ast.AST | None,
    aliases: list[tuple[str, str, ast.AST | None, tuple[int, int]]],
) -> list[tuple[str, ast.AST | None, tuple[int, int]]]:
    call_pos = (call.lineno, call.col_offset)
    if func is not None:
        local = [(v, s, p) for (t, v, s, p) in aliases if t == name and s is func]
        if local:
            return [(v, s, p) for v, s, p in local if p < call_pos]
        func_pos = (func.lineno, func.col_offset)
        return [
            (v, s, p)
            for (t, v, s, p) in aliases
            if t == name and s is None and p < func_pos and p < call_pos
        ]
    if isinstance(call_scope, ast.ClassDef):
        same = [
            (v, s, p) for (t, v, s, p) in aliases if t == name and s is call_scope and p < call_pos
        ]
        mod = [(v, s, p) for (t, v, s, p) in aliases if t == name and s is None and p < call_pos]
        return same + mod
    return [(v, s, p) for (t, v, s, p) in aliases if t == name and s is None and p < call_pos]


def _has_other_binding(
    name: str,
    scope: ast.AST | None,
    exclude: tuple[int, int] | None,
    tree: ast.AST,
    parents: dict[int, ast.AST],
) -> bool:
    for node in ast.walk(tree):
        if not _binds_name(node, name):
            continue
        if _scope_of(node, parents) is not scope:
            continue
        pos: tuple[int, int] | None = None
        if isinstance(node, (ast.stmt, ast.expr, ast.excepthandler)):
            pos = (node.lineno, node.col_offset)
        if pos is None:
            return True
        if exclude is not None and pos == exclude:
            continue
        return True
    return False


def _alias_identity(
    name: str,
    call: ast.Call,
    tree: ast.AST,
    parents: dict[int, ast.AST],
    direct: dict[str, str],
    direct_pos: dict[str, tuple[int, int]],
    aliases: list[tuple[str, str, ast.AST | None, tuple[int, int]]],
) -> tuple[bool, BuilderIdentity | None, str]:
    edges: dict[str, set[str]] = {}
    for target, value, _, _ in aliases:
        edges.setdefault(target, set()).add(value)
    root_leaf = _alias_root_leaf(name, edges, direct)
    if root_leaf is None:
        return False, None, "upgrade"
    current: ast.AST | None = parents.get(id(call))
    func: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    scopes: list[ast.AST] = []
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if func is None:
                func = current
            scopes.append(current)
        elif isinstance(
            current,
            (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
        ):
            return True, None, root_leaf
        elif isinstance(current, ast.ClassDef):
            scopes.append(current)
        current = parents.get(id(current))
    call_scope = _scope_of(call, parents)
    if func is not None and name in _func_arg_names(func):
        return True, None, root_leaf
    if func is not None and _has_any_binding(name, func, tree, parents):
        found = [a for a in aliases if a[0] == name and a[2] is func]
        if len(found) != 1:
            return True, None, root_leaf
        vals = {a[1] for a in found}
        if len(vals) != 1:
            return True, None, root_leaf
        if _has_other_binding(name, func, found[0][3], tree, parents):
            return True, None, root_leaf
    seen_names: set[str] = set()
    current_name = name
    while current_name not in direct:
        if current_name in seen_names:
            return True, None, root_leaf
        seen_names.add(current_name)
        terminal: BuilderIdentity | None = None
        leaf: str | None = None
        if current_name == "migrated_db":
            leaf = "migrated_db"
            terminal = _terminal_identity(current_name, call, func, tree, parents)
        elif current_name == "metadata.create_all":
            leaf = "create_all"
            terminal = _terminal_identity(current_name, call, func, tree, parents)
        elif "." in current_name and current_name.split(".")[-1] == "executescript":
            leaf = "executescript"
            terminal = _terminal_identity(current_name, call, func, tree, parents)
        if leaf is not None:
            if terminal is not None:
                return True, terminal, leaf
            return True, None, leaf
        if func is not None and current_name != name and current_name in _func_arg_names(func):
            return True, None, root_leaf
        vals_scoped = _visible_alias_values(current_name, call, func, call_scope, aliases)
        if len(vals_scoped) != 1:
            return True, None, root_leaf
        nxt, use_scope, use_pos = vals_scoped[0]
        if func is not None:
            if _has_any_binding(current_name, func, tree, parents):
                others = [a for a in aliases if a[0] == current_name and a[2] is func]
                if len(others) != 1:
                    return True, None, root_leaf
                if _has_other_binding(current_name, func, others[0][3], tree, parents):
                    return True, None, root_leaf
        else:
            if _has_other_binding(current_name, use_scope, use_pos, tree, parents):
                return True, None, root_leaf
        current_name = nxt
    canonical_text = direct[current_name]
    canonical: BuilderIdentity | None = None
    if canonical_text == "alembic.command.upgrade":
        canonical = "alembic.command.upgrade"
    elif canonical_text == "alembic.command.stamp":
        canonical = "alembic.command.stamp"
    elif canonical_text == "alembic.command.downgrade":
        canonical = "alembic.command.downgrade"
    pos = direct_pos.get(current_name)
    if not _binding_valid_for_call(current_name, pos, call, func, tree, parents):
        return True, None, root_leaf
    return True, canonical, root_leaf


def _module_import_positions(
    tree: ast.AST, parents: dict[int, ast.AST]
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    mod_pos: dict[str, tuple[int, int]] = {}
    direct_pos: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if _scope_of(node, parents) is not None:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "alembic.command":
                    if alias.asname is not None:
                        mod_pos.setdefault(alias.asname, (node.lineno, node.col_offset))
                    else:
                        mod_pos.setdefault("alembic", (node.lineno, node.col_offset))
                elif alias.name == "alembic":
                    local = alias.asname if alias.asname is not None else "alembic"
                    mod_pos.setdefault(local, (node.lineno, node.col_offset))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "alembic.command":
                for alias in node.names:
                    if alias.name in ("upgrade", "stamp", "downgrade"):
                        local = alias.asname if alias.asname is not None else alias.name
                        direct_pos.setdefault(local, (node.lineno, node.col_offset))
            elif node.module == "alembic":
                for alias in node.names:
                    if alias.name == "command":
                        local = alias.asname if alias.asname is not None else "command"
                        mod_pos.setdefault(local, (node.lineno, node.col_offset))
    return mod_pos, direct_pos


def _has_rebinding(
    name: str,
    scope: ast.AST | None,
    after: tuple[int, int] | None,
    before: tuple[int, int],
    tree: ast.AST,
    parents: dict[int, ast.AST],
) -> bool:
    for node in ast.walk(tree):
        if not _binds_name(node, name):
            continue
        if _scope_of(node, parents) is not scope:
            continue
        pos: tuple[int, int] | None = None
        if isinstance(node, (ast.stmt, ast.expr, ast.excepthandler)):
            pos = (node.lineno, node.col_offset)
        if pos is None:
            return True
        if after is not None and pos <= after:
            continue
        if pos >= before:
            continue
        if after is not None and pos == after:
            continue
        return True
    return False


def _has_any_binding(
    name: str,
    scope: ast.AST | None,
    tree: ast.AST,
    parents: dict[int, ast.AST],
) -> bool:
    for node in ast.walk(tree):
        if not _binds_name(node, name):
            continue
        if _scope_of(node, parents) is not scope:
            continue
        return True
    return False


def _binding_valid_for_call(
    name: str,
    import_pos: tuple[int, int] | None,
    call: ast.Call,
    func: ast.FunctionDef | ast.AsyncFunctionDef | None,
    tree: ast.AST,
    parents: dict[int, ast.AST],
) -> bool:
    if import_pos is None:
        return False
    call_pos = (call.lineno, call.col_offset)
    if import_pos >= call_pos:
        return False
    current: ast.AST | None = parents.get(id(call))
    scopes: list[ast.AST] = []
    while current is not None:
        if isinstance(
            current,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.Lambda,
                ast.ClassDef,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            scopes.append(current)
        current = parents.get(id(current))
    seen_function_like = False
    for scope in scopes:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if name in _func_arg_names(scope):
                return False
            if _has_any_binding(name, scope, tree, parents):
                return False
            seen_function_like = True
        elif isinstance(scope, ast.Lambda):
            if name in _lambda_arg_names(scope):
                return False
            if _has_rebinding(name, scope, import_pos, call_pos, tree, parents):
                return False
            seen_function_like = True
        elif isinstance(scope, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            if name in _comp_target_names(scope):
                return False
            if _has_rebinding(name, scope, import_pos, call_pos, tree, parents):
                return False
            seen_function_like = True
        elif isinstance(scope, ast.ClassDef):
            if seen_function_like:
                continue
            if _has_rebinding(name, scope, import_pos, call_pos, tree, parents):
                return False
    return not _has_rebinding(name, None, import_pos, call_pos, tree, parents)


def _arg_binding_valid(
    name: str,
    call: ast.Call,
    func: ast.FunctionDef | ast.AsyncFunctionDef | None,
    tree: ast.AST,
    parents: dict[int, ast.AST],
) -> bool:
    if func is None:
        return False
    if name not in _func_arg_names(func):
        return False
    call_pos = (call.lineno, call.col_offset)
    return not _has_rebinding(name, func, None, call_pos, tree, parents)


def _is_canonical_issue(value: str) -> bool:
    if len(value) == 0 or len(value) > 32:
        return False
    return _OWNER_RE.fullmatch(value) is not None


def _is_canonical_receipt(value: str) -> bool:
    if len(value) == 0 or len(value) > 256:
        return False
    if value != value.strip():
        return False
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    if any(c.isspace() for c in value):
        return False
    if value.startswith("/") or posixpath.isabs(value):
        return False
    if not value.endswith(".json"):
        return False
    if not any(value.startswith(prefix) for prefix in _RECEIPT_PREFIXES):
        return False
    if ".." in PurePosixPath(value).parts:
        return False
    if posixpath.normpath(value) != value:
        return False
    if "//" in value or value.startswith("./"):
        return False
    for c in value:
        if c.isalnum() or c in "._-/":
            continue
        return False
    return True


def _is_canonical_reason(value: str) -> bool:
    if len(value) == 0 or len(value) > _MAX_REASON_LEN:
        return False
    if value != value.strip():
        return False
    return not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _collect_getattr_aliases(tree: ast.AST, modules: dict[str, str]) -> dict[str, str | None]:
    aliases: dict[str, str | None] = {}
    for node in ast.walk(tree):
        target: str | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
                value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
        ):
            target = node.target.id
            value = node.value
        if target is None or value is None:
            continue
        if isinstance(value, ast.Call):
            func = value.func
            is_getattr = (isinstance(func, ast.Name) and func.id == "getattr") or (
                isinstance(func, ast.Attribute) and func.attr == "getattr"
            )
            if is_getattr:
                leaf = _getattr_string_leaf(value)
                if leaf is not None and leaf in _BUILDER_LEAVES:
                    aliases[target] = leaf
                elif leaf is None and len(value.args) >= 1:
                    base = value.args[0]
                    if isinstance(base, ast.Name) and base.id in modules:
                        aliases[target] = None
                continue
        if isinstance(value, ast.Attribute) and value.attr in _BUILDER_LEAVES:
            base = value.value
            if isinstance(base, ast.Name) and base.id in modules:
                aliases[target] = value.attr
    return aliases


def _enclosing_arg_names(call: ast.AST, parents: dict[int, ast.AST]) -> frozenset[str]:
    current: ast.AST | None = parents.get(id(call))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names: set[str] = set()
            names.update(a.arg for a in current.args.posonlyargs)
            names.update(a.arg for a in current.args.args)
            names.update(a.arg for a in current.args.kwonlyargs)
            if current.args.vararg is not None:
                names.add(current.args.vararg.arg)
            if current.args.kwarg is not None:
                names.add(current.args.kwarg.arg)
            return frozenset(names)
        current = parents.get(id(current))
    return frozenset()


def _resolve_identity(
    dotted: str,
    modules: dict[str, str],
    direct: dict[str, str],
    arg_names: frozenset[str],
) -> BuilderIdentity | None:
    if dotted in direct:
        value = direct[dotted]
        if value == "alembic.command.upgrade":
            return "alembic.command.upgrade"
        if value == "alembic.command.stamp":
            return "alembic.command.stamp"
        if value == "alembic.command.downgrade":
            return "alembic.command.downgrade"
        return None
    parts = dotted.split(".")
    leaf = parts[-1]
    if leaf in ("upgrade", "stamp", "downgrade"):
        if len(parts) == 2 and modules.get(parts[0]) == "alembic.command":
            if leaf == "upgrade":
                return "alembic.command.upgrade"
            if leaf == "stamp":
                return "alembic.command.stamp"
            return "alembic.command.downgrade"
        if len(parts) == 3 and parts[0] == "alembic" and parts[1] == "command":
            if parts[0] in modules or modules.get(parts[0]) == "alembic":
                if leaf == "upgrade":
                    return "alembic.command.upgrade"
                if leaf == "stamp":
                    return "alembic.command.stamp"
                return "alembic.command.downgrade"
            return None
        return None
    if leaf == "migrated_db":
        if dotted == "migrated_db" and "migrated_db" in arg_names:
            return "migrated_db"
        return None
    if leaf == "executescript":
        if dotted == "connection.executescript":
            if "connection" in arg_names:
                return "connection.executescript"
            return None
        if len(parts) == 2 and parts[0] in arg_names:
            return "connection.executescript"
        return None
    if leaf == "create_all":
        if dotted == "metadata.create_all":
            if "metadata" in arg_names:
                return "metadata.create_all"
            return None
        return None
    return None


def _invocation_id(
    path: str, file_sha: str, locator: SourceLocator, observed: str, canonical: str | None
) -> str:
    basis = (
        path
        + "\x00"
        + file_sha
        + "\x00"
        + str(locator.start_line)
        + ":"
        + str(locator.start_col)
        + ":"
        + str(locator.end_line)
        + ":"
        + str(locator.end_col)
        + "\x00"
        + observed
        + "\x00"
        + (canonical if canonical is not None else "")
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate-key")
        seen.add(key)
        out[key] = value
    return out


def _secure_parity_ok(
    repo_root: Path | None,
    receipt_ref: str,
    invocation: BuilderInvocation,
    match: InvocationConversion,
) -> bool:
    try:
        if not _is_canonical_receipt(receipt_ref):
            return False
        if repo_root is None:
            return False
        repo = Path(repo_root)
        try:
            repo_resolved = repo.resolve()
        except OSError:
            return False
        candidate = repo_resolved / PurePosixPath(receipt_ref).as_posix()
        try:
            before = os.lstat(candidate)
        except OSError:
            return False
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return False
        if before.st_nlink != 1:
            return False
        try:
            resolved = Path(os.path.realpath(candidate))
            resolved.relative_to(repo_resolved)
        except (OSError, ValueError):
            return False
        try:
            after_lstat = os.lstat(resolved)
        except OSError:
            return False
        if stat.S_ISLNK(after_lstat.st_mode) or not stat.S_ISREG(after_lstat.st_mode):
            return False
        if (after_lstat.st_dev, after_lstat.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            return False
        if after_lstat.st_nlink != 1:
            return False
        nofollow: int = getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        data = b""
        close_failed = False
        try:
            fd = os.open(resolved, os.O_RDONLY | nofollow)
            st_open = os.fstat(fd)
            if stat.S_ISLNK(st_open.st_mode) or not stat.S_ISREG(st_open.st_mode):
                return False
            if st_open.st_nlink != 1:
                return False
            if (st_open.st_dev, st_open.st_ino) != (before.st_dev, before.st_ino):
                return False
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            st_after = os.fstat(fd)
            if (st_after.st_dev, st_after.st_ino) != (before.st_dev, before.st_ino):
                return False
            data = b"".join(chunks)
        except OSError:
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    close_failed = True
        if close_failed:
            return False
        try:
            text = data.decode("utf-8")
            raw = json.loads(text, object_pairs_hook=_reject_duplicates)
        except (UnicodeDecodeError, ValueError):
            return False
        receipt = ParityReceipt.model_validate(raw)
        if receipt.status != "PASS":
            return False
        return (
            receipt.invocation_id == invocation.invocation_id == match.invocation_id
            and receipt.path == invocation.path == match.path
            and receipt.locator == invocation.locator == match.locator
            and receipt.source_sha256 == invocation.source_sha256 == match.source_sha256
        )
    except Exception:
        return False


def _enclosing_factory(call: ast.AST, parents: dict[int, ast.AST]) -> str | None:
    current: ast.AST | None = parents.get(id(call))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(id(current))
    return None


def _getattr_string_leaf(call: ast.Call) -> str | None:
    if len(call.args) >= 2:
        second = call.args[1]
        if (
            isinstance(second, ast.Constant)
            and isinstance(second.value, str)
            and second.value in _BUILDER_LEAVES
        ):
            return second.value
    for keyword in call.keywords:
        value = keyword.value
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value in _BUILDER_LEAVES
        ):
            return value.value
    return None


def _narrowed_for(canonical: BuilderIdentity | None, leaf: str) -> tuple[Evidence, Taxonomy]:
    if canonical is not None:
        evidence: Evidence = _IDENTITY_EVIDENCE[canonical]
        taxonomy: Taxonomy = _IDENTITY_TAXONOMY[canonical]
        return evidence, taxonomy
    fallback = _LEAF_EVIDENCE.get(leaf)
    if fallback is not None:
        return fallback, "unclassified"
    return "call:upgrade", "unclassified"


def collect_invocations(path: str, tree: ast.AST, file_sha: str) -> list[BuilderInvocation]:
    modules, direct = _import_maps(tree)
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    mod_pos, direct_pos = _module_import_positions(tree, parents)
    getattr_aliases = _collect_getattr_aliases(tree, modules)
    aliases_cache = _simple_aliases(tree, parents)
    found: list[BuilderInvocation] = []

    def emit(
        node: ast.Call,
        observed: str,
        canonical: BuilderIdentity | None,
        leaf_for_evidence: str,
    ) -> None:
        end_line = node.end_lineno if node.end_lineno is not None else node.lineno
        end_col = node.end_col_offset if node.end_col_offset is not None else node.col_offset
        locator = SourceLocator(
            start_line=node.lineno,
            start_col=node.col_offset,
            end_line=end_line,
            end_col=end_col,
        )
        evidence, taxonomy = _narrowed_for(canonical, leaf_for_evidence)
        canonical_text: str | None = canonical
        invocation = _invocation_id(path, file_sha, locator, observed, canonical_text)
        factory = _enclosing_factory(node, parents)
        disposition: Disposition = "RETAIN" if canonical is not None else "HOLD"
        found.append(
            BuilderInvocation(
                path=path,
                locator=locator,
                source_sha256=file_sha,
                invocation_id=invocation,
                observed_call=observed,
                canonical_identity=canonical,
                factory=factory,
                evidence=evidence,
                taxonomy=taxonomy,
                disposition=disposition,
            )
        )

    def validated(
        dotted: str, canonical: BuilderIdentity | None, call: ast.Call
    ) -> BuilderIdentity | None:
        if canonical is None:
            return None
        current: ast.AST | None = parents.get(id(call))
        func: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = current
                break
            current = parents.get(id(current))
        if dotted in direct:
            pos = direct_pos.get(dotted)
            if not _binding_valid_for_call(dotted, pos, call, func, tree, parents):
                return None
            return canonical
        parts = dotted.split(".")
        leaf = parts[-1]
        if leaf in ("upgrade", "stamp", "downgrade"):
            root = parts[0]
            pos = mod_pos.get(root)
            if not _binding_valid_for_call(root, pos, call, func, tree, parents):
                return None
            return canonical
        if leaf == "migrated_db":
            if not _arg_binding_valid("migrated_db", call, func, tree, parents):
                return None
            return canonical
        if leaf == "executescript":
            recv = parts[0] if len(parts) == 2 else "connection"
            if not _arg_binding_valid(recv, call, func, tree, parents):
                return None
            return canonical
        if leaf == "create_all":
            if not _arg_binding_valid("metadata", call, func, tree, parents):
                return None
            return canonical
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Call):
            inner = node.func
            if isinstance(inner.func, ast.Name) and inner.func.id == "getattr":
                leaf = _getattr_string_leaf(inner)
                if leaf is not None and leaf in _BUILDER_LEAVES:
                    emit(node, "getattr(...)", None, leaf)
                elif leaf is None and len(inner.args) >= 1:
                    base = inner.args[0]
                    if isinstance(base, ast.Name) and base.id in modules:
                        emit(node, "getattr(...)", None, "upgrade")
            continue
        dotted = _dotted_name(node.func)
        if dotted is None:
            func_expr = node.func
            if isinstance(func_expr, ast.Attribute) and func_expr.attr in _BUILDER_LEAVES:
                base_call = func_expr.value
                if isinstance(base_call, ast.Call):
                    base_func = base_call.func
                    base_dotted: str | None = None
                    if isinstance(base_func, ast.Name):
                        base_dotted = base_func.id
                    elif isinstance(base_func, ast.Attribute):
                        base_dotted = _dotted_name(base_func)
                    if base_dotted in ("__import__", "import_module") or (
                        base_dotted is not None
                        and base_dotted.endswith(("__import__", "import_module"))
                    ):
                        emit(node, base_dotted + "(...)." + func_expr.attr, None, func_expr.attr)
                    else:
                        emit(
                            node,
                            "<unresolved>." + func_expr.attr,
                            None,
                            func_expr.attr,
                        )
                else:
                    emit(node, "<unresolved>." + func_expr.attr, None, func_expr.attr)
            continue
        if dotted == "getattr" or dotted.endswith(".getattr"):
            parent = parents.get(id(node))
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            leaf = _getattr_string_leaf(node)
            if leaf is not None:
                emit(node, "getattr", None, leaf)
            continue
        if dotted in ("__import__", "import_module") or dotted.endswith(
            (".__import__", ".import_module")
        ):
            continue
        if "." not in dotted and dotted in getattr_aliases:
            alias_leaf = getattr_aliases[dotted]
            emit(node, dotted, None, alias_leaf if alias_leaf is not None else "upgrade")
            continue
        if dotted in direct:
            arg_names = _enclosing_arg_names(node, parents)
            canonical = _resolve_identity(dotted, modules, direct, arg_names)
            checked = validated(dotted, canonical, node)
            target_leaf = direct[dotted].split(".")[-1]
            emit(node, dotted, checked, target_leaf)
            continue
        if "." not in dotted and dotted not in _BUILDER_LEAVES:
            rel, ident, leaf = _alias_identity(
                dotted, node, tree, parents, direct, direct_pos, aliases_cache
            )
            if not rel:
                continue
            if ident is None:
                emit(node, dotted, None, leaf)
            else:
                emit(node, dotted, ident, leaf)
            continue
        leaf_name = dotted.split(".")[-1]
        if leaf_name not in _BUILDER_LEAVES:
            continue
        canonical = _resolve_identity(dotted, modules, direct, _enclosing_arg_names(node, parents))
        checked = validated(dotted, canonical, node)
        emit(node, dotted, checked, leaf_name)
    return found


def apply_conversions(
    invocations: list[BuilderInvocation],
    conversions: tuple[InvocationConversion, ...],
    repo_root: Path | None = None,
) -> list[BuilderInvocation]:
    if not conversions:
        return invocations
    counts: dict[str, int] = {}
    for item in conversions:
        counts[item.invocation_id] = counts.get(item.invocation_id, 0) + 1
    duplicate_ids = {key for key, value in counts.items() if value > 1}
    by_id: dict[str, InvocationConversion] = {}
    for item in conversions:
        if item.invocation_id not in by_id:
            by_id[item.invocation_id] = item
    invocation_ids = {inv.invocation_id for inv in invocations}
    force_hold_all = bool(set(by_id) - invocation_ids)
    applied: list[BuilderInvocation] = []
    for invocation in invocations:
        if invocation.canonical_identity is None:
            applied.append(invocation)
            continue
        if force_hold_all:
            applied.append(invocation.model_copy(update={"disposition": "HOLD"}))
            continue
        if invocation.invocation_id in duplicate_ids:
            applied.append(invocation.model_copy(update={"disposition": "HOLD"}))
            continue
        match = by_id.get(invocation.invocation_id)
        if match is None:
            applied.append(invocation)
            continue
        if (
            match.path != invocation.path
            or match.source_sha256 != invocation.source_sha256
            or match.locator != invocation.locator
        ):
            applied.append(invocation.model_copy(update={"disposition": "HOLD"}))
            continue
        if not _is_canonical_receipt(match.parity_receipt):
            applied.append(invocation.model_copy(update={"disposition": "HOLD"}))
            continue
        if not _is_canonical_issue(match.owner_issue):
            applied.append(invocation.model_copy(update={"disposition": "HOLD"}))
            continue
        if not _is_canonical_reason(match.reason):
            applied.append(invocation.model_copy(update={"disposition": "HOLD"}))
            continue
        expires = match.expires_at
        if expires.tzinfo is None or expires.utcoffset() is None:
            applied.append(invocation.model_copy(update={"disposition": "HOLD"}))
            continue
        now = datetime.now(UTC)
        if expires <= now:
            applied.append(invocation.model_copy(update={"disposition": "HOLD"}))
            continue
        if not _secure_parity_ok(repo_root, match.parity_receipt, invocation, match):
            applied.append(invocation.model_copy(update={"disposition": "HOLD"}))
            continue
        applied.append(invocation.model_copy(update={"disposition": "CONVERT"}))
    return applied


def normalize_conversions(
    raw: tuple[object, ...] | list[object] | None,
) -> tuple[tuple[InvocationConversion, ...], bool]:
    if raw is None:
        return tuple(), False
    items: list[InvocationConversion] = []
    malformed = False
    for entry in raw:
        if isinstance(entry, InvocationConversion):
            items.append(entry)
        elif isinstance(entry, dict):
            entry_dict = cast(dict[object, object], entry)
            try:
                candidate = dict(entry_dict)
                raw_expires = candidate.get("expires_at")
                if isinstance(raw_expires, str):
                    text = raw_expires.strip()
                    if text.endswith("Z"):
                        text = text[:-1] + "+00:00"
                    parsed = datetime.fromisoformat(text)
                    if parsed.tzinfo is None:
                        raise ValueError("invalid-expires")
                    candidate["expires_at"] = parsed
                items.append(InvocationConversion.model_validate(candidate))
            except Exception:
                malformed = True
        else:
            malformed = True
    return tuple(items), malformed
