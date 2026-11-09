"""In-process fake of the Bilinc Cloud memory endpoints used by the plugin.

Pass an instance as `client=` to `cloud.push`, `cloud.pull_plan`, ...: it only needs
`.call(path, payload, idempotency_key=None) -> dict`. Errors are raised as the same
`CloudError` the real HTTP client raises.

Supported: commit (ifAbsent -> memory_exists), revise (expectedVersion ->
version_conflict, memory_not_found), list (prefix, limit <= 100, cursor paging,
`entries` + `nextCursor`), history, Idempotency-Key replay with a
manually expirable TTL, a `secret_redacted` injection hook, failure injection and
a lost-response mode. No network, no clock, deterministic version ids.
"""

import copy
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bilinc_memory.http import CloudError  # noqa: E402

REDACTED = "[REDACTED]"
STATUS = {
    "memory_exists": 409,
    "version_conflict": 409,
    "memory_not_found": 404,
    "idempotency_conflict": 409,
    "invalid_request": 400,
    "secret_detected": 422,
}


def _json_clone(value):
    return json.loads(json.dumps(value))


class FakeCloud:
    def __init__(self, camel=False):
        self.store = {}  # key -> {"versions": [ {version, value, source, at, op} ]}
        self.calls = []  # [(path, payload, idempotency_key)]
        self.idempotency = {}  # idem key -> (payload fingerprint, response)
        # Real shapes (client.py / runtime.py): write responses carry `entry_version`,
        # list entries carry `entryVersion`, warnings are [{code, rule, field}].
        # camel=True makes write responses use `entryVersion` too (both are accepted).
        self.camel = camel
        self.redact_when = None  # callable(key, value) -> rule name | None
        self.reject_when = None  # callable(key, value) -> True => secret_detected
        self.fail_next = []  # [(path_suffix or None, CloudError)]
        self.lose_response_next = 0  # apply the write, then raise a network error
        self.clock = 0
        self.page_size_cap = 100

    # -- helpers ---------------------------------------------------------

    def _tick(self):
        self.clock += 1
        return "2026-11-09T10:%02d:%02dZ" % (self.clock // 60 % 60, self.clock % 60)

    def _err(self, code, message=""):
        return CloudError(code, STATUS.get(code, 400), False, message or code)

    def _public(self, key, with_value=True):
        rec = self.store[key]["versions"][-1]
        out = {
            "key": key,
            "memoryType": "semantic",
            "importance": 0.5,
            "source": rec["source"],
            "updatedAt": rec["at"],
            "createdAt": self.store[key]["versions"][0]["at"],
        }
        out["entryVersion"] = rec["version"]
        if with_value:
            out["value"] = copy.deepcopy(rec["value"])
        return out

    def _write(self, key, value, source, op):
        value = _json_clone(value)
        warnings = []
        if self.reject_when and self.reject_when(key, value):
            raise self._err("secret_detected", "rejected")
        if self.redact_when:
            rule = self.redact_when(key, value)
            if rule:
                value["content"] = REDACTED
                warnings.append({"code": "secret_redacted", "rule": rule, "field": "value"})
        history = self.store.setdefault(key, {"versions": []})["versions"]
        number = len(history) + 1
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:8]
        record = {"version": "v%d_%s" % (number, digest), "value": value, "source": source, "at": self._tick(), "op": op}
        history.append(record)
        response = {"success": True, "key": key}
        response["entryVersion" if self.camel else "entry_version"] = record["version"]
        if warnings:
            response["warnings"] = warnings
        return response

    # -- direct manipulation for tests ---------------------------------------

    def put_raw(self, key, value, source="claude-code-plugin"):
        """Write as another client would (bypasses CAS and idempotency)."""
        return self._write(key, value, source, "raw")

    def expire_idempotency(self):
        self.idempotency.clear()

    def keys(self, prefix=""):
        return sorted(k for k in self.store if k.startswith(prefix))

    def head(self, key):
        return copy.deepcopy(self.store[key]["versions"][-1])

    def writes(self):
        return [c for c in self.calls if c[0].endswith(("/commit", "/revise"))]

    # -- the endpoint surface ------------------------------------------------

    def call(self, path, payload, idempotency_key=None):
        payload = _json_clone(payload)
        self.calls.append((path, payload, idempotency_key))
        for i, (suffix, error) in enumerate(self.fail_next):
            if suffix is None or path.endswith(suffix):
                del self.fail_next[i]
                raise error
        name = path.rsplit("/", 1)[-1]
        handler = getattr(self, "_h_" + name, None)
        if handler is None:
            raise self._err("invalid_request", "unknown endpoint")
        if name in ("commit", "revise"):
            return self._idempotent(handler, payload, idempotency_key)
        return handler(payload)

    def _idempotent(self, handler, payload, idem):
        fingerprint = json.dumps(payload, sort_keys=True)
        if idem and idem in self.idempotency:
            seen, response = self.idempotency[idem]
            if seen != fingerprint:
                raise self._err("idempotency_conflict")
            return copy.deepcopy(response)
        response = handler(payload)
        if idem:
            self.idempotency[idem] = (fingerprint, copy.deepcopy(response))
        if self.lose_response_next:
            self.lose_response_next -= 1
            raise CloudError("connection_failed", None, True, "response lost")
        return response

    def _h_commit(self, p):
        key, value = p.get("key"), p.get("value")
        if not isinstance(key, str) or not key or "value" not in p:
            raise self._err("invalid_request")
        if p.get("ifAbsent") and key in self.store:
            raise self._err("memory_exists")
        return self._write(key, value, p.get("source"), "commit")

    def _h_revise(self, p):
        key = p.get("key")
        if key not in self.store:
            raise self._err("memory_not_found")
        expected = p.get("expectedVersion")
        if expected is not None and expected != self.store[key]["versions"][-1]["version"]:
            raise self._err("version_conflict")
        previous_source = self.store[key]["versions"][-1]["source"]
        return self._write(key, p.get("value"), previous_source, "revise")

    def _h_list(self, p):
        limit = p.get("limit", 50)
        if not isinstance(limit, int) or limit < 1 or limit > self.page_size_cap:
            raise self._err("invalid_request", "limit")
        prefix = p.get("prefix") or ""
        keys = sorted(k for k in self.store if k.startswith(prefix))
        cursor = p.get("cursor")
        if cursor:
            keys = [k for k in keys if k > cursor]
        page = keys[:limit]
        values = p.get("values", "preview")
        entries = [self._public(k, with_value=values != "none") for k in page]
        out = {"entries": entries, "total": len(keys), "order": "key"}
        out["nextCursor"] = page[-1] if len(keys) > limit else None
        return out

    def _h_history(self, p):
        key = p.get("key")
        versions = self.store.get(key, {"versions": []})["versions"]
        newest_first = list(reversed(versions))[: p.get("limit", 20)]
        return {
            "key": key,
            "exists": key in self.store,
            "entries": [
                {"op": r["op"], "at": r["at"], "source": r["source"], "entryVersion": r["version"], "after": copy.deepcopy(r["value"])}
                for r in newest_first
            ],
            "truncated": len(versions) > len(newest_first),
            "values": p.get("values", "full"),
        }
