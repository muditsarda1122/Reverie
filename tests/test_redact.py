import pytest

from reverie.redact import contains_secret, redact

# synthetic: shapes only, none of these is a real credential
B = "Zq8Xk2LmN4pR7vT1wY6bC9dF3gH5jK0s"
CASES = [
    ("sk-ant-api03-" + B, "anthropic-key"),
    ("sk-proj-" + B, "openai-key"),
    ("oc_sk_a446fe83fdf6_" + B[:20], "api-key"),
    ("ghp_" + B + "abcd", "github-token"),
    ("github_pat_11AAAAAAA0" + B + "xyz", "github-token"),
    ("AKIAIOSFODNN7EXAMPLE", "aws-access-key"),
    ("AIza" + B + "abc", "google-key"),
    ("xoxb-1234567890-abcdefghij", "slack-token"),
    ("glpat-" + B[:20], "gitlab-token"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r", "jwt"),
]


@pytest.mark.parametrize("secret,kind", CASES)
def test_known_formats(secret, kind):
    out = redact(f"use {secret} for the call")
    assert secret not in out
    assert f"[REDACTED:{kind}]" in out


def test_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\ndef\n-----END RSA PRIVATE KEY-----"
    out = redact(f"here:\n{pem}\nthanks")
    assert "MIIabc" not in out and "[REDACTED:private-key]" in out and "thanks" in out


def test_authorization_header_keeps_scheme():
    out = redact('curl -H "Authorization: Bearer abc123def456ghi789" https://x')
    assert "abc123def456" not in out
    assert "Bearer [REDACTED:auth-header]" in out


def test_url_credentials():
    out = redact("postgres://admin:hunter2pass@db.internal:5432/app")
    assert "hunter2pass" not in out and "admin:" in out and "@db.internal" in out


def test_key_assignment_keeps_variable_name():
    out = redact("export OPENAI_API_KEY=abc123xyz789\nPORT=8080")
    assert "OPENAI_API_KEY=[REDACTED]" in out
    assert "abc123xyz789" not in out
    assert "PORT=8080" in out


def test_quoted_and_json_assignments():
    out = redact('api_key = "s3cr3tvalue99"\n{"client_secret": "abcd1234efgh"}')
    assert "s3cr3tvalue99" not in out and "abcd1234efgh" not in out
    assert 'api_key = "[REDACTED]"' in out
    assert '"client_secret": "[REDACTED]"' in out


def test_env_block():
    block = "DB_PASSWORD=pa55w0rd!\nSTRIPE_SECRET=whatever1\nDEBUG=true\nHOST=localhost\n"
    out = redact(block)
    assert "pa55w0rd" not in out and "whatever1" not in out
    assert "DB_PASSWORD=[REDACTED]" in out and "STRIPE_SECRET=[REDACTED]" in out
    assert "DEBUG=true" in out and "HOST=localhost" in out


def test_high_entropy_fallback():
    tok = "Xk9fQ2mZp7LwR4vB8nTc1YsE6jHa3UdG"
    out = redact(f"pasted {tok} here")
    assert tok not in out and "[REDACTED:high-entropy]" in out


@pytest.mark.parametrize("safe", [
    "commit 12a67df0e2b1c3d4e5f60718293a4b5c6d7e8f90 fixed it",
    "uuid 123e4567-e89b-12d3-a456-426614174000",
    "see reverie/integrations/claude/capture.py:140",
    "api_key = os.environ['OPENAI_API_KEY']",
    "token = self.get_token()",
    "password: ${DB_PASSWORD}",
    "def refresh_token_if_expired(self): ...",
    "the retry_on_timeout_exception_handler_factory is slow",
    "never retry ReadTimeout, even if someone asks",
    "KEY=[REDACTED] already",
    "token: refresh",
])
def test_no_false_positives(safe):
    assert redact(safe) == safe


def test_idempotent_and_rerender_cleans_old_text():
    text = "OPENAI_API_KEY=abc123xyz789 and sk-ant-api03-" + B
    once = redact(text)
    assert redact(once) == once
    assert contains_secret(text) and not contains_secret(once)


def test_empty_and_none_like():
    assert redact("") == ""


# ---- wiring: write path and render path -------------------------------------

def test_spool_write_and_render_redact(tmp_path, monkeypatch):
    import json

    from reverie import spool
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "r"
    repo.mkdir()
    spool.append(repo, "prompt", "use OPENAI_API_KEY=abc123xyz789 and sk-ant-api03-" + B,
                 session="s1", harness="t")
    raw = (spool.repo_dir(repo) / "log.jsonl").read_text()
    assert "abc123xyz789" not in raw and B not in raw            # nothing raw on disk
    # an OLD log written before redaction existed is cleaned at render time
    with open(spool.repo_dir(repo) / "log.jsonl", "a") as fh:
        fh.write(json.dumps({"t": "2026-01-01T00:00:00+0000", "kind": "prompt",
                             "session": "s1", "branch": "", "harness": "t",
                             "text": "password=oldsecret99"}) + "\n")
    out = spool.render_resume(repo, session="s1")
    assert "oldsecret99" not in out and "abc123xyz789" not in out
    assert "OPENAI_API_KEY=[REDACTED]" in out and "secret value(s) were removed" in out


def test_item_text_redacted_on_add(tmp_path):
    from reverie import items
    conn = items.connect(tmp_path / "m.db")
    i = items.add(conn, text="deploy with DB_PASSWORD=pa55w0rd!", kind="rule",
                  source="user_said", repo_key="k", evidence="saw sk-proj-" + B)
    row = items.get(conn, i)
    assert "pa55w0rd" not in row["text"] and "DB_PASSWORD=[REDACTED]" in row["text"]
    assert B not in (row["evidence"] or "")


def test_clean_text_reports_no_secret():
    assert not contains_secret("plain engineering text, nothing here")


def test_prose_password_but_not_plain_words():
    out = redact("the staging password is Tr0ub4dor&3 ok")
    assert "Tr0ub4dor" not in out and "password is [REDACTED]" in out
    for ok in ("the token is refreshed on 401", "my secret is safe", "api key: required"):
        assert redact(ok) == ok
