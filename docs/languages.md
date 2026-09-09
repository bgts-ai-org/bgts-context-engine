# Languages

## What is supported

| Language | Extensions | Grammar | Availability |
| --- | --- | --- | --- |
| Python | `.py`, `.pyi` | `tree-sitter-python` | built in |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` | `tree-sitter-javascript` | built in |
| TypeScript | `.ts`, `.tsx` | `tree-sitter-typescript` | built in |
| Java | `.java` | `tree-sitter-java` | `pip install "bgts-context-engine[langs]"` |
| C# | `.cs` | `tree-sitter-c-sharp` | `[langs]` |
| Go | `.go` | `tree-sitter-go` | `[langs]` |

`bce languages` lists what the running installation actually has; it needs no database.
The optional providers are registered only if their grammar imports successfully, so a
missing grammar degrades to "that language is not indexed" rather than an error.

## What each provider extracts

Every provider produces `File` and `Symbol` nodes with `DEFINED_IN` and `BELONGS_TO` edges,
resolves imports into `IMPORTS` edges, and emits whatever `CALLS`, `REFERENCES`, `INHERITS`
and `IMPLEMENTS` edges it can see within the file. What differs is which declarations
become symbols and which relationships the language has at all.

**Python.** Top-level `def` becomes `function`, `def` inside a class becomes `method`,
`class` becomes `class`. Module-level assignments become `constant` when the name is
uppercase and `variable` otherwise. Base classes become `INHERITS`. Python has no separate
interface concept, so nothing emits `IMPLEMENTS`.

**JavaScript and TypeScript.** Function and generator declarations become `function`; class
declarations, including `abstract class`, become `class`; `interface` becomes `interface`;
method definitions and signatures become `method`. A `variable_declarator` whose value is
an arrow function or function expression becomes `function` — otherwise most modern code
would index as a pile of constants. Plain declarators become `constant` when lexically
scoped and `variable` for `var`. `class_heritage` becomes `INHERITS`. Both ES module
imports and CommonJS `require()` are resolved. TSX is a separate provider because the
grammar differs.

**Java.** Classes and enums become `class`, interfaces become `interface`, methods become
`method`, constructors become `constructor`. `extends` becomes `INHERITS` and `implements`
becomes `IMPLEMENTS` — Java is the language where that distinction is cleanest.

**C#.** Classes, structs and records all become `class`; interfaces become `interface`;
methods become `method`, constructors `constructor`, properties `property`. C# does not
distinguish base classes from interfaces syntactically in its base list, so every entry
becomes `INHERITS`.

**Go.** Function declarations become `function`, method declarations become `method`, a
`type_spec` over an `interface_type` becomes `interface`, other type specs become `type`.
Go has no inheritance, so no `INHERITS` or `IMPLEMENTS` is emitted; satisfying an interface
is structural and not visible in the syntax of either party.

## HTTP routes

Route extraction is what makes a path from a bug report resolve to a handler.

| Language | Framework | Recognised as |
| --- | --- | --- |
| Python | `fastapi` | `@app.get("/path")` and the other verb decorators |
| Python | `flask` | `@app.route("/path", methods=[...])`, one route per method, default `GET` |
| JS / TS | `express` | `app.get('/path', ...)` and friends |
| JS / TS | `nestjs` | `@Get`, `@Post`, `@Put`, `@Patch`, `@Delete`, `@Head`, `@Options`, `@All` |
| Java | `spring` | `@GetMapping` … `@DeleteMapping`, and `@RequestMapping` |
| C# | `aspnet` | `[HttpGet]` … `[HttpOptions]` |
| Go | `gin` | `r.GET("/path", ...)` and friends |

Where the handler is a named declaration next to the decorator — FastAPI, Flask, NestJS,
Spring, ASP.NET — a `ROUTES_TO` edge binds the route to it. Express and Gin usually pass an
inline closure, so the `Route` node is created without a handler edge.

## Cross-language bridges

React Native and Expo split a single logical call across two languages: JavaScript invokes
`NativeModules.Foo.bar()`, and `bar` is defined in Objective-C, Swift or Kotlin. No single
parser sees both sides, so the call graph has a hole exactly where the interesting bugs
are.

The bridge extractor scans `.m`, `.mm`, `.swift`, `.kt`, `.js`, `.jsx`, `.ts` and `.tsx`
for matching names and emits a `CALLS` edge with `provenance: heuristic` when a name is
both exposed natively and consumed from JavaScript.

| Detected | `synthesized_by` |
| --- | --- |
| `RCT_EXPORT_METHOD(...)`, `RCT_REMAP_METHOD(...)`, `NativeModules.X.y()` | `rn-bridge` |
| `@objc(name)`, `@objc func name` | `swift-objc-bridge` |
| `Function("name")`, `AsyncFunction("name")` | `expo-module-extract` |
| `sendEvent(withName: "name")`, `addListener("name")` | `rn-event-channel` |

These are name matches, not resolution, which is why their provenance weight is the lowest
of the three. Both endpoints must already exist as symbols — the extractor never invents a
node. Since there is no provider for `.m`, `.swift` or `.kt`, native declarations only
become symbols if you add one.

## Design notes

Comments matching `WHY`, `NOTE`, `HACK`, `XXX`, `TODO` or `FIXME` (case-insensitive,
followed by `:`, whitespace or `-`) become `DesignNote` nodes with an `EXPLAINS` edge to
the nearest symbol, preferring the following line over the preceding one.

`WHY:` is the one that matters. A comment explaining why code is the way it is answers the
question a graph cannot, and carrying it into the context pack stops an agent from
"fixing" a deliberate workaround.

## SCIP

When `scip-python` or `scip-typescript` is on `PATH`, the indexer runs it and merges the
result. SCIP comes from a real type checker, so it resolves what syntax cannot: which
`process` in a codebase with nine of them is actually being called.

Its effect is twofold. Edges the parser already found get their provenance upgraded from
`treesitter` to `scip`, which raises their score. Unambiguous name pairs that the parser
missed entirely get new `CALLS` or `REFERENCES` edges at `scip` provenance.

Without SCIP everything stays at `treesitter` and the engine works normally, with slightly
less confident cross-file edges. It is a quality upgrade, not a requirement.

## Adding a language

This is the most common outside contribution, and the pipeline is built so that it touches
few files. Nothing in the extractor, linker, indexer or upserter needs to change: they
consume the registry abstractly.

**1. Add the grammar** to `[project.optional-dependencies].langs` in `pyproject.toml`, or
to `dependencies` if it should be built in.

**2. Write the provider** at `src/bce/indexing/parser/languages/<lang>_provider.py`,
subclassing `LanguageProvider` from `parser/base.py`. Three methods are required:

```python
class RustProvider(LanguageProvider):
    language = "rust"
    line_comment_markers = ("//",)

    def extensions(self) -> tuple[str, ...]:
        return (".rs",)

    def parse(self, source: bytes):
        return self._parser.parse(source)

    def extract(self, tree, ctx: ParseContext) -> GraphFragment:
        ...
```

Two are optional: `extract_routes(tree, ctx, symbol_lines)` if the language has a web
framework worth recognising, and `derive_package(ctx)` if the default dotted path from the
file path is wrong for the language's module system.

**3. Emit the right things** from `extract`. Create the `File` node and its `BELONGS_TO`
edge, then a `Symbol` node per declaration with an id from `make_symbol_id(...)` and a
`DEFINED_IN` edge. Add intra-file `CALLS`, `REFERENCES`, `INHERITS` and `IMPLEMENTS` where
the language has them.

The part that is easy to miss: populate `FragmentLinkData` with the file's `exports`,
`imports` and `unresolved` references. Anything you cannot resolve inside the file goes in
`unresolved`, and the repository-wide linker resolves it in pass two. Skip this and your
language will index with no cross-file edges at all, which is most of the value.

**4. Register it** in `parser/registry.py`: `registry.register(RustProvider())` inside
`build_default_registry()` for a built-in, or add the module and class name to
`_OPTIONAL_PROVIDERS` for one behind the `langs` extra.

**5. Add tests** following `tests/test_extractor_langs.py`. A small representative source
file, assertions on the symbol kinds and edges extracted, and — because determinism is the
core promise — an assertion that extracting the same input twice gives the same ordered
output.

Extensions resolve by longest suffix match, so `.tsx` can take a different provider from
`.ts` without ambiguity.

Open a [language support issue](https://github.com/bgts-ai/bgts-context-engine/issues/new?template=language_support.yml)
first if you want to agree on which declarations should become nodes before writing code.
