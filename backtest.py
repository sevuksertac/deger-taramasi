"""
ABD Değer Taraması - geriye dönük test

Taramanın kurallarını (Google Sheet'teki "Tarama" sekmesinin formüllerini) geçmişe uygular:
her dönemde o güne kadar AÇIKLANMIŞ verilerle 1.000 hisselik evreni kurar, puanlar,
"ADAY" çıkan bütün hisselere eşit para koyar, listeden çıkanları satar.
Haftalık, aylık ve üç aylık değişim sıklıkları karşılaştırılır.

Sonuçlar backtest/ klasörüne yazılır: rapor.html, ozet.csv, yillik.csv, egri.csv, dilimler.csv

GitHub Actions'ta elle çalıştırılır (backtest.yml). Ek paketler: pandas, numpy, yfinance, matplotlib.
"""
import base64
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

import numpy as np
import pandas as pd

import tarama as T

# ---------------------------------------------------------------- test ayarları
BASLANGIC = os.environ.get('BT_BASLANGIC', '2015-01-02')
ADAY_SAYISI = int(os.environ.get('ADAY_SAYISI', '500'))
def _maliyet_oku():
    """Tek yön işlem maliyeti YÜZDE olarak girilir: 0.10 = %0,10 (komisyon + alış/satış farkı)."""
    ham = str(os.environ.get('BT_MALIYET_YUZDE') or '0.10').strip().replace('%', '').replace(',', '.')
    yuzde = float(ham)
    if not 0 <= yuzde <= 2:
        sys.exit(f'İşlem maliyeti %{yuzde} anlamsız görünüyor. Yüzde olarak gir: 0.10 = %0,10.')
    return yuzde / 100


ISLEM_MALIYETI = _maliyet_oku()
CIKTI = 'backtest'

# Sheet'teki Ayarlar sekmesinin varsayılanları
AGIRLIK = {'ucuzluk': 0.40, 'kalite': 0.30, 'saglamlik': 0.15, 'buyume': 0.15}
MIN_TOPLAM, MIN_UCUZLUK, IZINLI_UYARI = 0.65, 0.60, 0
ALTMAN_ALT, BORC_UST, FAIZ_ALT, SATIS_DUSUS, SULANMA, FIN_SERMAYE = 1.81, 4.0, 2.0, -0.10, 0.05, 0.06

YILLIK_FORMLAR = {'10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '20-F/A', '40-F', '40-F/A', '10-KT'}


# ---------------------------------------------------------------- "o güne kadar açıklanmış" filtresi
_KESIM = None
_orijinal_olgular = T.olgular


def _olgular_kesimli(facts, etiketler, birim='USD', ad='us-gaap'):
    """tarama.olgular'ın aynısı; yalnızca _KESIM tarihine kadar açıklanmış (filed) rakamları görür."""
    if _KESIM is None:
        return _orijinal_olgular(facts, etiketler, birim, ad)
    sonuc = {}
    ns = (facts.get('facts') or {}).get(ad) or {}
    for tag in etiketler:
        u = ((ns.get(tag) or {}).get('units') or {}).get(birim)
        if not u:
            continue
        bu = {}
        for f in u:
            if f.get('filed', '9999') > _KESIM:
                continue
            key = (f.get('start') or '') + '|' + f['end']
            if key not in bu or f.get('filed', '') > bu[key].get('filed', ''):
                bu[key] = f
        for k, v in bu.items():
            sonuc.setdefault(k, v)
    return list(sonuc.values())


T.olgular = _olgular_kesimli  # tarama.py'deki bütün hesaplar artık kesim tarihine uyar


def _dei(facts, ad, birim):
    return (((facts.get('facts') or {}).get('dei') or {}).get(ad) or {}).get('units', {}).get(birim) or []


def hisse_adedi_kesimli(facts, kesim):
    bilanco = T.son_bilanco_tarihi(facts)
    lst = [f for f in _dei(facts, 'EntityCommonStockSharesOutstanding', 'shares')
           if f.get('filed', '9') <= kesim and T._taze(f, bilanco)]
    if lst:
        son = max(f['end'] for f in lst)
        ayni = [f for f in lst if f['end'] == son]
        en_yeni = max(f.get('filed', '') for f in ayni)
        toplam = sum(f['val'] for f in ayni if f.get('filed', '') == en_yeni)
        if toplam > 0:
            return T.hisse_dogrula(toplam, facts)   # çok sınıflı şirket sağlaması (kesim tarihine uyar)
    tarih = T.son_bilanco_tarihi(facts)
    us = T.anlik(facts, T.ETIKET['hisseDolasim'], tarih, 'shares') if tarih else None
    return T.hisse_dogrula(us, facts)


def halka_acik_deger_kesimli(facts, kesim):
    lst = [f for f in _dei(facts, 'EntityPublicFloat', 'USD') if f.get('filed', '9') <= kesim]
    if not lst:
        return None, None
    son = max(lst, key=lambda f: (f['end'], f.get('filed', '')))
    return son['val'], son['end']


def olay_tarihleri(facts):
    """Şirketin rapor açıkladığı tarihler (her 10-K / 10-Q bir olay)."""
    tarihler = set()
    for f in ((facts.get('facts') or {}).get('us-gaap') or {}).get('Assets', {}).get('units', {}).get('USD', []):
        if f.get('form') in YILLIK_FORMLAR and f.get('filed'):
            tarihler.add(f['filed'])
    alt = (date.fromisoformat(BASLANGIC) - timedelta(days=500)).isoformat()
    return sorted(t for t in tarihler if t >= alt)


# ---------------------------------------------------------------- 1. geçiş: büyüklük ve kârlılık (tüm şirketler)
def _gecis1(arg):
    zip_yolu, cikler = arg
    global _KESIM
    z = zipfile.ZipFile(zip_yolu)
    kayit = []
    for cik in cikler:
        facts = T.zipten_oku(z, cik)
        if not facts or 'us-gaap' not in (facts.get('facts') or {}):
            continue
        for e in olay_tarihleri(facts):
            _KESIM = e
            deger, deger_tarih = halka_acik_deger_kesimli(facts, e)
            net = T.ttm(facts, T.ETIKET['netKar'])[0]
            kayit.append((cik, e, deger, deger_tarih, net))
    _KESIM = None
    return kayit


# ---------------------------------------------------------------- 2. geçiş: tam temel veriler (evrene girenler)
def _gecis2(arg):
    zip_yolu, cikler, sic_harita = arg
    global _KESIM
    z = zipfile.ZipFile(zip_yolu)
    kayit = []
    for cik in cikler:
        facts = T.zipten_oku(z, cik)
        if not facts:
            continue
        sic = sic_harita.get(cik, '')
        for e in olay_tarihleri(facts):
            _KESIM = e
            try:
                r = T.sirket_verisi_cikar(facts, sic)
            except Exception as ex:  # tek bir rapor bozuksa atla
                r = {'not': f'Hata: {ex}'}
            r['hisseAdedi'] = hisse_adedi_kesimli(facts, e)
            r['cik'] = cik
            r['acik'] = e
            r['sic'] = sic
            kayit.append(r)
    _KESIM = None
    return kayit


def paralel(fonk, zip_yolu, cikler, ek=None, parca=8):
    gruplar = [cikler[i::parca] for i in range(parca)]
    argumanlar = [(zip_yolu, g) if ek is None else (zip_yolu, g, ek) for g in gruplar]
    sonuc = []
    with ProcessPoolExecutor(max_workers=os.cpu_count() or 2) as ex:
        for parca_sonuc in ex.map(fonk, argumanlar):
            sonuc.extend(parca_sonuc)
    return sonuc


# ---------------------------------------------------------------- S&P 500 geçmiş üyeliği
class TabloOkuyucu(HTMLParser):
    """Belirli id'li bir HTML tablosunun bütün hücrelerini satır satır okur."""

    def __init__(self, tablo_id):
        super().__init__()
        self.hedef, self.derinlik, self.satir, self.hucre, self.satirlar = tablo_id, 0, None, None, []

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            if self.derinlik:
                self.derinlik += 1
            elif dict(attrs).get('id') == self.hedef:
                self.derinlik = 1
        elif self.derinlik == 1:
            if tag == 'tr':
                self.satir = []
            elif tag in ('td', 'th') and self.satir is not None:
                self.hucre = []

    def handle_endtag(self, tag):
        if tag == 'table' and self.derinlik:
            self.derinlik -= 1
        elif self.derinlik == 1:
            if tag in ('td', 'th') and self.hucre is not None:
                self.satir.append(''.join(self.hucre).strip())
                self.hucre = None
            elif tag == 'tr' and self.satir is not None:
                self.satirlar.append(self.satir)
                self.satir = None

    def handle_data(self, data):
        if self.hucre is not None:
            self.hucre.append(data)


class TumTablolar(HTMLParser):
    """Sayfadaki bütün tabloları okur; rowspan/colspan birleştirmelerini açarak düz satırlar üretir."""

    def __init__(self):
        super().__init__()
        self.yigin, self.tablolar = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'table':
            self.yigin.append({'id': a.get('id'), 'satirlar': [], 'satir': None, 'hucre': None})
        elif self.yigin:
            t = self.yigin[-1]
            if tag == 'tr':
                t['satir'] = []
            elif tag in ('td', 'th') and t['satir'] is not None:
                sayi = lambda v: int(re.sub(r'\D', '', v or '') or 1)
                t['hucre'] = {'metin': [], 'rs': sayi(a.get('rowspan')), 'cs': sayi(a.get('colspan')), 'th': tag == 'th'}

    def handle_endtag(self, tag):
        if not self.yigin:
            return
        t = self.yigin[-1]
        if tag in ('td', 'th') and t['hucre'] is not None:
            h = t['hucre']
            h['metin'] = ' '.join(''.join(h['metin']).split())
            t['satir'].append(h)
            t['hucre'] = None
        elif tag == 'tr' and t['satir'] is not None:
            t['satirlar'].append(t['satir'])
            t['satir'] = None
        elif tag == 'table':
            self.yigin.pop()
            self.tablolar.append((t['id'], self._ac(t['satirlar'])))

    def handle_data(self, data):
        if self.yigin and self.yigin[-1]['hucre'] is not None:
            self.yigin[-1]['hucre']['metin'].append(data)

    @staticmethod
    def _ac(satirlar):
        bekleyen, cikti = {}, []   # sütun -> (metin, kalan satır, başlık mı)
        for satir in satirlar:
            yeni, j, hucreler = [], 0, list(satir)
            while hucreler or j in bekleyen:
                if j in bekleyen:
                    metin, kalan, th = bekleyen[j]
                    yeni.append((metin, th))
                    if kalan <= 1:
                        del bekleyen[j]
                    else:
                        bekleyen[j] = (metin, kalan - 1, th)
                    j += 1
                    continue
                h = hucreler.pop(0)
                for _ in range(h['cs']):
                    yeni.append((h['metin'], h['th']))
                    if h['rs'] > 1:
                        bekleyen[j] = (h['metin'], h['rs'] - 1, h['th'])
                    j += 1
            cikti.append(yeni)
        return cikti


def _tarih_coz(metin):
    metin = re.sub(r'\[.*?\]', '', metin).strip()
    for bicim in ('%B %d, %Y', '%b %d, %Y', '%Y-%m-%d', '%d %B %Y'):
        try:
            return datetime.strptime(metin, bicim).date()
        except ValueError:
            pass
    return None


ESKI_SURUM_NOTU = []


def _degisim_tablosu(html):
    o = TumTablolar()
    o.feed(html)
    for tid, satirlar in o.tablolar:  # başlığında "Added" ve "Removed" geçen tablo
        baslik = ' '.join(m for sat in satirlar[:3] for m, th in sat if th).lower()
        if tid == 'changes' or ('added' in baslik and 'removed' in baslik):
            return satirlar
    return None


def _eski_surumden_tablo(ua, deneme=4):
    """Wikipedia sayfa geçmişinde, boyutu belirgin büyük olan (tabloyu içeren) en yeni sürümü bulur."""
    api = ('https://en.wikipedia.org/w/api.php?action=query&prop=revisions'
           '&titles=List_of_S%26P_500_companies&rvlimit=500&rvprop=ids|timestamp|size&format=json')
    try:
        d = json.loads(T.indir(api, ua))
        surumler = list(d['query']['pages'].values())[0]['revisions']
    except Exception as e:
        print(f'Wikipedia sayfa geçmişi alınamadı: {e}')
        return None
    simdiki = surumler[0]['size']
    for r in [r for r in surumler if r['size'] >= simdiki * 1.5][:deneme]:
        try:
            html = T.indir(f"https://en.wikipedia.org/w/index.php?oldid={r['revid']}", ua).decode('utf-8')
        except Exception as e:
            print(f"Eski sürüm {r['revid']} alınamadı: {e}")
            continue
        tablo = _degisim_tablosu(html)
        if tablo:
            print(f"S&P 500 değişiklikleri Wikipedia'nın {r['timestamp'][:10]} tarihli sürümünden alındı")
            ESKI_SURUM_NOTU.append(r['timestamp'][:10])
            return tablo
    return None


def sp500_degisiklikleri(ua, html=None):
    """Wikipedia'daki S&P 500 değişiklik tablosu: [(tarih, eklenen, çıkarılan), ...]"""
    if html is None:
        html = T.indir(T.SP500_URL, ua).decode('utf-8')
    hedef = _degisim_tablosu(html)
    if hedef is None:
        # Kasım 2026'da tablo sayfadan kaldırıldı: sayfanın tabloyu içeren son eski sürümünü kullan
        hedef = _eski_surumden_tablo(ua)
    if hedef is None:
        raise RuntimeError('değişiklik tablosu bulunamadı')
    degisim = []
    for sat in hedef:
        if all(th for _, th in sat) or len(sat) < 4:
            continue  # başlık satırı
        metin = [m for m, _ in sat]
        t = _tarih_coz(metin[0])
        if t is None:
            continue
        temiz = lambda x: x.strip().upper().replace('.', '-') if re.fullmatch(r'[A-Za-z][A-Za-z0-9.\-]{0,9}', x.strip()) else ''
        degisim.append((t, temiz(metin[1]), temiz(metin[3]) if len(metin) > 3 else ''))
    return degisim


def sp500_gecmis(guncel, degisim, tarihler):
    """Her tarih için o günkü S&P 500 üyeleri (bugünkü listeden geriye doğru giderek)."""
    uyeler = set(t.replace('.', '-') for t in guncel)
    degisim = sorted(degisim, key=lambda x: x[0], reverse=True)
    sonuc, i = {}, 0
    for t in sorted(tarihler, reverse=True):
        while i < len(degisim) and degisim[i][0] > t:
            _, eklenen, cikan = degisim[i]
            if eklenen:
                uyeler.discard(eklenen)
            if cikan:
                uyeler.add(cikan)
            i += 1
        sonuc[t] = set(uyeler)
    return sonuc


# ---------------------------------------------------------------- fiyatlar
def fiyatlari_indir(tickerlar, baslangic):
    """Yahoo Finance: günlük kapanış (bölünmeye göre düzeltilmiş), temettü dahil düzeltilmiş kapanış, bölünmeler."""
    import yfinance as yf
    kapanis, duz, bolunme = [], [], []
    tickerlar = sorted(set(tickerlar))
    for i in range(0, len(tickerlar), 150):
        grup = tickerlar[i:i + 150]
        for deneme in range(3):
            try:
                d = yf.download(grup, start=baslangic, interval='1d', auto_adjust=False, actions=True,
                                group_by='column', threads=True, progress=False)
                break
            except Exception as e:
                print(f'Fiyat indirme hatası ({e}), tekrar deneniyor')
                time.sleep(10)
        else:
            continue
        if d is None or d.empty:
            continue
        for alan, liste in (('Close', kapanis), ('Adj Close', duz), ('Stock Splits', bolunme)):
            if alan in d.columns.get_level_values(0):
                x = d[alan]
                if isinstance(x, pd.Series):
                    x = x.to_frame(grup[0])
                liste.append(x)
        print(f'Fiyatlar: {min(i + 150, len(tickerlar))}/{len(tickerlar)}')
    birlestir = lambda l: pd.concat(l, axis=1) if l else pd.DataFrame()
    return birlestir(kapanis), birlestir(duz), birlestir(bolunme)


def haftalik(df):
    """Günlük veriden her haftanın son işlem günü (cuma kapanışı)."""
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.resample('W-FRI').last()


def bolunme_carpani(bolunme, haftalar):
    """Her hisse ve hafta için: o tarihten SONRA gerçekleşen bölünmelerin çarpımı."""
    b = bolunme.copy()
    b.index = pd.to_datetime(b.index).tz_localize(None)
    b = b.fillna(0).replace(0, 1.0)
    # sonraki bölünmelerin çarpımı = tersten kümülatif çarpım, kendi günü hariç
    ters = b.iloc[::-1].cumprod().iloc[::-1]
    sonraki = ters.shift(-1).fillna(1.0)
    return sonraki.reindex(haftalar, method='ffill').fillna(1.0)


# ---------------------------------------------------------------- puanlama (Sheet'teki formüllerin aynısı)
def yuzdelik(df, kolon, ters=False, sadece=None):
    """Aynı tarih ve grup içinde 0-1 arası sıralama: (sıra - 1) / (sayı - 1). Eşitlikte ortalama sıra."""
    x = df[kolon].where(df['grup'] == sadece) if sadece else df[kolon]
    g = pd.concat([df[['tarih', 'grup']], x.rename('_x')], axis=1).groupby(['tarih', 'grup'])['_x']
    sira = g.rank(method='average')
    n = g.transform('count')
    p = (sira - 1) / np.maximum(1, n - 1)
    return 1 - p if ters else p


def puanla(df):
    """df: her satır bir (tarih, hisse); temel veriler ve piyasa değeri (pd, $) dolu."""
    num = lambda c: pd.to_numeric(df[c], errors='coerce')
    d = {c: num(c) for c in ['satisTTM', 'brutKarTTM', 'ebitTTM', 'netKarTTM', 'amortismanTTM', 'inaTTM',
                             'capexTTM', 'faizTTM', 'varlik', 'yukumluluk', 'ozkaynak', 'donenVarlik',
                             'kvYukumluluk', 'nakit', 'borc', 'gecmisKar', 'satisFY', 'satisFY1', 'netKarFY',
                             'netKarFY1', 'varlikFY', 'varlikFY1', 'inaFY', 'uvBorcFY', 'uvBorcFY1', 'donenFY',
                             'donenFY1', 'kvYukFY', 'kvYukFY1', 'hisseFY', 'hisseFY1', 'brutKarFY', 'brutKarFY1']}
    N = lambda s: s.fillna(0)
    diger = df['grup'] == 'Diğer'
    fin = df['grup'] == 'Finansal'
    pdg = df['pd']
    fd = pdg + N(d['borc']) - N(d['nakit'])
    favok = d['ebitTTM'] + N(d['amortismanTTM'])
    sna = d['inaTTM'] - N(d['capexTTM'])
    m = pd.DataFrame(index=df.index)
    m['ebit_fd'] = (d['ebitTTM'] / fd).where(diger & (fd > 0))
    m['sna_get'] = (sna / pdg).where(diger & (pdg > 0))
    m['kar_get'] = (d['netKarTTM'] / pdg).where(pdg > 0)
    m['dd_pd'] = (d['ozkaynak'] / pdg).where(fin & (pdg > 0))
    sermaye = d['ozkaynak'] + N(d['borc']) - N(d['nakit'])
    m['roce'] = (d['ebitTTM'] / sermaye).where(diger & (sermaye > 0))
    m['brut_karlilik'] = (d['brutKarTTM'] / d['varlik']).where(diger & (d['varlik'] > 0))
    m['nakit_donusum'] = (sna / d['netKarTTM']).clip(upper=2).where(diger & (d['netKarTTM'] > 0))
    m['roe'] = (d['netKarTTM'] / d['ozkaynak']).where(d['ozkaynak'] > 0)

    def kosul(sart, *gerekli):
        ok = pd.Series(True, index=df.index)
        for s in gerekli:
            ok &= s.notna()
        return sart.astype(float).where(ok)

    with np.errstate(divide='ignore', invalid='ignore'):
        f = pd.DataFrame(index=df.index)
        f['f1'] = kosul(d['netKarFY'] > 0, d['netKarFY'])
        f['f2'] = kosul(d['inaFY'] > 0, d['inaFY'])
        f['f3'] = kosul(d['netKarFY'] / d['varlikFY'] > d['netKarFY1'] / d['varlikFY1'],
                        d['netKarFY'], d['netKarFY1'], d['varlikFY'], d['varlikFY1'])
        f['f4'] = kosul(d['inaFY'] > d['netKarFY'], d['inaFY'], d['netKarFY'])
        f['f5'] = kosul(N(d['uvBorcFY']) / d['varlikFY'] <= N(d['uvBorcFY1']) / d['varlikFY1'],
                        d['varlikFY'], d['varlikFY1'])
        f['f6'] = kosul(d['donenFY'] / d['kvYukFY'] > d['donenFY1'] / d['kvYukFY1'],
                        d['donenFY'], d['donenFY1'], d['kvYukFY'], d['kvYukFY1'])
        f['f7'] = kosul(d['hisseFY'] <= d['hisseFY1'], d['hisseFY'], d['hisseFY1'])
        f['f8'] = kosul(d['brutKarFY'] / d['satisFY'] > d['brutKarFY1'] / d['satisFY1'],
                        d['brutKarFY'], d['brutKarFY1'], d['satisFY'], d['satisFY1'])
        f['f9'] = kosul(d['satisFY'] / d['varlikFY'] > d['satisFY1'] / d['varlikFY1'],
                        d['satisFY'], d['satisFY1'], d['varlikFY'], d['varlikFY1'])
    f = f.replace([np.inf, -np.inf], np.nan)
    say = f.count(axis=1)
    m['piotroski'] = (f.sum(axis=1) / say * 9).where(say >= 5)
    net_borc = N(d['borc']) - N(d['nakit'])
    m['netborc_favok'] = (net_borc / favok).where(diger & (favok > 0))
    m['faiz_karsilama'] = np.where(N(d['faizTTM']) <= 0, 50.0,
                                   np.minimum(50.0, d['ebitTTM'] / d['faizTTM']))
    m['faiz_karsilama'] = m['faiz_karsilama'].where(diger & d['ebitTTM'].notna())
    altman = (1.2 * (N(d['donenVarlik']) - N(d['kvYukumluluk'])) / d['varlik'] + 1.4 * N(d['gecmisKar']) / d['varlik']
              + 3.3 * d['ebitTTM'] / d['varlik'] + 0.6 * pdg / d['yukumluluk'] + N(d['satisTTM']) / d['varlik'])
    m['altman'] = altman.replace([np.inf, -np.inf], np.nan).where(diger)
    m['ozk_varlik'] = (d['ozkaynak'] / d['varlik']).where(d['varlik'] > 0)
    m['satis_buyume'] = (d['satisFY'] / d['satisFY1'] - 1).where(d['satisFY1'] > 0)
    m['hbk_buyume'] = ((d['netKarFY'] / d['hisseFY']) / (d['netKarFY1'] / d['hisseFY1']) - 1).where(
        (d['netKarFY1'] > 0) & (d['hisseFY'] > 0) & (d['hisseFY1'] > 0))
    m['hisse_degisim'] = (d['hisseFY'] / d['hisseFY1'] - 1).where(d['hisseFY1'] > 0)
    m = m.replace([np.inf, -np.inf], np.nan)

    t = pd.concat([df[['tarih', 'grup']], m], axis=1)
    y = pd.DataFrame(index=df.index)
    for k in ['ebit_fd', 'sna_get', 'kar_get', 'dd_pd', 'roce', 'brut_karlilik', 'nakit_donusum', 'piotroski',
              'faiz_karsilama', 'altman', 'satis_buyume', 'hbk_buyume']:
        y[k] = yuzdelik(t, k)
    y['roe'] = yuzdelik(t, 'roe', sadece='Finansal')
    y['ozk_varlik'] = yuzdelik(t, 'ozk_varlik', sadece='Finansal')
    y['netborc_favok'] = yuzdelik(t, 'netborc_favok', ters=True)
    y['hisse_degisim'] = yuzdelik(t, 'hisse_degisim', ters=True)

    out = df[['tarih', 'ticker', 'grup']].copy()
    out['ucuzluk'] = y[['ebit_fd', 'sna_get', 'kar_get', 'dd_pd']].mean(axis=1)
    out['kalite'] = y[['roce', 'brut_karlilik', 'nakit_donusum', 'roe', 'piotroski']].mean(axis=1)
    out['saglamlik'] = y[['netborc_favok', 'faiz_karsilama', 'altman', 'ozk_varlik']].mean(axis=1)
    out['buyume'] = y[['satis_buyume', 'hbk_buyume', 'hisse_degisim']].mean(axis=1)
    toplam_agirlik = sum(AGIRLIK.values())
    out['toplam'] = ((AGIRLIK['ucuzluk'] * out['ucuzluk'] + AGIRLIK['kalite'] * out['kalite']
                      + AGIRLIK['saglamlik'] * out['saglamlik'].fillna(0.5)
                      + AGIRLIK['buyume'] * out['buyume'].fillna(0.5)) / toplam_agirlik)
    out.loc[out['ucuzluk'].isna() | out['kalite'].isna(), 'toplam'] = np.nan

    uyari = pd.DataFrame(index=df.index)
    uyari['zarar'] = d['netKarTTM'] < 0
    uyari['nakit'] = sna < 0
    uyari['altman'] = m['altman'] < ALTMAN_ALT
    uyari['favok'] = diger & (favok <= 0)
    uyari['borc'] = m['netborc_favok'] > BORC_UST
    uyari['faiz'] = m['faiz_karsilama'] < FAIZ_ALT
    uyari['satis'] = m['satis_buyume'] < SATIS_DUSUS
    uyari['sulanma'] = m['hisse_degisim'] > SULANMA
    uyari['ozkaynak'] = d['ozkaynak'] < 0
    uyari['sermaye'] = fin & (m['ozk_varlik'] < FIN_SERMAYE)
    uyari['eksik'] = df['not'].fillna('').astype(str) != ''
    out['uyari'] = uyari.sum(axis=1)
    out['aday'] = (out['toplam'] >= MIN_TOPLAM) & (out['ucuzluk'].fillna(0) >= MIN_UCUZLUK) & (out['uyari'] <= IZINLI_UYARI)
    for k in y.columns:  # tek tek kriterlerin yüzdelikleri (hangi kriterin işe yaradığı analizi için)
        out['y_' + k] = y[k]
    return out


# ---------------------------------------------------------------- portföy simülasyonu
def simule_et(getiri, hedefler, maliyet=ISLEM_MALIYETI):
    """
    getiri: haftalık getiri tablosu (satır = hafta sonu t, değer = t'den t+1'e getiri)
    hedefler: {değişim tarihi: [tickerlar]} - o tarihte eşit ağırlıkla tutulacak hisseler
    Döner: haftalık portföy getirisi, işlem hacmi, tutulan hisse sayısı
    """
    agirlik = pd.Series(dtype=float)
    pg, hacim, adet = [], [], []
    for t in getiri.index:
        islem = 0.0
        if t in hedefler:
            liste = [x for x in hedefler[t] if x in getiri.columns]
            yeni = pd.Series(1.0 / len(liste), index=liste) if liste else pd.Series(dtype=float)
            fark = yeni.reindex(yeni.index.union(agirlik.index), fill_value=0) - \
                agirlik.reindex(yeni.index.union(agirlik.index), fill_value=0)
            islem = float(fark.abs().sum())
            agirlik = yeni
        r = getiri.loc[t, agirlik.index].fillna(0.0) if len(agirlik) else pd.Series(dtype=float)
        brut = float((agirlik * r).sum()) if len(agirlik) else 0.0
        pg.append(brut - islem * maliyet)
        hacim.append(islem)
        adet.append(len(agirlik))
        if len(agirlik):  # fiyat hareketiyle ağırlıklar kayar
            deger = agirlik * (1 + r).clip(lower=0)
            agirlik = deger / deger.sum() if deger.sum() > 0 else deger
    return pd.Series(pg, index=getiri.index), pd.Series(hacim, index=getiri.index), pd.Series(adet, index=getiri.index)


def olcumler(seri, hacim=None, adet=None):
    seri = seri.dropna()
    n = len(seri)
    if n < 2:
        return {}
    egri = (1 + seri).cumprod()
    yil = n / 52.0
    cagr = egri.iloc[-1] ** (1 / yil) - 1
    oyn = seri.std() * math.sqrt(52)
    dusus = (egri / egri.cummax() - 1).min()
    o = {'Yıllık getiri': cagr, 'Oynaklık (yıllık)': oyn,
         'Sharpe (risksiz faiz 0)': (seri.mean() * 52) / oyn if oyn > 0 else np.nan,
         'En büyük düşüş': dusus, 'Toplam getiri': egri.iloc[-1] - 1}
    if hacim is not None:
        o['Yıllık işlem hacmi (portföyün katı)'] = hacim.sum() / yil
    if adet is not None:
        o['Ortalama hisse sayısı'] = adet.mean()
    return o


# ---------------------------------------------------------------- ana akış

def gecmis_kaydi(puan, hafta_sayisi=54):
    """Son ~1 yılın haftalık aday giriş-çıkışları (sheet'teki Geçmiş sekmesine 'Test' etiketiyle yüklenir)."""
    tarihler = sorted(puan['tarih'].unique())[-hafta_sayisi:]
    a = puan[puan['tarih'].isin(tarihler) & puan['aday']]
    tum = puan[puan['tarih'].isin(tarihler)].set_index(['tarih', 'ticker'])['toplam']
    kayit, onceki = [], None
    for d in tarihler:
        s = set(a.loc[a['tarih'] == d, 'ticker'])
        if onceki is not None:
            for t in sorted(s - onceki):
                kayit.append((d, t, 'Listeye girdi', tum.get((d, t))))
            for t in sorted(onceki - s):
                kayit.append((d, t, 'Listeden çıktı', tum.get((d, t))))
        onceki = s
    g = pd.DataFrame(kayit, columns=['tarih', 'hisse', 'olay', 'toplam'])
    g['toplam'] = g['toplam'].round(4)
    return g.sort_values(['tarih', 'olay', 'hisse'], ascending=[False, False, True])  # en yeni üstte, girenler önce


def main():
    ua = os.environ.get('SEC_UA', '').strip()
    if '@' not in ua:
        sys.exit('SEC_UA tanımlı değil.')
    os.makedirs(CIKTI, exist_ok=True)
    t0 = time.time()

    ticker_harita, cik_ticker = T.borsa_listesi(ua)
    guncel_sp = T.sp500_cek(ua)

    print('SEC toplu veri dosyası indiriliyor...')
    zip_yolu = T.indir(T.FACTS_ZIP, ua, os.path.join(tempfile.gettempdir(), 'companyfacts.zip'))

    # Değişim tarihleri: her cuma
    bitis = pd.Timestamp(date.today())
    haftalar = pd.date_range(BASLANGIC, bitis, freq='W-FRI')
    hafta_str = [h.strftime('%Y-%m-%d') for h in haftalar]

    # Tarihsel S&P 500 üyeliği
    uyarilar = []
    try:
        degisim = sp500_degisiklikleri(ua)
        print(f'S&P 500 değişiklik kaydı: {len(degisim)} satır')
        if len(degisim) < 50:
            raise RuntimeError('değişiklik tablosu beklenenden kısa')
        sp_gecmis = sp500_gecmis(guncel_sp, degisim, [h.date() for h in haftalar])
        if ESKI_SURUM_NOTU:
            uyarilar.append(f"S&P 500 değişiklik tablosu Wikipedia'dan kaldırıldığı için sayfanın {ESKI_SURUM_NOTU[0]} tarihli sürümü kullanıldı; "
                            "bu tarihten sonraki endeks değişiklikleri geçmiş üyeliğe yansımadı (etkisi çok küçük).")
    except Exception as e:
        uyarilar.append(f'S&P 500 geçmiş üyeliği kurulamadı ({e}); bugünkü liste bütün dönem için kullanıldı.')
        sp_gecmis = {h.date(): set(t.replace('.', '-') for t in guncel_sp) for h in haftalar}

    # 1. geçiş: bütün NYSE/Nasdaq şirketlerinin her rapor tarihindeki büyüklüğü ve kârlılığı
    print('1. geçiş: büyüklük ve kârlılık...')
    tum_cik = sorted(cik_ticker)
    g1 = pd.DataFrame(paralel(_gecis1, zip_yolu, tum_cik), columns=['cik', 'acik', 'deger', 'deger_tarih', 'net'])
    g1['acik'] = pd.to_datetime(g1['acik'])
    print(f'  {g1["cik"].nunique()} şirket, {len(g1)} rapor ({time.time() - t0:.0f} sn)')

    # Her hafta için evren: o günkü S&P 500 + endeks dışı kârlı en büyük ADAY_SAYISI şirket
    cik_of = {t: c for t, (c, _) in ticker_harita.items()}
    g1 = g1.sort_values('acik')
    evren = {}
    bulunamayan_sp = set()
    for h, hs in zip(haftalar, hafta_str):
        sp_tickers = sp_gecmis[h.date()]
        sp_cik = {}
        for t in sp_tickers:
            c = cik_of.get(t)
            if c:
                sp_cik.setdefault(c, t)
            else:
                bulunamayan_sp.add(t)
        son = g1[g1['acik'] <= h].groupby('cik').tail(1)
        son = son[(~son['cik'].isin(sp_cik)) & (son['net'] > 0) & son['deger'].notna()
                  & (pd.to_datetime(son['deger_tarih']) >= h - pd.Timedelta(days=730))]
        adaylar = son.nlargest(ADAY_SAYISI, 'deger')['cik']
        uyeler = [(c, t, 'S&P 500') for c, t in sp_cik.items()]
        uyeler += [(c, cik_ticker[c], 'Aday') for c in adaylar if c in cik_ticker]
        evren[hs] = uyeler
    if bulunamayan_sp:
        uyarilar.append(f'Geçmişte S&P 500\'de olup bugün borsada bulunmayan {len(bulunamayan_sp)} şirket '
                        f'(iflas, birleşme, satın alma) teste dahil edilemedi. Bu, sonuçları bir miktar iyimser yapar.')

    # 2. geçiş: evrene en az bir kez giren şirketlerin tam verileri
    gerekli = sorted({c for u in evren.values() for c, _, _ in u})
    print(f'2. geçiş: {len(gerekli)} şirketin tam verileri...')
    sic = {}
    for i, c in enumerate(gerekli):
        try:
            s = T.get_json(f'https://data.sec.gov/submissions/CIK{c}.json', ua) or {}
            sic[c] = s.get('sic', '')
        except Exception:
            sic[c] = ''
        time.sleep(T.BEKLEME)
    g2 = pd.DataFrame(paralel(_gecis2, zip_yolu, gerekli, sic))
    g2['acik'] = pd.to_datetime(g2['acik'])
    g2['grup'] = g2['cik'].map(lambda c: 'Finansal' if T.finansal_mi(sic.get(c)) else ('Diğer' if sic.get(c) else ''))
    print(f'  {len(g2)} rapor ({time.time() - t0:.0f} sn)')

    # Fiyatlar
    tickerlar = sorted({t for u in evren.values() for _, t, _ in u}) + ['SPY']
    print(f'{len(tickerlar)} hissenin fiyatları indiriliyor...')
    kap, duz, bol = fiyatlari_indir(tickerlar, (pd.Timestamp(BASLANGIC) - pd.Timedelta(days=10)).strftime('%Y-%m-%d'))
    kap_h, duz_h = haftalik(kap).reindex(haftalar), haftalik(duz).reindex(haftalar)
    # Yahoo'nun düzeltilmiş fiyatları bazı hisselerde eski tarihler için sıfır veya negatif çıkabiliyor: at
    kap_h = kap_h.where(kap_h > 0)
    duz_h = duz_h.where(duz_h > 0)
    carpan = bolunme_carpani(bol, haftalar) if not bol.empty else pd.DataFrame(1.0, index=haftalar, columns=kap_h.columns)
    fiyatsiz = [t for t in tickerlar if t not in duz_h.columns or duz_h[t].notna().sum() == 0]
    if fiyatsiz:
        uyarilar.append(f'{len(fiyatsiz)} hissenin fiyat verisi bulunamadı ve teste alınmadı.')

    # Her hafta için puanlama tablosu
    print('Puanlama...')
    satirlar = [(pd.Timestamp(hs), c, t, e) for hs, u in evren.items() for c, t, e in u]
    df = pd.DataFrame(satirlar, columns=['tarih', 'cik', 'ticker', 'endeks']).sort_values('tarih')
    g2s = g2.sort_values('acik')
    df = pd.merge_asof(df, g2s, left_on='tarih', right_on='acik', by='cik', direction='backward')
    df = df[df['grup'].isin(['Diğer', 'Finansal'])].copy()
    # Piyasa değeri = o haftanın kapanışı x o tarihte bilinen hisse adedi (aradaki bölünmeler düzeltilerek)
    kap_uzun = kap_h.stack().rename('kapanis').reset_index().rename(columns={'level_0': 'tarih', 'level_1': 'ticker'})
    kap_uzun.columns = ['tarih', 'ticker', 'kapanis']
    car_uzun = carpan.stack().reset_index()
    car_uzun.columns = ['tarih', 'ticker', 'carpan_t']
    df = df.merge(kap_uzun, on=['tarih', 'ticker'], how='left')
    # rapor tarihindeki bölünme çarpanı
    acik_carpan = []
    for t, a in zip(df['ticker'], df['acik']):
        if t in carpan.columns:
            idx = carpan.index.searchsorted(a)
            acik_carpan.append(carpan[t].iloc[min(idx, len(carpan) - 1)] if idx < len(carpan) else 1.0)
        else:
            acik_carpan.append(1.0)
    df['carpan_acik'] = acik_carpan
    df['pd'] = df['kapanis'] * pd.to_numeric(df['hisseAdedi'], errors='coerce') * df['carpan_acik']
    df = df[df['pd'].notna() & (df['pd'] > 0)].reset_index(drop=True)
    puan = puanla(df)
    puan['endeks'] = df['endeks'].values
    puan['sic'] = df['sic'].values
    puan.to_csv(os.path.join(CIKTI, 'puanlar.csv.gz'), index=False, compression='gzip')
    gecmis_kaydi(puan).to_csv(os.path.join(CIKTI, 'gecmis.csv'), index=False)
    print(f'  {len(puan)} satır puanlandı ({time.time() - t0:.0f} sn)')

    # Haftalık getiriler: t'den bir sonraki haftaya
    getiri = duz_h.pct_change(fill_method=None).shift(-1)
    getiri = getiri.iloc[:-1].replace([np.inf, -np.inf], np.nan)
    # Bir haftada %99'dan fazla kayıp veya %300'den fazla kazanç neredeyse her zaman veri hatasıdır
    supheli = (getiri <= -0.99) | (getiri >= 3.0)
    if int(supheli.sum().sum()):
        uyarilar.append(f'Fiyat verisindeki {int(supheli.sum().sum())} şüpheli haftalık hareket (bir haftada '
                        f'%99 kayıp veya %300 kazançtan fazlası) veri hatası sayılıp hesaba katılmadı.')
    getiri = getiri.where(~supheli)

    # Değişim sıklıkları
    hafta_idx = pd.Series(getiri.index, index=getiri.index)
    siklik = {
        'Haftalık': list(getiri.index),
        'Aylık': list(hafta_idx.groupby([hafta_idx.dt.year, hafta_idx.dt.month]).max()),
        'Üç aylık': list(hafta_idx.groupby([hafta_idx.dt.year, hafta_idx.dt.quarter]).max()),
    }
    aday_listesi = puan[puan['aday']].groupby('tarih')['ticker'].apply(list).to_dict()
    tum_liste = puan[puan['toplam'].notna()].groupby('tarih')['ticker'].apply(list).to_dict()

    egriler, ozet, yillik = {}, [], {}
    spy = getiri['SPY'] if 'SPY' in getiri.columns else None
    if spy is not None:
        egriler['S&P 500 (SPY)'] = spy
        ozet.append({'Strateji': 'S&P 500 (SPY)', **olcumler(spy)})
    # Sinyal cuma kapanışındaki verilerle üretilir, işlem BİR SONRAKİ haftanın kapanışında yapılır
    # (aynı fiyattan hem karar verip hem işlem yapmak sonuçları olduğundan iyi gösterirdi).
    sonraki = dict(zip(getiri.index[:-1], getiri.index[1:]))

    def gecikmeli(liste_sozluk, tarihler):
        return {sonraki[t]: liste_sozluk.get(t, []) for t in tarihler if t in sonraki}

    en_iyi10 = (puan[puan['aday']].sort_values('toplam', ascending=False).groupby('tarih').head(10)
                .groupby('tarih')['ticker'].apply(list).to_dict())
    for ad, tarihler in siklik.items():
        pg10, hacim10, adet10 = simule_et(getiri, gecikmeli(en_iyi10, tarihler))
        egriler[f'En iyi 10 aday - {ad}'] = pg10
        o10 = olcumler(pg10, hacim10, adet10)
        if spy is not None:
            o10['SPY\'yi geçtiği hafta oranı'] = float((pg10 > spy).mean())
        ozet.append({'Strateji': f'En iyi 10 aday - {ad}', **o10})
        hedef = gecikmeli(aday_listesi, tarihler)
        pg, hacim, adet = simule_et(getiri, hedef)
        egriler[f'Adaylar - {ad}'] = pg
        o = olcumler(pg, hacim, adet)
        if spy is not None:
            o['SPY\'yi geçtiği hafta oranı'] = float((pg > spy).mean())
        ozet.append({'Strateji': f'Adaylar - {ad}', **o})
        # Kıyas: aynı sıklıkla, puanlanan bütün evrene eşit ağırlık
        hedef_tum = gecikmeli(tum_liste, tarihler)
        pg2, hacim2, adet2 = simule_et(getiri, hedef_tum)
        egriler[f'Bütün evren eşit ağırlık - {ad}'] = pg2
        ozet.append({'Strateji': f'Bütün evren eşit ağırlık - {ad}', **olcumler(pg2, hacim2, adet2)})

    egri_df = pd.DataFrame({k: (1 + v.fillna(0)).cumprod() for k, v in egriler.items()})
    egri_df.index.name = 'Hafta'
    egri_df.to_csv(os.path.join(CIKTI, 'egri.csv'))
    for k, v in egriler.items():
        yillik[k] = (1 + v.fillna(0)).groupby(v.index.year).prod() - 1
    yillik_df = pd.DataFrame(yillik)
    yillik_df.index.name = 'Yıl'
    yillik_df.to_csv(os.path.join(CIKTI, 'yillik.csv'))
    ozet_df = pd.DataFrame(ozet)
    ozet_df.to_csv(os.path.join(CIKTI, 'ozet.csv'), index=False)

    # Puanın öngörü gücü: her ay sonunda hisseleri puana göre 5 gruba böl, sonraki ayın getirisi
    aylik = siklik['Aylık']
    aylik_getiri = {}
    for a, b in zip(aylik[:-1], aylik[1:]):
        if a in sonraki and b in sonraki:  # yine bir hafta gecikmeyle
            donem = getiri.loc[sonraki[a]:sonraki[b]].iloc[:-1]
            aylik_getiri[a] = (1 + donem).prod(min_count=1) - 1
    dilim_kayit = []
    for a, g in aylik_getiri.items():
        p = puan[(puan['tarih'] == a) & puan['toplam'].notna()][['ticker', 'toplam']].copy()
        p['getiri'] = p['ticker'].map(g)
        p = p.dropna()
        if len(p) < 50:
            continue
        p['dilim'] = pd.qcut(p['toplam'].rank(method='first'), 5, labels=['1 (en düşük)', '2', '3', '4', '5 (en yüksek)'])
        for dl, gr in p.groupby('dilim', observed=True):
            dilim_kayit.append((a, str(dl), gr['getiri'].mean()))
    dilim = pd.DataFrame(dilim_kayit, columns=['ay', 'dilim', 'getiri'])
    dilim_ozet = dilim.groupby('dilim')['getiri'].agg(
        lambda s: (1 + s).prod() ** (12 / len(s)) - 1).rename('Yıllık getiri').to_frame()
    dilim_ozet['İsabet (aylık pozitif)'] = dilim.groupby('dilim')['getiri'].apply(lambda s: (s > 0).mean())
    dilim_ozet.index.name = 'Puan dilimi'
    dilim_ozet.to_csv(os.path.join(CIKTI, 'dilimler.csv'))

    analiz = aday_analizi(puan, aylik_getiri)
    varyant = varyant_testi(puan, getiri, duz_h, sonraki, siklik)

    bilgi = {
        'calisma': datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
        'donem': f'{haftalar[0].date()} - {getiri.index[-1].date()}',
        'islem_maliyeti': ISLEM_MALIYETI, 'uyarilar': uyarilar,
        'sure_dk': round((time.time() - t0) / 60, 1),
    }
    with open(os.path.join(CIKTI, 'bilgi.json'), 'w', encoding='utf-8') as f:
        json.dump(bilgi, f, ensure_ascii=False, indent=1)
    rapor_yaz(ozet_df, yillik_df, egri_df, dilim_ozet, bilgi, analiz, varyant)
    print(ozet_df.to_string())
    print(f'Bitti: {bilgi["sure_dk"]} dk')


# ---------------------------------------------------------------- iyileştirme denemeleri
GELISTIRME_SONU = '2020-12-31'   # 2015-2020 geliştirme dönemi; sonrası sınama dönemi
VERSIYONLAR = {
    'Mevcut sistem': dict(agirlik=None, sektor_siniri=None, trend_alt=None),
    '1) Kalite ve geri alım ağırlıklı': dict(
        agirlik={'ucuzluk': 0.25, 'kalite': 0.45, 'saglamlik': 0.10, 'buyume': 0.20}, sektor_siniri=None, trend_alt=None),
    '2) Sektör sınırı (en fazla 2)': dict(agirlik=None, sektor_siniri=2, trend_alt=None),
    '3) Trend filtresi (12 ayda %30+ düşeni alma)': dict(agirlik=None, sektor_siniri=None, trend_alt=-0.30),
    '4) Üçü birlikte': dict(agirlik={'ucuzluk': 0.25, 'kalite': 0.45, 'saglamlik': 0.10, 'buyume': 0.20},
                            sektor_siniri=2, trend_alt=-0.30),
}


def sektor_adi(sic):
    """SIC kodunun ilk iki hanesi; bütün perakende (52-59) tek sektör sayılır."""
    try:
        s = int(float(sic))
    except (TypeError, ValueError):
        return 'Bilinmiyor'
    iki = s // 100
    return 'Perakende' if 52 <= iki <= 59 else f'SIC {iki:02d}'


def secim_listesi(puan, momentum, agirlik=None, sektor_siniri=None, trend_alt=None, adet=10):
    p = puan[['tarih', 'ticker', 'ucuzluk', 'kalite', 'saglamlik', 'buyume', 'toplam', 'uyari', 'sic']].copy()
    if agirlik:
        tw = sum(agirlik.values())
        p['toplam'] = ((agirlik['ucuzluk'] * p['ucuzluk'] + agirlik['kalite'] * p['kalite']
                        + agirlik['saglamlik'] * p['saglamlik'].fillna(0.5)
                        + agirlik['buyume'] * p['buyume'].fillna(0.5)) / tw)
        p.loc[p['ucuzluk'].isna() | p['kalite'].isna(), 'toplam'] = np.nan
    p = p[(p['toplam'] >= MIN_TOPLAM) & (p['ucuzluk'].fillna(0) >= MIN_UCUZLUK) & (p['uyari'] <= IZINLI_UYARI)]
    if trend_alt is not None:
        m = pd.Series([momentum.get((t, k), np.nan) for t, k in zip(p['tarih'], p['ticker'])], index=p.index)
        p = p[~(m < trend_alt)]  # verisi olmayan hisse elenmez
    p = p.sort_values(['tarih', 'toplam'], ascending=[True, False])
    if sektor_siniri:
        p['sektor'] = p['sic'].map(sektor_adi)
        p['_n'] = p.groupby(['tarih', 'sektor']).cumcount()
        p = p[p['_n'] < sektor_siniri]
    return p.groupby('tarih').head(adet).groupby('tarih')['ticker'].apply(list).to_dict()


def varyant_testi(puan, getiri, duz_h, sonraki, siklik):
    mom = (duz_h / duz_h.shift(52) - 1).stack()
    momentum = {(t, k): v for (t, k), v in mom.items()}
    spy = getiri['SPY'] if 'SPY' in getiri.columns else None
    donemler = {'Tüm dönem': (None, None), 'Geliştirme (2015-2020)': (None, GELISTIRME_SONU),
                'Sınama (2021 sonrası)': (GELISTIRME_SONU, None)}

    def kes(seri, bas, son):
        if bas:
            seri = seri[seri.index > pd.Timestamp(bas)]
        if son:
            seri = seri[seri.index <= pd.Timestamp(son)]
        return seri

    kayit, egriler, secimler = [], {}, {}
    if spy is not None:
        satir = {'Versiyon': 'S&P 500 (SPY)', 'Sıklık': '-'}
        for dad, (b, e) in donemler.items():
            o = olcumler(kes(spy, b, e))
            satir[f'{dad}: yıllık getiri'] = o.get('Yıllık getiri')
            satir[f'{dad}: Sharpe'] = o.get('Sharpe (risksiz faiz 0)')
        o = olcumler(spy)
        satir['En büyük düşüş'], satir['Oynaklık'] = o.get('En büyük düşüş'), o.get('Oynaklık (yıllık)')
        kayit.append(satir)
    for vad, ayar in VERSIYONLAR.items():
        liste = secim_listesi(puan, momentum, **ayar)
        secimler[vad] = liste
        for sad, tarihler in siklik.items():
            hedef = {sonraki[t]: liste.get(t, []) for t in tarihler if t in sonraki}
            pg, hacim, adet = simule_et(getiri, hedef)
            egriler[f'{vad} - {sad}'] = pg
            satir = {'Versiyon': vad, 'Sıklık': sad}
            for dad, (b, e) in donemler.items():
                o = olcumler(kes(pg, b, e))
                satir[f'{dad}: yıllık getiri'] = o.get('Yıllık getiri')
                satir[f'{dad}: Sharpe'] = o.get('Sharpe (risksiz faiz 0)')
            o = olcumler(pg, hacim, adet)
            satir['En büyük düşüş'], satir['Oynaklık'] = o.get('En büyük düşüş'), o.get('Oynaklık (yıllık)')
            satir['Yıllık işlem hacmi'] = o.get('Yıllık işlem hacmi (portföyün katı)')
            satir['Ortalama hisse sayısı'] = o.get('Ortalama hisse sayısı')
            kayit.append(satir)
    kars = pd.DataFrame(kayit)

    # Karar: aynı sıklıkta, iki dönemde de hem getiri hem Sharpe mevcut sistemden iyi mi?
    karar = []
    for _, r in kars.iterrows():
        if r['Versiyon'] in ('Mevcut sistem', 'S&P 500 (SPY)'):
            karar.append('')
            continue
        m = kars[(kars['Versiyon'] == 'Mevcut sistem') & (kars['Sıklık'] == r['Sıklık'])].iloc[0]
        iyi = all(r[f'{d}: yıllık getiri'] > m[f'{d}: yıllık getiri'] and r[f'{d}: Sharpe'] > m[f'{d}: Sharpe']
                  for d in ('Geliştirme (2015-2020)', 'Sınama (2021 sonrası)'))
        yarim = any(r[f'{d}: yıllık getiri'] > m[f'{d}: yıllık getiri']
                    for d in ('Geliştirme (2015-2020)', 'Sınama (2021 sonrası)'))
        karar.append('Gerçek iyileşme' if iyi else ('Karışık sonuç' if yarim else 'İyileşme yok'))
    kars['Karar (mevcut sisteme göre)'] = karar

    yillik = pd.DataFrame({k: (1 + v.fillna(0)).groupby(v.index.year).prod() - 1
                           for k, v in egriler.items() if k.endswith('Aylık')})
    if spy is not None:
        yillik['S&P 500 (SPY)'] = (1 + spy.fillna(0)).groupby(spy.index.year).prod() - 1
    yillik.index.name = 'Yıl'
    # Her versiyonun bugünkü (son tarih) seçimi
    son_tarih = max(puan['tarih'])
    bugun = pd.DataFrame({vad: pd.Series(l.get(son_tarih, [])) for vad, l in secimler.items()})

    aciklama = pd.DataFrame({'Bu dosya': [
        'Her versiyon, her dönem ADAY çıkan hisseler arasından puanı en yüksek 10 hisseyi eşit ağırlıkla tutar; '
        'listeden çıkan satılır. İşlem bir hafta gecikmeyle yapılır, her alım ve satımda maliyet düşülür.',
        '1) Kalite ve geri alım ağırlıklı: puan ağırlıkları ucuzluk %25, kalite %45, sağlamlık %10, büyüme %20 '
        '(büyüme puanının içinde hisse adedini azaltma, yani geri alım var).',
        '2) Sektör sınırı: ilk 10\'da aynı sektörden en fazla 2 hisse. Bütün perakende (SIC 52-59) tek sektör sayılır.',
        '3) Trend filtresi: son 12 ayda %30\'dan fazla düşmüş hisse aday listesine alınmaz (değer tuzağı önlemi).',
        'Karar: bir versiyon ancak hem 2015-2020 hem 2021 sonrası dönemde, aynı sıklıkta, mevcut sistemden hem '
        'daha yüksek getiri hem daha yüksek Sharpe verdiyse "Gerçek iyileşme" sayılır.',
        'Geçmiş performans gelecek için garanti değildir. Bu dosya yatırım tavsiyesi değildir.']})
    with pd.ExcelWriter(os.path.join(CIKTI, 'varyant_testi.xlsx'), engine='openpyxl') as w:
        for ad, df in [('Karşılaştırma', kars), ('Yıllık (aylık değişim)', yillik.reset_index()),
                       ('Bugünkü seçimler', bugun), ('Açıklama', aciklama)]:
            df.to_excel(w, sheet_name=ad, index=False)
            ws = w.sheets[ad]
            for j, c in enumerate(df.columns, 1):
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = max(12, min(60, len(str(c)) + 4))
                if df[c].dtype.kind == 'f':
                    fmt = '0.00' if ('Sharpe' in str(c) or 'hacmi' in str(c) or 'sayısı' in str(c)) else '0.0%'
                    for i in range(2, len(df) + 2):
                        ws.cell(row=i, column=j).number_format = fmt
            if ad == 'Açıklama':
                ws.column_dimensions['A'].width = 140
    print(kars.round(3).to_string())
    return {'karsilastirma': kars, 'yillik': yillik}


# ---------------------------------------------------------------- aday listesinin iç analizi
KRITER_ADI = {
    'toplam': 'Toplam puan', 'ucuzluk': 'Ucuzluk puanı', 'kalite': 'Kalite puanı', 'saglamlik': 'Sağlamlık puanı',
    'buyume': 'Büyüme puanı', 'y_ebit_fd': 'Faaliyet kârı / FD', 'y_sna_get': 'Serbest nakit akışı getirisi',
    'y_kar_get': 'Kâr getirisi (1/FK)', 'y_dd_pd': 'Defter / piyasa (finansal)', 'y_roce': 'Sermaye getirisi (ROCE)',
    'y_brut_karlilik': 'Brüt kârlılık', 'y_nakit_donusum': 'Nakit dönüşümü', 'y_roe': 'Özkaynak kârlılığı (finansal)',
    'y_piotroski': 'Piotroski skoru', 'y_netborc_favok': 'Düşük net borç / FAVÖK', 'y_faiz_karsilama': 'Faiz karşılama',
    'y_altman': 'Altman Z', 'y_ozk_varlik': 'Özkaynak / varlık (finansal)', 'y_satis_buyume': 'Satış büyümesi',
    'y_hbk_buyume': 'Hisse başı kâr büyümesi', 'y_hisse_degisim': 'Hisse adedini azaltma (geri alım)',
}
SIRA_DILIMI = [(1, 10, '1-10'), (11, 25, '11-25'), (26, 50, '26-50'), (51, 10 ** 6, '51 ve sonrası')]


def _yilliklastir(aylik):
    aylik = aylik.dropna()
    return (1 + aylik).prod() ** (12 / len(aylik)) - 1 if len(aylik) else np.nan


def aday_analizi(puan, aylik_getiri):
    """Her ay sonundaki aday listesini sırasıyla birlikte sonraki ayın getirisiyle eşleştirir."""
    parcalar, evren_parcalar = [], []
    for a, g in aylik_getiri.items():
        p = puan[(puan['tarih'] == a) & puan['toplam'].notna()].copy()
        if p.empty:
            continue
        p['getiri_1ay'] = p['ticker'].map(g)
        p['spy_1ay'] = g.get('SPY', np.nan)
        evren_parcalar.append(p)
        ad = p[p['aday']].copy()
        if ad.empty:
            continue
        ad['sira'] = ad['toplam'].rank(ascending=False, method='first').astype(int)
        parcalar.append(ad)
    if not parcalar:
        return None
    ad = pd.concat(parcalar)
    ad = ad[ad['getiri_1ay'].notna()].copy()
    evren = pd.concat(evren_parcalar)
    evren = evren[evren['getiri_1ay'].notna()]
    ad['spy_farki'] = ad['getiri_1ay'] - ad['spy_1ay']
    ad['ilk_10'] = ad['sira'] <= 10
    g = ad.groupby('tarih')['getiri_1ay']
    ad['z'] = (ad['getiri_1ay'] - g.transform('mean')) / g.transform('std')
    # Beklenmedik: o ayki diğer adaylardan çok farklı (2,5 standart sapma) ya da bir ayda %25'ten fazla hareket
    ad['beklenmedik'] = (ad['z'].abs() > 2.5) | (ad['getiri_1ay'].abs() > 0.25)

    # Sıra dilimleri
    sira_kayit = []
    for bas, son, etiket in SIRA_DILIMI:
        d = ad[(ad['sira'] >= bas) & (ad['sira'] <= son)]
        if d.empty:
            continue
        aylik = d.groupby('tarih')['getiri_1ay'].mean()
        temiz = d[~d['beklenmedik']].groupby('tarih')['getiri_1ay'].mean()
        spy = d.groupby('tarih')['spy_1ay'].first()
        sira_kayit.append({'Aday sırası': etiket, 'Yıllık getiri': _yilliklastir(aylik),
                           'Beklenmedik aylar hariç yıllık getiri': _yilliklastir(temiz),
                           'SPY\'yi geçtiği ay oranı': float((aylik > spy.reindex(aylik.index)).mean()),
                           'Ortalama aylık SPY farkı': float((aylik - spy.reindex(aylik.index)).mean()),
                           'Ortalama hisse sayısı': float(d.groupby('tarih').size().mean())})
    sira = pd.DataFrame(sira_kayit)

    # Hangi kriter işe yarıyor: her ay kriter sıralaması ile sonraki ayın getiri sıralaması arasındaki ilişki
    def ic_tablosu(veri):
        kayit = []
        for k, ad_ in KRITER_ADI.items():
            if k not in veri.columns:
                continue
            ic = veri.groupby('tarih').apply(
                lambda x: x[k].rank().corr(x['getiri_1ay'].rank()) if x[k].notna().sum() >= 8 else np.nan,
                include_groups=False).dropna()
            if len(ic) < 6:
                continue
            kayit.append({'Kriter': ad_, 'Ortalama ilişki (IC)': ic.mean(),
                          't değeri': ic.mean() / (ic.std() / math.sqrt(len(ic))) if ic.std() > 0 else np.nan,
                          'Pozitif ay oranı': float((ic > 0).mean()), 'Ay sayısı': len(ic)})
        kolonlar = ['Kriter', 'Ortalama ilişki (IC)', 't değeri', 'Pozitif ay oranı', 'Ay sayısı']
        if not kayit:
            return pd.DataFrame(columns=kolonlar)
        return pd.DataFrame(kayit, columns=kolonlar).sort_values('Ortalama ilişki (IC)', ascending=False)
    kriter = ic_tablosu(ad)
    kriter_evren = ic_tablosu(evren)

    # Hisse bazında: aday listesindeyken en çok kazandıranlar
    hisse = ad.groupby('ticker').agg(
        aday_ay=('getiri_1ay', 'size'), ilk10_ay=('ilk_10', 'sum'), ort_sira=('sira', 'mean'),
        ort_aylik=('getiri_1ay', 'mean'), birlesik=('getiri_1ay', lambda r: (1 + r).prod() - 1),
        ort_spy_farki=('spy_farki', 'mean'), beklenmedik_ay=('beklenmedik', 'sum'),
        endeks=('endeks', 'last')).reset_index()
    hisse = hisse.sort_values('birlesik', ascending=False)
    hisse.columns = ['Hisse', 'Aday olduğu ay', 'İlk 10\'da olduğu ay', 'Ortalama sıra', 'Ortalama aylık getiri',
                     'Aday iken birleşik getiri', 'Ortalama aylık SPY farkı', 'Beklenmedik ay sayısı', 'Endeks']

    # Beklenmedik hareketler
    bek = ad[ad['beklenmedik']].copy()
    bek['Yön'] = np.where(bek['getiri_1ay'] > 0, 'Yükseliş', 'Düşüş')
    bek = bek.reindex(bek['getiri_1ay'].abs().sort_values(ascending=False).index)
    bek = bek[['tarih', 'ticker', 'endeks', 'sira', 'ilk_10', 'toplam', 'getiri_1ay', 'spy_1ay', 'z', 'Yön']]
    bek.columns = ['Ay sonu', 'Hisse', 'Endeks', 'Aday sırası', 'İlk 10', 'Toplam puan', 'Sonraki ay getirisi',
                   'SPY sonraki ay', 'Sapma (z)', 'Yön']
    bek['Not'] = 'Habere veya şirkete özel bir olaya bağlı olabilir; o ayki haberlere bak.'

    # Sonuç beklenmedik hareketlere ne kadar bağlı
    ilk10 = ad[ad['ilk_10']]
    ilk10_tum = ilk10.groupby('tarih')['getiri_1ay'].mean()
    ilk10_temiz = ilk10[~ilk10['beklenmedik']].groupby('tarih')['getiri_1ay'].mean()
    tum_tum = ad.groupby('tarih')['getiri_1ay'].mean()
    spy_ay = ad.groupby('tarih')['spy_1ay'].first()
    en_iyi5 = hisse.head(5)['Hisse'].tolist()
    ilk10_en_iyi5_haric = ilk10[~ilk10['ticker'].isin(en_iyi5)].groupby('tarih')['getiri_1ay'].mean()
    bagimlilik = pd.DataFrame([
        ('İlk 10 aday: yıllık getiri', _yilliklastir(ilk10_tum)),
        ('İlk 10 aday: beklenmedik aylar çıkarılınca', _yilliklastir(ilk10_temiz)),
        ('İlk 10 aday: en çok kazandıran 5 hisse çıkarılınca', _yilliklastir(ilk10_en_iyi5_haric)),
        ('Bütün adaylar: yıllık getiri', _yilliklastir(tum_tum)),
        ('S&P 500 (SPY): yıllık getiri', _yilliklastir(spy_ay)),
        ('Beklenmedik hareket oranı (aday-ay)', float(ad['beklenmedik'].mean())),
        ('Beklenmedik hareketlerde yükseliş payı', float((ad.loc[ad['beklenmedik'], 'getiri_1ay'] > 0).mean())
         if ad['beklenmedik'].any() else np.nan),
    ], columns=['Ölçüm', 'Değer'])

    detay = ad[['tarih', 'ticker', 'endeks', 'sira', 'ilk_10', 'toplam', 'ucuzluk', 'kalite', 'saglamlik', 'buyume',
                'getiri_1ay', 'spy_1ay', 'spy_farki', 'beklenmedik']].sort_values(['tarih', 'sira'])
    detay.columns = ['Ay sonu', 'Hisse', 'Endeks', 'Aday sırası', 'İlk 10', 'Toplam puan', 'Ucuzluk', 'Kalite',
                     'Sağlamlık', 'Büyüme', 'Sonraki ay getirisi', 'SPY sonraki ay', 'SPY farkı', 'Beklenmedik']

    sonuc = {'bagimlilik': bagimlilik, 'sira': sira, 'kriter': kriter, 'kriter_evren': kriter_evren,
             'hisse': hisse, 'beklenmedik': bek, 'detay': detay}
    excel_yaz(sonuc, os.path.join(CIKTI, 'aday_analizi.xlsx'))
    return sonuc


def excel_yaz(a, yol):
    aciklama = pd.DataFrame({'Bu dosya': [
        'Her ay sonunda ADAY çıkan hisseler toplam puana göre sıralandı (1 = en yüksek puan) ve bir hafta gecikmeyle '
        'sonraki ayın getirisi ölçüldü.',
        'Sıra dilimleri: ilk 10 aday gerçekten daha çok kazandırmış mı?',
        'Hangi kriter işe yarıyor: her kriterin sıralaması ile sonraki ayın getiri sıralaması arasındaki ortalama ilişki '
        '(IC). 0 = ilişki yok, 0,05 ve üzeri hisse seçiminde anlamlı kabul edilir. t değeri 2\'nin üzerindeyse sonuç '
        'şansa bağlı olmayabilir.',
        'Beklenmedik hareketler: o ay diğer adaylardan çok farklı hareket eden ya da bir ayda %25\'ten fazla oynayan '
        'hisseler. Sebep (haber, bilanço sürprizi, satın alma) bu analizde bilinmez; o ayki haberlere bakmak gerekir.',
        'Geçmiş performans gelecek için garanti değildir. Bu dosya yatırım tavsiyesi değildir.']})
    sayfalar = [('Özet', a['bagimlilik']), ('Sıra dilimleri', a['sira']),
                ('Hangi kriter işe yarıyor', a['kriter']), ('Kriterler - bütün evren', a['kriter_evren']),
                ('En çok kazandıranlar', a['hisse']), ('Beklenmedik hareketler', a['beklenmedik']),
                ('Tüm aday ayları', a['detay']), ('Açıklama', aciklama)]
    yuzde_kelime = ('getiri', 'oranı', 'farkı', 'Değer', 'puan', 'Ucuzluk', 'Kalite', 'Sağlamlık', 'Büyüme',
                    'SPY sonraki', 'SPY farkı')
    with pd.ExcelWriter(yol, engine='openpyxl') as w:
        for ad, df in sayfalar:
            df = df.copy()
            for c in df.columns:
                if str(df[c].dtype).startswith('datetime'):
                    df[c] = df[c].dt.date
            df.to_excel(w, sheet_name=ad, index=False)
            ws = w.sheets[ad]
            for j, c in enumerate(df.columns, 1):
                harf = ws.cell(row=1, column=j).column_letter
                ws.column_dimensions[harf].width = max(12, min(60, len(str(c)) + 4))
                if ad == 'Özet' and c == 'Değer':
                    fmt = '0.0%'
                elif c in ('Ortalama ilişki (IC)',):
                    fmt = '0.000'
                elif c in ('t değeri', 'Sapma (z)', 'Ortalama sıra', 'Ortalama hisse sayısı'):
                    fmt = '0.00'
                elif any(k in str(c) for k in yuzde_kelime) and df[c].dtype.kind == 'f':
                    fmt = '0.0%'
                else:
                    fmt = None
                if fmt:
                    for i in range(2, len(df) + 2):
                        ws.cell(row=i, column=j).number_format = fmt
            ws.freeze_panes = 'B2'
            if ad == 'Açıklama':
                ws.column_dimensions['A'].width = 140


# ---------------------------------------------------------------- rapor
def _grafik(fig):
    tampon = io.BytesIO()
    fig.savefig(tampon, format='png', dpi=110, bbox_inches='tight')
    return '<img src="data:image/png;base64,' + base64.b64encode(tampon.getvalue()).decode() + '" style="max-width:100%">'


def rapor_yaz(ozet, yillik, egri, dilim, bilgi, analiz=None, varyant=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    renk = {'S&P 500 (SPY)': '#555555', 'Adaylar - Haftalık': '#1f77b4', 'Adaylar - Aylık': '#2ca02c',
            'Adaylar - Üç aylık': '#d62728'}
    fig, ax = plt.subplots(figsize=(10, 5))
    for k in egri.columns:
        if k in renk:
            ax.plot(egri.index, egri[k], label=k, color=renk[k], lw=1.6)
        elif 'Aylık' in k:
            ax.plot(egri.index, egri[k], label=k, color='#999999', lw=1, ls='--')
    ax.set_yscale('log')
    ax.set_title('1 dolar nereye gitti (logaritmik ölçek, işlem maliyeti dahil)')
    ax.grid(alpha=.3)
    ax.legend(fontsize=8)
    g1 = _grafik(fig)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.bar(dilim.index.astype(str), dilim['Yıllık getiri'] * 100, color='#1f77b4')
    ax.set_ylabel('Yıllık getiri (%)')
    ax.set_title('Puan dilimlerine göre sonraki ayın getirisi')
    ax.grid(alpha=.3, axis='y')
    g2 = _grafik(fig)
    plt.close(fig)

    def tablo(df, yuzde_kolonlar=None):
        df = df.copy()
        for c in df.columns:
            if df[c].dtype.kind == 'f':
                if yuzde_kolonlar is None or c in yuzde_kolonlar:
                    df[c] = df[c].map(lambda v: '' if pd.isna(v) else f'{v * 100:.1f}%')
                else:
                    df[c] = df[c].map(lambda v: '' if pd.isna(v) else f'{v:.2f}')
        return df.to_html(border=0, classes='t')

    varyant_html = ''
    if varyant is not None:
        v = varyant['karsilastirma']
        v = v[v['Sıklık'] == 'Aylık'].set_index('Versiyon').drop(columns=['Sıklık'])
        varyant_html = ('<h2>İyileştirme denemeleri (en iyi 10 aday, aylık)</h2><p>Kurallar 2015-2020 dönemine göre '
                        'düşünüldü; 2021 ve sonrası hiç dokunulmamış sınama dönemidir. Bir değişiklik ancak iki dönemde de '
                        'mevcut sistemden iyiyse gerçek iyileşme sayılır. Ayrıntılar varyant_testi.xlsx dosyasında.</p>'
                        + tablo(v, [c for c in v.columns if 'getiri' in c or 'düşüş' in c or 'Oynaklık' in c]))
    analiz_html = ''
    if analiz:
        analiz_html = ('<h2>Aday listesinin içi: ilk 10 gerçekten en iyisi mi?</h2>'
                       '<p>Ayrıntılar aday_analizi.xlsx dosyasında.</p>' + tablo(analiz['bagimlilik'].set_index('Ölçüm'), ['Değer'])
                       + '<h3>Aday sırasına göre sonraki ayın getirisi</h3>'
                       + tablo(analiz['sira'].set_index('Aday sırası'), ['Yıllık getiri', 'Beklenmedik aylar hariç yıllık getiri',
                                                                      'SPY\'yi geçtiği ay oranı', 'Ortalama aylık SPY farkı'])
                       + '<h3>Hangi kriter sonraki ayın getirisini öngörüyor (adaylar arasında)</h3>'
                       + tablo(analiz['kriter'].head(10).set_index('Kriter'), ['Pozitif ay oranı']))
    yuzdeler = ['Yıllık getiri', 'Oynaklık (yıllık)', 'En büyük düşüş', 'Toplam getiri', 'SPY\'yi geçtiği hafta oranı']
    html = f"""<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8"><title>Geriye dönük test</title>
<style>body{{font-family:Arial;max-width:1000px;margin:24px auto;color:#222}}
table.t{{border-collapse:collapse;font-size:13px}}table.t td,table.t th{{border-bottom:1px solid #ddd;padding:4px 8px;text-align:right}}
table.t th{{background:#1F3864;color:#fff}}.uyari{{background:#FFF4E5;padding:8px 12px;border-left:4px solid #E69500}}</style></head><body>
<h1>Değer taraması: geriye dönük test</h1>
<p>Dönem: {bilgi['donem']}. Her değişim tarihinde o güne kadar açıklanmış verilerle puanlama yapıldı; "ADAY" çıkan bütün hisselere eşit para kondu, listeden çıkanlar satıldı. İşlemler sinyalden bir hafta sonraki kapanıştan yapıldı ve her alım ve satımda %{bilgi['islem_maliyeti'] * 100:.2f} maliyet düşüldü. Çalışma: {bilgi['calisma']}.</p>
{''.join(f'<p class="uyari">{u}</p>' for u in bilgi['uyarilar'])}
<h2>Özet</h2>{tablo(ozet.set_index('Strateji'), yuzdeler)}
<h2>Büyüme eğrileri</h2>{g1}
<h2>Puanın öngörü gücü</h2><p>Her ay sonunda evrendeki hisseler toplam puana göre beş eşit gruba bölündü. Puan işe yarıyorsa 5. grubun getirisi 1. gruptan belirgin şekilde yüksek olmalı.</p>{g2}{tablo(dilim)}
{varyant_html}
{analiz_html}
<h2>Yıllara göre getiri</h2>{tablo(yillik)}
<p style="color:#777;font-size:12px">Geçmiş performans gelecek için garanti değildir. Bu test yatırım tavsiyesi değildir.</p>
</body></html>"""
    with open(os.path.join(CIKTI, 'rapor.html'), 'w', encoding='utf-8') as f:
        f.write(html)


if __name__ == '__main__':
    main()
