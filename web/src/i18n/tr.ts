import type { TranslationKey } from "./en";

/** Turkish catalog. Typed against the English keys so the two stay in sync. */
export const tr: Record<TranslationKey, string> = {
  "app.title": "BCE Graf Gezgini",
  "app.subtitle": "Kod grafı görselleştirici",
  "app.company": "BilgeAdam Technology & Software",
  "app.language": "Dil",

  "nav.tabsLabel": "Kenar çubuğu sekmeleri",
  "nav.explore": "Keşfet",
  "nav.analyze": "Analiz",
  "nav.filters": "Filtreler",
  "nav.hiddenCount": "{count} gizli",

  "repo.heading": "Depo",
  "repo.placeholder": "Bir depo seçin...",

  "search.heading": "Arama",
  "search.placeholder": "Sembol veya dosya ara...",
  "search.searching": "aranıyor...",
  "search.failed": "Arama başarısız: {details}",
  "search.noResults": "Sonuç bulunamadı.",

  "filters.nodeTypes": "Düğüm tipleri",
  "filters.edgeTypes": "Kenar tipleri",

  "stats.loading": "graf yükleniyor...",
  "stats.selectRepo": "Başlamak için soldan bir depo seçin.",
  "stats.nodes": "düğüm",
  "stats.edges": "kenar",
  "stats.truncated": "graf limit nedeniyle kırpıldı",

  "empty.title": "Kod grafınızı keşfedin",
  "empty.body":
    "Soldan indekslenmiş bir depo seçin. Dosyalar, semboller ve aralarındaki çağrı ve referans kenarları etkileşimli olarak çizilir.",
  "empty.loadingGraph": "Graf yükleniyor ve yerleşim hesaplanıyor...",
  "empty.searchingContext": "Bağlam aranıyor...",

  "trace.repoLabel": "Depo",
  "trace.selectRepoFirst": "Önce bir depo seçin.",
  "trace.heading": "Görev analizi",
  "trace.taskPlaceholder": "Bir görev tanımlayın, örnek: fix login timeout in meeting webhook...",
  "trace.topN": "Top-N",
  "trace.maxTokens": "max_tokens",
  "trace.run": "Analiz Et",
  "trace.running": "Çalışıyor...",

  "player.prev": "Önceki adım",
  "player.playPause": "Oynat / duraklat",
  "player.next": "Sonraki adım",
  "player.speed": "Oynatma hızı",
  "player.close": "Analizi kapat",

  "results.candidatesChip": "ADAYLAR",
  "results.topChip": "İLK {count}",
  "results.confidence": "güven",
  "results.anchors": "çapa",
  "results.candidates": "aday",
  "results.selectedHeading": "Seçilen adaylar",
  "results.scoreHeading": "Skor sıralaması (ilk {count})",
  "results.waiting": "Skorlanan adaylar animasyon ilerledikçe burada listelenir.",

  "detail.close": "Kapat",
  "detail.docstring": "Docstring",
  "detail.note": "Not",
  "detail.code": "Kod",
  "detail.connections": "Bağlantılar ({count})",

  "error.apiUnreachable": "API'ye ulaşılamadı. `bce serve` çalışıyor mu? Detay: {details}",
  "error.graphLoad": "Graf yüklenemedi: {details}",
  "error.traceRun": "Analiz çalıştırılamadı: {details}",
  "error.noCandidates": "Pipeline hiçbir aday üretmedi. Farklı bir görev metni deneyin.",
  "error.expandNeighbors": "Komşular genişletilemedi: {details}",

  "steps.semantic.title": "Semantik adaylar",
  "steps.semantic.subtitle":
    "Embedding araması {count} aday buldu (en düşük öncelikli çapa kaynağı)",
  "steps.anchors.title": "Çapalar",
  "steps.anchors.subtitle": "{count} çapa atıldı ({sources})",
  "steps.anchors.noSources": "kaynak yok",
  "steps.expand.title": "Genişleme ({distance}. adım)",
  "steps.expand.subtitle":
    "{distance} mesafede {count} yeni düğüm keşfedildi (toplam {total})",
  "steps.score.title": "Skorlama",
  "steps.score.subtitle":
    "{signals} görev sinyali kullanılarak {count} aday skorlandı; sıcak renk yüksek skor demektir",
  "steps.narrow.title": "Daraltma: ilk {count}",
  "steps.narrow.subtitle": "{total} aday içinden ilk {count} seçildi",
  "steps.result.title": "Sonuç",
  "steps.result.subtitle": "Bağlam paketi hazır - güven {confidence}, {included} öğe dahil",
};
