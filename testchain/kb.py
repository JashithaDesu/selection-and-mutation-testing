"""Architecture knowledge base: Application Components, artifacts, interfaces and tests.

A code graph sees calls inside one process. It cannot see that an end-to-end test
starts the server as a separate process, that the HTTP framework calls `do_GET`
for every request, or which route of the API a test's request exercises. The
knowledge base (kb/architecture.json) records those facts, and this module:

  * splits the HTTP dispatcher into one graph node per route (method + path),
  * adds the cross-process edges to the code property graph (kind "kb:*"),
  * maps files to Application Components and components to the tests linked to them,
  * checks the declared architecture against the code (drift), and
  * exports everything as one knowledge graph (results/knowledge_graph.json).
"""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB_PATH = ROOT / "kb" / "architecture.json"


class KnowledgeBase:
    def __init__(self, data):
        self.data = data
        self.components = {c["name"]: c for c in data["components"]}
        self.component_of_file = {a: c["name"] for c in data["components"] for a in c["artifacts"]}
        iface = data.get("interface", {})
        self.dispatcher = iface.get("dispatcher")
        self.clients = {c["function"]: c for c in iface.get("http_clients", [])}
        self.passthrough = set(iface.get("passthrough", []))
        self.launchers = set(data.get("processes", {}).get("launchers", []))
        self.callbacks = data.get("framework_callbacks", [])
        self.boundary = {c["name"] for c in data["components"] if c.get("boundary")}

    @classmethod
    def load(cls, path=KB_PATH):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------ components
    def dependents(self, component):
        """The component plus every component that depends on it, directly or not."""
        out, changed = {component}, True
        while changed:
            changed = False
            for c in self.components.values():
                if c["name"] not in out and out & set(c["depends_on"]):
                    out.add(c["name"])
                    changed = True
        return out

    def test_components(self, root, tests_dir="tests"):
        """{test module: components it is linked to}: through the artifacts it imports,
        and through the processes it launches (an e2e test is linked to the component
        whose module it starts)."""
        links = {}
        for path in sorted((Path(root) / tests_dir).rglob("test_*.py")):
            mod = ".".join(path.relative_to(root).with_suffix("").parts)
            comps = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                elif isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.Call) and _dotted(node.func) in self.launchers:
                    launched = _launched_module(node)
                    modules = [launched] if launched else []
                for m in modules:
                    c = self.component_of_file.get(m.replace(".", "/") + ".py")
                    if c:
                        comps.add(c)
            links[mod] = comps
        return links

    # ------------------------------------------------------------ interface
    @staticmethod
    def route_regions(fn, spec, patterns):
        """Branches of the dispatcher that test the request method or path.

        Yields (method or None, path literal or None, path regex or None, first line, last line).
        A branch nested in another inherits its constraints, so `if path == "/f":` with an
        inner `if method == "GET":` gives a route "* /f" and a narrower route "GET /f".
        A route covers the branch body only; the `if` line is shared routing logic."""
        method_p, path_p = spec["method_param"], spec["path_param"]
        match_vars = {}
        regions = []

        def constraints(test):
            method = literal = regex = None
            parts = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) else [test]
            for p in parts:
                if (isinstance(p, ast.Compare) and len(p.ops) == 1 and isinstance(p.ops[0], ast.Eq)
                        and isinstance(p.left, ast.Name) and isinstance(p.comparators[0], ast.Constant)):
                    if p.left.id == method_p:
                        method = p.comparators[0].value
                    elif p.left.id == path_p:
                        literal = p.comparators[0].value
                elif isinstance(p, ast.Name) and p.id in match_vars:
                    regex = match_vars[p.id]
            return method, literal, regex

        def walk(stmts, method, literal, regex):
            for st in stmts:
                if (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                        and isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Attribute)
                        and st.value.func.attr in ("match", "fullmatch")
                        and isinstance(st.value.func.value, ast.Name) and st.value.func.value.id in patterns):
                    match_vars[st.targets[0].id] = patterns[st.value.func.value.id]
                if isinstance(st, ast.If):
                    m, lit, rx = constraints(st.test)
                    if m or lit or rx:
                        nm, nl, nr = m or method, lit or literal, rx or regex
                        # Only the body belongs to the route. The condition itself runs for
                        # every request that gets this far, so it stays with the enclosing code.
                        regions.append((nm, nl, nr, st.body[0].lineno, st.body[-1].end_lineno))
                        walk(st.body, nm, nl, nr)
                        walk(st.orelse, method, literal, regex)
                        continue
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(st, field, []) or [], method, literal, regex)
                for h in getattr(st, "handlers", []) or []:
                    walk(h.body, method, literal, regex)

        walk(fn.body, None, None, None)
        return regions

    @staticmethod
    def request_of(call, spec):
        """(method, example path) of a client call with literal arguments; None = unknown."""
        def arg(pos, name):
            if len(call.args) > pos:
                return call.args[pos]
            return next((k.value for k in call.keywords if k.arg == name), None)
        m = arg(spec["method_arg"], spec.get("method_param", "method"))
        p = arg(spec["path_arg"], spec.get("path_param", "path"))
        method = m.value if isinstance(m, ast.Constant) and isinstance(m.value, str) else None
        return method, _example_path(p)

    @staticmethod
    def route_matches(route, method, path):
        _, r_method, literal, regex = route
        if r_method and method and r_method != method:
            return False
        if path is None:
            return True
        if literal is not None:
            return path == literal
        return re.match(regex, path) is not None

    # ------------------------------------------------------------ graph edges
    def add_call_edges(self, builder, src, call, targets, scope):
        g = builder.graph
        # 1. a test starting `python -m <module>` runs that module's __main__ block in a new process
        if _dotted(call.func) in self.launchers:
            launched = _launched_module(call)
            if launched in builder.modules:
                g.add_edge(src, f"{launched}:<main>", "kb:process")
                g.add_edge(src, f"{launched}:<module>", "kb:process")
        # 2. frameworks that call back into project code
        if isinstance(call.func, ast.Attribute):
            for cb in self.callbacks:
                if call.func.attr == cb["trigger"]:
                    for cid, info in builder.classes.items():
                        if any(_dotted(b).split(".")[-1] == cb["base_class"] for b in info["bases"]):
                            for name, mid in info["methods"].items():
                                if name.startswith(cb["method_prefix"]):
                                    g.add_edge(src, mid, "kb:framework")
        # 3. a request with a known method and path exercises only the matching routes
        if not self.dispatcher or src in self.passthrough:
            return
        for t in targets:
            spec = self.clients.get(t) or (self.dispatcher if t == self.dispatcher["function"] else None)
            if spec is None:
                continue
            method, path = self.request_of(call, spec)
            for route in builder.routes:
                if self.route_matches(route, method, path):
                    g.add_edge(src, route[0], "kb:endpoint")
            g.add_edge(src, self.dispatcher["function"], "kb:endpoint")

    # ------------------------------------------------------------ drift check
    def check(self, root):
        """Compare the declared architecture with the code. Returns a list of problems."""
        problems = []
        root = Path(root)
        source_files = {p.relative_to(root).as_posix() for p in (root / "sut").rglob("*.py")
                        if p.name != "__init__.py"}
        for f in sorted(source_files - set(self.component_of_file)):
            problems.append(f"{f} is not assigned to any component")
        for f in sorted(set(self.component_of_file) - source_files):
            problems.append(f"{f} is listed as an artifact but does not exist")
        for name, comp in self.components.items():
            for d in comp["depends_on"]:
                if d not in self.components:
                    problems.append(f"{name} depends on unknown component {d}")
            actual = set()
            for art in comp["artifacts"]:
                path = root / art
                if not path.exists():
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        c = self.component_of_file.get(node.module.replace(".", "/") + ".py")
                        if c and c != name:
                            actual.add(c)
            declared = set(comp["depends_on"])
            for d in sorted(actual - declared):
                problems.append(f"{name} imports {d} but the model does not declare that dependency")
            for d in sorted(declared - actual):
                problems.append(f"{name} declares a dependency on {d} that the code does not have")
        return problems

    # ------------------------------------------------------------ export
    def export(self, index, tests, root):
        """One knowledge graph: components, artifacts, routes and tests, with typed edges."""
        nodes, edges = [], []
        for c in self.data["components"]:
            nodes.append({"id": f"component:{c['name']}", "type": "ApplicationComponent",
                          "boundary": bool(c.get("boundary")), "description": c.get("description", "")})
            for a in c["artifacts"]:
                nodes.append({"id": f"artifact:{a}", "type": "Artifact"})
                edges.append({"from": f"component:{c['name']}", "to": f"artifact:{a}", "type": "realized_by"})
            for d in c["depends_on"]:
                edges.append({"from": f"component:{c['name']}", "to": f"component:{d}", "type": "depends_on"})
        for rid, method, literal, regex in index.routes:
            nodes.append({"id": f"route:{rid.split('#', 1)[1]}", "type": "Endpoint"})
            comp = self.component_of_file.get(index.graph.nodes[rid].file)
            edges.append({"from": f"component:{comp}", "to": f"route:{rid.split('#', 1)[1]}", "type": "exposes"})
        links = self.test_components(root)
        for t in tests:
            nodes.append({"id": f"test:{t}", "type": "Test", "stage": t.split(".")[1]})
            for c in sorted(links.get(t.rsplit(".", 2)[0], ())):
                edges.append({"from": f"test:{t}", "to": f"component:{c}", "type": "linked_to"})
            reach = index.reach(t)
            for rid, *_ in index.routes:
                if rid in reach:
                    edges.append({"from": f"test:{t}", "to": f"route:{rid.split('#', 1)[1]}", "type": "exercises"})
        return {"nodes": nodes, "edges": edges}


def _dotted(expr):
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        inner = _dotted(expr.value)
        return f"{inner}.{expr.attr}" if inner else expr.attr
    return ""


def _launched_module(call):
    """`Popen([sys.executable, "-m", "pkg.mod", ...])` -> "pkg.mod"."""
    if not call.args or not isinstance(call.args[0], (ast.List, ast.Tuple)):
        return None
    elts = call.args[0].elts
    for a, b in zip(elts, elts[1:]):
        if isinstance(a, ast.Constant) and a.value == "-m" and isinstance(b, ast.Constant):
            return b.value
    return None


def _example_path(node):
    """A concrete example of the path a client call requests, or None if unknowable.
    f"/functions/{name}" and "/invoke/" + name become "/functions/x" and "/invoke/x"."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "x" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _example_path(node.left), _example_path(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "x") + (right if right is not None else "x")
    return None
