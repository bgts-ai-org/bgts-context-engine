# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

While the project is pre-1.0, only the latest minor release receives security fixes.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through either channel:

- [GitHub private vulnerability reporting](https://github.com/bilgeadamtechnology/BGTS-Context-Engine/security/advisories/new) (preferred)
- Email **security@bilgeadam.com**

Please include the affected version, what an attacker can achieve, and the steps to
reproduce it. A minimal proof of concept helps a great deal.

We aim to acknowledge a report within three business days and to share an assessment and
a remediation plan within ten. We will keep you updated while a fix is prepared and will
credit you in the advisory unless you prefer otherwise.

## Deployment notes worth knowing

These are documented behaviours rather than vulnerabilities, but they affect how safely
an instance can be exposed:

- **Only the Layer-3 endpoints apply the scope filter.** Layer 1, Layer 2, the indexing
  and job endpoints, and everything under `/v1/ui/*` do not. Anyone who can reach the port
  can read the indexed graph, including symbol bodies. Put the engine behind a reverse
  proxy that handles authentication before exposing it beyond a trusted network.
- **`X-BCE-User` is trusted as given.** The header names the user whose rows in the
  `scopes` table decide which repositories a Layer-3 answer may include; the header itself
  is not verified. Whatever sits in front of the engine is responsible for authenticating
  the user and for setting or overwriting this header.
- **The row-level security policies are not the enforcement path.** Migration
  `0004_rls.sql` defines policies on `repos` and `embeddings` keyed on the
  `bce.user_id` session variable, but the application never sets that variable. Scoping is
  enforced in application code (`ScopeFilter`), and only on Layer 3. Do not rely on RLS
  alone; if you connect to the database directly, set `bce.user_id` yourself.
- **The MCP stdio server runs unscoped.** It does not pass a user id, so Layer-3 tools
  behave as the system principal and can see every indexed repository. Run one process per
  trust boundary.
- **The Compose file ships development credentials.** `deploy/docker-compose.yml` and
  `.env.example` use `bce` as database name, role and password so that a local setup works
  immediately. Change all three before running anywhere else.
- **Indexing a repository stores its source.** Symbol bodies and docstrings are persisted
  so they can be returned as context. Treat the database with the same care as the source
  it indexes.
- **Credentials for remote indexing.** `BCE_BITBUCKET_TOKEN` is injected into the HTTPS
  remote URL for the duration of a single git invocation and `origin` is reset to the
  clean URL afterwards, so no token lingers in `.git/config`. Tokens are masked out of
  error messages. Supply the value through the environment rather than committing it.
- **`BCE_GIT_SSL_VERIFY=false` disables TLS verification** for git operations. It exists
  for corporate TLS-inspecting proxies and is off by default. Leave it alone unless you
  know exactly why you need it.
