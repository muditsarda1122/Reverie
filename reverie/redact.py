"""Secret redaction, applied at write time AND again at render time.

STDLIB ONLY: hooks and the spool import this on every prompt.

Policy (settled): secrets are removed permanently, no raw copy is kept.
``KEY=value`` keeps the variable name (``KEY=[REDACTED]``) so an agent can
tell the user which variable is needed; every other secret becomes
``[REDACTED:<kind>]``. The function is idempotent, so applying it again at
render time to old, already-redacted text is a no-op and to old, unredacted
text is a cleanup.

Detection, in order: private-key blocks, ``Authorization`` headers, URL
credentials, well-known key formats, ``KEY=value`` / ``"key": "value"``
assignments (name looks secret), then a high-entropy fallback for long
mixed-class tokens. Git SHAs, UUIDs, paths and plain identifiers are left
alone: engineering memory is full of them and they are not secrets.
"""

from __future__ import annotations

import math
import re

__all__ = ["redact", "redact_with_kinds", "contains_secret", "PLACEHOLDER"]

PLACEHOLDER = "[REDACTED"          # every output marker starts with this

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.S)

_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)(['\"]?)"
    r"((?:bearer|basic|token|digest|negotiate)\s+)?([^\s'\"]{6,})")

_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)([^\s/:@'\"]+):([^\s/@'\"]{3,})@")

#: (kind, pattern) for tokens whose shape alone identifies them.
_FORMATS: list[tuple[str, re.Pattern[str]]] = [(k, re.compile(p)) for k, p in [
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    ("openai-key", r"\bsk-(?:proj-|svcacct-|live-|or-v1-)?[A-Za-z0-9_\-]{20,}"),
    ("api-key", r"\b(?:oc|ak|pk|rk)_(?:sk|live|test)_[A-Za-z0-9_\-]{12,}"),
    ("stripe-key", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    ("gitlab-token", r"\bglpat-[A-Za-z0-9_\-]{16,}"),
    ("aws-access-key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
    ("google-key", r"\bAIza[0-9A-Za-z_\-]{30,}"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    ("npm-token", r"\bnpm_[A-Za-z0-9]{30,}"),
    ("pypi-token", r"\bpypi-[A-Za-z0-9_\-]{30,}"),
    ("huggingface-token", r"\bhf_[A-Za-z0-9]{30,}"),
    ("sendgrid-key", r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]]

_SECRET_NAME = (r"[A-Za-z0-9_.\-]*(?:api[_-]?key|apikey|secret|token|passw(?:or)?d|passwd|pwd|"
                r"credential|auth[_-]?key|private[_-]?key|access[_-]?key|signing[_-]?key|"
                r"client[_-]?secret|bearer)[A-Za-z0-9_.\-]*")

# .env / shell / yaml style:  NAME=value   NAME: value   export NAME="value"
_ASSIGN = re.compile(
    r"(?im)(?P<pre>(?:^|[\s;,{(\[+>|\"'`])(?:export\s+|set\s+)?)(?P<name>" + _SECRET_NAME + r")"
    r"(?P<op>\s*[=:]\s*)(?P<q>['\"]?)(?P<val>[^\s'\",;)}\]]+)(?P=q)")

# JSON style:  "api_key": "value"
_JSON_ASSIGN = re.compile(
    r"(?i)([\"'])(?P<name>" + _SECRET_NAME + r")\1(?P<op>\s*:\s*)([\"'])(?P<val>[^\"']{4,})\4")

# prose:  "the password is Tr0ub4dor&3", "api key: 9f8e7d..."
_PROSE = re.compile(
    r"(?i)\b(?P<lead>(?:password|passphrase|passcode|api[ _-]?key|secret|token)\s+(?:is|was|=|:)\s+)"
    r"(?P<q>['\"`]?)(?P<val>[^\s'\"`,;]{6,})(?P=q)")

_ALLCAPS_ENV_LINE = re.compile(r"(?m)^(?P<mark>[+\- ]?)(?P<name>[A-Z][A-Z0-9_]{2,})=(?P<val>\S+)\s*$")

_TOKEN = re.compile(r"[A-Za-z0-9_\-+=]{24,}")
_HEX = re.compile(r"^[0-9a-fA-F]+$")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in
                (s.count(ch) for ch in set(s)))


def _looks_like_code_ref(val: str) -> bool:
    """A value that names where a secret comes from, not the secret itself."""
    if val.startswith(("$", "%", "<", "{{", "${", "@")) or val.endswith(("()", ")")):
        return True
    if val.lower() in {"true", "false", "none", "null", "nil", "undefined", "your_key",
                       "changeme", "xxx", "x", "..."}:
        return True
    if "(" in val or "[" in val or "$" in val:
        return True
    return False


def _is_identifier_path(val: str) -> bool:
    # foo, foo.bar_baz, self.api_key: code reference, not a literal secret
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z_]*(?:\.[A-Za-z_][A-Za-z_]*)*", val)) and len(val) < 40


def _mark(kinds: list[str] | None, kind: str) -> str:
    if kinds is not None:
        kinds.append(kind)
    return f"[REDACTED:{kind}]"


def redact_with_kinds(text: str) -> tuple[str, list[str]]:
    """-> (redacted text, kinds found). ``kinds`` is empty when nothing changed."""
    if not text:
        return text, []
    kinds: list[str] = []
    def pem(_m: re.Match[str]) -> str:
        return _mark(kinds, "private-key")
    out = _PEM.sub(pem, text)

    def auth(m: re.Match[str]) -> str:
        if m.group(5).startswith(PLACEHOLDER):
            return m.group(0)
        _mark(kinds, "auth-header")
        scheme = (m.group(4) or "")
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{scheme}[REDACTED:auth-header]"
    out = _AUTH_HEADER.sub(auth, out)

    def urlcred(m: re.Match[str]) -> str:
        if m.group(3).startswith(PLACEHOLDER):
            return m.group(0)
        _mark(kinds, "url-credential")
        return f"{m.group(1)}{m.group(2)}:[REDACTED:url-credential]@"
    out = _URL_CRED.sub(urlcred, out)

    def fmt_sub(kind: str, pat: re.Pattern[str], s: str) -> str:
        def one(_m: re.Match[str]) -> str:
            return _mark(kinds, kind)
        return pat.sub(one, s)

    for kind, pat in _FORMATS:
        out = fmt_sub(kind, pat, out)

    def assign(m: re.Match[str]) -> str:
        val = m.group("val")
        if val.startswith(PLACEHOLDER) or _looks_like_code_ref(val):
            return m.group(0)
        spaced = bool(re.search(r"\s", m.group("op")))
        if _is_identifier_path(val) and not m.group("q"):
            return m.group(0)
        if spaced and not m.group("q") and not (len(val) >= 12 and re.search(r"\d", val)):
            return m.group(0)               # `token: refresh` prose, not a secret
        if len(val) < 4 and not m.group("q"):
            return m.group(0)
        kinds.append("assignment")
        q = m.group("q")
        return f"{m.group('pre')}{m.group('name')}{m.group('op')}{q}[REDACTED]{q}"
    out = _ASSIGN.sub(assign, out)

    def prose(m: re.Match[str]) -> str:
        val = m.group("val")
        if val.startswith(PLACEHOLDER) or _looks_like_code_ref(val):
            return m.group(0)
        if not re.search(r"[\d\W_]", val.rstrip(".!?")):    # plain word: prose, not a secret
            return m.group(0)
        kinds.append("assignment")
        return f"{m.group('lead')}{m.group('q')}[REDACTED]{m.group('q')}"
    out = _PROSE.sub(prose, out)

    def jassign(m: re.Match[str]) -> str:
        val = m.group("val")
        if val.startswith(PLACEHOLDER) or _looks_like_code_ref(val):
            return m.group(0)
        kinds.append("assignment")
        q = m.group(1)
        return f"{q}{m.group('name')}{q}{m.group('op')}{m.group(4)}[REDACTED]{m.group(4)}"
    out = _JSON_ASSIGN.sub(jassign, out)

    def envline(m: re.Match[str]) -> str:
        val = m.group("val")
        if val.startswith(PLACEHOLDER) or _looks_like_code_ref(val):
            return m.group(0)
        name = m.group("name")
        # URLs and DSNs often hold credentials; plain config (PORT=8080) stays
        if re.search(r"(?:KEY|SECRET|TOKEN|PASS|PWD|CRED|AUTH|DSN)", name):
            kinds.append("assignment")
            return f"{m.group('mark')}{name}=[REDACTED]"
        return m.group(0)
    out = _ALLCAPS_ENV_LINE.sub(envline, out)

    def entropic(m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok.startswith(PLACEHOLDER) or "REDACTED" in tok:
            return tok
        if _HEX.match(tok) and len(tok) in (32, 40, 56, 64, 96, 128):
            return tok                      # commit SHAs and digests
        if not (re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok)):
            return tok
        if not (re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok)) and not _HEX.match(tok):
            return tok
        if _entropy(tok) < 4.0:
            return tok
        return _mark(kinds, "high-entropy")
    out = _TOKEN.sub(entropic, out)
    return out, kinds


def redact(text: str) -> str:
    return redact_with_kinds(text)[0]


def contains_secret(text: str) -> bool:
    return bool(redact_with_kinds(text)[1])
