# Web UI

The graph explorer and context trace player that ship with BGTS Context Engine. React 19 +
TypeScript + Vite, rendering with [sigma.js](https://www.sigmajs.org/) over a
[graphology](https://graphology.github.io/) graph.

This is a released part of the product, not a demo. `scripts/build_ui.py` compiles this
directory and vendors the output into the Python package, where the API serves it at
`/ui`. See the repository [README](../README.md) for the user-facing description.

## Development

The API and the frontend run as two processes. Start the API first:

```bash
bce serve            # http://127.0.0.1:8000
```

Then the dev server, from this directory:

```bash
npm install
npm run dev          # http://localhost:5173/ui/
```

`npm run dev` proxies `/v1` to `http://127.0.0.1:8000`, so the frontend talks to the API
over the same relative paths it uses in production and no CORS setup is needed. Point the
proxy elsewhere with `BCE_API_URL`, or bypass it entirely by building with `VITE_BCE_API`
set to an absolute API origin (in which case add that origin to `BCE_CORS_ORIGINS` on the
API side).

| Command             | Purpose                                  |
| ------------------- | ---------------------------------------- |
| `npm run dev`       | Dev server with hot reload               |
| `npm run build`     | Type-check and produce `dist/`           |
| `npm run typecheck` | `tsc --noEmit` only                      |
| `npm test`          | Vitest unit tests                        |

To preview exactly what users get, run `python scripts/build_ui.py` from the repository
root and then `bce serve`; the compiled bundle is served at <http://127.0.0.1:8000/ui/>.

## Endpoints consumed

All under `/v1/ui/*`, all read-only:

| Endpoint                     | Used for                                                     |
| ---------------------------- | ------------------------------------------------------------ |
| `GET /config`                | Engine version and default locale, fetched before first paint |
| `GET /repos`                 | Repository picker                                            |
| `GET /stats`                 | Node, edge and language counts in the top bar                |
| `GET /graph`                 | Whole-repo graph for the initial render                      |
| `GET /neighbors`             | Expanding a node on double click                            |
| `GET /node`                  | Detail panel: properties, docstring, source                  |
| `GET /search`                | Symbol and file search in the sidebar                        |
| `POST /context-trace`        | Stage-by-stage pipeline trace driving the player             |

These endpoints are unauthenticated by design, unlike the Layer-3 endpoints which honour
the `X-BCE-User` scope header. Put the engine behind a reverse proxy before exposing it
beyond a trusted network; see [docs/deployment.md](../docs/deployment.md).

## Layout

```
src/
├── api.ts              typed client for /v1/ui/*
├── App.tsx             state container: repo selection, playback, panels
├── i18n/               en + tr catalogs, useTranslation hook
├── graph/
│   ├── buildGraph.ts   API payload -> graphology graph + ForceAtlas2 layout
│   └── GraphView.tsx   sigma renderer, highlight and camera handling
├── trace/
│   └── tracePlayback.ts  pipeline trace -> ordered animation steps
├── components/         sidebar, stats bar, detail panel, trace panel and player
└── theme.ts            node, edge and language colour scales
```

`buildGraph.ts` and `tracePlayback.ts` hold the non-trivial logic and are pure functions,
which is where the unit tests are focused.

## Adding UI text

Never hardcode a user-visible string. Add a key to `src/i18n/en.ts`, which is the source of
truth, then add the Turkish counterpart in `src/i18n/tr.ts`. `tr.ts` is typed as
`Record<TranslationKey, string>`, so a missing key fails the type check, and a unit test
asserts both catalogs use the same `{placeholders}`.
