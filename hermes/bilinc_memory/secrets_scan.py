"""Local secret scan for memory versions before they leave the machine.

`scan(text)` returns the NAMES of the rules that matched, never the matched
text. A version with any hit is skipped whole (never partly redacted). Any
internal error propagates, so the caller fails closed.
"""

import math
import re

_RULES = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b")),
    (
        "aws_secret_key",
        re.compile(r"(?i)aws[_-]?secret[_-]?access[_-]?key[\"']?\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}"),
    ),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("slack_token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}|\bxapp-[A-Za-z0-9-]{10,}")),
    ("stripe_live_key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}")),
    ("openai_anthropic_key", re.compile(r"\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}")),
    ("bilinc_key", re.compile(r"\bbil_(?:live|test)_[A-Za-z0-9_-]{8,}")),
    ("private_key_pem", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
)

_CREDENTIAL_ASSIGNMENT = re.compile(
    r"""(?i)(?:password|passwd|secret|token|api[_-]?key)["']?\s*(?:=\s*["']?|:\s*["'])([^\s"'`<>]{12,})"""
)
_ENTROPY_ASSIGNMENT = re.compile(r"""[A-Za-z0-9_.-]{1,64}["']?\s*[=:]\s*["']?([A-Za-z0-9+/_=-]{40,})""")
_ENTROPY_MIN = 4.2


def _entropy(value):
    counts = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    total = float(len(value))
    return -sum((n / total) * math.log(n / total, 2) for n in counts.values())


def _is_placeholder(value):
    return value.startswith(("$", "{", "%", "*")) or len(set(value)) <= 3


def _high_entropy(value):
    return (
        any(c.islower() for c in value)
        and any(c.isupper() for c in value)
        and any(c.isdigit() for c in value)
        and _entropy(value) >= _ENTROPY_MIN
    )


def scan(text):
    """Return the unique rule names that match `text`. Raises on a non-str input."""
    if not isinstance(text, str):
        raise TypeError("scan() needs text")
    hits = []
    for name, pattern in _RULES:
        if pattern.search(text):
            hits.append(name)
    if any(not _is_placeholder(m.group(1)) for m in _CREDENTIAL_ASSIGNMENT.finditer(text)):
        hits.append("credential_assignment")
    if any(_high_entropy(m.group(1)) for m in _ENTROPY_ASSIGNMENT.finditer(text)):
        hits.append("high_entropy_assignment")
    return hits
