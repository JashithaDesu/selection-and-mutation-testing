"""AST-based mutation engine.

Each mutant is a single, small, syntactically valid change to one source file,
standing in for a buggy commit. Mutants are applied by replacing only the exact
source segment of one AST node, so the rest of the file (formatting, comments,
line numbers) is untouched and the resulting diff looks like a real one-line
change.

Operators (classic Offutt/Ammann set, trimmed to the useful ones):
  AOR  arithmetic operator replacement      a + b  -> a - b
  ROR  relational operator replacement      a < b  -> a <= b,  == <-> !=
  LCR  logical connector replacement        and <-> or
  UOD  unary 'not' deletion                 not x  -> x
  CRP  constant replacement                 3 -> 4,  True <-> False
  RVR  return value replacement             return x -> return None
  ASR  augmented assignment replacement     x += 1 -> x -= 1
"""

import ast
import copy
import difflib
from dataclasses import asdict, dataclass
from pathlib import Path

AOR = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
       ast.FloorDiv: ast.Div, ast.Mod: ast.FloorDiv}
ROR = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
       ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In,
       ast.Is: ast.IsNot, ast.IsNot: ast.Is}
LCR = {ast.And: ast.Or, ast.Or: ast.And}
SKIP_MARKER = "pragma: no mutate"
SYMBOL = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//",
          ast.Mod: "%", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
          ast.Eq: "==", ast.NotEq: "!=", ast.In: "in", ast.NotIn: "not in",
          ast.Is: "is", ast.IsNot: "is not", ast.And: "and", ast.Or: "or"}


@dataclass
class Mutant:
    id: str
    file: str            # relative path, forward slashes
    lineno: int
    end_lineno: int
    start: int           # byte offset of replaced segment
    end: int
    operator: str
    original: str
    replacement: str

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Mutant(**d)

    def apply(self, source_bytes):
        return source_bytes[:self.start] + self.replacement.encode() + source_bytes[self.end:]

    def diff(self, source_bytes, context=3):
        before = source_bytes.decode().splitlines(keepends=True)
        after = self.apply(source_bytes).decode().splitlines(keepends=True)
        return "".join(difflib.unified_diff(before, after, f"a/{self.file}", f"b/{self.file}", n=context))


def _line_offsets(src):
    offsets, pos = [0], 0
    for line in src.splitlines(keepends=True):
        pos += len(line)
        offsets.append(pos)
    return offsets


def _skip_regions(tree):
    """Nodes inside f-strings, docstrings or `if __name__ == "__main__":` are not mutated."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(n) for n in ast.walk(node))
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"):
            skip.update(id(n) for n in ast.walk(node))
    return skip


def _candidates(tree):
    """Yield (node, operator_name, replacement_node_or_text)."""
    skip = _skip_regions(tree)
    for node in ast.walk(tree):
        if id(node) in skip or not hasattr(node, "lineno"):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in AOR:
            new = copy.deepcopy(node)
            new.op = AOR[type(node.op)]()
            yield node, "AOR", new
        elif isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in ROR:
                    new = copy.deepcopy(node)
                    new.ops[i] = ROR[type(op)]()
                    yield node, "ROR", new
        elif isinstance(node, ast.BoolOp):
            new = copy.deepcopy(node)
            new.op = LCR[type(node.op)]()
            yield node, "LCR", new
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield node, "UOD", node.operand
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                yield node, "CRP", repr(not node.value)
            elif isinstance(node.value, int):
                yield node, "CRP", repr(node.value + 1)
        elif isinstance(node, ast.Return) and node.value is not None:
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                yield node.value, "RVR", "None"
        elif isinstance(node, ast.AugAssign) and type(node.op) in (ast.Add, ast.Sub):
            new = copy.deepcopy(node)
            new.op = ast.Sub() if isinstance(node.op, ast.Add) else ast.Add()
            yield node, "ASR", new


def _is_docstring_parent(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def mutants_for_source(rel_path, src_bytes, start_index=1):
    text = src_bytes.decode()
    tree = ast.parse(text)
    lines = text.splitlines()
    # ast col offsets are UTF-8 byte offsets; build byte line offsets.
    offsets = _line_offsets(src_bytes)
    docstrings = _is_docstring_parent(tree)
    out, seen = [], set()
    idx = start_index
    for node, op, repl in _candidates(tree):
        if id(node) in docstrings:
            continue
        if any(SKIP_MARKER in lines[ln - 1] for ln in range(node.lineno, node.end_lineno + 1)):
            continue
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        original = src_bytes[start:end].decode()
        if op == "UOD":
            replacement = _segment(repl, src_bytes, offsets)
            if _needs_parens(op, repl):
                replacement = f"({replacement})"
        else:
            replacement = None
            if original.isascii():
                replacement = _token_swap(node, repl, src_bytes, offsets, start)
            if replacement is None:
                replacement = repl if isinstance(repl, str) else ast.unparse(repl)
            if op == "LCR":
                replacement = f"({replacement})"
        key = (start, end, replacement)
        if replacement == original or key in seen:
            continue
        seen.add(key)
        m = Mutant(id=f"M{idx:04d}", file=rel_path, lineno=node.lineno, end_lineno=node.end_lineno,
                   start=start, end=end, operator=op, original=original, replacement=replacement)
        try:
            ast.parse(m.apply(src_bytes).decode())
        except SyntaxError:
            continue
        out.append(m)
        idx += 1
    return out


def _span(node, offsets):
    return (offsets[node.lineno - 1] + node.col_offset,
            offsets[node.end_lineno - 1] + node.end_col_offset)


def _segment(node, src_bytes, offsets):
    s, e = _span(node, offsets)
    return src_bytes[s:e].decode()


def _token_swap(node, new, src_bytes, offsets, start):
    """Swap only the operator token(s) in the original text, keeping the
    author's formatting, so the mutant's diff is a natural one-token edit."""
    if not isinstance(new, ast.AST):
        return None
    if isinstance(node, ast.BinOp):
        pairs = [(node.left, node.right, node.op, new.op)]
    elif isinstance(node, ast.Compare):
        operands = [node.left] + node.comparators
        pairs = [(operands[i], operands[i + 1], node.ops[i], new.ops[i]) for i in range(len(node.ops))]
    elif isinstance(node, ast.BoolOp):
        pairs = [(node.values[i], node.values[i + 1], node.op, new.op) for i in range(len(node.values) - 1)]
    elif isinstance(node, ast.AugAssign):
        s = _span(node.target, offsets)[1]
        e = _span(node.value, offsets)[0]
        region = src_bytes[s:e].decode()
        old_sym, new_sym = SYMBOL[type(node.op)] + "=", SYMBOL[type(new.op)] + "="
        if region.count(old_sym) != 1:
            return None
        text = bytearray(src_bytes[start:_span(node, offsets)[1]])
        rs = s - start
        text[rs:rs + len(region)] = region.replace(old_sym, new_sym).encode()
        return text.decode()
    else:
        return None
    text = src_bytes[start:_span(node, offsets)[1]].decode()
    edits = []
    for left, right, old_op, new_op in pairs:
        if type(old_op) is type(new_op):
            continue
        ls, rs = _span(left, offsets)[1] - start, _span(right, offsets)[0] - start
        region = src_bytes[start + ls:start + rs].decode()
        old_sym, new_sym = SYMBOL[type(old_op)], SYMBOL[type(new_op)]
        # Operator token is the region minus brackets/whitespace around it.
        core = region.strip().strip("()").strip()
        if core != old_sym:
            return None
        edits.append((ls, rs, region.replace(old_sym, new_sym, 1)))
    for ls, rs, rep in sorted(edits, reverse=True):
        text = text[:ls] + rep + text[rs:]
    return text


def _needs_parens(op, node):
    # AOR/ROR keep the operator's precedence class, so no parentheses are
    # needed. and<->or changes precedence, and deleting `not` can expose a
    # lower-precedence operand, so those are wrapped to keep the parse tree
    # identical apart from the intended change.
    if op == "LCR":
        return True
    if op == "UOD":
        return isinstance(node, (ast.BoolOp, ast.Compare, ast.BinOp, ast.IfExp))
    return False


def generate(project_root, source_dir="sut"):
    root = Path(project_root)
    mutants = []
    for path in sorted((root / source_dir).rglob("*.py")):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(root).as_posix()
        mutants.extend(mutants_for_source(rel, path.read_bytes(), start_index=len(mutants) + 1))
    return mutants
