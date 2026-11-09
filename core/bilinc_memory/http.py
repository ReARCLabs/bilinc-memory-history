"""Stdlib HTTP transport for Bilinc Cloud, plus API key and base URL lookup.

Nothing here prints or logs request headers: the API key must never reach a
log line, an exception message or a status report.
"""

import functools
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://bilinc.space"
CONFIG_DIR_ENV = "BILINC_CONFIG_DIR"
CONFIG_FILE_NAME = "config.json"
# The userConfig sensitive value is exported to hooks and scripts under this
# name by Claude Code; BILINC_API_KEY is the generic fallback.
API_KEY_ENVS = ("CLAUDE_PLUGIN_OPTION_API_KEY", "BILINC_API_KEY")
SYSTEM_CA_FILES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt")

RETRYABLE_CODES = frozenset(
    {"rate_limited", "cloud_runtime_unavailable", "capability_unavailable", "connection_failed", "timeout"}
)
_MAX_MESSAGE = 200
_MAX_RESPONSE = 16 * 1024 * 1024


class CloudError(Exception):
    """A Cloud request failed. `code` is the canonical Bilinc error code."""

    def __init__(self, code, status=None, retryable=False, message=""):
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.retryable = bool(retryable)
        self.message = message or code

    def __str__(self):
        return "%s%s: %s" % (self.code, " (HTTP %s)" % self.status if self.status else "", self.message)


# --- configuration ---------------------------------------------------------

def config_path():
    """Path of the CLI config written by `bilinc login` (SDK `config_path`)."""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser() / CONFIG_FILE_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "bilinc" / CONFIG_FILE_NAME
    return Path.home() / ".config" / "bilinc" / CONFIG_FILE_NAME


def _load_config():
    try:
        payload = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def api_key():
    """Resolve the API key: plugin userConfig env, BILINC_API_KEY, then the saved config."""
    for name in API_KEY_ENVS:
        value = os.environ.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = _load_config().get("api_key")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def base_url():
    value = os.environ.get("BILINC_BASE_URL")
    return (value.strip() if value and value.strip() else DEFAULT_BASE_URL).rstrip("/")


# --- TLS -------------------------------------------------------------------

def ca_candidates():
    """CA bundle paths in preference order; None stands for the system default.

    Order: explicit override (BILINC_CA_BUNDLE / SSL_CERT_FILE), certifi if it
    is importable, /etc/ssl/cert.pem, /etc/ssl/certs/ca-certificates.crt, then
    the interpreter's default. macOS python.org builds often ship no CA file,
    which is why the fallbacks exist.
    """
    out = []
    override = os.environ.get("BILINC_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if override:
        out.append(override)
    try:
        import certifi  # optional

        path = certifi.where()
        if path:
            out.append(path)
    except Exception:  # ImportError or a broken certifi
        pass
    for path in SYSTEM_CA_FILES:
        if os.path.isfile(path):
            out.append(path)
    out.append(None)
    return out


@functools.lru_cache(maxsize=8)
def _context_for(ca_path):
    if ca_path:
        return ssl.create_default_context(cafile=ca_path)
    return ssl.create_default_context()


def ssl_context():
    """First usable TLS context from `ca_candidates()`, cached per CA path."""
    last = None
    for path in ca_candidates():
        try:
            return _context_for(path)
        except (ssl.SSLError, OSError) as exc:
            last = exc
    raise CloudError("tls_unavailable", None, False, "No usable CA bundle for TLS (%s)" % type(last).__name__)


# --- transport -------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward the bearer token to another location


def _check_url(url):
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
        return
    raise CloudError("invalid_request", None, False, "Refusing to send the API key over a non-https URL")


def _error_code(payload):
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, str):
        return err
    if isinstance(err, dict) and isinstance(err.get("code"), str):
        return err["code"]
    if isinstance(payload.get("code"), str):
        return payload["code"]
    return None


def _error_message(payload, status):
    msg = None
    if isinstance(payload, dict):
        msg = payload.get("message")
        err = payload.get("error")
        if not isinstance(msg, str) and isinstance(err, dict):
            msg = err.get("message")
    if not isinstance(msg, str) or not msg:
        msg = "HTTP %s" % status
    return "".join(ch for ch in msg if ch.isprintable())[:_MAX_MESSAGE]


def request(method, url, headers, body, timeout):
    """Send one request and return the decoded JSON object. Raises CloudError."""
    _check_url(url)
    if isinstance(body, (dict, list)):
        body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    handlers = [_NoRedirect()]
    if url.lower().startswith("https:"):
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context()))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(_MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(_MAX_RESPONSE)
            payload = json.loads(detail.decode("utf-8", "replace")) if detail else {}
        except (ValueError, OSError):
            payload = {}
        code = _error_code(payload) or ("http_%d" % exc.code)
        retryable = payload.get("retryable") if isinstance(payload, dict) else None
        if not isinstance(retryable, bool):
            retryable = exc.code in (429, 503) or exc.code >= 500 or code in RETRYABLE_CODES
        raise CloudError(code, exc.code, retryable, _error_message(payload, exc.code)) from None
    except (socket.timeout, TimeoutError):
        raise CloudError("timeout", None, True, "Bilinc Cloud request timed out") from None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            raise CloudError("timeout", None, True, "Bilinc Cloud request timed out") from None
        reason = getattr(exc, "reason", None)
        raise CloudError(
            "connection_failed", None, True, "Bilinc Cloud unreachable (%s)" % type(reason).__name__
        ) from None
    except (OSError, ssl.SSLError) as exc:
        raise CloudError("connection_failed", None, True, "Bilinc Cloud unreachable (%s)" % type(exc).__name__) from None
    if len(raw) > _MAX_RESPONSE:
        raise CloudError("invalid_response", None, False, "Response too large")
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise CloudError("invalid_response", None, False, "Bilinc Cloud returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise CloudError("invalid_response", None, False, "Unexpected response shape")
    return payload
