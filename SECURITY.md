# Security Policy

## Reporting a vulnerability

Please report security issues **privately** by email to
**muditsarda23@gmail.com**.

Do not open a public GitHub issue for anything you believe has security
impact. Include a description of the issue, the steps to reproduce it, and
the affected version. You will get a response as soon as possible.

## Scope

Reverie is a local-first tool. The security-relevant surfaces are:

- **`~/.ec/ec.db`** — a single SQLite file holding your engineering
  conclusions. It never leaves your machine by itself. Treat it as private:
  it may contain conclusions drawn from your proprietary code.
- **LLM calls** — extraction, diffusion, review-gate and mode-detection calls
  go to the configured endpoint (hosted by default, or a local model through
  Ollama). See *Data and privacy* in the [README](README.md) for exactly what
  is sent.
- **API keys** — read from environment variables only (never written to disk
  by Reverie). Configure them in your agent or shell; do not paste them into
  issues or pull requests.

## Supported versions

Security fixes are made against the latest release.
