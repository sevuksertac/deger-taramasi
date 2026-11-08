# Haftalık haber bülteni

Her pazartesi öğlen Claude (zamanlanmış görev) aday listesinin ilk 25 hissesi ve o hafta listeye
yeni girenler için haberleri toplar, kısa Türkçe özet yazar ve bu klasöre koyar.
Apps Script (`bultenGonder`) pazartesi 14:00'te `bulten/son.html` dosyasını okuyup e-postayla gönderir;
dosya hazır değilse 18:00'de tekrar dener, yine yoksa "hazırlanamadı" e-postası atar.

## Hazırlama adımları (Claude için)

1. **Listeyi oku** — Google Sheets "ABD Değer Taraması" (`15T69wFT4SDW5gKQLy9CW0FUsfVV5dfcZNzUSjkxGX4Y`):
   - `Tarama!A2:B1001` (Hisse, Şirket) ve `Tarama!BD2:BI1001` (Toplam Puan, Uyarı Sayısı, Uyarılar, Aday, Sıra, Endeks).
   - `Aday` sütunu `ADAY` olanları `Sıra`ya göre artan sırala; ilk 25'i al. `sira` alanına 1..25 aday sırasını yaz.
   - `Geçmiş!A1:E60`: son 7 günde `Olay = Listeye girdi` ve `Kaynak = Canlı` olan hisseler "yeni girenler"dir
     (`yeni: true`). İlk 25'te değilse de bültene eklenir.
2. **Haberleri topla** — her şirket için son 7 günün haberleri (bilanço, rehberlik, analist notu, satın alma,
   dava, yönetim değişikliği, sert fiyat hareketi). `extended` arama modu daha güncel sonuç verir.
   O hafta haber yoksa en son önemli olayı tarihiyle tek cümlede belirt ve sıradaki bilanço tarihini yaz.
3. **Yaz** — `bulten/veri/YYYY-MM-DD.json` (biçim `olustur.py` başında). Her şirkete 2-3 cümle, sade Türkçe,
   rakamlarla. `yon`: olumlu / olumsuz / notr / yok. Her şirkete 1-2 kaynak linki. `genel`: haftanın
   piyasa özeti + gelecek haftanın bilanço takvimi (listedeki şirketler için). Siyasi yorum yapma, yatırım
   tavsiyesi verme.
4. **Üret ve gönder** — `python bulten/olustur.py bulten/veri/YYYY-MM-DD.json`, sonra `bulten/` klasörünü
   commit edip `main`'e push et (tarama iş akışını tetiklemez).
