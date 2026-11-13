"""Rename a QuixBugs program's identifiers to neutral names (memorization check).

QuixBugs is public, so a model may "repair" a program by recalling the famous
version rather than by reasoning. Renaming hides the obvious cues:

* functions -> f_1, f_2, ... (the main function is always f_1)
* parameters, including lambda parameters -> a_1, a_2, ...
* other variables (locals, loop/comprehension targets, module-level assignments) -> v_1, ...
* comments and docstrings are removed (ast.unparse drops comments; string
  statements, including QuixBugs' trailing module "docstring", are deleted)

Builtins, imported names and attributes are never renamed. Renaming is
scope-aware: a name is renamed only where it refers to a binding made in this
file, so e.g. a parameter called `max` is renamed while a use of the builtin
`max()` in another function is not.

The tests import the original name, so the file actually tested gets one
alias line appended (see `alias_line`). The model never sees it.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

# Constructs this simple renamer does not handle. None occur in QuixBugs' 40 tested programs.
UNSUPPORTED = (
    ast.ClassDef, ast.AsyncFunctionDef, ast.Global, ast.Nonlocal, ast.NamedExpr,
    ast.Match, ast.AsyncFor, ast.AsyncWith,
)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_FRESH = re.compile(r"[fav]_\d+")
PRIORITY = {"function": 0, "param": 1, "variable": 2}
PREFIX = {"function": "f", "param": "a", "variable": "v"}


class RenameError(Exception):
    """The program uses something the renamer can't handle safely."""


@dataclass
class Renamed:
    source: str  # renamed program, without comments, docstrings or the alias
    mapping: dict[str, str]  # old name -> new name, for every renamed identifier
    main_old: str  # e.g. "bitcount"
    main_new: str = "f_1"
    program: str = ""  # QuixBugs program (file) name; normally == main_old
    _scrub_re: re.Pattern | None = field(default=None, repr=False)

    @property
    def alias_line(self) -> str:
        """Appended to the tested file so `from python_programs.X import X` still works.

        It also restores __name__, because most QuixBugs tests load their data
        with load_json_testcases(<function>.__name__).
        """
        o = self.main_old
        return f'{o} = {self.main_new}; {o}.__name__ = {o}.__qualname__ = "{o}"'

    def with_alias(self, code: str) -> str:
        """The file that is written to the workspace and tested."""
        return code.rstrip("\n") + "\n\n" + self.alias_line + "\n"

    def scrub(self, text: str) -> str:
        """Replace the original program/function name in test output with the new name.

        Matches the name wherever it isn't part of a longer alphanumeric word, so
        `test_bitcount`, `python_programs/bitcount.py` and `bitcount(...)` are all
        caught, while e.g. `list` is left alone when the program is `lis`.
        """
        if self._scrub_re is None:
            names = sorted({self.main_old, self.program} - {""}, key=len, reverse=True)
            alts = "|".join(re.escape(n) for n in names)
            self._scrub_re = re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])")
        return self._scrub_re.sub(self.main_new, text)


class _Scope:
    def __init__(self, parent: _Scope | None):
        self.parent = parent
        self.bindings: set[str] = set()
        self.imports: set[str] = set()

    def resolve(self, name: str) -> _Scope | None:
        """The scope whose binding `name` refers to, or None (builtin / import / unknown)."""
        scope = self
        while scope is not None:
            if name in scope.imports:
                return None
            if name in scope.bindings:
                return scope
            scope = scope.parent
        return None


class _Collector:
    """Pass 1: find every binding and every place a name is used, with its scope."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []  # (name, kind) in source order
        self.names: list[tuple[ast.Name, _Scope]] = []
        self.args: list[ast.arg] = []
        self.defs: list[tuple[ast.FunctionDef, _Scope]] = []
        self.handlers: list[ast.ExceptHandler] = []
        self.calls: list[tuple[ast.Call, _Scope]] = []
        self.identifiers: set[str] = set()  # every identifier-like string, to detect clashes

    def bind(self, scope: _Scope, name: str, kind: str) -> None:
        if name in scope.imports:
            raise RenameError(f"{name!r} is both imported and assigned")
        scope.bindings.add(name)
        self.events.append((name, kind))

    def visit(self, node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, UNSUPPORTED):
            raise RenameError(f"unsupported construct: {type(node).__name__}")

        if isinstance(node, ast.FunctionDef):
            self.bind(scope, node.name, "function")
            self.defs.append((node, scope))
            # Decorators, defaults and annotations are evaluated in the enclosing scope.
            for child in [*node.decorator_list, *node.args.defaults,
                          *(d for d in node.args.kw_defaults if d is not None)]:
                self.visit(child, scope)
            inner = _Scope(scope)
            self._bind_args(node.args, inner)
            for stmt in node.body:
                self.visit(stmt, inner)
            return

        if isinstance(node, ast.Lambda):
            for child in [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]:
                self.visit(child, scope)
            inner = _Scope(scope)
            self._bind_args(node.args, inner)
            self.visit(node.body, inner)
            return

        if isinstance(node, COMPREHENSIONS):
            # The first iterable is evaluated outside the comprehension's own scope.
            self.visit(node.generators[0].iter, scope)
            inner = _Scope(scope)
            for i, gen in enumerate(node.generators):
                if i:
                    self.visit(gen.iter, inner)
                self.visit(gen.target, inner)
                for cond in gen.ifs:
                    self.visit(cond, inner)
            parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            for part in parts:
                self.visit(part, inner)
            return

        if isinstance(node, ast.Name):
            self.identifiers.add(node.id)
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.bind(scope, node.id, "variable")
            self.names.append((node, scope))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name != "*":
                    name = a.asname or a.name.split(".")[0]
                    if name in scope.bindings:
                        raise RenameError(f"{name!r} is both imported and assigned")
                    scope.imports.add(name)
                    self.identifiers.add(name)
        elif isinstance(node, ast.Attribute):
            self.identifiers.add(node.attr)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self.bind(scope, node.name, "variable")
            self.handlers.append(node)
        elif isinstance(node, ast.Call):
            self.calls.append((node, scope))

        for child in ast.iter_child_nodes(node):
            self.visit(child, scope)

    def _bind_args(self, args: ast.arguments, scope: _Scope) -> None:
        every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        every += [a for a in (args.vararg, args.kwarg) if a is not None]
        for a in every:
            self.identifiers.add(a.arg)
            self.bind(scope, a.arg, "param")
            self.args.append(a)


def _strip_docstrings(tree: ast.AST) -> None:
    """Delete every statement that is just a string (docstrings and the like)."""
    for node in ast.walk(tree):
        for field_name in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field_name, None)
            if not isinstance(stmts, list) or not stmts or not isinstance(stmts[0], ast.stmt):
                continue
            kept = [s for s in stmts
                    if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                            and isinstance(s.value.value, str))]
            if not kept and field_name == "body":
                kept = [ast.Pass()]
            setattr(node, field_name, kept)


def _assign_names(events: list[tuple[str, str]], main: str) -> dict[str, str]:
    """One new name per old name. If a name is bound as several kinds, function > param > variable."""
    kind: dict[str, str] = {}
    for name, k in events:  # dicts keep first-appearance order
        if name not in kind or PRIORITY[k] < PRIORITY[kind[name]]:
            kind[name] = k
    mapping = {main: "f_1"}
    counters = {"function": 1, "param": 0, "variable": 0}
    for name, k in kind.items():
        if name == main:
            continue
        counters[k] += 1
        mapping[name] = f"{PREFIX[k]}_{counters[k]}"
    return mapping


def rename_program(source: str, main: str, program: str | None = None) -> Renamed:
    """Rename `source`, whose entry point is the function `main`. Raises RenameError."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise RenameError(f"does not parse: {e}") from e
    if not any(isinstance(n, ast.FunctionDef) and n.name == main for n in tree.body):
        raise RenameError(f"no top-level function named {main!r}")

    _strip_docstrings(tree)
    col = _Collector()
    col.visit(tree, _Scope(None))

    clashes = sorted(n for n in col.identifiers if _FRESH.fullmatch(n))
    if clashes:
        raise RenameError(f"already uses neutral-style names: {clashes}")

    mapping = _assign_names(col.events, main)
    params = {}  # function name -> its parameter names (for keyword arguments)
    for fn, _ in col.defs:
        if fn.name in params:
            raise RenameError(f"function {fn.name!r} is defined twice")
        a = fn.args
        params[fn.name] = {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]}

    # Keyword arguments in calls to this file's own functions name its parameters.
    # (Must run before the Name nodes below are renamed.)
    for call, scope in col.calls:
        f = call.func
        if isinstance(f, ast.Name) and f.id in params and scope.resolve(f.id) is not None:
            for kw in call.keywords:
                if kw.arg in params[f.id]:
                    kw.arg = mapping[kw.arg]

    for node, scope in col.names:
        if scope.resolve(node.id) is not None:
            node.id = mapping[node.id]
    for a in col.args:
        a.arg = mapping[a.arg]
    for fn, _ in col.defs:
        fn.name = mapping[fn.name]
    for h in col.handlers:
        h.name = mapping[h.name]

    new_source = ast.unparse(tree) + "\n"
    result = Renamed(new_source, mapping, main, program=program or main)
    if result.scrub(new_source) != new_source:  # e.g. the name inside a string literal
        raise RenameError(f"{main!r} still appears in the renamed source")
    compile(new_source, "<renamed>", "exec")
    return result
