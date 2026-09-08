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

- **The `/v1/ui/*` endpoints are unauthenticated.** They are read-only, but unlike the
  Layer-3 endpoints they do not apply the `X-BCE-User` scope filter. Anyone who can reach
  the port can read the indexed graph, including source bodies. Put the engine behind a
  reverse proxy that handles authentication before exposing it beyond a trusted network.
- **`X-BCE-User` is trusted as given.** The header selects a row-level security scope and
  is not itself verified. Whatever sits in front of the engine is responsible for
  authenticating the user and setting or overwriting this header.
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
