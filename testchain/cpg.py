"""A lightweight code property graph (CPG) for change-impact test selection.

A code property graph (Yamaguchi et al., 2014) merges a program's syntax tree with
control-flow and data-dependence information into one graph that can be queried.
Tools such as Joern build a full one. This module builds a deliberately small
version with Python's standard `ast` module, enough to answer one question:
"which tests can reach the code that a change touched?"

Nodes: one per region of source code, so every line belongs to exactly one
innermost node.
  module    import-time code of a module
  script    the `if __name__ == "__main__":` block (runs only when started as a process)
  var       a module-level assignment (constants, dispatch tables)
  class     a class body (field defaults, decorators), excluding its methods
  function  a function or method body
  route     one branch of the HTTP dispatcher (only with a knowledge base, see kb.py)

Edges: "executing the source node may execute, or depend on, the target node".
  call      a call that resolves to a project function, method or class
  ref       a function or class used as a value (for example stored in a table)
  read      a read of a module-level name (data dependence on a constant)
  import    a module imports another project module
  define    a function defines a class (its body runs when the function runs)
  fixture   unittest runs setUp/setUpClass/tearDown around every test of its class
  kb:*      edges that only the architecture knowledge base can supply (see kb.py)

Compared with a full CPG, this one has no control flow or data flow inside a
function, and a call on an object of unknown type is resolved by name to every
project method with that name. Both make it over-approximate: it tends to select
too many tests rather than too few. Its real blind spots are measured in the report.
"""

import ast
from collections import deque
from pathlib import Path

UNITTEST_FIXTURES = ("setUp", "tearDown", "setUpClass", "tearDownClass", "asyncSetUp", "asyncTearDown")
# Python calls these methods implicitly (the data model), so `len(x)` is a call to x.__len__.
BUILTIN_PROTOCOLS = {"len": ["__len__"], "iter": ["__iter__"], "next": ["__next__"], "str": ["__str__"],
                     "repr": ["__repr__"], "bool": ["__bool__", "__len__"], "hash": ["__hash__"],
                     "abs": ["__abs__"], "list": ["__iter__"], "tuple": ["__iter__"], "set": ["__iter__"],
                     "sorted": ["__iter__"], "reversed": ["__reversed__"], "dict": ["__iter__"]}
COMPARE_PROTOCOLS = {ast.Eq: "__eq__", ast.NotEq: "__ne__", ast.Lt: "__lt__", ast.LtE: "__le__",
                     ast.Gt: "__gt__", ast.GtE: "__ge__"}
CONTAINER_NODES = (ast.Dict, ast.List, ast.Set, ast.Tuple, ast.Constant, ast.JoinedStr,
                   ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.Lambda,
                   ast.BinOp, ast.Compare, ast.UnaryOp)


class Node:
    __slots__ = ("id", "kind", "module", "file", "start", "end")

    def __init__(self, node_id, kind, module, file, start, end):
        self.id, self.kind, self.module, self.file = node_id, kind, module, file
        self.start, self.end = start, end

    def __repr__(self):
        return f"<{self.kind} {self.id} {self.file}:{self.start}-{self.end}>"


class Graph:
    def __init__(self):
        self.nodes = {}
        self.edges = {}          # source id -> {target id: set of edge kinds}
        self._by_file = {}

    def add_node(self, node):
        self.nodes[node.id] = node
        self._by_file.setdefault(node.file, []).append(node)
        return node.id

    def add_edge(self, src, dst, kind):
        if src and dst and src != dst and dst in self.nodes:
            self.edges.setdefault(src, {}).setdefault(dst, set()).add(kind)

    def locate(self, file, line):
        """The innermost node whose region contains `line` of `file`."""
        best = None
        for n in self._by_file.get(file, ()):
            if n.start <= line <= n.end and (best is None or n.end - n.start < best.end - best.start):
                best = n
        return best.id if best else None

    def reachable(self, starts):
        seen, todo = set(starts), deque(starts)
        while todo:
            for nxt in self.edges.get(todo.popleft(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    def stats(self):
        kinds, edge_kinds = {}, {}
        for n in self.nodes.values():
            kinds[n.kind] = kinds.get(n.kind, 0) + 1
        for targets in self.edges.values():
            for ks in targets.values():
                for k in ks:
                    edge_kinds[k] = edge_kinds.get(k, 0) + 1
        return {"nodes": len(self.nodes), "node_kinds": kinds,
                "edges": sum(len(t) for t in self.edges.values()), "edge_kinds": edge_kinds}


class Types:
    """What an expression may evaluate to: project classes, plus whether it may be anything."""
    __slots__ = ("classes", "unknown")

    def __init__(self, classes=(), unknown=False):
        self.classes, self.unknown = set(classes), unknown

    def __or__(self, other):
        return Types(self.classes | other.classes, self.unknown or other.unknown)


UNKNOWN, EXTERNAL = Types(unknown=True), Types()


class Scope:
    __slots__ = ("module", "cls", "func", "parent")

    def __init__(self, module, cls=None, func=None, parent=None):
        self.module, self.cls, self.func, self.parent = module, cls, func, parent


class Builder:
    def __init__(self, root, dirs, kb=None):
        self.root, self.kb = Path(root), kb
        self.graph = Graph()
        self.modules = {}       # module name -> {"file", "tree", "symbols", "imports"}
        self.classes = {}       # class id -> {"bases": [expr], "module", "methods": {name: id}, "attrs": {name: [(expr, scope)]}}
        self.funcs = {}         # function id -> {"ast", "params", "locals": {name: [expr]}, "nested": {name: id}, "scope"}
        self.methods_by_name = {}
        self.routes = []        # (route id, method or None, path literal or None, path regex or None)
        for d in dirs:
            for path in sorted((self.root / d).rglob("*.py")):
                rel = path.relative_to(self.root).as_posix()
                parts = list(path.relative_to(self.root).with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts.pop()
                name = ".".join(parts)
                self.modules[name] = {"file": rel, "tree": ast.parse(path.read_text(encoding="utf-8")),
                                      "symbols": {}, "imports": {}}

    # ------------------------------------------------------------ pass 1: nodes
    def collect(self):
        for mod, info in self.modules.items():
            tree, file = info["tree"], info["file"]
            last = max((getattr(n, "end_lineno", 0) or 0) for n in ast.walk(tree)) or 1
            self.graph.add_node(Node(f"{mod}:<module>", "module", mod, file, 1, last))
            for stmt in tree.body:
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    self._record_import(info, stmt)
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    info["symbols"][stmt.name] = self._collect_func(stmt, mod, file, stmt.name, Scope(mod))
                elif isinstance(stmt, ast.ClassDef):
                    info["symbols"][stmt.name] = self._collect_class(stmt, mod, file, stmt.name, Scope(mod))
                elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    names = [n.id for t in _targets(stmt) for n in ast.walk(t) if isinstance(n, ast.Name)]
                    if names:
                        vid = self.graph.add_node(Node(f"{mod}:{','.join(names)}", "var", mod, file,
                                                       stmt.lineno, stmt.end_lineno))
                        for n in names:
                            info["symbols"][n] = vid
                elif _is_main_guard(stmt):
                    self.graph.add_node(Node(f"{mod}:<main>", "script", mod, file, stmt.lineno, stmt.end_lineno))
        if self.kb:
            self._collect_routes()

    def _record_import(self, info, stmt):
        if isinstance(stmt, ast.Import):
            for a in stmt.names:
                info["imports"][a.asname or a.name.split(".")[0]] = ("module", a.name if a.asname else a.name.split(".")[0])
        elif stmt.module and stmt.level == 0:
            for a in stmt.names:
                info["imports"][a.asname or a.name] = ("symbol", stmt.module, a.name)

    def _collect_func(self, fn, mod, file, qual, scope):
        start = min([d.lineno for d in fn.decorator_list] + [fn.lineno])
        fid = self.graph.add_node(Node(f"{mod}:{qual}", "function", mod, file, start, fn.end_lineno))
        args = fn.args
        params = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        params |= {a.arg for a in (args.vararg, args.kwarg) if a}
        info = {"ast": fn, "params": params, "locals": {}, "nested": {},
                "scope": Scope(mod, scope.cls, fid, scope)}
        self.funcs[fid] = info
        inner = info["scope"]
        for node in _own_body(fn):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info["nested"][node.name] = self._collect_func(node, mod, file, f"{qual}.{node.name}", inner)
            elif isinstance(node, ast.ClassDef):
                info["nested"][node.name] = self._collect_class(node, mod, file, f"{qual}.{node.name}", inner)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        info["locals"].setdefault(t.id, []).append(node.value)
            elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
                info["locals"].setdefault(node.optional_vars.id, []).append(node.context_expr)
        return fid

    def _collect_class(self, cls, mod, file, qual, scope):
        start = min([d.lineno for d in cls.decorator_list] + [cls.lineno])
        cid = self.graph.add_node(Node(f"{mod}:{qual}", "class", mod, file, start, cls.end_lineno))
        info = {"bases": cls.bases, "module": mod, "methods": {}, "attrs": {}, "scope": scope}
        self.classes[cid] = info
        cscope = Scope(mod, cid, scope.func, scope)
        for stmt in cls.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mid = self._collect_func(stmt, mod, file, f"{qual}.{stmt.name}", cscope)
                info["methods"][stmt.name] = mid
                self.methods_by_name.setdefault(stmt.name, []).append(mid)
                # self.x = ... / cls.x = ... anywhere in a method gives the attribute a type
                for node in ast.walk(stmt):
                    if isinstance(node, ast.Assign):
                        for t in node.targets:
                            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                                    and t.value.id in ("self", "cls")):
                                info["attrs"].setdefault(t.attr, []).append((node.value, self.funcs[mid]["scope"]))
            elif isinstance(stmt, ast.ClassDef):
                self._collect_class(stmt, mod, file, f"{qual}.{stmt.name}", cscope)
        return cid

    def _collect_routes(self):
        spec = self.kb.dispatcher
        if not spec:
            return
        fn = self.funcs.get(spec["function"])
        if fn is None:
            raise ValueError(f"knowledge base names dispatcher {spec['function']}, which is not in the code")
        mod = self.graph.nodes[spec["function"]].module
        patterns = {}
        for stmt in self.modules[mod]["tree"].body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                pat = _regex_literal(stmt.value)
                if pat:
                    patterns[stmt.targets[0].id] = pat
        file = self.modules[mod]["file"]
        for method, literal, regex, start, end in self.kb.route_regions(fn["ast"], spec, patterns):
            label = f"{method or '*'} {literal or regex}"
            rid = self.graph.add_node(Node(f"{spec['function']}#{label}", "route", mod, file, start, end))
            self.routes.append((rid, method, literal, regex))

    # ------------------------------------------------------------ pass 2: edges
    def connect(self):
        g = self.graph
        for mod, info in self.modules.items():
            mid = f"{mod}:<module>"
            # A module-level constant or class body also runs at import time, but a change
            # to it only matters to code that reads the constant or creates the class, so
            # those are reached through read/call edges, not from every importer.
            for stmt in info["tree"].body:
                for t in self._import_targets(stmt):
                    g.add_edge(mid, f"{t}:<module>", "import")
            self._visit_block(info["tree"].body, Scope(mod), info["file"])
        # unittest runs class fixtures around every test method
        for cid, cinfo in self.classes.items():
            for name, method in cinfo["methods"].items():
                if name.startswith("test"):
                    for fx in UNITTEST_FIXTURES:
                        for target in self.methods_named({cid}, fx):
                            g.add_edge(method, target, "fixture")

    def _import_targets(self, stmt):
        names = []
        if isinstance(stmt, ast.Import):
            names = [a.name for a in stmt.names]
        elif isinstance(stmt, ast.ImportFrom) and stmt.module and stmt.level == 0:
            names = [stmt.module] + [f"{stmt.module}.{a.name}" for a in stmt.names]
        return [n for n in names if n in self.modules]

    def _visit_block(self, stmts, scope, file):
        for stmt in stmts:
            self._visit(stmt, scope, file)

    def _visit(self, node, scope, file):
        g = self.graph
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fid = self._lookup_def(node.name, scope, kind="function")
            outer = self._scope_owner(scope)
            # decorators and default values run when the function is defined, not when it is called
            for expr in node.decorator_list + node.args.defaults + [d for d in node.args.kw_defaults if d]:
                self._visit_expr(expr, scope, file, outer)
            if fid:
                self._visit_block(node.body, self.funcs[fid]["scope"], file)
            return
        if isinstance(node, ast.ClassDef):
            cid = self._lookup_def(node.name, scope, kind="class")
            if scope.func:  # a class defined inside a function is created when the function runs
                g.add_edge(scope.func, cid, "define")
            if cid:
                for expr in node.bases + node.decorator_list:
                    self._visit_expr(expr, scope, file, cid)
                self._visit_block(node.body, Scope(scope.module, cid, scope.func, scope), file)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)) and scope.func:
            for t in self._import_targets(node):
                g.add_edge(scope.func, f"{t}:<module>", "import")
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._protocol(g.locate(file, node.lineno), node.iter, ["__iter__", "__next__"], scope)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                self._protocol(g.locate(file, node.lineno), item.context_expr, ["__enter__", "__exit__"], scope)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self._visit(child, scope, file)
            else:
                self._visit_expr(child, scope, file, None)

    def _visit_expr(self, expr, scope, file, owner):
        """Walk an expression, adding edges from the node that owns each line."""
        skip = set()
        for node in ast.walk(expr):
            if id(node) in skip or not hasattr(node, "lineno"):
                continue
            src = owner or self.graph.locate(file, node.lineno)
            if isinstance(node, ast.Call):
                targets = self.resolve_call(node.func, scope)
                for t in targets:
                    self.graph.add_edge(src, t, "call")
                if isinstance(node.func, (ast.Name, ast.Attribute)):
                    skip.add(id(node.func))
                if (isinstance(node.func, ast.Name) and node.func.id in BUILTIN_PROTOCOLS
                        and node.args and not self.resolve_name(node.func.id, scope)):
                    self._protocol(src, node.args[0], BUILTIN_PROTOCOLS[node.func.id], scope)
                if self.kb:
                    self.kb.add_call_edges(self, src, node, targets, scope)
            elif isinstance(node, ast.Compare):
                left = node.left
                for op, right in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.In, ast.NotIn)):
                        self._protocol(src, right, ["__contains__"], scope)
                    elif type(op) in COMPARE_PROTOCOLS:
                        self._protocol(src, left, [COMPARE_PROTOCOLS[type(op)]], scope)
                    left = right
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                for kind, ref in self.resolve_name(node.id, scope):
                    if kind == "node":
                        k = self.graph.nodes[ref].kind
                        self.graph.add_edge(src, ref, "read" if k == "var" else "ref")
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                for t in self._attribute_refs(node, scope):
                    k = self.graph.nodes[t].kind
                    self.graph.add_edge(src, t, "read" if k == "var" else "ref")

    def _protocol(self, src, expr, dunders, scope):
        """Edges for methods Python calls implicitly on the value of `expr`."""
        if isinstance(expr, CONTAINER_NODES):
            return
        types = self.type_of(expr, scope)
        for name in dunders:
            targets = self.methods_named(types.classes, name)
            if types.unknown:
                targets += self.methods_by_name.get(name, [])
            for t in targets:
                self.graph.add_edge(src, t, "call")

    # ------------------------------------------------------------ resolution
    def _scope_owner(self, scope):
        return scope.func or scope.cls or f"{scope.module}:<module>"

    def _lookup_def(self, name, scope, kind):
        if scope.cls and not scope.func:
            table = self.classes[scope.cls]["methods"] if kind == "function" else {}
            if name in table:
                return table[name]
            prefix = self.graph.nodes[scope.cls].id
            return f"{prefix}.{name}" if f"{prefix}.{name}" in self.graph.nodes else None
        if scope.cls and scope.func and kind == "function":
            mid = self.classes[scope.cls]["methods"].get(name)
            if mid:
                return mid
        if scope.func:
            return self.funcs[scope.func]["nested"].get(name)
        return self.modules[scope.module]["symbols"].get(name)

    def resolve_name(self, name, scope):
        s = scope
        while s is not None:
            if s.func and name in self.funcs[s.func]["nested"]:
                return [("node", self.funcs[s.func]["nested"][name])]
            s = s.parent
        info = self.modules[scope.module]
        if name in info["symbols"]:
            return [("node", info["symbols"][name])]
        imp = info["imports"].get(name)
        if imp and imp[0] == "module" and imp[1] in self.modules:
            return [("module", imp[1])]
        if imp and imp[0] == "symbol":
            mod, sym = imp[1], imp[2]
            if mod in self.modules and sym in self.modules[mod]["symbols"]:
                return [("node", self.modules[mod]["symbols"][sym])]
            if f"{mod}.{sym}" in self.modules:
                return [("module", f"{mod}.{sym}")]
        return []

    def methods_named(self, class_ids, name, _seen=None):
        seen = _seen if _seen is not None else set()
        found = []
        for cid in class_ids:
            if cid in seen or cid not in self.classes:
                continue
            seen.add(cid)
            info = self.classes[cid]
            if name in info["methods"]:
                found.append(info["methods"][name])
            else:
                found += self.methods_named(self._bases(cid), name, seen)
        return found

    def _bases(self, cid):
        info = self.classes[cid]
        out = []
        for b in info["bases"]:
            out += [ref for kind, ref in self._resolve_expr_symbol(b, info["scope"])
                    if kind == "node" and self.graph.nodes[ref].kind == "class"]
        return out

    def _resolve_expr_symbol(self, expr, scope):
        if isinstance(expr, ast.Name):
            return self.resolve_name(expr.id, scope)
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            for kind, ref in self.resolve_name(expr.value.id, scope):
                if kind == "module" and expr.attr in self.modules[ref]["symbols"]:
                    return [("node", self.modules[ref]["symbols"][expr.attr])]
        return []

    def _constructor(self, cid):
        return [cid] + self.methods_named({cid}, "__init__") + self.methods_named({cid}, "__post_init__")

    def resolve_call(self, func, scope):
        if isinstance(func, ast.Name):
            out = []
            for kind, ref in self.resolve_name(func.id, scope):
                if kind == "node":
                    k = self.graph.nodes[ref].kind
                    out += self._constructor(ref) if k == "class" else [ref] if k == "function" else []
            return out
        if not isinstance(func, ast.Attribute):
            return []
        value, attr = func.value, func.attr
        if isinstance(value, ast.Name) and value.id in ("self", "cls") and scope.cls:
            own = self.methods_named({scope.cls}, attr)
            if own:
                return own
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "super" and scope.cls:
            return self.methods_named(set(self._bases(scope.cls)), attr)
        if isinstance(value, ast.Name):
            for kind, ref in self.resolve_name(value.id, scope):
                if kind == "module":
                    target = self.modules[ref]["symbols"].get(attr)
                    if target:
                        k = self.graph.nodes[target].kind
                        return self._constructor(target) if k == "class" else [target] if k == "function" else []
                    return []
                if kind == "node" and self.graph.nodes[ref].kind == "class":
                    return self.methods_named({ref}, attr)
        types = self.type_of(value, scope)
        out = []
        for cid in types.classes:
            out += self.methods_named({cid}, attr)
        if types.unknown:
            out += self.methods_by_name.get(attr, [])
        return list(dict.fromkeys(out))

    def _attribute_refs(self, node, scope):
        value = node.value
        if isinstance(value, ast.Name):
            for kind, ref in self.resolve_name(value.id, scope):
                if kind == "module":
                    target = self.modules[ref]["symbols"].get(node.attr)
                    return [target] if target else []
            if value.id in ("self", "cls") and scope.cls:
                return self.methods_named({scope.cls}, node.attr)
        return []

    def type_of(self, expr, scope, depth=0):
        if depth > 8:
            return UNKNOWN
        if isinstance(expr, ast.Call):
            out = Types()
            project = False
            for kind, ref in self._resolve_expr_symbol(expr.func, scope):
                if kind == "node":
                    project = True
                    if self.graph.nodes[ref].kind == "class":
                        out = out | Types({ref})
                    else:
                        out = out | UNKNOWN
            if not project and isinstance(expr.func, ast.Attribute):
                # obj.method(...): may return a project object only if some project class has that method
                return UNKNOWN if expr.func.attr in self.methods_by_name else EXTERNAL
            return out
        if isinstance(expr, CONTAINER_NODES):
            return EXTERNAL
        if isinstance(expr, ast.BoolOp):
            out = Types()
            for v in expr.values:
                out = out | self.type_of(v, scope, depth + 1)
            return out
        if isinstance(expr, ast.IfExp):
            return self.type_of(expr.body, scope, depth + 1) | self.type_of(expr.orelse, scope, depth + 1)
        if isinstance(expr, ast.Await):
            return self.type_of(expr.value, scope, depth + 1)
        if isinstance(expr, ast.Name):
            s = scope
            while s is not None:
                if s.func:
                    f = self.funcs[s.func]
                    if expr.id in f["locals"]:
                        out = Types()
                        for v in f["locals"][expr.id]:
                            out = out | self.type_of(v, f["scope"], depth + 1)
                        return out
                    if expr.id in f["params"]:
                        return UNKNOWN
                s = s.parent
            return UNKNOWN
        if (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)
                and expr.value.id in ("self", "cls") and scope.cls):
            return self._attr_types(scope.cls, expr.attr, depth)
        return UNKNOWN

    def _attr_types(self, cid, attr, depth, _seen=None):
        seen = _seen if _seen is not None else set()
        if cid in seen:
            return Types()
        seen.add(cid)
        exprs = self.classes[cid]["attrs"].get(attr)
        if exprs:
            out = Types()
            for value, s in exprs:
                out = out | self.type_of(value, s, depth + 1)
            return out
        bases = self._bases(cid)
        if not bases:
            return UNKNOWN
        out = Types()
        for b in bases:
            out = out | self._attr_types(b, attr, depth, seen)
        return out


def _targets(stmt):
    return stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]


def _is_main_guard(stmt):
    if not isinstance(stmt, ast.If) or not isinstance(stmt.test, ast.Compare):
        return False
    parts = [stmt.test.left] + stmt.test.comparators
    return (any(isinstance(p, ast.Name) and p.id == "__name__" for p in parts)
            and any(isinstance(p, ast.Constant) and p.value == "__main__" for p in parts))


def _own_body(fn):
    """Nodes inside a function, not descending into nested functions or classes."""
    todo = list(fn.body)
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(node))


def _regex_literal(value):
    """`re.compile(r"...")` -> the pattern string."""
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and value.func.attr == "compile" and value.args and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)):
        return value.args[0].value
    return None


class Index:
    """A built graph plus, for every test, the set of nodes it can reach."""

    def __init__(self, builder):
        self.builder, self.graph = builder, builder.graph
        self.routes = builder.routes
        self._reach, self._reaching = {}, None

    def test_node(self, test_id):
        mod, cls, meth = test_id.rsplit(".", 2)
        cid = f"{mod}:{cls}"
        found = self.builder.methods_named({cid}, meth) if cid in self.builder.classes else []
        return found[0] if found else None

    def reach(self, test_id):
        if test_id not in self._reach:
            mod = test_id.rsplit(".", 2)[0]
            starts = [n for n in (self.test_node(test_id), f"{mod}:<module>") if n in self.graph.nodes]
            self._reach[test_id] = self.graph.reachable(starts)
        return self._reach[test_id]

    def index_tests(self, tests):
        self._reaching = {}
        for t in tests:
            for n in self.reach(t):
                self._reaching.setdefault(n, set()).add(t)

    def nodes_for(self, file, start, end):
        return {n for line in range(start, end + 1) for n in [self.graph.locate(file, line)] if n}

    def tests_reaching(self, nodes):
        out = set()
        for n in nodes:
            out |= self._reaching.get(n, set())
        return out


def build(root, dirs=("sut", "tests"), kb=None):
    builder = Builder(root, dirs, kb)
    builder.collect()
    builder.connect()
    return Index(builder)
