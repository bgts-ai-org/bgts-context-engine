## What this changes

<!-- Why the change is needed, and the approach you took. Link the issue it closes. -->

## How it was verified

<!--
The commands you ran, and anything you checked by hand. For example:

  ruff check src tests scripts && pytest
  cd web && npm run typecheck && npm test
  bce index --repo ./fixtures/example --name example, then queried /v1/context/pack
-->

## Checklist

- [ ] `ruff check` and `ruff format --check` pass over `src`, `tests` and `scripts`
- [ ] `pytest` passes
- [ ] Frontend changes: `npm run typecheck`, `npm test` and `npm run build` pass under `web/`
- [ ] New behaviour has a test; a bug fix has a test that failed before it
- [ ] Determinism is preserved, or an opt-in flag guards the exception
- [ ] New user-visible text goes through the i18n catalogs, not string literals
- [ ] Documentation under `docs/` and the README were updated if behaviour changed
- [ ] `CHANGELOG.md` has an entry under `Unreleased`
- [ ] Commits follow Conventional Commits and target the `development` branch
