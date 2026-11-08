/**
 * ABD Değer Taraması — sheet tarafı (Google Apps Script), sürüm 6 (1000 hisse + geriye dönük test + trend bilgisi + Geçmiş en yeni üstte)
 *
 * Veriler artık GitHub'da çalışan programdan gelir (SEC, Google sunucularını engelliyor).
 * Bu script:
 *  1. GitHub'daki veri.csv dosyasını "Veri" sekmesine yazar.
 *  2. "Tarama" sekmesindeki formüllerin puanlamasını bekler.
 *  3. Aday listesindeki değişiklikleri e-postayla bildirir.
 */

const SAYFA = { AYAR: 'Ayarlar', VERI: 'Veri', TARAMA: 'Tarama', GECMIS: 'Geçmiş' };
const SUTUN_SAYISI = 46;
const METIN_SUTUNLARI = [0, 1, 2, 4, 5, 6, 7, 42, 43, 44, 45]; // hisse, şirket, CIK, grup, sektör, tarih, kaynak, not, güncelleme, endeks, trend

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Değer Taraması')
    .addItem('1) Veriyi şimdi çek', 'veriyiCek')
    .addItem('2) Otomatik çalışmayı kur', 'kurulum')
    .addSeparator()
    .addItem('Raporu şimdi gönder', 'raporGonder')
    .addSeparator()
    .addItem('Geriye dönük test sonuçlarını çek', 'testSonuclariniCek')
    .addToUi();
}

function ayarOku_() {
  const vals = SpreadsheetApp.getActive().getSheetByName(SAYFA.AYAR).getRange('A1:B40').getValues();
  const h = {};
  vals.forEach(r => { if (r[0]) h[String(r[0]).trim()] = r[1]; });
  const adres = String(h['Veri adresi (GitHub)'] || '').trim();
  if (!adres.startsWith('https://')) {
    throw new Error('Ayarlar sekmesinde "Veri adresi (GitHub)" satırına veri.csv dosyasının raw adresini yaz. ' +
                    'Örnek: https://raw.githubusercontent.com/KULLANICI_ADIN/deger-taramasi/main/veri.csv');
  }
  const eposta = String(h['Rapor e-posta adresi'] || '').trim() || Session.getEffectiveUser().getEmail();
  return { adres: adres, eposta: eposta };
}

function uyar_(mesaj) {
  try { SpreadsheetApp.getUi().alert(mesaj); } catch (e) { Logger.log(mesaj); }
}

function kurulum() {
  ayarOku_();
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  // GitHub pazartesi 07:00'de çalışır; script 10:00'da sonucu alıp raporu yollar.
  ScriptApp.newTrigger('haftalikIs').timeBased()
    .onWeekDay(ScriptApp.WeekDay.MONDAY).atHour(10).inTimezone('Europe/Istanbul').create();
  uyar_('Kuruldu. Her pazartesi sabah veriler güncellenecek ve rapor e-postana gelecek.');
}

function haftalikIs() {
  veriyiCek(true);
  SpreadsheetApp.flush();
  Utilities.sleep(30000); // formüllerin ve fiyatların yenilenmesi için
  raporGonder();
}

/** GitHub'daki veri.csv'yi okuyup Veri sekmesine yazar. */
function veriyiCek(sessiz) {
  const ayar = ayarOku_();
  const res = UrlFetchApp.fetch(ayar.adres + '?t=' + Date.now(), { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) {
    throw new Error('veri.csv okunamadı (kod ' + res.getResponseCode() + '). Ayarlar\'daki adresi ve ' +
                    'GitHub deposunun herkese açık olduğunu kontrol et.');
  }
  const csv = Utilities.parseCsv(res.getContentText('UTF-8'));
  if (!csv.length || csv[0][0] !== 'ticker') throw new Error('veri.csv beklenen biçimde değil.');
  const satirlar = csv.slice(1).filter(r => r[0]).map(r => {
    const s = [];
    for (let i = 0; i < SUTUN_SAYISI; i++) {
      const v = r[i] === undefined ? '' : r[i];
      if (v === '') { s.push(''); continue; }
      if (i === 2 || i === 6) { s.push("'" + v); continue; }            // CIK ve tarih metin kalsın
      if (METIN_SUTUNLARI.indexOf(i) >= 0) { s.push(v); continue; }
      const n = Number(v);
      s.push(isNaN(n) ? v : n);
    }
    return s;
  });
  const sh = SpreadsheetApp.getActive().getSheetByName(SAYFA.VERI);
  const son = sh.getLastRow();
  if (son >= 2) sh.getRange(2, 1, son - 1, SUTUN_SAYISI).clearContent();
  if (satirlar.length) sh.getRange(2, 1, satirlar.length, SUTUN_SAYISI).setValues(satirlar);
  const hatali = satirlar.filter(r => String(r[42]).indexOf('Hata') === 0).length;
  const zaman = satirlar.length ? satirlar[0][43] : '';
  if (sessiz !== true) {
    uyar_(satirlar.length + ' hisse yüklendi (' + hatali + ' hatalı). Veri zamanı: ' + zaman +
          '\nPuanlar birkaç saniye içinde Sonuçlar sekmesinde görünür.');
  }
  return zaman;
}

function raporGonder() {
  const ayar = ayarOku_();
  const ss = SpreadsheetApp.getActive();
  const tar = ss.getSheetByName(SAYFA.TARAMA);
  const son = tar.getLastRow();
  if (son < 2) return;
  const baslik = tar.getRange(1, 1, 1, tar.getLastColumn()).getValues()[0];
  const kol = ad => baslik.indexOf(ad);
  const satirlar = tar.getRange(2, 1, son - 1, tar.getLastColumn()).getValues().filter(r => r[kol('Hisse')]);
  const iHisse = kol('Hisse'), iSirket = kol('Şirket'), iTop = kol('Toplam Puan'),
        iU = kol('Ucuzluk'), iK = kol('Kalite'), iS = kol('Sağlamlık'), iB = kol('Büyüme'),
        iAday = kol('Aday'), iBayrak = kol('Uyarılar'), iPort = kol('Portföyde'), iSira = kol('Sıra'), iEndeks = kol('Endeks'), iTrend = kol('Trend');

  const adaylar = satirlar.filter(r => r[iAday] === 'ADAY').sort((a, b) => b[iTop] - a[iTop]);
  const p = PropertiesService.getScriptProperties();
  const oncekiMetin = p.getProperty('oncekiAdaylar');
  const onceki = JSON.parse(oncekiMetin || '[]');
  const simdiki = adaylar.map(r => r[iHisse]);
  const yeni = oncekiMetin ? simdiki.filter(t => onceki.indexOf(t) < 0) : [];
  const cikan = onceki.filter(t => simdiki.indexOf(t) < 0);
  p.setProperty('oncekiAdaylar', JSON.stringify(simdiki));

  const gec = gecmisSayfasi_();
  const bugun = new Date();
  const kayit = [];
  if (!oncekiMetin) kayit.push([bugun, '', 'Canlı takip başladı: ' + simdiki.length + ' aday', '', 'Canlı']);
  yeni.forEach(t => { const r = adaylar.find(x => x[iHisse] === t); kayit.push([bugun, t, 'Listeye girdi', r ? r[iTop] : '', 'Canlı']); });
  cikan.forEach(t => { const r = satirlar.find(x => x[iHisse] === t); kayit.push([bugun, t, 'Listeden çıktı', r ? r[iTop] : '', 'Canlı']); });
  if (kayit.length) {  // en yeni kayıtlar her zaman en üstte
    gec.insertRowsBefore(2, kayit.length);
    gec.getRange(2, 1, kayit.length, 5).setValues(kayit);
    gecmisBicimle_(gec);
  }

  // Veri eski mi? (GitHub çalışmadıysa uyar)
  const veriZamani = String(ss.getSheetByName(SAYFA.VERI).getRange('AR2').getValue());
  const veriTarihi = new Date(veriZamani.replace(' UTC', 'Z').replace(' ', 'T'));
  const eski = isNaN(veriTarihi) || (bugun - veriTarihi) > 8 * 24 * 3600 * 1000;

  const yuzde = v => (typeof v === 'number' ? Math.round(v * 100) : '-');
  const tablo = (liste, baslikMetni) => {
    if (!liste.length) return '<p><i>' + baslikMetni + ': yok</i></p>';
    let h = '<h3>' + baslikMetni + '</h3><table border="1" cellpadding="4" style="border-collapse:collapse;font-family:Arial;font-size:13px">' +
      '<tr style="background:#1F3864;color:#fff"><th>Sıra</th><th>Hisse</th><th>Şirket</th><th>Endeks</th><th>Toplam</th><th>Ucuzluk</th><th>Kalite</th><th>Sağlamlık</th><th>Büyüme</th><th>Trend</th><th>Uyarılar</th></tr>';
    liste.forEach(r => {
      h += '<tr><td>' + r[iSira] + '</td><td><b>' + r[iHisse] + '</b></td><td>' + r[iSirket] + '</td><td>' + (r[iEndeks] || '') + '</td><td>' + yuzde(r[iTop]) +
           '</td><td>' + yuzde(r[iU]) + '</td><td>' + yuzde(r[iK]) + '</td><td>' + yuzde(r[iS]) + '</td><td>' + yuzde(r[iB]) +
           '</td><td>' + (r[iTrend] || '') + '</td><td>' + (r[iBayrak] || '') + '</td></tr>';
    });
    return h + '</table>';
  };
  const bul = t => satirlar.find(x => x[iHisse] === t);
  let html = '<div style="font-family:Arial">';
  if (eski) html += '<p style="color:#C00000"><b>Dikkat:</b> Veriler bir haftadan eski (' + veriZamani +
                    '). GitHub\'daki haftalık çalışma başarısız olmuş olabilir, Actions sekmesine bak.</p>';
  html += '<p>Haftalık değer taraması sonucu. Puanlar 0-100 arası, aynı gruptaki diğer hisselere göre yüzdeliktir.</p>';
  html += tablo(yeni.map(bul).filter(Boolean), 'Bu hafta listeye yeni girenler');
  html += cikan.length ? '<h3>Listeden çıkanlar</h3><p>' + cikan.join(', ') + '</p>' : '';
  html += tablo(adaylar.slice(0, 15), 'Mevcut adaylar (ilk 15)');
  html += tablo(satirlar.filter(r => r[iPort] === 'Evet').sort((a, b) => b[iTop] - a[iTop]), 'Portföyündeki hisselerin puanları');
  html += '<p style="color:#777;font-size:12px">Bu liste yatırım tavsiyesi değildir; araştırmaya nereden başlayacağını gösterir. ' +
          'Detaylar: ' + ss.getUrl() + '</p></div>';
  const konu = 'Değer taraması: ' + adaylar.length + ' aday' + (yeni.length ? ', ' + yeni.length + ' yeni giriş' : '');
  MailApp.sendEmail({ to: ayar.eposta, subject: konu, htmlBody: html });
}

// ---------------------------------------------------------------- geriye dönük test
function csvOku_(url) {
  const res = UrlFetchApp.fetch(url + '?t=' + Date.now(), { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) throw new Error(url + ' okunamadı (kod ' + res.getResponseCode() + '). Test henüz çalıştırılmamış olabilir.');
  return Utilities.parseCsv(res.getContentText('UTF-8')).map(r => r.map(v => (v !== '' && !isNaN(Number(v))) ? Number(v) : v));
}

function testSonuclariniCek() {
  const ayar = ayarOku_();
  const taban = ayar.adres.replace(/veri\.csv.*$/, 'backtest/');
  const ozet = csvOku_(taban + 'ozet.csv');
  const yillik = csvOku_(taban + 'yillik.csv');
  const dilim = csvOku_(taban + 'dilimler.csv');
  const egri = csvOku_(taban + 'egri.csv');
  const bilgi = JSON.parse(UrlFetchApp.fetch(taban + 'bilgi.json?t=' + Date.now()).getContentText('UTF-8'));

  const sh = SpreadsheetApp.getActive().getSheetByName('Geriye Dönük Test');
  sh.getRange('A3:L200').clearContent().clearFormat();
  sh.getRange('Z1:AH1000').clearContent();
  const yaz = (satir, baslik, tablo, yuzdeKolonlari) => {
    sh.getRange(satir, 1).setValue(baslik).setFontWeight('bold').setFontSize(12).setFontColor('#1F3864');
    const r = sh.getRange(satir + 1, 1, tablo.length, tablo[0].length);
    r.setValues(tablo.map(x => { while (x.length < tablo[0].length) x.push(''); return x; }));
    sh.getRange(satir + 1, 1, 1, tablo[0].length).setFontWeight('bold').setBackground('#1F3864').setFontColor('#FFFFFF').setWrap(true);
    for (let c = 1; c < tablo[0].length; c++) {
      const yuzde = !yuzdeKolonlari || yuzdeKolonlari.indexOf(tablo[0][c]) >= 0;
      sh.getRange(satir + 2, c + 1, tablo.length - 1, 1).setNumberFormat(yuzde ? '0.0%' : '0.00');
    }
    return satir + tablo.length + 2;
  };
  sh.getRange('A3').setValue('Dönem: ' + bilgi.donem + '   |   İşlem maliyeti (tek yön): %' + (bilgi.islem_maliyeti * 100).toFixed(2) +
                             '   |   Çalışma: ' + bilgi.calisma);
  let s = 4;
  (bilgi.uyarilar || []).forEach(u => { sh.getRange(s, 1).setValue('Not: ' + u).setFontColor('#B45F06'); s++; });
  s = yaz(s + 1, 'Özet', ozet, ['Yıllık getiri', 'Oynaklık (yıllık)', 'En büyük düşüş', 'Toplam getiri', "SPY'yi geçtiği hafta oranı"]);
  s = yaz(s, 'Puanın öngörü gücü: puan dilimlerine göre sonraki ayın getirisi (5 en yüksek puan)', dilim, null);
  s = yaz(s, 'Yıllara göre getiri', yillik, null);
  sh.getRange(1, 26, egri.length, egri[0].length).setValues(egri);
  sh.getRange(2, 26, egri.length - 1, 1).setNumberFormat('dd.mm.yyyy');
  const n = testGecmisiniYukle_(taban);
  uyar_('Geriye dönük test sonuçları yüklendi.' + (n ? ' Geçmiş sekmesine testten ' + n + ' giriş-çıkış kaydı eklendi.' : ''));
}

/** Geçmiş sekmesi: Tarih | Hisse | Olay | Toplam Puan | Kaynak (Canlı = sistemin o hafta gerçekten verdiği sinyal, Test = geriye dönük test). */
function gecmisSayfasi_() {
  const gec = SpreadsheetApp.getActive().getSheetByName(SAYFA.GECMIS);
  if (gec.getMaxColumns() < 5) gec.insertColumnsAfter(gec.getMaxColumns(), 5 - gec.getMaxColumns());
  gec.getRange(1, 1, 1, 5).setValues([['Tarih', 'Hisse', 'Olay', 'Toplam Puan', 'Kaynak']]).setFontWeight('bold');
  gec.setFrozenRows(1);
  return gec;
}

function gecmisBicimle_(gec) {
  const n = gec.getLastRow() - 1;
  if (n < 1) return;
  gec.getRange(2, 1, n, 1).setNumberFormat('dd.mm.yyyy');
  gec.getRange(2, 4, n, 1).setNumberFormat('0%');
  gec.getRange(2, 1, n, 5).setFontWeight('normal');
}

/** Testin son bir yıllık giriş-çıkışlarını Geçmiş'e 'Test' etiketiyle yazar; eski test satırlarını yeniler, canlı kayıtlara dokunmaz. */
function testGecmisiniYukle_(taban) {
  const res = UrlFetchApp.fetch(taban + 'gecmis.csv?t=' + Date.now(), { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) return 0;
  const test = Utilities.parseCsv(res.getContentText('UTF-8')).slice(1).filter(r => r[0]).map(r => {
    const d = r[0].split('-').map(Number);
    return [new Date(d[0], d[1] - 1, d[2]), r[1], r[2], r[3] === '' ? '' : Number(r[3]), 'Test'];
  });
  const gec = gecmisSayfasi_();
  const son = gec.getLastRow();
  const mevcut = son >= 2 ? gec.getRange(2, 1, son - 1, 5).getValues().filter(r => r[4] !== 'Test' && (r[0] || r[1] || r[2])) : [];
  mevcut.forEach(r => { if (!r[4]) r[4] = 'Canlı'; });
  // En yeni üstte; aynı tarihte canlı kayıt önce, sonra girenler, sonra çıkanlar
  const tum = mevcut.concat(test).sort((a, b) => (b[0] - a[0]) || (a[4] === b[4] ? 0 : a[4] === 'Canlı' ? -1 : 1) ||
    (a[2] === b[2] ? 0 : a[2] === 'Listeye girdi' ? -1 : 1) || String(a[1]).localeCompare(String(b[1])));
  if (son >= 2) gec.getRange(2, 1, son - 1, 5).clearContent();
  if (gec.getMaxRows() < tum.length + 1) gec.insertRowsAfter(gec.getMaxRows(), tum.length + 1 - gec.getMaxRows());
  if (tum.length) gec.getRange(2, 1, tum.length, 5).setValues(tum);
  gecmisBicimle_(gec);
  return test.length;
}
