# Contributing

Thanks for considering a contribution. This document covers how to get the project
running locally and what a mergeable change looks like.

## Getting set up

You need Python 3.11 or newer, Node.js 20 or newer, and Docker for the database.

```bash
git clone https://github.com/bgts-ai/bgts-context-engine.git
cd bgts-context-engine

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,mcp]"

docker compose -f deploy/docker-compose.yml up -d
cp .env.example .env
bce migrate
```

Index this repository to get something to work against:

```bash
bce index --repo . --name bce
bce serve
```

For frontend work, see [web/README.md](web/README.md).

## Before you open a pull request

```bash
ruff check src tests scripts
ruff format --check src tests scripts
pytest

cd web && npm ci && npm run typecheck && npm test && npm run build
```

CI runs the same commands across Python 3.11 to 3.13 on Linux and Windows.

## What we look for

**Determinism above all.** The core promise is that the same task text against the same
commit produces the same context pack. A change that introduces nondeterminism, whether
through unordered iteration, wall-clock time, randomness or an unpinned model, will not
be merged without an explicit opt-in flag. Anything affecting scoring, ordering or
selection needs a test that pins the output.

**Tests alongside behaviour.** New behaviour needs a test. Bug fixes need a test that
fails before the fix. Most of the suite runs without a database by faking at the
repository boundary; follow the pattern in `tests/test_api_rest.py`.

**English everywhere.** Code, comments, docstrings, commit messages and user-visible
strings are written in English. Turkish lives in exactly two places: `README.tr.md` and
the `tr` catalogs (`web/src/i18n/tr.ts` and `src/bce/core/i18n/catalogs/tr.json`).

**No hardcoded UI text.** Add the key to `web/src/i18n/en.ts` and its Turkish counterpart
in `tr.ts`. The type checker enforces that both catalogs stay in sync.

## Commits and pull requests

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(api): add repository filter to the search endpoint
fix(indexer): keep symbol ids stable when a file is renamed
docs: explain the anchor scoring weights
```

Types in use: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `build`, `ci`, `chore`,
`style`. Append `!` and add a `BREAKING CHANGE:` footer for incompatible changes.

Write the body as prose explaining why the change is needed, not a restatement of the
diff. Keep each commit self-contained and green on its own.

Branch off `development` and target it with your pull request. `main` only ever receives
merges from `development` and is what release tags are cut from.

## Adding a language

New language support is the most common contribution and has a dedicated guide in
[docs/languages.md](docs/languages.md). In short: add a provider under
`src/bce/indexing/parser/languages/`, register it, and add extraction tests with a small
representative source file.

## Reporting bugs and requesting features

Use the issue templates. For bugs, the single most useful thing you can include is the
exact command you ran, the indexed language, and what you expected versus what happened.

Security issues go to the process in [SECURITY.md](SECURITY.md), not to public issues.

## Releasing

For maintainers. Releases are cut from `main` and driven entirely by the tag.

1. Merge `development` into `main`.
2. Bump `__version__` in `src/bce/__init__.py`. That is the only place a version is
   written; `pyproject.toml` reads it from there.
3. Move the `CHANGELOG.md` entries from `Unreleased` under the new version.
4. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.

The release workflow then verifies that the tag matches `__version__`, compiles the web
interface into the package, builds the wheel and sdist, asserts the UI bundle is actually
inside the wheel, publishes to PyPI through Trusted Publishing, and opens the GitHub
release. No PyPI token is stored in this repository.

## Licence

By contributing you agree that your contribution is licensed under the MIT License, the
same terms that cover the rest of the project. There is no separate CLA to sign.
