# Contributing to bosscli

Thanks for your interest. This is a research/educational project around the
BOSS直聘 (`com.hpbr.bosszhipin`) Android client; before contributing, please
read the [disclaimer](README.md#disclaimer) — the maintainers do not endorse
using this code against the platform in violation of its Terms of Service.

## Development setup

```bash
git clone https://github.com/xaoLiu1222/bosscli.git
cd bosscli
pip install -e ".[dev]"     # requests, lz4, Pillow + pytest
```

## Running the tests

```bash
python -m pytest tests -q
```

The suite is fully offline and synthetic: network calls are mocked or served
from localhost, fixtures contain no real credentials, and no test needs a
phone, an account or a session token. Keep it that way — a test that requires
live credentials will not be merged.

## Ground rules

- **Never commit real secrets.** No `t2` tokens, no `secretKey`, no
  `session.local.json`, no TLS client keys, no `securityId` values. The
  repository ships only `session.example.json` with placeholders.
- **Never paste private captures into issues or PRs.** If you want to report
  a protocol detail, redact account identifiers and authorized URLs first.
- **Writes are opt-in only.** Anything that can contact a recruiter
  (`friend/add`, image upload, IM publish) must stay behind an explicit
  confirmation and must never retry automatically after an uncertain result.
- **One behavior per PR**, with tests. New protocol code needs offline tests
  (synthetic bytes / fake sockets), not "works on my account".
- Keep CLI output free of tokens and credentials, including in error paths.

## Reporting bugs

Include: the subcommand, the Python version, the traceback with any
credential-looking strings removed, and — for protocol issues — what the
official client did differently. Do **not** include your session file.
