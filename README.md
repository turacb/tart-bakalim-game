# Tart Bakalım — Render yayını

Bu depo yalnızca yayımlanan oyun dosyasını içerir. Kaynak proje ve soru kataloğu bu deponun dışındadır.

Yeni sürüm için kaynak projede `python build_offline.py` çalıştırılır, çıkan `public/index.html` buradaki `public/index.html` üzerine kopyalanır ve yeni commit GitHub'a gönderilir. Render, bağlı dalın her gönderiminde statik siteyi yeniden yayımlar.
