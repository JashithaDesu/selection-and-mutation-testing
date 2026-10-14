"""Built-in function handlers.

The gateway never executes user-supplied code: a function spec names one of
these handlers. That keeps the system under test deterministic and safe while
still exercising realistic input validation and error paths.
"""


class HandlerError(ValueError):
    """Raised when the payload is invalid for the handler (maps to HTTP 400)."""


def echo(payload):
    return payload


def uppercase(payload):
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise HandlerError("'text' must be a string")
    return {"text": text.upper()}


def sum_numbers(payload):
    numbers = payload.get("numbers") if isinstance(payload, dict) else None
    if not isinstance(numbers, list) or len(numbers) == 0:
        raise HandlerError("'numbers' must be a non-empty list")
    total = 0
    for n in numbers:
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            raise HandlerError("'numbers' may only contain numbers")
        total += n
    return {"sum": total, "count": len(numbers), "mean": total / len(numbers)}


def word_count(payload):
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise HandlerError("'text' must be a string")
    counts = {}
    for word in text.lower().split():
        word = word.strip(".,;:!?\"'()")
        if word:
            counts[word] = counts.get(word, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return {"words": sum(counts.values()), "unique": len(counts), "top": top}


def always_fail(payload):
    raise RuntimeError("handler crashed")


HANDLERS = {
    "echo": echo,
    "uppercase": uppercase,
    "sum": sum_numbers,
    "wordcount": word_count,
    "fail": always_fail,
}
