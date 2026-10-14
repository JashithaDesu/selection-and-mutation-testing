"""LLM-based test selection.

The model sees exactly what a CI bot would see on a pull request: the diff and
a catalogue of available tests (id, stage, one-line description). It never sees
the kill matrix. Its answer is parsed strictly: test ids that do not exist are
counted as hallucinations and dropped (never silently "fixed").

Backends (stdlib HTTP only):
  ollama     local model, e.g. qwen2.5-coder:7b   (http://localhost:11434)
  anthropic  Claude API, needs ANTHROPIC_API_KEY
Responses are cached on disk keyed by (backend, model, prompt) so experiments
are reproducible and re-running the evaluation costs nothing.
"""

import ast
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

SYSTEM_PROMPT = (
    "You are a regression test selection assistant in a CI pipeline. Given a code "
    "diff and a catalogue of tests, choose the smallest set of tests that is likely "
    "to fail if the change introduced a bug. Prefer cheap unit tests when they "
    "exercise the changed behaviour, but include integration or e2e tests when the "
    "change affects behaviour only they observe. The catalogue is numbered. "
    "Decide from the diff which tests exercise the changed code; the right number "
    "of tests differs from diff to diff. "
    'Answer with JSON only, in the form {"selected": [<catalogue numbers>]}.'
    # No concrete example numbers here: small models copy them verbatim
    # (an earlier run returned the example list for 32 of 40 diffs).
)


def test_catalogue(project_root, tests):
    """[(test_id, stage, description)] using each test's docstring or name."""
    docs = {}
    root = Path(project_root)
    for path in sorted(root.glob("tests/**/test_*.py")):
        mod = ".".join(path.relative_to(root).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                doc = ast.get_docstring(fn) or ""
                docs[f"{mod}.{cls.name}.{fn.name}"] = doc.strip().splitlines()[0] if doc else ""
    out = []
    for t in tests:
        stage = t.split(".")[1]
        desc = docs.get(t) or _name_as_text(t)
        out.append((t, stage, desc))
    return out


def short_name(test_id):
    """tests.unit.test_registry.TestValidate.test_x -> test_registry.TestValidate.test_x"""
    parts = test_id.split(".")
    return ".".join(parts[2:]) if len(parts) > 2 and parts[0] == "tests" else test_id


def build_prompt(diff, catalogue):
    # Numbered entries keep the answer tiny ("[3, 17]" instead of full ids):
    # on a laptop CPU, generated tokens are the slowest part of a request.
    lines = []
    for i, (tid, stage, desc) in enumerate(catalogue, 1):
        doc = f" - {desc}" if desc and desc != _name_as_text(tid) else ""
        lines.append(f"{i}. [{stage}] {short_name(tid)}{doc}")
    return ("## Diff under review\n```diff\n" + diff.strip() + "\n```\n\n"
            "## Test catalogue (number. [stage] test)\n" + "\n".join(lines) +
            "\n\nReturn the JSON object now.")


def _name_as_text(test_id):
    return test_id.rsplit(".", 1)[1].replace("test_", "").replace("_", " ")


def parse_selection(text, known_ids):
    """Return (selected_known_ids, hallucinated_ids, parse_ok)."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return [], [], False
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return [], [], False
    raw = data.get("selected") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return [], [], False
    known = set(known_ids)
    by_suffix = {}
    for k in known_ids:
        parts = k.split(".")
        for i in range(1, len(parts)):
            by_suffix.setdefault(".".join(parts[i:]), set()).add(k)
    selected, hallucinated = [], []
    for item in raw:
        if isinstance(item, str) and item.strip().isdigit():
            item = int(item.strip())
        if isinstance(item, int) and not isinstance(item, bool):
            # catalogue number (1-based); anything outside the catalogue is invented
            if 1 <= item <= len(known_ids):
                selected.append(known_ids[item - 1])
            else:
                hallucinated.append(str(item))
            continue
        if not isinstance(item, str):
            continue
        item = item.strip()
        if item in known:
            selected.append(item)
        elif len(by_suffix.get(item, ())) == 1:   # unambiguous shortened id
            selected.append(next(iter(by_suffix[item])))
        else:
            hallucinated.append(item)
    return sorted(set(selected)), hallucinated, True


def _post_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class OllamaBackend:
    name = "ollama"

    def __init__(self, model, url=None, timeout=900):
        self.model = model
        self.url = (url or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        self.timeout = timeout

    def complete(self, prompt):
        out = _post_json(f"{self.url}/api/chat", {
            "model": self.model, "stream": False, "format": "json",
            "keep_alive": "30m",  # keep the model loaded between requests
            # num_ctx: Ollama's default context can be smaller than the prompt and
            # would silently cut off its beginning (the instructions and the diff).
            # num_predict: room for every catalogue number (~3 tokens each); a smaller
            # limit cut long answers off mid-list, leaving invalid JSON.
            "options": {"temperature": 0, "seed": 7, "num_ctx": 4096, "num_predict": 512},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": prompt}],
        }, {}, self.timeout)
        return out["message"]["content"]


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, model, timeout=120):
        self.model = model
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.timeout = timeout

    def complete(self, prompt):
        out = _post_json("https://api.anthropic.com/v1/messages", {
            "model": self.model, "max_tokens": 1024, "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }, {"x-api-key": self.key, "anthropic-version": "2023-06-01"}, self.timeout)
        return "".join(b.get("text", "") for b in out.get("content", []))


BACKENDS = {"ollama": OllamaBackend, "anthropic": AnthropicBackend}


def slug(backend, model):
    return re.sub(r"[^A-Za-z0-9.]+", "-", f"{backend}-{model}").strip("-")


def select_for_mutants(backend, mutants, diffs, catalogue, cache_dir, log=print):
    """Run the LLM once per mutant (cached). Returns {mutant_id: record}."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    known = [t for t, _, _ in catalogue]
    records = {}
    consecutive_errors = 0
    for i, m in enumerate(mutants, 1):
        prompt = build_prompt(diffs[m.id], catalogue)
        key = hashlib.sha256(f"{backend.name}|{backend.model}|{SYSTEM_PROMPT}|{prompt}".encode()).hexdigest()[:24]
        cache_file = cache_dir / f"{key}.json"
        from_cache = cache_file.exists()
        if from_cache:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            # An answer that cannot be parsed (e.g. cut off by an earlier, smaller
            # length limit) is not a result worth keeping: ask again.
            if not parse_selection(cached["response"], known)[2]:
                from_cache = False
        if not from_cache:
            t0 = time.perf_counter()
            try:
                text = backend.complete(prompt)
                error = None
            except Exception as exc:  # network/model errors are data, not crashes
                text, error = "", f"{type(exc).__name__}: {exc}"
            cached = {"response": text, "latency_s": time.perf_counter() - t0, "error": error}
            if error is None:
                cache_file.write_text(json.dumps(cached), encoding="utf-8")
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise RuntimeError(f"LLM backend failing repeatedly, last error: {error}")
        selected, hallucinated, ok = parse_selection(cached["response"], known)
        records[m.id] = {"selected": selected, "hallucinated": hallucinated, "parse_ok": ok,
                         "latency_s": cached["latency_s"], "error": cached.get("error")}
        log(f"  {i}/{len(mutants)} {m.id}: {len(selected)} tests selected, "
            f"{cached['latency_s']:.1f}s{' (cached)' if from_cache else ''}"
            f"{'  ERROR ' + cached['error'] if cached.get('error') else ''}")
    return records
