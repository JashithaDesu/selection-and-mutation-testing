"""Code property graph and knowledge base, first on a miniature project where every
edge can be worked out on paper, then on the real system under test."""

import ast
import copy
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from testchain import cpg, strategies
from testchain.kb import KnowledgeBase, _example_path, _launched_module

FILES = {
    "sut/__init__.py": "",
    "sut/store.py": """
        LIMIT = 3


        class Store:
            def __init__(self):
                self.items = []

            def add(self, x):
                if len(self.items) >= LIMIT:
                    raise ValueError("full")
                self.items.append(x)

            def __len__(self):
                return len(self.items)
        """,
    "sut/server.py": """
        import re
        import sys
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from sut.store import Store

        _ITEM = re.compile(r"^/items/([^/]+)$")


        class App:
            def __init__(self):
                self.store = Store()

            def handle(self, method, path):
                if path == "/health" and method == "GET":
                    return 200
                m = _ITEM.match(path)
                if m:
                    if method == "PUT":
                        self.store.add(m.group(1))
                        return 201
                return 404


        def make_handler(app):
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self._dispatch("GET")

                def _dispatch(self, method):
                    self.send_response(app.handle(method, self.path))
            return Handler


        def main():
            HTTPServer(("127.0.0.1", 0), make_handler(App())).serve_forever()


        if __name__ == "__main__":
            sys.exit(main())
        """,
    "tests/__init__.py": "",
    "tests/unit/__init__.py": "",
    "tests/unit/test_store.py": """
        import unittest

        from sut.store import Store


        class T(unittest.TestCase):
            def setUp(self):
                self.s = Store()

            def test_add(self):
                self.s.add(1)

            def test_len(self):
                self.assertEqual(len(Store()), 0)
        """,
    "tests/integration/__init__.py": "",
    "tests/integration/test_app.py": """
        import unittest

        from sut.server import App


        class T(unittest.TestCase):
            def test_put(self):
                self.assertEqual(App().handle("PUT", f"/items/{'a'}"), 201)
        """,
    "tests/e2e/__init__.py": "",
    "tests/e2e/test_http.py": """
        import subprocess
        import sys
        import unittest


        class Client:
            def __init__(self):
                self.proc = subprocess.Popen([sys.executable, "-m", "sut.server"])

            def request(self, method, path):
                return 200


        class T(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.c = Client()

            def test_health(self):
                self.c.request("GET", "/health")
        """,
}

KB = {
    "components": [
        {"name": "Store", "artifacts": ["sut/store.py"], "depends_on": []},
        {"name": "API", "artifacts": ["sut/server.py"], "depends_on": ["Store"], "boundary": True},
    ],
    "interface": {
        "dispatcher": {"function": "sut.server:App.handle", "method_param": "method",
                       "path_param": "path", "method_arg": 0, "path_arg": 1},
        "http_clients": [{"function": "tests.e2e.test_http:Client.request", "method_arg": 0, "path_arg": 1}],
        "passthrough": ["sut.server:make_handler.Handler._dispatch"],
    },
    "processes": {"launchers": ["subprocess.Popen"]},
    "framework_callbacks": [{"base_class": "BaseHTTPRequestHandler", "method_prefix": "do_",
                             "trigger": "serve_forever"}],
}

UNIT_ADD = "tests.unit.test_store.T.test_add"
UNIT_LEN = "tests.unit.test_store.T.test_len"
INTEG = "tests.integration.test_app.T.test_put"
E2E = "tests.e2e.test_http.T.test_health"
TESTS = [UNIT_ADD, UNIT_LEN, INTEG, E2E]


def line_of(root, rel, text):
    for i, line in enumerate((Path(root) / rel).read_text().splitlines(), 1):
        if text in line:
            return i
    raise AssertionError(f"{text!r} not in {rel}")


class MiniProject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        for rel, src in FILES.items():
            path = cls.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(src).lstrip())
        cls.kb = KnowledgeBase(copy.deepcopy(KB))
        cls.plain = cpg.build(cls.root)
        cls.full = cpg.build(cls.root, kb=cls.kb)
        for idx in (cls.plain, cls.full):
            idx.index_tests(TESTS)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def node_at(self, idx, rel, text):
        return idx.graph.locate(rel, line_of(self.root, rel, text))

    def edge_kinds(self, idx, src, dst):
        return idx.graph.edges.get(src, {}).get(dst, set())

    # --- graph structure ---
    def test_every_line_maps_to_its_innermost_region(self):
        self.assertEqual(self.node_at(self.plain, "sut/store.py", "LIMIT = 3"), "sut.store:LIMIT")
        self.assertEqual(self.node_at(self.plain, "sut/store.py", 'raise ValueError("full")'), "sut.store:Store.add")
        self.assertEqual(self.node_at(self.plain, "sut/server.py", "sys.exit(main())"), "sut.server:<main>")
        self.assertEqual(self.node_at(self.plain, "sut/server.py", "self._dispatch"),
                         "sut.server:make_handler.Handler.do_GET")

    def test_calls_on_typed_attributes_resolve_to_the_right_method(self):
        self.assertIn("call", self.edge_kinds(self.plain, "sut.server:App.handle", "sut.store:Store.add"))
        self.assertIn("read", self.edge_kinds(self.plain, "sut.store:Store.add", "sut.store:LIMIT"))

    def test_implicit_protocol_calls_are_edges(self):
        # len(Store()) calls Store.__len__ although the name never appears in the test
        self.assertIn("sut.store:Store.__len__", self.plain.reach(UNIT_LEN))

    def test_fixtures_run_around_every_test(self):
        reach = self.plain.reach(UNIT_ADD)
        self.assertIn("tests.unit.test_store:T.setUp", reach)
        self.assertIn("sut.store:Store.__init__", reach)

    def test_build_is_deterministic(self):
        again = cpg.build(self.root, kb=self.kb)
        self.assertEqual(again.graph.edges, self.full.graph.edges)

    # --- the process boundary ---
    def test_without_kb_the_e2e_test_cannot_see_into_the_server(self):
        reach = self.plain.reach(E2E)
        self.assertNotIn("sut.server:main", reach)
        self.assertFalse({n for n in reach if n.startswith("sut.")})

    def test_kb_follows_the_e2e_test_into_the_server_process(self):
        reach = self.full.reach(E2E)
        for node in ("sut.server:<main>", "sut.server:main", "sut.server:make_handler.Handler.do_GET",
                     "sut.server:make_handler.Handler._dispatch"):
            self.assertIn(node, reach)
        self.assertIn("kb:process", self.edge_kinds(self.full, "tests.e2e.test_http:Client.__init__", "sut.server:<main>"))
        self.assertIn("kb:framework", self.edge_kinds(self.full, "sut.server:main", "sut.server:make_handler.Handler.do_GET"))

    # --- routes and endpoint precision ---
    def test_routes_are_mined_from_the_dispatcher(self):
        labels = {r[0].split("#", 1)[1] for r in self.full.routes}
        self.assertEqual(labels, {"GET /health", "* ^/items/([^/]+)$", "PUT ^/items/([^/]+)$"})

    def test_route_condition_is_shared_but_its_body_belongs_to_the_route(self):
        self.assertEqual(self.node_at(self.full, "sut/server.py", 'if path == "/health"'), "sut.server:App.handle")
        self.assertEqual(self.node_at(self.full, "sut/server.py", "return 200"), "sut.server:App.handle#GET /health")

    def test_a_request_exercises_only_the_matching_route(self):
        health, put = "sut.server:App.handle#GET /health", "sut.server:App.handle#PUT ^/items/([^/]+)$"
        self.assertIn(health, self.full.reach(E2E))
        self.assertNotIn(put, self.full.reach(E2E))
        self.assertIn(put, self.full.reach(INTEG))          # f-string path still matches the regex
        self.assertNotIn(health, self.full.reach(INTEG))

    def test_passthrough_code_does_not_select_every_route(self):
        kinds = set().union(*self.full.graph.edges.get("sut.server:make_handler.Handler._dispatch", {}).values())
        self.assertNotIn("kb:endpoint", kinds)

    def test_change_in_a_route_body_selects_only_tests_using_that_route(self):
        node = self.node_at(self.full, "sut/server.py", "return 201")
        self.assertEqual(self.full.tests_reaching({node}), {INTEG})

    # --- knowledge base ---
    def test_model_matches_the_code(self):
        self.assertEqual(self.kb.check(self.root), [])

    def test_drift_is_reported(self):
        data = copy.deepcopy(KB)
        data["components"][1]["depends_on"] = []
        data["components"][0]["artifacts"].append("sut/gone.py")
        problems = KnowledgeBase(data).check(self.root)
        self.assertTrue(any("imports Store but the model does not declare" in p for p in problems), problems)
        self.assertTrue(any("sut/gone.py" in p for p in problems), problems)

    def test_tests_are_linked_to_components(self):
        links = self.kb.test_components(self.root)
        self.assertEqual(links["tests.unit.test_store"], {"Store"})
        self.assertEqual(links["tests.e2e.test_http"], {"API"})  # through the process it starts

    def test_dependents(self):
        self.assertEqual(self.kb.dependents("Store"), {"Store", "API"})
        self.assertEqual(self.kb.dependents("API"), {"API"})


class Helpers(unittest.TestCase):
    def call(self, src):
        return ast.parse(src).body[0].value

    def test_example_path(self):
        self.assertEqual(_example_path(self.call('f(f"/functions/{name}")').args[0]), "/functions/x")
        self.assertEqual(_example_path(self.call('f("/invoke/" + name)').args[0]), "/invoke/x")
        self.assertIsNone(_example_path(self.call("f(path)").args[0]))

    def test_launched_module(self):
        self.assertEqual(_launched_module(self.call('Popen([sys.executable, "-m", "pkg.mod", "--x"])')), "pkg.mod")
        self.assertIsNone(_launched_module(self.call("Popen(cmd)")))


class RealSystem(unittest.TestCase):
    """The shipped architecture model against the real system under test."""

    @classmethod
    def setUpClass(cls):
        root = strategies.PROJECT_ROOT
        cls.root = root
        cls.ctx = strategies.Context(json.loads((root / "results" / "baseline.json").read_text()))

    def mutant_at(self, text):
        line = line_of(self.root, "sut/server.py", text)
        return {"id": "X", "file": "sut/server.py", "lineno": line, "end_lineno": line}

    def test_architecture_model_has_no_drift(self):
        self.assertEqual(self.ctx.kb().check(self.root), [])

    def test_health_route_change_needs_exactly_the_e2e_tests_that_call_it(self):
        m = self.mutant_at('return 200, {"status": "ok"}, {}')
        # Without the knowledge base, App.handle is one node: every integration test that calls
        # handle() looks affected, none of them requests /healthz, and no e2e test is found.
        plain = strategies.cpg_static(self.ctx, m)
        self.assertTrue(plain)
        self.assertEqual({self.ctx.stage[t] for t in plain}, {"integration"})
        # With it, the change is one route, exercised only by the two e2e tests that request it.
        selected = {t.rsplit(".", 1)[1] for t in strategies.cpg_kb_boundary(self.ctx, m)}
        self.assertEqual(selected, {"test_01_health", "test_05_handler_crash_is_isolated"})

    def test_change_outside_the_boundary_never_pays_for_e2e(self):
        line = line_of(self.root, "sut/ratelimit.py", "def allow")
        m = {"id": "X", "file": "sut/ratelimit.py", "lineno": line + 1, "end_lineno": line + 1}
        stages = {self.ctx.stage[t] for t in strategies.cpg_kb_boundary(self.ctx, m)}
        self.assertTrue(stages)
        self.assertNotIn("e2e", stages)


if __name__ == "__main__":
    unittest.main()
