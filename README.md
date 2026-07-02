# Cortex Context Engine (CCE)

**Cortex Context Engine**, çok dilli kod depolarını deterministik bir **kod grafiğine** dönüştüren ve bu grafik üzerinden bağlam (context) sorguları sunan bir motorudur. LLM içermez; aynı kaynak kodu her zaman aynı düğüm/kenar yapısını üretir.

| Özellik | Açıklama |
|---------|----------|
| **Determinizm (P1)** | Aynı commit + aynı kaynak → byte-identical payload |
| **Çok dil** | Python, JavaScript, TypeScript, TSX (tree-sitter AST) |
| **Tek veritabanı (P5)** | PostgreSQL + Apache AGE (graf) + pgvector (embedding) |
| **Katman-1 araçlar** | resolve_symbol, find_references, find_implementers, get_call_graph, get_dependencies, get_type_hierarchy |
| **Katman-2 araçlar** | semantic_search, hybrid_search, find_similar_code (yalnızca çapa bulma, P2) |
| **Katman-3 araçlar** | get_context_for_task, suggest_change_sites, expand_blast_radius, select_repos, assemble_context |
| **Route & DesignNote** | Framework-aware HTTP route (FastAPI/Flask/Express/NestJS) + `WHY/NOTE/HACK/TODO/FIXME` çıkarımı |
| **Skorlama & coverage** | Deterministik skor (§6.4) + coverage/confidence (§8) + god-node uyarısı |
| **Auth / RLS** | Repo-bazlı scope filtresi + PostgreSQL RLS + audit_log |
| **Arayüz** | REST (FastAPI) + MCP server (agent-native, opsiyonel `mcp` paketi) |
| **Uzak indeksleme** | Bitbucket Cloud URL'den clone/fetch + indeks |
| **i18n** | Mesajlar `en` / `tr`; payload locale'den etkilenmez |

> **Sürüm:** 0.0.1 — Faz 0-5 mimarisi (Katman 1-2-3, indeksleme, skorlama, coverage, auth/RLS, REST+MCP) implemente edildi. Embedding **Voyage AI** (`voyage-code-3`, 1024 boyut) ile çalışır (`CCE_EMBEDDING_PROVIDER=voyage`); ağ/anahtar yoksa deterministik hash tabanlı encoder'a düşer (P2 fallback). Embedding yalnızca çapa bulmada; genişletme/skorlama/montaj %100 deterministik.

---

## İçindekiler

- [Mimari](#mimari)
- [Gereksinimler](#gereksinimler)
- [Kurulum](#kurulum)
- [Veritabanı](#veritabanı)
- [Yapılandırma](#yapılandırma)
- [CLI Kullanımı](#cli-kullanımı)
- [REST API](#rest-api)
- [Desteklenen Diller](#desteklenen-diller)
- [Veri Modeli](#veri-modeli)
- [Proje Yapısı](#proje-yapısı)
- [Geliştirme](#geliştirme)
- [Yol Haritası](#yol-haritası)

---

## Mimari

CCE üç ana hattadan oluşur: **indeksleme**, **depolama** ve **sorgulama**.

```mermaid
flowchart LR
    subgraph Indexing
        Git[Git Sync\nlocal / Bitbucket]
        Parser[Tree-sitter\nParser Registry]
        Extractor[Extractor]
        Upserter[Upserter]
        Git --> Extractor
        Parser --> Extractor
        Extractor --> Upserter
    end

    subgraph Storage
        AGE[(Apache AGE\nCode Graph)]
        SQL[(PostgreSQL\nRelational)]
        Vec[(pgvector\nEmbeddings)]
        Upserter --> AGE
        Upserter --> SQL
    end

    subgraph Query
        CLI[cce CLI]
        API[FastAPI REST]
        MCP[MCP Server]
        L3[Layer-3\nget_context_for_task ...]
        L2[Layer-2\nsemantic / hybrid search]
        L1[Layer-1\nresolve / references / call graph]
        CLI --> L1
        API --> L1
        API --> L2
        API --> L3
        MCP --> L1
        MCP --> L2
        MCP --> L3
        L3 --> L2 --> L1
        L1 --> AGE
        L2 --> Vec
        L3 --> SQL
    end
```

### İndeksleme hattı

1. **Git sync** — Yerel dizin veya Bitbucket Cloud URL'si; uzak repolar `.cce_data/repos` altında önbelleğe alınır (re-index'te `git fetch` yeterli).
2. **Extractor** — Dosya uzantısına göre dil sağlayıcısı seçilir, AST ayrıştırılır, `GraphFragment` üretilir.
3. **Upserter** — Düğüm/kenarlar Apache AGE grafiğine `MERGE` ile yazılır; repo meta verisi SQL tablolarına kaydedilir.

### Sorgulama (Katman 1-2-3)

- **Katman 1 (deterministik graf primitive'leri):** `resolve_symbol`, `find_references`, `find_implementers`, `get_call_graph`, `get_dependencies`, `get_type_hierarchy`
- **Katman 2 (hibrit retrieval — yalnızca çapa bulma, P2):** `semantic_search`, `hybrid_search`, `find_similar_code`
- **Katman 3 (task-aware orkestrasyon):** `get_context_for_task`, `suggest_change_sites`, `expand_blast_radius`, `select_repos`, `assemble_context`

Retrieval akışı (§6, 5 aşama): commit sabitleme → çok-kaynaklı çapa → deterministik genişletme → skorlama+daraltma (1000→~8) → RLS filtresi → token-budget montaj. Skorlama embedding kullanmaz (P2); embedding yalnızca grafa giriş kapısı bulur.

Her araç `{ tool, payload, message, locale }` zarfını döner. **`payload` locale'den bağımsızdır**; yalnızca `message` çevrilir. Katman-3 çıktıları ayrıca bir `coverage` objesi taşır (§8): güven seviyesi, çapa kaynakları, provenance dağılımı, god-node uyarısı.

---

## Gereksinimler

| Bileşen | Minimum |
|---------|---------|
| Python | 3.11+ |
| PostgreSQL | 16 (Apache AGE + pgvector ile) |
| Git | Uzak indeksleme için |
| Docker | Önerilen (DB kurulumu için) |

---

## Kurulum

```bash
# Repoyu klonlayın
git clone <repo-url> context-engine
cd context-engine

# Sanal ortam (önerilir)
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# Paketi editable modda kurun
pip install -e ".[dev]"
```

Kurulum sonrası `cce` komutu kullanılabilir olur:

```bash
cce --help
```

---

## Veritabanı

CCE tek bir PostgreSQL sunucusunda üç katmanı birleştirir:

- **Apache AGE** — Kod grafiği (`code_graph`)
- **pgvector** — Sembol/dosya embedding'leri (Phase 2)
- **İlişkisel tablolar** — Repo meta, görev geçmişi, audit, RLS kapsamları

### Docker ile hızlı başlangıç

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Bu komut PostgreSQL 16 + pgvector + Apache AGE içeren `cce-postgres-age-pgvector:pg16` imajını derler ve `localhost:5432` üzerinde ayağa kaldırır.

Varsayılan kimlik bilgileri:

| Değişken | Değer |
|----------|-------|
| Host | `localhost` |
| Port | `5432` |
| Database | `cce` |
| User | `cce` |
| Password | `cce` |

### Migrasyon

Veritabanı hazır olduktan sonra şemayı uygulayın:

```bash
cce migrate
```

Migrasyonlar `cce/storage/relational/migrations/` altındaki SQL dosyalarını sırayla çalıştırır:

| Dosya | İçerik |
|-------|--------|
| `0001_extensions_graph.sql` | AGE + pgvector extension, `code_graph` oluşturma |
| `0002_relational.sql` | `repos`, `tasks`, `users`, `scopes`, `audit_log` |
| `0003_vector.sql` | `embeddings` tablosu (768 boyut, HNSW indeks) |
| `0004_rls.sql` | Row-Level Security politikaları (`repos`, `embeddings`); `cce.user_id` session değişkeni ile scope |
| `0005_embeddings_dim.sql` | `embeddings.embedding` → `vector(1024)` (Voyage `voyage-code-3`); HNSW indeks yeniden kurulur (reindex sınırı) |
| `0006_fts.sql` | `symbol_fts` tablosu (tsvector + GIN): Layer-2 lexical kanalın FTS yolu |

> **Not:** Embedding içeriğine sembol gövdesi (kırpılmış) eklendi ve lexical arama FTS tablosunu
> kullanıyor; daha önce indekslenmiş repolar için tam re-index (`cce index`) gerekir — embedding'ler
> Voyage API ile yeniden üretilir.

---

## Yapılandırma

`.env.example` dosyasını kopyalayın:

```bash
cp .env.example .env
```

Tüm ayarlar `CCE_` öneki ile ortam değişkenlerinden okunur:

```env
# PostgreSQL
CCE_DB_HOST=localhost
CCE_DB_PORT=5432
CCE_DB_NAME=cce
CCE_DB_USER=cce
CCE_DB_PASSWORD=cce

# i18n (yalnızca mesajlar; payload etkilenmez)
CCE_DEFAULT_LOCALE=en

# Embedding (Faz 2; yalnızca çapa bulma, P2)
CCE_EMBEDDING_PROVIDER=voyage        # "voyage" veya "hashing" (fallback)
CCE_EMBEDDING_MODEL=voyage-code-3
CCE_EMBEDDING_DIM=1024
CCE_VOYAGE_API_KEY=                   # provider=voyage için gerekli

# Bitbucket Cloud uzak indeksleme
CCE_BITBUCKET_USERNAME=
CCE_BITBUCKET_TOKEN=
CCE_REPO_CACHE_DIR=.cce_data/repos
CCE_GIT_SSL_VERIFY=true
```

Voyage embedding'i için: `pip install -e ".[embed]"`. Ek dil grameri için: `pip install -e ".[langs]"` (Java/C#/Go).

### Bitbucket kimlik doğrulama

| Token türü | Username | Clone formatı |
|------------|----------|---------------|
| Workspace/repo access token | Boş bırakın | `x-token-auth:<token>` |
| App password / ATATT API token | Bitbucket kullanıcı adı | `username:token` |

Token'lar `.git/config` dosyasına **asla yazılmaz**; yalnızca tek seferlik git çağrısında URL'ye enjekte edilir.

---

## CLI Kullanımı

### Desteklenen dilleri listele (DB gerekmez)

```bash
cce languages
```

### Yerel repo indeksle

```bash
cce index --repo /path/to/my-repo --name my-org/my-repo
```

Opsiyonel parametreler:

- `--commit <sha>` — Commit override
- `--locale tr` — Çıktı mesaj dili

### Bitbucket'dan uzak indeksle

```bash
cce index-remote \
  --url https://bitbucket.org/workspace/repo-slug \
  --name workspace/repo-slug \
  --branch main
```

Desteklenen URL biçimleri:

- `https://bitbucket.org/acme/widgets`
- `https://bitbucket.org/acme/widgets.git`
- `https://bitbucket.org/acme/widgets/src/main/...` (web Source URL)
- `git@bitbucket.org:acme/widgets.git` (SSH — clone HTTPS üzerinden yapılır)

### Incremental re-index (git-diff)

Sadece son indekslemeden bu yana değişen dosyaları yeniden işler:

```bash
cce reindex --repo ./my-repo --name my-org/my-repo
# Belirli commit aralığı:
cce reindex --repo ./my-repo --name my-org/my-repo --since <sha> --to HEAD
```

REST karşılığı: `POST /v1/reindex` (`{repo_path, name, since_commit?, to_commit?}`).

### POC benchmark

Bir task seti üzerinde latency/recall/precision/determinizm/RLS ölçer ve JSON rapor yazar:

```bash
cce bench --cases cases.json --out report.json
```

### Sembol çözümle

```bash
cce resolve-symbol --name MyClass --repo my-org/my-repo
```

### Referans bul

```bash
cce find-references --symbol-id "python::pkg::ns::MyClass#abc123..."
```

### Task için context derle (Katman 3)

```bash
cce context --task "login endpoint 500 hatası veriyor" --max-tokens 4000 --locale tr
```

### REST API sunucusu

```bash
cce serve --host 0.0.0.0 --port 8000
# Geliştirme modu (auto-reload)
cce serve --reload
```

### MCP sunucusu (agent-native, opsiyonel)

```bash
pip install -e ".[mcp]"
cce serve-mcp   # stdio üzerinden MCP server
```

---

## REST API

Sunucu başlatıldığında OpenAPI dokümantasyonu şu adreste:

- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`

### Endpoint'ler

| Method | Path | Katman | Açıklama |
|--------|------|--------|----------|
| `GET` | `/healthz` | meta | Canlılık + DB erişilebilirliği |
| `GET` | `/v1/languages` | meta | Desteklenen diller/uzantılar |
| `POST` | `/v1/index` | indeksleme | Yerel repo indeksle |
| `POST` | `/v1/index-remote` | indeksleme | Bitbucket repo indeksle |
| `GET` | `/v1/resolve-symbol?name=...&repo=...` | 1 | Sembol çözümle |
| `GET` | `/v1/find-references?symbol_id=...` | 1 | Referans bul |
| `GET` | `/v1/find-implementers?symbol_id=...` | 1 | Implement eden tipler |
| `GET` | `/v1/get-call-graph?symbol_id=...&hops=&direction=` | 1 | Çağrı grafı (callers/callees) |
| `GET` | `/v1/get-dependencies?file_id=...&transitive=` | 1 | Dosya IMPORTS bağımlılıkları |
| `GET` | `/v1/get-type-hierarchy?symbol_id=...` | 1 | Üst/alt tip hiyerarşisi |
| `POST` | `/v1/semantic-search` | 2 | Semantik çapa arama |
| `POST` | `/v1/hybrid-search` | 2 | Keyword + semantik + yapısal blend |
| `POST` | `/v1/find-similar-code` | 2 | Kod parçasına benzer semboller |
| `POST` | `/v1/get-context-for-task` | 3 | Task → assembled context + coverage |
| `POST` | `/v1/suggest-change-sites` | 3 | Skorlu değişiklik adayları |
| `POST` | `/v1/expand-blast-radius` | 3 | Etki yüzeyi (dosya/repo/sembol) |
| `POST` | `/v1/select-repos` | 3 | Task için aday repo kümesi |
| `POST` | `/v1/assemble-context` | 3 | Sembol listesini budget'a montaj |

> Katman-3 uçları isteğe bağlı `X-CCE-User` başlığı ile scope filtresi uygular (RLS, §9). Başlık yoksa sistem principal'ı (allow-all) kullanılır.

### Örnek istekler

**Yerel indeks:**

```bash
curl -X POST http://127.0.0.1:8000/v1/index \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/path/to/repo", "name": "my-org/my-repo"}'
```

**Uzak indeks:**

```bash
curl -X POST http://127.0.0.1:8000/v1/index-remote \
  -H "Content-Type: application/json" \
  -d '{"url": "https://bitbucket.org/workspace/repo", "branch": "main"}'
```

**Sembol çözümle (Türkçe mesaj):**

```bash
curl "http://127.0.0.1:8000/v1/resolve-symbol?name=MyClass&locale=tr"
```

### Yanıt zarfı

```json
{
  "tool": "resolve_symbol",
  "payload": {
    "matches": [
      {
        "symbol_id": "python::mypkg::MyClass::MyClass#...",
        "kind": "class",
        "signature": null,
        "docstring": "...",
        "file_id": "my-repo:src/main.py",
        "line": 42,
        "indexed_at_commit": "abc123..."
      }
    ],
    "indexed_at_commit": "abc123..."
  },
  "message": null,
  "locale": "tr"
}
```

Locale, `Accept-Language` başlığı veya `?locale=tr` sorgu parametresi ile belirlenir.

---

## Desteklenen Diller

Varsayılan olarak kayıtlı diller (Python + JS/TS her zaman; Java/C#/Go grameri kuruluysa):

| Dil | Uzantılar | Çıkarılan yapılar | Route |
|-----|-----------|-------------------|-------|
| Python | `.py`, `.pyi` | Sınıf, fonksiyon, method; IMPORTS, INHERITS, CALLS, REFERENCES (`ref_kind`) | FastAPI, Flask |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` | Modül sembolleri, import/call/reference kenarları | Express |
| TypeScript | `.ts` | Arayüz, tip, sınıf, fonksiyon | NestJS |
| TSX | `.tsx` | TS + JSX bileşenleri | — |
| Java | `.java` | Sınıf, arayüz, method; INHERITS/IMPLEMENTS, CALLS, REFERENCES | Spring MVC |
| C# | `.cs` | Sınıf, method, property; CALLS, REFERENCES | ASP.NET |
| Go | `.go` | Fonksiyon, method, tip; CALLS, REFERENCES | Gin |

> Java/C#/Go opsiyoneldir: `pip install -e ".[langs]"`. Gramer yoksa ilgili sağlayıcı sessizce atlanır.
>
> **Diller-arası köprüler** (heuristic): React Native legacy (`RCT_EXPORT_METHOD`), Swift↔ObjC selector, Expo Modules DSL, RN event kanalları → `provenance='heuristic'` + `synthesized_by` etiketli CALLS kenarları.

Atlanan dizinler: `.git`, `node_modules`, `.venv`, `__pycache__`, `dist`, `build` ve benzeri.

Yeni dil eklemek için `LanguageProvider` implementasyonu yazıp `build_default_registry()` içinde kaydedin.

---

## Veri Modeli

### Graf düğümleri (`NodeLabel`)

| Düğüm | Kimlik alanı | Açıklama |
|-------|--------------|----------|
| `Repo` | `repo_id` | Mantıksal depo |
| `File` | `file_id` | Kaynak dosya |
| `Symbol` | `symbol_id` | Fonksiyon, sınıf, method vb. |
| `Module` | `module_id` | Paket/modül |
| `Route` | `route_id` | HTTP route (Phase 1+) |
| `DesignNote` | `note_id` | TODO/FIXME/HACK yorumları (Phase 1+) |

### Graf kenarları (`EdgeLabel`)

| Kenar | Yön | Açıklama |
|-------|-----|----------|
| `DEFINED_IN` | Symbol → File | Tanım konumu |
| `BELONGS_TO` | File → Repo | Dosya-depo ilişkisi |
| `IMPORTS` | File/Module → File/Module | Import |
| `CALLS` | Symbol → Symbol | Çağrı |
| `INHERITS` | Symbol → Symbol | Kalıtım |
| `IMPLEMENTS` | Symbol → Symbol | Arayüz implementasyonu |
| `REFERENCES` | Symbol → Symbol | Referans (`ref_kind` ile) |
| `ROUTES_TO` | Route → Symbol | Route handler |
| `EXPLAINS` | DesignNote → Symbol | Tasarım notu |

### Kimlik üretimi

Sembol kimlikleri SCIP-moniker mantığıyla üretilir ve **commit'ten bağımsızdır**:

```
{language}::{package}::{namespace}::{name}#{blake2b_hash}
```

Örnek: `python::mypkg::services::UserService#create_user#a1b2c3d4e5f67890`

Bu sayede araçlar zincirlenebilir: bir aracın `symbol_id` çıktısı, diğerinin girdisi olur.

### Provenance

Her kenar kaynağını taşır:

| Değer | Anlam |
|-------|-------|
| `treesitter` | AST çıkarımı (varsayılan) |
| `scip` | SCIP çözümlemesi (opsiyonel adaptör; binary varsa yükseltilir) |
| `heuristic` | Sentezlenmiş köprü kenarları |

---

## Proje Yapısı

```
context-engine/
├── cce/                          # Ana Python paketi
│   ├── cli.py                    # Typer CLI giriş noktası
│   ├── config.py                 # Pydantic Settings (CCE_* env)
│   ├── api/
│   │   ├── rest/                 # FastAPI REST katmanı (Katman 1-2-3)
│   │   └── mcp/                  # MCP tool kataloğu + server adaptörü
│   ├── core/
│   │   ├── i18n/                 # Çeviri katalogları (en, tr)
│   │   ├── scoring/              # Skorlama motoru (§6.4)
│   │   ├── orchestrator/         # Çapa bulma + genişletme + 5-aşama akış
│   │   ├── coverage/             # Coverage/Confidence + god-node (§8)
│   │   ├── assembler/            # Token-budget montaj (§6.5)
│   │   └── auth/                 # Scope filtresi + audit_log (§9)
│   ├── bench/                    # POC benchmark harness (latency/recall/precision/determinizm/RLS)
│   ├── domain/                   # GraphNode, GraphEdge, enum'lar
│   ├── indexing/
│   │   ├── indexer.py            # İndeksleme orkestratörü (+embedding, incremental, SCIP)
│   │   ├── extractor/            # Dil-agnostik çıkarım + route + designnote + bridges
│   │   ├── parser/               # Tree-sitter sağlayıcıları (Py/JS/TS/Java/C#/Go) + scip/ adaptörü
│   │   ├── embedder/             # Encoder (Hashing + Voyage) + embedding yazımı (P2)
│   │   ├── gitsync/              # Yerel/uzak git sync + git-diff (changed_files)
│   │   └── upserter/             # AGE graf yazımı
│   ├── storage/
│   │   ├── graph/                # Apache AGE Cypher client + repository
│   │   ├── relational/           # SQL + migrator + queries
│   │   └── vector/               # pgvector store
│   └── tools/
│       ├── layer1/               # Deterministik graf primitive'leri
│       ├── layer2/               # Hibrit retrieval
│       └── layer3/               # Task-aware orkestrasyon
├── deploy/
│   ├── docker-compose.yml        # PostgreSQL + AGE + pgvector
│   ├── Dockerfile
│   └── initdb/                   # İlk container init SQL
├── tests/                        # pytest testleri
├── pyproject.toml
└── .env.example
```

---

## Geliştirme

### Testler

```bash
pytest
# Coverage ile
pytest --cov=cce
```

Test kapsamı: API, git sync, Bitbucket URL parsing, Python/JS-TS extractor, symbol_id, i18n.

### Lint

```bash
ruff check cce tests
ruff format cce tests
```

### Yerel geliştirme akışı

```bash
# 1. DB'yi başlat
docker compose -f deploy/docker-compose.yml up -d

# 2. Migrasyon
cce migrate

# 3. Örnek repo indeksle
cce index --repo . --name context-engine

# 4. Sorgula
cce resolve-symbol --name Indexer --repo context-engine

# 5. API (opsiyonel)
cce serve --reload
```

---

## Yol Haritası

| Faz | Durum | Kapsam |
|-----|-------|--------|
| **Faz 0** | ✅ | AST çıkarım, full-index, symbol_id, i18n iskeleti |
| **Faz 1** | ✅ | Tüm Katman-1 araçlar, REST/CLI/MCP, `delete_file_subgraph` altyapısı |
| **Faz 2** | ✅ | Embedding (encoder + pgvector), Katman-2, çapa bulma, deterministik genişletme, skorlama, route + designnote çıkarımı, provenance |
| **Faz 3** | ✅ | Katman-3 orkestrasyon, coverage/confidence + god-node, token-budget montaj |
| **Faz 4** | ✅ | Scope filtresi + RLS + audit_log, MCP server, API versiyonlama (`/v1`) |
| **Faz 5** | ✅ | Voyage AI embedding (`voyage-code-3`, 1024), `ref_kind` çıkarımı, git-diff incremental re-index, opsiyonel SCIP adaptörü, dil genişletme (Java/C#/Go + köprüler), POC benchmark |

**Faz 5 detayları:**

- **Voyage embedding** — `CCE_EMBEDDING_PROVIDER=voyage` ile `voyage-code-3` (1024 boyut); `document`/`query` input ayrımı; ağ yoksa deterministik hash fallback. Boyut migration: `0005_embeddings_dim.sql`.
- **`REFERENCES.ref_kind`** — define/write/read/pass çıkarımı (Python + JS/TS + Java/C#/Go); genişletme `get_referrers` ile besler, skorlamada define/write >> read/pass (§6.4).
- **Incremental re-index** — `Indexer.index_incremental` (git-diff), `cce reindex` CLI + `POST /v1/reindex`; değişen/silinen dosyalar için subgraph + embedding güncelleme.
- **SCIP adaptörü** — opsiyonel `scip-python`/`scip-typescript`; binary varsa ilgili kenarların provenance'ı `scip`'e yükseltilir, yoksa tree-sitter davranışı korunur.
- **Dil genişletme** — Java (Spring), C# (ASP.NET), Go (Gin) tree-sitter sağlayıcıları (`pip install -e ".[langs]"`); diller-arası köprüler (RN/Expo/Swift-ObjC) `provenance='heuristic'` + `synthesized_by`.
- **POC benchmark** — `cce bench --cases cases.json` → JSON rapor (latency, recall, precision, determinizm regresyon, RLS sızıntı kontrolü).

**Sonraki adımlar (ürünleştirme öncesi):**

- Incremental re-index'in webhook/polling ile otomatik tetiklenmesi
- Route framework genişletme (Rails/Laravel/Axum)
- SBOM/lisans taraması (AGPL kaçınma)

---

## Lisans

Proprietary — Cortex
