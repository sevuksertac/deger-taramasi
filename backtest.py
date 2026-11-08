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
ISLEM_MALIYETI = float(os.environ.get('BT_MALIYET', '0.001'))  # tek yön: %0,10 (komisyon + alış/satış farkı)
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
    lst = [f for f in _dei(facts, 'EntityCommonStockSharesOutstanding', 'shares') if f.get('filed', '9') <= kesim]
    if lst:
        son = max(f['end'] for f in lst)
        ayni = [f for f in lst if f['end'] == son]
        en_yeni = max(f.get('filed', '') for f in ayni)
        toplam = sum(f['val'] for f in ayni if f.get('filed', '') == en_yeni)
        if toplam > 0:
            return toplam
    tarih = T.son_bilanco_tarihi(facts)
    us = T.anlik(facts, T.ETIKET['hisseDolasim'], tarih, 'shares') if tarih else None
    if us:
        return us
    return T.ttm(facts, T.ETIKET['hisseSeyreltilmis'], 'shares')[0]


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


def sp500_degisiklikleri(ua):
    """Wikipedia'daki S&P 500 değişiklik tablosu: [(tarih, eklenen, çıkarılan), ...]"""
    html = T.indir(T.SP500_URL, ua).decode('utf-8')
    o = TabloOkuyucu('changes')
    o.feed(html)
    degisim = []
    for s in o.satirlar:
        if len(s) < 4:
            continue
        try:
            t = datetime.strptime(re.sub(r'\[.*?\]', '', s[0]).strip(), '%B %d, %Y').date()
        except ValueError:
            continue
        eklenen = s[1].strip().upper().replace('.', '-')
        cikan = s[3].strip().upper().replace('.', '-') if len(s) > 3 else ''
        degisim.append((t, eklenen, cikan))
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
            deger = agirlik * (1 + r)
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
    puan.to_csv(os.path.join(CIKTI, 'puanlar.csv.gz'), index=False, compression='gzip')
    print(f'  {len(puan)} satır puanlandı ({time.time() - t0:.0f} sn)')

    # Haftalık getiriler: t'den bir sonraki haftaya
    getiri = duz_h.pct_change(fill_method=None).shift(-1)
    getiri = getiri.iloc[:-1]

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

    for ad, tarihler in siklik.items():
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
            aylik_getiri[a] = duz_h.loc[sonraki[b]] / duz_h.loc[sonraki[a]] - 1
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

    bilgi = {
        'calisma': datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
        'donem': f'{haftalar[0].date()} - {getiri.index[-1].date()}',
        'islem_maliyeti': ISLEM_MALIYETI, 'uyarilar': uyarilar,
        'sure_dk': round((time.time() - t0) / 60, 1),
    }
    with open(os.path.join(CIKTI, 'bilgi.json'), 'w', encoding='utf-8') as f:
        json.dump(bilgi, f, ensure_ascii=False, indent=1)
    rapor_yaz(ozet_df, yillik_df, egri_df, dilim_ozet, bilgi)
    print(ozet_df.to_string())
    print(f'Bitti: {bilgi["sure_dk"]} dk')


# ---------------------------------------------------------------- rapor
def _grafik(fig):
    tampon = io.BytesIO()
    fig.savefig(tampon, format='png', dpi=110, bbox_inches='tight')
    return '<img src="data:image/png;base64,' + base64.b64encode(tampon.getvalue()).decode() + '" style="max-width:100%">'


def rapor_yaz(ozet, yillik, egri, dilim, bilgi):
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
<h2>Yıllara göre getiri</h2>{tablo(yillik)}
<p style="color:#777;font-size:12px">Geçmiş performans gelecek için garanti değildir. Bu test yatırım tavsiyesi değildir.</p>
</body></html>"""
    with open(os.path.join(CIKTI, 'rapor.html'), 'w', encoding='utf-8') as f:
        f.write(html)


if __name__ == '__main__':
    main()
