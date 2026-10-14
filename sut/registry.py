"""Function registry: validation and versioned CRUD for function specs."""

import re
from dataclasses import asdict, dataclass

from sut.handlers import HANDLERS

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
RUNTIMES = frozenset({"python3.12", "go1.22", "node20"})
MIN_MEMORY_MB = 64
MAX_MEMORY_MB = 2048
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 300
MAX_REPLICAS_LIMIT = 50


class ValidationError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class NotFound(KeyError):
    pass


class Conflict(Exception):
    pass


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    runtime: str
    handler: str
    memory_mb: int = 128
    timeout_s: int = 30
    min_replicas: int = 0
    max_replicas: int = 5
    target_concurrency: int = 10

    def to_dict(self):
        return asdict(self)


_INT_FIELDS = ("memory_mb", "timeout_s", "min_replicas", "max_replicas", "target_concurrency")
_STR_FIELDS = ("name", "runtime", "handler")


def spec_from_dict(data):
    """Build a FunctionSpec from untrusted JSON, rejecting wrong types."""
    if not isinstance(data, dict):
        raise ValidationError(["spec must be a JSON object"])
    errors = []
    for key in _STR_FIELDS:
        if key not in data:
            errors.append(f"missing field '{key}'")
        elif not isinstance(data[key], str):
            errors.append(f"'{key}' must be a string")
    for key in _INT_FIELDS:
        if key in data and (isinstance(data[key], bool) or not isinstance(data[key], int)):
            errors.append(f"'{key}' must be an integer")
    unknown = set(data) - set(_STR_FIELDS) - set(_INT_FIELDS)
    if unknown:
        errors.append("unknown fields: " + ", ".join(sorted(unknown)))
    if errors:
        raise ValidationError(errors)
    return FunctionSpec(**data)


def validate(spec):
    """Return a list of human-readable validation errors (empty if valid)."""
    errors = []
    if not NAME_RE.match(spec.name):
        errors.append("name must be 3-32 chars of a-z, 0-9 and '-', starting with a letter")
    if spec.runtime not in RUNTIMES:
        errors.append(f"unsupported runtime '{spec.runtime}'")
    if spec.handler not in HANDLERS:
        errors.append(f"unknown handler '{spec.handler}'")
    if spec.memory_mb < MIN_MEMORY_MB or spec.memory_mb > MAX_MEMORY_MB:
        errors.append(f"memory_mb must be between {MIN_MEMORY_MB} and {MAX_MEMORY_MB}")
    elif spec.memory_mb % 64 != 0:
        errors.append("memory_mb must be a multiple of 64")
    if spec.timeout_s < MIN_TIMEOUT_S or spec.timeout_s > MAX_TIMEOUT_S:
        errors.append(f"timeout_s must be between {MIN_TIMEOUT_S} and {MAX_TIMEOUT_S}")
    if spec.min_replicas < 0:
        errors.append("min_replicas must be >= 0")
    if spec.max_replicas < 1 or spec.max_replicas > MAX_REPLICAS_LIMIT:
        errors.append(f"max_replicas must be between 1 and {MAX_REPLICAS_LIMIT}")
    if spec.min_replicas > spec.max_replicas:
        errors.append("min_replicas must not exceed max_replicas")
    if spec.target_concurrency < 1:
        errors.append("target_concurrency must be >= 1")
    return errors


class Registry:
    def __init__(self):
        self._items = {}

    def create(self, spec):
        errors = validate(spec)
        if errors:
            raise ValidationError(errors)
        if spec.name in self._items:
            raise Conflict(spec.name)
        self._items[spec.name] = (spec, 1)
        return 1

    def update(self, spec):
        errors = validate(spec)
        if errors:
            raise ValidationError(errors)
        if spec.name not in self._items:
            raise NotFound(spec.name)
        _, version = self._items[spec.name]
        self._items[spec.name] = (spec, version + 1)
        return version + 1

    def get(self, name):
        try:
            return self._items[name]
        except KeyError:
            raise NotFound(name) from None

    def delete(self, name):
        if name not in self._items:
            raise NotFound(name)
        del self._items[name]

    def names(self):
        return sorted(self._items)

    def __len__(self):
        return len(self._items)
