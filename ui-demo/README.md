# BCE Graph Explorer (demo UI)

BGTS Context Engine'in indexlediği kod graph'ını (düğümler + kenarlar) görselleştiren
bağımsız demo arayüz. Ana projeyle hiçbir kod bağı yoktur; yalnızca `/v1/ui/*` REST
uçlarını tüketir. Bu klasör `.gitignore`'dadır.

## Çalıştırma

1. Ana projede API'yi başlat (varsayılan `http://127.0.0.1:8000`):

```bash
bce serve
```

2. Bu klasörde:

```bash
npm install
npm run dev
```

Tarayıcıda `http://localhost:5173` açılır.

API farklı bir adresteyse `VITE_BCE_API` ortam değişkeniyle belirtin
(ör. `.env.local` içine `VITE_BCE_API=http://localhost:9000`).

## Özellikler

- Repo seçimi ve tüm reponun graph görünümü (Sigma.js WebGL + ForceAtlas2 yerleşimi)
- Düğüm rengi = tip (File, Symbol, Module, Route, ...), boyut = bağlantı derecesi
- Yakınlaştıkça etiketler görünür; hover komşuları vurgular
- Tek tıklama: sağda detay paneli (imza, docstring, kod gövdesi, komşular)
- Çift tıklama: komşuları genişlet (filtreyle gizlenen / repo dışı düğümler eklenir)
- Sembol/dosya arama ve bulunan düğüme odaklanma
- Düğüm/kenar tipine göre filtreleme, repo istatistikleri

## Kullanılan API uçları

| Uç | Amaç |
| --- | --- |
| `GET /v1/ui/repos` | Indexlenmiş repo listesi |
| `GET /v1/ui/graph?repo_id=` | Tüm repo graph'ı (`{nodes, edges}`) |
| `GET /v1/ui/stats?repo_id=` | Düğüm/kenar sayıları, dil dağılımı |
| `GET /v1/ui/node?gid=` | Düğüm detayı (body/docstring dahil) |
| `GET /v1/ui/neighbors?gid=` | Komşu genişletme |
| `GET /v1/ui/search?q=&repo_id=` | Sembol + dosya arama |
