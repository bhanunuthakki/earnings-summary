"""Which code a Python source change can alter when it runs.

A changed line inside a function, including its signature and a pure
decorator, alters only calls of that function. A changed module-level
assignment or import alters whatever reads the names it binds. A changed
class-level statement alters the class. A module-level statement that binds
nothing (a bare call, a loop, a registration decorator) can alter every
importer, so it marks the whole module. Comments, blank lines and module or
class docstrings alter nothing.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# Decorators that only wrap, annotate or register nothing outside the object.
PURE_DECORATORS = frozenset(
    {
        "abstractmethod",
        "asynccontextmanager",
        "cache",
        "cached_property",
        "classmethod",
        "contextmanager",
        "dataclass",
        "deleter",
        "final",
        "getter",
        "lru_cache",
        "overload",
        "override",
        "property",
        "runtime_checkable",
        "setter",
        "staticmethod",
        "total_ordering",
        "unique",
        "wraps",
    }
)
# A cached function's body does not run again for a later caller, so its
# callers' callers depend on it without the recorder seeing a call.
CACHING_DECORATORS = frozenset({"cache", "lru_cache", "cached_property"})
_NOT_CODE = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
    }
)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
# A ``type X = ...`` statement is not listed: it counts as a whole-module change.
_BINDINGS = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.AugAssign)


@dataclass
class Regions:
    """What a set of changed lines can alter."""

    functions: set[str] = field(default_factory=set[str])
    classes: set[str] = field(default_factory=set[str])
    names: set[str] = field(default_factory=set[str])
    cached: set[str] = field(default_factory=set[str])
    module: bool = False

    def update(self, other: Regions) -> None:
        self.functions |= other.functions
        self.classes |= other.classes
        self.names |= other.names
        self.cached |= other.cached
        self.module = self.module or other.module


def code_lines(source: str) -> set[int]:
    """Lines that hold code, as opposed to comments and blank lines."""
    lines: set[int] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type not in _NOT_CODE:
                lines.update(range(token.start[0], token.end[0] + 1))
    except (tokenize.TokenError, SyntaxError):
        return set(range(1, source.count("\n") + 2))
    return lines


def code_tokens(source: str) -> dict[int, list[str]]:
    """Code token text by the line it starts on; comments and layout are dropped."""
    found: dict[int, list[str]] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type not in _NOT_CODE:
                found.setdefault(token.start[0], []).append(token.string)
    except (tokenize.TokenError, SyntaxError):
        return {}
    return found


def same_code(
    before: Mapping[int, list[str]], after: Mapping[int, list[str]], old: range, new: range
) -> bool:
    """True when a hunk only moves, reflows or comments the same code tokens."""
    old_tokens = [item for line in old for item in before.get(line, [])]
    new_tokens = [item for line in new for item in after.get(line, [])]
    return bool(before) and bool(after) and old_tokens == new_tokens


def _start(node: ast.stmt) -> int:
    decorators: list[ast.expr] = getattr(node, "decorator_list", [])
    return min([node.lineno, *(item.lineno for item in decorators)])


def _span(node: ast.AST) -> range:
    start = _start(node) if isinstance(node, ast.stmt) else getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", None) or start
    return range(start, end + 1)


def decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def bound_names(statement: ast.stmt) -> set[str] | None:
    """Names a binding statement binds, or None when it also mutates something."""
    if isinstance(statement, ast.Import):
        return {(alias.asname or alias.name).split(".")[0] for alias in statement.names}
    if isinstance(statement, ast.ImportFrom):
        if any(alias.name == "*" for alias in statement.names):
            return None
        return {alias.asname or alias.name for alias in statement.names}
    targets: list[ast.expr]
    if isinstance(statement, ast.Assign):
        targets = list(statement.targets)
    elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        targets = [statement.target]
    else:
        return None
    names: set[str] = set()
    pending = list(targets)
    while pending:
        target = pending.pop()
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        else:
            return None
    return names


def _children(statement: ast.stmt) -> list[list[ast.stmt]]:
    if isinstance(statement, ast.If):
        return [statement.body, statement.orelse]
    if isinstance(statement, (ast.Try, ast.TryStar)):
        handlers = [handler.body for handler in statement.handlers]
        return [statement.body, *handlers, statement.orelse, statement.finalbody]
    if isinstance(statement, ast.With):
        return [statement.body]
    return []


class _Walker:
    def __init__(self, lines: set[int]) -> None:
        self.lines = lines
        self.out = Regions()

    def hit(self, node: ast.AST) -> set[int]:
        return self.lines.intersection(_span(node))

    def walk(self, body: Sequence[ast.stmt], prefix: str, owner: str | None) -> None:
        for index, statement in enumerate(body):
            hit = self.hit(statement)
            if not hit or (index == 0 and _is_docstring(statement)):
                continue
            if isinstance(statement, _FUNCTIONS):
                self.function(statement, prefix, hit)
            elif isinstance(statement, ast.ClassDef):
                self.klass(statement, prefix, hit)
            elif owner is not None:
                self.out.classes.add(owner)
            else:
                self.module_statement(statement, prefix, hit)

    def decorators(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
        """True when a changed decorator may act outside the decorated object."""
        changed = [item for item in node.decorator_list if self.hit(item)]
        return any(decorator_name(item) not in PURE_DECORATORS for item in changed)

    def function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str, hit: set[int]
    ) -> None:
        qualname = prefix + node.name
        if self.decorators(node):
            self.out.module = True
        if {decorator_name(item) for item in node.decorator_list} & CACHING_DECORATORS:
            self.out.cached.add(qualname)
        if hit:
            self.out.functions.add(qualname)

    def klass(self, node: ast.ClassDef, prefix: str, hit: set[int]) -> None:
        qualname = prefix + node.name
        body_start = _start(node.body[0])
        if any(line < body_start for line in hit):
            if self.decorators(node):
                self.out.module = True
            self.out.classes.add(qualname)
        self.walk(node.body, qualname + ".", qualname)

    def module_statement(self, statement: ast.stmt, prefix: str, hit: set[int]) -> None:
        children = _children(statement)
        if children:
            inner = {line for body in children for item in body for line in _span(item)}
            if hit - inner:
                self.out.module = True
            for body in children:
                self.walk(body, prefix, None)
            return
        names = bound_names(statement) if isinstance(statement, _BINDINGS) else None
        if names is None:
            self.out.module = True
        else:
            self.out.names |= names


def analyze(tree: ast.Module, lines: Iterable[int]) -> Regions:
    """Regions altered by changes on ``lines`` of the module ``tree``."""
    walker = _Walker(set(lines))
    walker.walk(tree.body, "", None)
    return walker.out


def definitions(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Qualified names of classes and functions, and the names bound at module scope."""
    qualnames: set[str] = set()
    module_names: set[str] = set()

    def visit(body: Sequence[ast.stmt], prefix: str, top: bool) -> None:
        for statement in body:
            if isinstance(statement, (*_FUNCTIONS, ast.ClassDef)):
                qualnames.add(prefix + statement.name)
                if top:
                    module_names.add(statement.name)
                if isinstance(statement, ast.ClassDef):
                    visit(statement.body, f"{prefix}{statement.name}.", False)
                continue
            if top:
                module_names.update(bound_names(statement) or set())
                for child in _children(statement):
                    visit(child, prefix, True)

    visit(tree.body, "", True)
    return qualnames, module_names


def changed_regions(
    base: ast.Module, head: ast.Module, base_lines: Iterable[int], head_lines: Iterable[int]
) -> Regions:
    """Regions a diff alters, judged on the base and head sides of each hunk.

    A function or class that exists only on the head side is new: no existing
    test runs it, so it alters existing behavior only where it overrides an
    inherited method (it then alters its class) or rebinds a module name.
    """
    before = analyze(base, base_lines)
    after = analyze(head, head_lines)
    qualnames, module_names = definitions(base)
    result = Regions(module=before.module or after.module)
    result.update(before)
    result.names |= after.names
    for qualname in after.functions | after.classes | after.cached:
        if qualname in qualnames:
            if qualname in after.classes:
                result.classes.add(qualname)
            if qualname in after.functions:
                result.functions.add(qualname)
            if qualname in after.cached:
                result.cached.add(qualname)
            continue
        parent, _, name = qualname.rpartition(".")
        if parent and parent in qualnames:
            result.classes.add(parent)
        elif not parent and name in module_names:
            result.names.add(name)
    return result
