<div align="center">

<img src="https://raw.githubusercontent.com/bgts-ai-org/bgts-context-engine/main/docs/assets/readme-hero.tr.gif" alt="BGTS Context Engine: depoyu tur tur tarayan bir kodlama ajanı ve aynı ajanın BCE'nin kod grafından başlaması" width="820">

**Yapay zekâ kodlama ajanları için deterministik kod-graf bağlamı.**

*"Toplantı webhook'unda login timeout neden tetikleniyor?"* diye sorun; cevabı gerçekten
veren sekiz sembolü sıralanmış, bütçelenmiş ve yeniden üretilebilir şekilde alın.

[![Token −%82](https://img.shields.io/badge/token-%E2%88%92%2582-39ff88?style=for-the-badge&labelColor=0b0f14)](#sonuçlar)
[![Maliyet −%63](https://img.shields.io/badge/maliyet-%E2%88%92%2563-f2ac0b?style=for-the-badge&labelColor=0b0f14)](#sonuçlar)
[![Araç çağrısı −%89](https://img.shields.io/badge/ara%C3%A7_%C3%A7a%C4%9Fr%C4%B1s%C4%B1-%E2%88%92%2589-4d8dff?style=for-the-badge&labelColor=0b0f14)](#sonuçlar)
[![Süre −%37](https://img.shields.io/badge/s%C3%BCre-%E2%88%92%2537-f1881e?style=for-the-badge&labelColor=0b0f14)](#sonuçlar)
[![Recall %92.7](https://img.shields.io/badge/recall-%2592.7_korundu-22c55e?style=for-the-badge&labelColor=0b0f14)](#sonuçlar)

<sub>İki kodlama ajanı, 600 gerçek birleşmiş değişiklik, 12 depo, 6 dil — ajan tek başına ile ilk adımı BCE olan aynı ajan karşılaştırması.</sub>

[![PyPI](https://img.shields.io/pypi/v/bgts-context-engine.svg)](https://pypi.org/project/bgts-context-engine/)
[![Python](https://img.shields.io/pypi/pyversions/bgts-context-engine.svg)](https://pypi.org/project/bgts-context-engine/)
[![CI](https://github.com/bgts-ai-org/bgts-context-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/bgts-ai-org/bgts-context-engine/actions/workflows/ci.yml)
[![Lisans: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-uyumlu-000000.svg)](docs/mcp.md)
[![Yıldızlar](https://img.shields.io/github/stars/bgts-ai-org/bgts-context-engine?style=flat&logo=github)](https://github.com/bgts-ai-org/bgts-context-engine/stargazers)

[Sonuçlar](#sonuçlar) · [Hızlı başlangıç](#hızlı-başlangıç) · [Ajanınızdan kullanma](#ajanınızdan-kullanma) · [Nasıl çalışır](#nasıl-çalışır) · [Desteklenen modeller](#desteklenen-modeller) · [Seçici modeller](docs/selector.md) · [Dokümantasyon](#dokümantasyon) · [Site](https://bgts-ai-org.github.io/bce-microsite/) · [English](README.md)

</div>

> Dokümantasyonun tamamı İngilizcedir. Bu dosya projeye Türkçe bir giriş sunar; teknik
> referanslar için [`docs/`](docs/) klasörüne bakın.

---

## Neden var

Tanımadığı bir depoda çalışan bir ajan, neyi değiştireceğine karar vermeden önce neyi
okuyacağına karar vermek zorundadır. Bunun alışılmış cevabı, parçalanmış dosyalar üzerinde
embedding aramasıdır. Kurmak ucuzdur ve çok belirli bir biçimde yanlıştır: davranışa
*katılan* kodu değil, soruya *benzeyen* metni döner. Login timeout'u sorduğunuzda,
timeout'tan bahseden beş dosyayı alırsınız; onu ayarlayan tek fonksiyonu ve
değiştirdiğinizde bozulacak üç çağıranı almazsınız.

Bu bilgi yapısaldır ve kesin bir cevabı vardır. `handleLogin`, `refreshSession`'ı çağırır;
o da `SESSION_TTL`'i okur; ona da tam olarak tek bir yerde değer atanır. Bu bir graf
gezinmesidir.

BGTS Context Engine depolarınızı bu grafa indeksler — semboller, çağrılar, referanslar, tip
hiyerarşileri, HTTP route'ları, diller arası köprüler — ve soruları bu grafı gezerek
yanıtlar. Embedding yalnızca tek bir yerde kullanılır: görev metni tanıdık hiçbir şey
adlandırmadığında giriş noktalarını bulmak için. Sıralamayı asla etkilemez.

**Aynı görev metni, aynı commit üzerinde, aynı sıralamayı döner.** Getirme yolunda
model yok, saat yok, rastgelelik yok. Bir ajan hatalı bir değişiklik yaptığında, ona tam
olarak ne söylendiğini yeniden oynatabilir, yanlış sembolü yüzeye çıkaran aşamayı bulabilir
ve o aşamayı düzeltebilirsiniz.

Bu sıralamanın üstünde isteğe bağlı, açıkça işaretlenmiş tek bir olasılıksal adım var:
sıralanan dosyalardan görevin gerçekten hangilerini değiştirdiğini bir karar modeline soran
*bağlam seçici*. Ajana bu dosyaları tam, muhtemelen ilgili olanları birer satır olarak verir,
gerisini atar. 600 gerçek değişiklikte ajana verilen token'ı 8.310'dan 1.121'e indirdi (−%87),
recall'dan yaklaşık bir puan verdi (94.4 → 93.3).
`BCE_SELECTOR` bir model adı (barındırılan Jev ya da kendi GPU'nuzda decider-2b /
decider-4b) verilene kadar kapalıdır; `--no-select` bayt bayt aynı paketi geri verir.

Motor, insanların çalıştırabilmesi için yayımlanır. Aynı şeyi kendi çevrelerinde isteyen
kurumlar — indeksleme, kurulum, depolarınıza göre ayarlanmış skorlama veya etrafındaki
ajan yığını konusunda yardım — [BGTS](https://www.bgts.com) ile
danışmanlık olarak iletişime geçebilir. **opensource-ai@bgts.com** adresine yazın.

## Sonuçlar

İki kodlama ajanı aynı 600 görevi koştu. Her görev, bir projenin gerçekten birleştirdiği bir
değişiklik: altı dilde 12 açık kaynak depo, depo başına 50 görev — flask ve requests
(Python), express ve axios (JavaScript), nest ve vite (TypeScript), guava ve netty (Java),
efcore ve PowerShell (C#), gin ve prometheus (Go). Ajana geliştiricinin cümlesi, geçmişi
silinmiş bir kopyada veriliyor; değişikliğin dokunduğu kaynak dosyaları bulması isteniyor.
Her görev iki kez koşuldu: ajan **tek başına**, kendi grep, glob ve okuma araçlarıyla; ve ajan
**ilk adımı BCE olarak** (`BCE_AGENT_MODE=hint`): önce motora soruyor, cevaptan başlıyor,
gerekirse ekliyor.

| Görev başına | Cursor CLI · grok-4.7-high-fast<br>tek başına → BCE ile | OpenCode · GLM 5.3 Flash<br>tek başına → BCE ile | Değişim<br>(iki ajanın ortalaması) |
| --- | --- | --- | --- |
| Token | 264k → 59k | 196k → 24k | **−%82** |
| Maliyet | −%60 | −%67 | **−%63** |
| Araç çağrısı | 17.4 → 2.0 | 11.3 → 1.1 | **−%89** |
| Model turu | 9.1 → 3.0 | 8.3 → 2.1 | **−%71** |
| Süre | 65 sn → 42 sn | 132 sn → 83 sn † | **−%37** |
| Dosya recall'u | %95.6 → %92.9 | %89.2 → %92.5 | **%92.4 → %92.7** |

Ajan depoyu taramayı bırakıyor: kazancın çoğu, grep yaparken her turda yeniden okuduğu
bağlamdan geliyor (cache read token'ı yaklaşık %90 düşüyor). Daha küçük model recall
kazanıyor — GLM 5.3 Flash'ın tek başına kaçırdığı dosyaları BCE'nin grafı veriyor (zor
görevlerde %74 → %81). Ajanın cevabı konum kümesi olarak alıp hiç arama yapmadığı
`BCE_AGENT_MODE=trust` ile Cursor CLI daha da ileri gitti: 45k token, 1.3 araç çağrısı,
36 sn, %92.3 recall.

> **Rakamları okurken bunları göz önünde tutun.**
> Hedef precision değil recall: motor ajana dosya listesini olduğu gibi aktarmasını söylüyor,
> görev başına yaklaşık 15 dosya (tek başına 1.9), bu yüzden precision düşüyor (Cursor CLI
> %91 → %11). Cursor CLI'ın tek başına koşularında web erişimi açıktı ve ajan bazen
> değişikliği GitHub'da buldu; bu, o tabanı yukarı çekiyor. Maliyet iki ajan için tek fiyat
> kartıyla hesaplandı (OpenRouter'ın GLM 5.3 Flash input, output ve cache read fiyatları);
> Cursor CLI sütunu Cursor'ın kendi faturası değildir. OpenCode'un iki
> koşusu, ikisinin de tamamladığı 597 görev üzerinden karşılaştırıldı. † OpenCode'un BCE
> koşuları boş belleği kalmamış bir makinedeydi (16 GB, %99 dolu); MCP açılışı ve motor
> çağrısı boş makinenin ortanca değerlerine çekildi, ölçülen ham ortalama 155 sn. Ölçüm
> düzeneği henüz bu depoda değil — [yol haritasına](#yol-haritası) bakın.

## Hızlı başlangıç

```bash
# 1. Tek veritabanında Apache AGE + pgvector ile PostgreSQL 16
docker compose -f deploy/docker-compose.yml up -d

# 2. Kur ve migrasyonları uygula
pip install bgts-context-engine
cp .env.example .env
bce migrate

# 3. Bir depoyu indeksle
bce index --repo /path/to/your/repo --name my-service

# 4. Sor
bce context --task "toplanti webhook'undaki login timeout'u duzelt"
```

Ardından servis edin:

```bash
bce serve        # :8000/docs adresinde REST, :8000/ui/ adresinde web arayüzü
bce serve-mcp    # ajanlar için stdio üzerinden MCP ([mcp] eki gerekir; aşağıya bakın)
```

## Ajanınızdan kullanma

MCP yüzeyi `mcp` ekinin arkasındadır (Python MCP SDK 1.x: `mcp>=1.0,<2`). `bce` komutunu
yalnızca proje `.venv` içine değil, **kullanıcı PATH'ine** kurun. Cursor ve VS Code
`bce serve-mcp` sürecini kendileri başlatır; sanal ortamı etkinleştirmezler:

```bash
pip install "bgts-context-engine[mcp]"
bce --version   # venv kapalı, yeni bir terminalde çalışmalı
```

Her MCP istemcisi aynı stdio komutunu ve ortamındaki veritabanı bağlantısını kullanır.
Editör için terminalde `bce serve-mcp` açık bırakmayın: stdout protokoldür, bu yüzden
süreç sessiz kalır; IDE kendi kopyasını başlatır.

**Editör başına tek komut.** Ajanın çalıştığı projede (indekslediğiniz depoda), motorun
`.env` dosyasını göstererek çalıştırın:

```bash
bce --env-file /path/to/engine/.env cursor-init --repo-id my-service   # Cursor
bce --env-file /path/to/engine/.env claude-init --repo-id my-service   # Claude Code
bce --env-file /path/to/engine/.env opencode-init --repo-id my-service # OpenCode
bce --env-file /path/to/engine/.env codex-init --repo-id my-service    # Codex
bce --env-file /path/to/engine/.env copilot-init --repo-id my-service  # GitHub Copilot
```

`opencode-init`, sunucuyu `opencode.json` dosyasına (OpenCode'un `mcp` biçiminde)
birleştirir ve aynı ajan yönergesini `AGENTS.md` içinde işaretli bir bölüm olarak yazar.
`codex-init`, sunucu tablosunu `.codex/config.toml` dosyasına ekler (diğer tablolar ve
yorumlar korunur) ve aynı `AGENTS.md` bölümünü yazar; Codex bu dosyayı projeye güven
verdikten sonra yükler. `copilot-init`, Copilot CLI ile VS Code'un birlikte okuduğu taşınabilir
`.mcp.json` dosyasını (Claude Code'un kullandığı girdinin aynısı) ve yönergeyi
`.github/copilot-instructions.md` içinde işaretli bir bölüm olarak yazar.

`cursor-init`, `.cursor/mcp.json` dosyasını (varsa üzerine birleştirerek) ve
`.cursor/rules/bgts-context-engine.mdc` kuralını yazar. Kural ajana şunları söyler:
`get_context_for_task` aracını *ilk iş olarak* çağır, döndürdüğü `dosya:satır`
girdilerini doğrulanmış konum say ve onları grep ile arama, listelendi diye bir dosyayı
düzenleme. `claude-init` ise `.mcp.json`, `CLAUDE.md` içinde işaretli bir bölüm ve
`bce precontext` komutunu çalıştıran bir `UserPromptSubmit` kancası yazar: kanca her
istemde grafı bir kez sorgular ve yanıtı ajana ilk turundan önce verir. React/TypeScript bir
kod tabanında 14 görevlik önceki bir ölçüm bu kancayı −%20 token, yarıya inen arama çıktısı
ve eşit veya daha iyi kontrollerle ölçtü; [Sonuçlar](#sonuçlar) bölümündeki 600 görevlik
rakamlar Cursor CLI ve OpenCode üzerinde MCP akışını ölçüyor.
Cursor'ın istem kancası bağlam ekleyemediği için orada bu işi kural yapar. `--no-hook`,
`--repo-id` (tekrarlanabilir) ve `--bce-command` dosyaları ayarlar; tüm init komutları tekrar
çalıştırılmaya uygundur.

**İki ajan modu.** Ajanın cevaba ne kadar dayanacağını `BCE_AGENT_MODE` belirler. Tüm init
komutları bu değeri sunucu girdisinin `env` bloğuna yazar (`.cursor/mcp.json`, `.mcp.json`,
`.codex/config.toml`; `opencode.json` içinde `environment`). Değeri değiştirip MCP sunucusunu yeniden yükledikten
sonra ajan diğer akışla çalışır:

- `hint` (**başlangıç**, varsayılan): ajan `payload.files` ile başlar. Kapsanmayan
  tanımlayıcıların (`coverage.unresolved_identifiers`) dosyalarını ekler; aramaya yalnızca
  motor cevabın eksik olabileceğini söylediğinde (`coverage.likely_incomplete`) geçer.
- `trust` (**doğru kabul**): `payload.files` cevabın kendisidir. Ajan bu dosyaları doğrudan
  açar ve depoda arama yapmaz.

Cursor CLI ile 600 görevde `hint` görev başına 59k token ile %92.9 dosya recall'una, `trust`
45k token ile %92.3'e ulaştı; CLI tek başına %95.6 ve 264k.

Sunucu aktif modun adımlarını araç açıklamasına ve her cevabın `payload.workflow` alanına
yazar; kurallar ajana bu adımları izlemesini söyler. Bu yüzden mod değiştirmek kural
dosyasında değişiklik gerektirmez. `--mode hint|trust` init sırasında modu seçer; tekrar
çalıştırma mevcut değeri korur. Ayrıntılar: [docs/mcp.md](docs/mcp.md#agent-mode).

MCP yapılandırmasını ekledikten veya değiştirdikten sonra **Cursor veya VS Code'u
yeniden başlatın** (veya Komut Paleti → “Developer: Reload Window”). Sunucu sekiz araçla
(indeksleme açıkken on araçla) etkin görünmelidir. Ayrıntı: [docs/mcp.md](docs/mcp.md).
Elle yapmanın karşılıkları:

**Cursor** — her proje için kullanıcı yapılandırması `~/.cursor/mcp.json`, ya da yerelde
kalan bir proje `.cursor/mcp.json` (dizin gitignore'dadır):

```json
{
  "mcpServers": {
    "bgts-context-engine": {
      "command": "bce",
      "args": ["serve-mcp"],
      "env": { "BCE_DB_HOST": "localhost", "BCE_DB_NAME": "bce" }
    }
  }
}
```

**VS Code** — kullanıcı MCP ayarları, ya da proje `.vscode/mcp.json` (o da gitignore'dadır):

```json
{
  "servers": {
    "bgts-context-engine": {
      "type": "stdio",
      "command": "bce",
      "args": ["serve-mcp"],
      "env": { "BCE_DB_HOST": "localhost", "BCE_DB_NAME": "bce" }
    }
  }
}
```

**Claude Code** — tek komut:

```bash
claude mcp add bgts-context-engine --env BCE_DB_HOST=localhost -- bce serve-mcp
```

**Claude Desktop** — `claude_desktop_config.json` içinde Cursor ile aynı blok.

`"command": "bce"` bağlı kalmazsa editör PATH'te `bce` görmüyordur. Yukarıdaki gibi
kurun, ya da kalıcı kurulum olmadan `uvx` kullanın:

```json
"command": "uvx",
"args": ["--from", "bgts-context-engine[mcp]", "bce", "serve-mcp"]
```

Sonra ajanınıza, açık olan dosyayı değil deponun tamamını gerektiren bir şey sorun:
*"session TTL'i değiştirirsem ne bozulur?"* Ajan `get_context_for_task`'ı çağırır;
[docs/mcp.md](docs/mcp.md) içindeki diğer araçlar ise oradan devam etmesini sağlar — kesin
çağıranlar, bir değişikliğin etki alanı, bir adın arkasındaki sembol — dosya adlarını tahmin
etmeden.

## Ne döner

Dosya yolu listesi değil. Gerekçesi ekli, sıralanmış bir paket:

```json
{
  "anchors": {
    "python::api::webhooks::handle_meeting_webhook#a3f1": ["explicit", "lexical"],
    "python::auth::session::refresh_session#88c2":        ["lexical", "semantic"]
  },
  "context": {
    "items": [
      { "symbol_id": "...refresh_session#88c2", "name": "refresh_session", "kind": "function",
        "file_id": "my-service:src/auth/session.py", "line": 41, "detail_level": "full",
        "graph_distance": 0, "score": 11.42, "tokens": 214, "content": "def refresh_session(...)" },
      { "symbol_id": "...SESSION_TTL#4b0d", "name": "SESSION_TTL", "kind": "constant",
        "file_id": "my-service:src/auth/config.py", "line": 12, "detail_level": "signature",
        "graph_distance": 2, "score": 6.10,  "tokens": 31,  "content": "SESSION_TTL: int" }
    ],
    "used_tokens": 1388, "budget": 1500, "included": 20, "skipped": 0
  },
  "coverage": {
    "anchor_source_count": 3, "connected_component_ratio": 0.875,
    "top_candidate_margin": 1.84, "orphan_ratio": 0.0,
    "touches_god_node": false, "commit_mismatch": false,
    "confidence": "high"
  }
}
```

Burada bir vektör veritabanının veremeyeceği üç şey var:

**`anchors`**, motorun *neden* oraya baktığını ve hangi bağımsız kaynakların hemfikir
olduğunu söyler. Üç kaynağın uyuşması genellikle doğrudur; bir kaynak tahmindir.

**`coverage`** bir güven raporudur. `confidence: "low"`, motorun bir şey bulduğu ama
doğrulayamadığı anlamına gelir — bir ajanın düzenlemeye başlamak yerine soru sorması
gereken an. `commit_mismatch` ise indeksin çalışma ağacınızın gerisinde kaldığını söyler.

**`detail_level`** graf mesafesiyle azalır: değiştirdiğiniz sembol tam gövdesiyle,
komşuları imza olarak, dış halka `name @ dosya:satır` biçiminde gelir. Gerçekten ilgili
yirmi sembolün 1500 token'a sığması bu sayede olur — bir ajanın her turda taşıyabileceği
kadar kısa. Her öge `file_id` ve `line` da taşır; ajan sembolü aramak yerine dosyayı açar.

Bağlam seçici açıkken ögeler bir de **`tier`** taşır: görevin en muhtemel değiştireceği iki
üç dosya için `full`, muhtemelen ilgili dosyalar için tek satırlık `stub`
(`yol - N aday sembol: …`); `coverage.selector` neyin neden kesildiğini söyler
([docs/retrieval.md](docs/retrieval.md#context-selection-optional); modeller ve kurulum
[docs/selector.md](docs/selector.md) içinde).

## Nasıl çalışır

<div align="center">
<img src="https://raw.githubusercontent.com/bgts-ai-org/bgts-context-engine/main/docs/assets/architecture-overview.tr.png" alt="BGTS Context Engine mimarisi: depo bir kod grafına dönüşür, embedding'ler giriş noktalarını bulur, sorgu anında motor çapaları atar, grafı genişletir, skorlar, daraltır, isteğe bağlı seçer ve paketi birleştirir" width="820">
</div>

<details>
<summary>Aynı hat, metin olarak</summary>

```
görev metni
   │
   ├─ çapalar       yedi bağımsız kaynak giriş noktası önerir:
   │                açık isimler ve route'lar, dosya yolları, görev geçmişi,
   │                tam metin, kod kullanımı, vektör, etki
   ├─ genişletme    sabit şekilli graf gezinmesi: çağıranlar 2 hop, çağrılanlar 1,
   │                referanslar, tip hiyerarşisi, aynı dosyadaki kardeşler
   ├─ skorlama      çapa gücü, referans türü, görev sinyali, yakınlık, tür önceliği,
   │                semantik sıra, churn, merkezîlik, yaprak ve test cezaları
   │                ve kenar kaynağı üzerinden ağırlıklı toplam
   ├─ kapsam        çağıranın göremeyeceği depoları düşür
   ├─ daraltma      en iyi N tanesini tut
   ├─ seçim         isteğe bağlı: bir karar modeli N dosyayı
   │                tam / tek satır stub / atıldı olarak katmanlar
   ├─ birleştirme   token bütçesine sığdır, uzaklaştıkça daha ucuz detay
   └─ kapsama       sonucun ne kadarının güvenilir olduğunu raporla
```

</details>

Çağıranlar iki hop, çağrılanlar bir hop uzağa gider; bu bilinçlidir: bir fonksiyonu
değiştirdiğinizde bozulan şey onun yukarısındadır. Yapısal sinyaller içinde en ağır ağırlığı
referans türü taşır, çünkü bir değere *yazan* yer hatanın yaşadığı yerdir, *okuyan* yer ise
genelde yalnızca sonuçtur. Merkezîlik derece 20'de doyar, çünkü bir logger her şeye dokunur
ve hiçbir şeyi açıklamaz.

Formülün tamamı, her ağırlık ve güven eşikleri
[docs/retrieval.md](docs/retrieval.md) içindedir.

## Web arayüzü

`bce serve`, `/ui/` adresinde gerçek bir getirme çağrısını aşama aşama oynatan bir arayüz
sunar: çapaların yanması, genişlemenin yayılması, adayların skorlanıp kesilmesi.

<div align="center">
<video src="https://github.com/user-attachments/assets/b602edaa-1189-480e-ac4d-294c173d0067" width="820" controls playsinline>
BGTS Context Engine web arayüzünün turu.
</video>
</div>

## Öne çıkanlar

- **Parçalar değil, kod grafı.** Semboller, `CALLS`, `REFERENCES`, `INHERITS`,
  `IMPLEMENTS`, `IMPORTS`, HTTP `ROUTES_TO` handler'ları ve açıkladıkları sembole bağlanmış
  `WHY:` yorumları.
- **Yapısı gereği deterministik.** Sıralı gezinme, kararlı eşitlik bozma, sürümlenmiş
  skorlama ağırlıkları. `bce bench` her vakayı tekrar tekrar koşup çıktıyı karşılaştırarak
  bunu doğrular.
- **İsteğe bağlı olarak token'ın yedide birinden azı.** Bağlam seçici görevin değiştirdiği
  dosyaları tutar, gerisini birer satırla listeler: 600 gerçek değişiklikte cevap başına
  8.310 → 1.121 token, Jev ile dosya recall'u 94.4 → 93.3; hata olursa düz sıralamaya düşer.
- **Altı dil.** Python, JavaScript ve TypeScript yerleşik; Java, C# ve Go `langs` ekiyle.
  [Yeni bir dil eklemek](docs/languages.md#adding-a-language) iki dosyaya dokunur.
- **Diller arası çağrı kenarları.** React Native ve Expo köprüleri, TypeScript'teki
  `NativeModules.Foo.bar()` çağrısını Objective-C, Swift veya Kotlin'deki `bar` ile
  birleştirir — tek bir parser'ın göremeyeceği bir boşluk.
- **Denetlenebilir kenar kaynağı.** Gerçek bir derleyici indeksinden `scip`, sözdiziminden
  `treesitter`, örüntü eşleşmesinden `heuristic`. Farklı skorlanır, her yanıtta raporlanır.
- **Artımlı yeniden indeksleme.** Neyin yeniden ayrıştırılacağına `git diff` karar verir.
  Sembol kimlikleri dosya taşımalarından ve yeniden biçimlendirmeden sağ çıkar.
- **Tek veritabanı.** Apache AGE ve pgvector aynı PostgreSQL içinde; tek sorgu bir graf
  gezinmesini, bir vektör aramasını ve bir SQL filtresini birleştirir.
- **Tek gerçeklemeden MCP ve REST.** stdio üzerinden odaklı bir araç kümesi, HTTP üzerinden
  aynı fonksiyonlar. Aralarında kayma olacak bir şey yok.
- **Kendini açıklayan bir arayüz.** `/ui` wheel içinde gelir ve gerçek bir getirme çağrısını
  aşama aşama oynatır: çapaların yanması, genişlemenin yayılması, adayların skorlanıp
  kesilmesi.
- **Çevrimdışı çalışır.** Varsayılan embedding sağlayıcısı, token özetleri üzerinde
  deterministik aritmetiktir. API anahtarı yok, ağ yok, tekrarlanabilir ölçümler. `openai`
  sağlayıcısı OpenAI uyumlu herhangi bir `/v1/embeddings` sunucusuna konuşur (vLLM, TEI,
  Ollama); [jina-code-embeddings-1.5b](https://huggingface.co/jinaai/jina-code-embeddings-1.5b)
  gibi bir model çevre içinde çalışır.

## Desteklenen modeller

Embedding yalnızca grafın tanımadığı bir görev metninde giriş noktası bulur. Cevabı asla
sıralamaz. Varsayılan `hashing`'dir: deterministik aritmetik, API anahtarı yok, ağ yok.
Gerçek bir kod modeli için `BCE_EMBEDDING_PROVIDER` ve `BCE_EMBEDDING_MODEL`'i şunlardan
birine ayarlayın:

| Model | Sağlayıcı | Boyut |
| --- | --- | --- |
| [`voyage-code-3`](https://blog.voyageai.com/2024/12/04/voyage-code-3/) | Voyage AI (`voyage`) | 1024 |
| [`voyage-code-4`](https://blog.voyageai.com/2026/08/13/voyage-code-4/) | Voyage AI (`voyage`) | 1024 |
| [`jina-code-embeddings-1.5b`](https://huggingface.co/jinaai/jina-code-embeddings-1.5b) | OpenAI uyumlu (`openai`) | 1536 |

Voyage barındırılan bir API'dir — `pip install "bgts-context-engine[embed]"` ve
`BCE_VOYAGE_API_KEY`. Jina çevre-içi yoldur: `/v1/embeddings` konuşan herhangi bir
sunucu (vLLM, TEI, Ollama). Model veya boyutu değiştirmek yeniden indekslemedir
(`bce migrate --reset-embeddings`). Ayarlar
[docs/deployment.md](docs/deployment.md) içindedir.

**Sırada** — aynı `openai` soketi, henüz ayrı bir getirme profili yok:

- [`jina-code-embeddings-0.5b`](https://huggingface.co/jinaai/jina-code-embeddings-0.5b)
  — 1.5b'nin küçük kardeşi; 1.5B parametreyi taşıyamayan makineler için.
- [`Nomic Embed Code`](https://huggingface.co/nomic-ai/nomic-embed-code) — açık kaynaklı
  7B kod getiricisi.

### Seçici modeller

İsteğe bağlı [bağlam seçici](#ne-döner), sıralanmış cevabın üzerinde bu karar modellerinden
birini çalıştırır. `BCE_SELECTOR`'ı modelin adına ayarlayın; ayarlanmazsa (`off`) motor
K=20'de düz sıralamayı döner.

| `BCE_SELECTOR` | Model | Nerede çalışır | Dosya recall @50 · token* |
| --- | --- | --- | --- |
| `jev` | [Jev 1.13](https://openrouter.ai/typesafe/jev-1.13) (`typesafe/jev-1.13`, TypeSafe) | barındırılan: [OpenRouter](https://openrouter.ai/docs/guides/community/jev) ya da [TypeSafe API'si](https://www.typesafeai.org/guides/jev-api-quickstart) | 93.3 · 1.121 |
| `decider-2b` | [Mapika/decider-2b](https://huggingface.co/Mapika/decider-2b) (açık ağırlık, Apache-2.0) | kendi GPU'nuz (16 GB+), `decider.serve` ile | 89.8 · 1.355 |
| `decider-4b` | [Mapika/decider-4b](https://huggingface.co/Mapika/decider-4b) (açık ağırlık, Apache-2.0) | kendi GPU'nuz (32 GB), `decider.serve` ile | 92.1 · 1.193 |

\* 12 depoda 600 gerçek değişiklik, K=50; seçicisiz 94.4 recall ve 8.310 token'a karşı. Jev
`OPENROUTER_API_KEY` ister ve görev metniyle kod alıntılarını üçüncü tarafa gönderir;
decider'lar her şeyi çevrenizin içinde tutar ve anahtar istemez.
**Kurulum, decider sunucusunun kurulumu ve RunPod notları: [docs/selector.md](docs/selector.md).**

## Nereye oturur

|  | Embedding RAG | Language server | BGTS Context Engine |
| --- | --- | --- | --- |
| Getirme temeli | metin benzerliği | derleyici indeksi | kod grafı + çapalar |
| Dosyalar/depolar arası | zayıf | proje bazlı | evet |
| Diller arası kenarlar | yok | yok | evet, sezgisel |
| Aynı sorgu, aynı cevap | hayır | evet | evet |
| *Bir göreve* göre sıralı | benzerliğe göre | sıralı değil | evet, kapsama ile |
| Token bütçesi farkında | parça sayısı | hayır | evet, mesafeye göre detay |
| Kendi cevabını açıklar | hayır | hayır | çapa + kaynak + güven |

Bir language server kesindir ama açık olanla sınırlıdır. Embedding araması geniştir ama
hesap veremez. Bu proje ikisinin arasında durur: ilki gibi depo çapında ve diller arası,
ikincisi gibi kesin ve yeniden üretilebilir.

## Ölçme

Getirme kalitesi iddiaları, hangi görev kümesinde ölçüldüğü bilinmeden değersizdir; bu
yüzden bir skor tablosu değil, ölçüm aracının kendisi gelir. Kendi görevlerinizi ve onları
yanıtladığına inandığınız sembolleri verirsiniz:

```bash
bce bench --cases my-tasks.json --out report.json
```

Her vaka bir görev metni ve onun doğru kabul edilen `symbol_id`'lerinden oluşur. Rapor vaka
başına recall, precision, precision@1 ve MRR ile medyan ve p95 gecikmeyi verir; ayrıca
skorlardan daha önemli iki geç/kal kontrolü içerir: her vaka tekrar tekrar koşulur ve
bayt-birebir aynı sırayı döndürmek zorundadır, kapsamlı bir principal ile koşulan vakalar
ise o principal'ın okuyamayacağı bir depoyu yüzeye çıkarmamalıdır.

Zor kısım vaka dosyasını hazırlamaktır — doğru cevabın ne olduğuna elle karar vermek
demektir. Skorlama ağırlıklarındaki bir değişikliğin gerçekten iyileştirme olup olmadığını
öğrenmenin tek dürüst yolu da budur. Biçim ve örnek bir dosya
[docs/deployment.md](docs/deployment.md#benchmarking) içindedir.

`bce bench` sıralamayı ölçer. [Sonuçlar](#sonuçlar) bölümündeki rakamlar ise bir ajanın bu
sıralamayla ne yaptığını ölçer: ikinci bir düzenek gerçek ajan CLI'larını (Cursor CLI,
OpenCode) 12 depoluk görev kümesi üzerinde bir kez tek başına, bir kez de her ajan modu için
koşturur ve her koşuda token, fatura, araç çağrısı, model turu, süre ve dosya recall'unu
kaydeder. Bu düzenek ve görev kümesi henüz yayımlanmadı.

## Yol haritası

Zorluğa göre değil, ne sıklıkta gündeme geldiğine göre sıralı:

- **Her katmanda kapsam denetimi.** Kullanıcı bazlı depo filtresini Layer 3 uygular,
  Layer 1 ve 2 uygulamaz. Bu kapanana kadar API bir proxy arkasında durmalıdır —
  [SECURITY.md](SECURITY.md).
- **MCP için streamable HTTP taşıması.** Bugün MCP yüzeyi yalnızca stdio olduğu için sunucu
  ajanın yanında koşar. Uzak taşıma, tek bir indeksin tüm ekibe hizmet etmesini sağlar.
- **Daha fazla dil.** En çok istenenler Rust, Kotlin ve PHP. Sağlayıcı arayüzü, sürtünmesi
  en az katkı yolu — [docs/languages.md](docs/languages.md#adding-a-language).
- **Daha geniş SCIP alımı.** Derleyici seviyesindeki kenarlar sözdiziminden türetilenleri
  yener ve öyle skorlanır; daha fazla araç zinciri, grafın daha büyük kısmının `scip`
  kaynağını taşıması demektir.
- **Yayımlanmış bir ölçüm kümesi.** [Sonuçlar](#sonuçlar) bölümünün arkasındaki 12 açık
  depoda 600 görev ve ajan düzeneği bugün bu deponun dışında koşuyor. Yayımlanmaları,
  sonuçları yeniden üretilebilir ve yalnızca kendi koşularınız arasında değil projeler
  arasında da karşılaştırılabilir kılar.

İstekler ve itirazlar
[issue'lara](https://github.com/bgts-ai-org/bgts-context-engine/issues) — insanların
gerçekten istediği şey bu listeyi yeniden sıralar.

## Dokümantasyon

Tümü İngilizcedir.

| | |
| --- | --- |
| [Architecture](docs/architecture.md) | deterministik hat, üç katman, indeksleme |
| [Retrieval](docs/retrieval.md) | çapalar, genişletme, her skorlama ağırlığı, güven |
| [Selector models](docs/selector.md) | Jev, decider-2b ve decider-4b: seçim, kurulum, bağlam seçici yapılandırması |
| [Data model](docs/data-model.md) | düğüm etiketleri, kenar tipleri, tablolar, sembol kimliği |
| [MCP and API](docs/mcp.md) | her araç ve uç nokta, MCP yapılandırması, CLI |
| [Languages](docs/languages.md) | her parser'ın çıkardıkları ve yeni dil ekleme |
| [Deployment](docs/deployment.md) | yapılandırma referansı, işler, yedekleme, ölçüm |
| [Web interface](web/README.md) | arayüz geliştirme |

## Katkı

Katkılar memnuniyetle karşılanır — özellikle yeni diller, ki hattın en hazır olduğu katkı
türü budur.

Önce [CONTRIBUTING.md](CONTRIBUTING.md) dosyasını okuyun. Baştan bilinmesi gereken tek
kural: **determinizm ürünün kendisidir.** Aynı görevin farklı sonuç döndürmesine yol açan
bir değişiklik, açık bir opt-in bayrağı olmadan birleştirilmez; skorlamaya veya sıralamaya
dokunan her şey çıktıyı sabitleyen bir test gerektirir.

Kod, yorumlar, docstring'ler, commit mesajları ve arayüz metinleri İngilizcedir. Türkçe
yalnızca bu dosyada ve `tr` çeviri kataloglarında bulunur.

```bash
pip install -e ".[dev,mcp]"
ruff check src tests scripts && pytest
cd web && npm ci && npm test
```

## Güvenlik

Motorun kendine ait bir kimlik doğrulaması yoktur ve önünde bunu yapan bir katman
beklemektedir. Kullanıcı bazlı depo kapsamını yalnızca Layer-3 uç noktaları uygular. Bir
portu dışa açmadan önce [SECURITY.md](SECURITY.md) dosyasını okuyun ve güvenlik açıklarını
issue olarak değil, özel kanaldan bildirin.

## Lisans

[MIT](LICENSE) © BGTS.
