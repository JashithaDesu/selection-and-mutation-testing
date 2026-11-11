# Architecture knowledge base

`architecture.json` describes the system under test at the level of **Application
Components** (the ArchiMate term): what a code graph cannot see on its own.
`testchain/kb.py` reads it, and `python -m testchain kb` checks it against the code and
exports it, joined with the code property graph, as `results/knowledge_graph.json`.

## What the model contains

| Section | Meaning | Used for |
|---|---|---|
| `components[].artifacts` | Files that realise the component | Mapping a change to a component |
| `components[].depends_on` | Declared dependencies between components | Component-level selection; drift check |
| `components[].boundary` | The component is the system's external interface, observable only by e2e tests | Deciding *when* a change needs e2e |
| `interface.dispatcher` | The function that routes requests, and which parameters hold method and path | Splitting it into one graph node per API route |
| `interface.http_clients` | Test-side functions that send a request | Linking a test to the routes it calls |
| `interface.passthrough` | Server code that forwards any request | Not treating it as "calls every route" |
| `processes.launchers` | Calls that start a project module in a new process | Following an e2e test into the server |
| `framework_callbacks` | Framework methods called implicitly (`do_GET` after `serve_forever`) | Calls no code shows explicitly |

## What is mined, not written by hand

- **API routes**: from the `if` branches of the dispatcher that test the method or path, with
  regex paths taken from module constants. A route covers the branch body only; the condition
  runs for every request and stays with the dispatcher.
- **Test to route links**: from literal arguments of client calls, including f-strings
  (`f"/functions/{name}"` is matched against the route regex).
- **Test to component links**: from imports, and from the module a test starts as a process.

## Drift check

An architecture model that disagrees with the code is worse than none, because selection
trusts it. `python -m testchain kb` exits non-zero and lists problems when:

- a source file belongs to no component, or an artifact no longer exists;
- a component imports another that it does not declare as a dependency, or declares one it
  does not use.

The CI job for the harness tests also runs this check against the real model.

## Knowledge graph export

`results/knowledge_graph.json` has typed nodes (`ApplicationComponent`, `Artifact`, `Endpoint`,
`Test`) and edges (`realized_by`, `depends_on`, `exposes`, `linked_to`, `exercises`), plus
statistics of the code property graph with and without the knowledge base. It can be loaded
into a graph database or visualised as it is.
