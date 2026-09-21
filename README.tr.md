<div align="center">

<img src="https://raw.githubusercontent.com/bgts-ai-org/bgts-context-engine/main/docs/assets/social-preview.png" alt="BGTS Context Engine" width="820">

**Yapay zekâ kodlama ajanları için deterministik kod-graf bağlamı.**

*"Toplantı webhook'unda login timeout neden tetikleniyor?"* diye sorun; cevabı gerçekten
veren sekiz sembolü sıralanmış, bütçelenmiş ve yeniden üretilebilir şekilde alın.

[![PyPI](https://img.shields.io/pypi/v/bgts-context-engine.svg)](https://pypi.org/project/bgts-context-engine/)
[![Python](https://img.shields.io/pypi/pyversions/bgts-context-engine.svg)](https://pypi.org/project/bgts-context-engine/)
[![CI](https://github.com/bgts-ai-org/bgts-context-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/bgts-ai-org/bgts-context-engine/actions/workflows/ci.yml)
[![Lisans: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-uyumlu-000000.svg)](docs/mcp.md)
[![Yıldızlar](https://img.shields.io/github/stars/bgts-ai-org/bgts-context-engine?style=flat&logo=github)](https://github.com/bgts-ai-org/bgts-context-engine/stargazers)

[Hızlı başlangıç](#hızlı-başlangıç) · [Ajanınızdan kullanma](#ajanınızdan-kullanma) · [Nasıl çalışır](#nasıl-çalışır) · [Desteklenen modeller](#desteklenen-modeller) · [Dokümantasyon](#dokümantasyon) · [English](README.md)

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

**Aynı görev metni, aynı commit üzerinde, aynı bağlam paketini döner.** Getirme yolunda
model yok, saat yok, rastgelelik yok. Bir ajan hatalı bir değişiklik yaptığında, ona tam
olarak ne söylendiğini yeniden oynatabilir, yanlış sembolü yüzeye çıkaran aşamayı bulabilir
ve o aşamayı düzeltebilirsiniz.

Motor, insanların çalıştırabilmesi için yayımlanır. Aynı şeyi kendi çevrelerinde isteyen
kurumlar — indeksleme, kurulum, depolarınıza göre ayarlanmış skorlama veya etrafındaki
ajan yığını konusunda yardım — [BGTS](https://www.bgts.com) ile
danışmanlık olarak iletişime geçebilir. **opensource-ai@bgts.com** adresine yazın.

<div align="center">
<video src="https://github.com/user-attachments/assets/b602edaa-1189-480e-ac4d-294c173d0067" width="820" controls playsinline>
BGTS Context Engine web arayüzünün turu.
</video>
</div>

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

MCP yapılandırmasını ekledikten veya değiştirdikten sonra **Cursor veya VS Code'u
yeniden başlatın** (veya Komut Paleti → “Developer: Reload Window”). Sunucu sekiz araçla
(indeksleme açıkken on araçla) etkin görünmelidir. Ayrıntı: [docs/mcp.md](docs/mcp.md).

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
      { "symbol_id": "...refresh_session#88c2", "detail_level": "full",
        "graph_distance": 0, "score": 11.42, "tokens": 214, "content": "def refresh_session(...)" },
      { "symbol_id": "...SESSION_TTL#4b0d",     "detail_level": "signature",
        "graph_distance": 2, "score": 6.10,  "tokens": 31,  "content": "SESSION_TTL: int" }
    ],
    "used_tokens": 2913, "budget": 4000, "included": 8, "skipped": 0
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
sekiz sembolün 4000 token'a sığması bu sayede olur.

## Nasıl çalışır

```
görev metni
   │
   ├─ çapalar       dört bağımsız kaynak giriş noktası önerir:
   │                açık isimler, görev geçmişi, tam metin, vektör
   ├─ genişletme    sabit şekilli graf gezinmesi: çağıranlar 2 hop, çağrılanlar 1,
   │                referanslar, tip hiyerarşisi, aynı dosyadaki kardeşler
   ├─ skorlama      referans türü, görev sinyali, merkezîlik, mesafe,
   │                yaprak cezası ve kenar kaynağı üzerinden ağırlıklı toplam
   ├─ kapsam        çağıranın göremeyeceği depoları düşür
   ├─ daraltma      en iyi N tanesini tut
   ├─ birleştirme   token bütçesine sığdır, uzaklaştıkça daha ucuz detay
   └─ kapsama       sonucun ne kadarının güvenilir olduğunu raporla
```

Çağıranlar iki hop, çağrılanlar bir hop uzağa gider; bu bilinçlidir: bir fonksiyonu
değiştirdiğinizde bozulan şey onun yukarısındadır. En ağır ağırlığı referans türü taşır,
çünkü bir değere *yazan* yer hatanın yaşadığı yerdir, *okuyan* yer ise genelde yalnızca
sonuçtur. Merkezîlik derece 20'de doyar, çünkü bir logger her şeye dokunur ve hiçbir şeyi
açıklamaz.

Formülün tamamı, her ağırlık ve güven eşikleri
[docs/retrieval.md](docs/retrieval.md) içindedir.

## Öne çıkanlar

- **Parçalar değil, kod grafı.** Semboller, `CALLS`, `REFERENCES`, `INHERITS`,
  `IMPLEMENTS`, `IMPORTS`, HTTP `ROUTES_TO` handler'ları ve açıkladıkları sembole bağlanmış
  `WHY:` yorumları.
- **Yapısı gereği deterministik.** Sıralı gezinme, kararlı eşitlik bozma, sürümlenmiş
  skorlama ağırlıkları. `bce bench` her vakayı tekrar tekrar koşup çıktıyı karşılaştırarak
  bunu doğrular.
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
- **Yayımlanmış bir ölçüm kümesi.** Açık depolar üzerinde açık bir görev kümesi, böylece
  sonuçlar yalnızca kendi koşularınız arasında değil projeler arasında da karşılaştırılabilir
  olur.

İstekler ve itirazlar
[issue'lara](https://github.com/bgts-ai-org/bgts-context-engine/issues) — insanların
gerçekten istediği şey bu listeyi yeniden sıralar.

## Dokümantasyon

Tümü İngilizcedir.

| | |
| --- | --- |
| [Architecture](docs/architecture.md) | deterministik hat, üç katman, indeksleme |
| [Retrieval](docs/retrieval.md) | çapalar, genişletme, her skorlama ağırlığı, güven |
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
