"""
ABD Değer Taraması - veri motoru (GitHub Actions'ta çalışır)

tickers.txt'teki hisselerin temel verilerini SEC EDGAR'dan çeker ve veri.csv'ye yazar.
Google Sheet'teki script bu dosyayı okuyup puanlamayı yapar.

Yalnızca Python standart kütüphanesini kullanır, ek kurulum gerekmez.
SEC iletişim bilgisi SEC_UA ortam değişkeninden okunur (örnek: "Ad Soyad ad@ornek.com").
"""
import csv
import gzip
from html.parser import HTMLParser
import io
import json
import os
import re
import tempfile
import zipfile
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

BEKLEME = 0.15  # SEC: saniyede en fazla 10 istek

VERI_SUTUNLARI = [
    'ticker', 'sirket', 'cik', 'sic', 'grup', 'sektor', 'bilancoTarihi', 'kaynak',
    'satisTTM', 'brutKarTTM', 'ebitTTM', 'netKarTTM', 'amortismanTTM', 'inaTTM', 'capexTTM', 'faizTTM',
    'varlik', 'yukumluluk', 'ozkaynak', 'donenVarlik', 'kvYukumluluk', 'nakit', 'borc', 'gecmisKar', 'hisseAdedi',
    'satisFY', 'satisFY1', 'netKarFY', 'netKarFY1', 'varlikFY', 'varlikFY1', 'inaFY',
    'uvBorcFY', 'uvBorcFY1', 'donenFY', 'donenFY1', 'kvYukFY', 'kvYukFY1',
    'hisseFY', 'hisseFY1', 'brutKarFY', 'brutKarFY1', 'not', 'guncelleme', 'endeks', 'trendKisa', 'trendOrta', 'trendUzun',
]

ETIKET = {
    'satis': ['Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax',
              'RevenueFromContractWithCustomerIncludingAssessedTax', 'SalesRevenueNet', 'SalesRevenueGoodsNet'],
    'satisBanka': ['InterestAndDividendIncomeOperating'],
    'brutKar': ['GrossProfit'],
    'satisMaliyeti': ['CostOfRevenue', 'CostOfGoodsAndServicesSold', 'CostOfGoodsSold'],
    'ebit': ['OperatingIncomeLoss'],
    'vergiOncesi': ['IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
                    'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments',
                    'IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic'],
    'netKar': ['NetIncomeLoss', 'ProfitLoss', 'NetIncomeLossAvailableToCommonStockholdersBasic'],
    'amortisman': ['DepreciationDepletionAndAmortization', 'DepreciationAndAmortization',
                   'DepreciationAmortizationAndAccretionNet', 'Depreciation'],
    'ina': ['NetCashProvidedByUsedInOperatingActivities',
            'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'],
    'capex': ['PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets'],
    'faiz': ['InterestExpense', 'InterestExpenseNonoperating', 'InterestExpenseDebt', 'InterestAndDebtExpense'],
    'varlik': ['Assets'],
    'yukumluluk': ['Liabilities'],
    'ozkaynak': ['StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'],
    'ozkaynakToplam': ['StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest', 'StockholdersEquity'],
    'donen': ['AssetsCurrent'],
    'kvYuk': ['LiabilitiesCurrent'],
    'nakit': ['CashAndCashEquivalentsAtCarryingValue',
              'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents', 'Cash'],
    'kvYatirim': ['ShortTermInvestments', 'MarketableSecuritiesCurrent',
                  'AvailableForSaleSecuritiesDebtSecuritiesCurrent'],
    'borcToplamUV': ['LongTermDebt', 'LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities'],
    'borcUV': ['LongTermDebtNoncurrent', 'LongTermDebtAndCapitalLeaseObligations'],
    'borcUVCari': ['LongTermDebtCurrent', 'LongTermDebtAndCapitalLeaseObligationsCurrent'],
    'borcKV': ['ShortTermBorrowings', 'DebtCurrent'],
    'ticariSenet': ['CommercialPaper'],
    'gecmisKar': ['RetainedEarningsAccumulatedDeficit'],
    'hisseSeyreltilmis': ['WeightedAverageNumberOfDilutedSharesOutstanding',
                          'WeightedAverageNumberOfSharesOutstandingBasic'],
    'hisseDolasim': ['CommonStockSharesOutstanding'],
}


# ---------------------------------------------------------------- yardımcılar
def gun_farki(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def finansal_mi(sic):
    try:
        s = int(sic)
    except (TypeError, ValueError):
        return False
    return 6000 <= s <= 6799


def olgular(facts, etiketler, birim='USD', ad='us-gaap'):
    """Etiket listesindeki olguları dönem bazında birleştirir; öncelik soldaki etiketindir."""
    sonuc = {}
    ns = (facts.get('facts') or {}).get(ad) or {}
    for tag in etiketler:
        u = ((ns.get(tag) or {}).get('units') or {}).get(birim)
        if not u:
            continue
        bu = {}
        for f in u:
            key = (f.get('start') or '') + '|' + f['end']
            if key not in bu or f.get('filed', '') > bu[key].get('filed', ''):
                bu[key] = f
        for k, v in bu.items():
            sonuc.setdefault(k, v)
    return list(sonuc.values())


def yillik_seri(facts, etiketler, birim='USD'):
    lst = [f for f in olgular(facts, etiketler, birim)
           if f.get('start') and abs(gun_farki(f['start'], f['end']) - 365) <= 20]
    return sorted(lst, key=lambda f: f['end'], reverse=True)


def seriden_bul(seri, bitis, tolerans=20):
    for x in seri:
        if abs(gun_farki(x['end'], bitis)) <= tolerans:
            return x['val']
    return None


def ttm(facts, etiketler, birim='USD'):
    """Son 12 ay: son yıllık + bu yılın birikimli dönemi - geçen yılın aynı dönemi."""
    hepsi = [f for f in olgular(facts, etiketler, birim) if f.get('start')]
    yillik = sorted([f for f in hepsi if abs(gun_farki(f['start'], f['end']) - 365) <= 20],
                    key=lambda f: f['end'], reverse=True)
    if not yillik:
        return None, None, ''
    fy = yillik[0]
    ytd_aday = [f for f in hepsi if f['end'] > fy['end']
                and abs(gun_farki(fy['end'], f['start']) - 1) <= 20
                and 80 <= gun_farki(f['start'], f['end']) <= 300]
    if not ytd_aday:
        return fy['val'], fy['end'], 'Yıllık'
    ytd = max(ytd_aday, key=lambda f: f['end'])
    sure = gun_farki(ytd['start'], ytd['end'])
    onceki = next((f for f in hepsi if abs(gun_farki(f['end'], ytd['end']) - 365) <= 20
                   and abs(gun_farki(f['start'], f['end']) - sure) <= 15 and f['end'] <= fy['end']), None)
    if not onceki:
        return fy['val'], fy['end'], 'Yıllık'
    return fy['val'] + ytd['val'] - onceki['val'], ytd['end'], 'TTM'


def anlik(facts, etiketler, tarih, birim='USD'):
    lst = [f for f in olgular(facts, etiketler, birim)
           if not f.get('start') and abs(gun_farki(f['end'], tarih)) <= 7]
    if not lst:
        return None
    return max(lst, key=lambda f: f.get('filed', ''))['val']


def son_bilanco_tarihi(facts):
    lst = [f['end'] for f in olgular(facts, ETIKET['varlik']) if not f.get('start')]
    return max(lst) if lst else None


def topla(*degerler):
    var = [d for d in degerler if d is not None]
    return sum(var) if var else None


def borc(facts, tarih):
    toplam_uv = anlik(facts, ETIKET['borcToplamUV'], tarih)
    uv = toplam_uv if toplam_uv is not None else topla(
        anlik(facts, ETIKET['borcUV'], tarih), anlik(facts, ETIKET['borcUVCari'], tarih))
    return topla(uv, anlik(facts, ETIKET['borcKV'], tarih), anlik(facts, ETIKET['ticariSenet'], tarih))


BERKSHIRE_CIK = 1067983   # raporlarını A sınıfı eşdeğeriyle verir; fiyatı kullanılan BRK-B, 1 A = 1500 B
SINIF_ESIGI = 0.75        # kapak sayfası adedi son çeyreğin seyreltilmiş adedinin bu oranından azsa eksik sayılır


TAZELIK_GUN = 400         # son bilançodan bu kadar eski hisse adedi bayat sayılır (bazı şirketler etiketlemeyi bırakmış)


def _taze(f, bilanco):
    return bilanco is None or gun_farki(f['end'], bilanco) <= TAZELIK_GUN


def son_seyreltilmis_adet(facts):
    """En son dönemin (tercihen çeyrek) seyreltilmiş ağırlıklı ortalama hisse adedi. Etiket bayatsa
    (V, BRK gibi yıllardır güncellenmeyenler) net kâr / hisse başı kârdan türetilir."""
    bilanco = son_bilanco_tarihi(facts)
    lst = [f for f in olgular(facts, ETIKET['hisseSeyreltilmis'], 'shares')
           if f.get('start') and f.get('val') and _taze(f, bilanco)]
    if lst:
        son = max(f['end'] for f in lst)
        ayni = [f for f in lst if f['end'] == son]
        return min(ayni, key=lambda f: gun_farki(f['start'], f['end']))['val']
    # Yedek: aynı döneme ait net kâr / hisse başı kâr
    hbk = [f for f in olgular(facts, ['EarningsPerShareDiluted', 'EarningsPerShareBasic'], 'USD/shares')
           if f.get('start') and f.get('val') and f['val'] > 0 and _taze(f, bilanco)]
    kar = {(f['start'], f['end']): f['val'] for f in olgular(facts, ETIKET['netKar']) if f.get('start')}
    adaylar = [(f['end'], -gun_farki(f['start'], f['end']), kar[(f['start'], f['end'])] / f['val'])
               for f in hbk if kar.get((f['start'], f['end']), 0) > 0]
    return max(adaylar)[2] if adaylar else None


def hisse_dogrula(adet, facts):
    """Çok sınıflı şirketlerde (MA, V, CMCSA, NKE...) SEC toplu dosyası kapak sayfasındaki adedi çoğu zaman
    yalnızca bir sınıf için verir; bu durumda piyasa değeri olduğundan düşük, hisse olduğundan ucuz görünür.
    Kapak adedi son çeyreğin seyreltilmiş adedinin %75'inden azsa seyreltilmiş adet kullanılır. Bölünme sonrası
    kapak adedi büyük çıkar; o durumda kapak adedi (güncel olan) korunur."""
    dil = son_seyreltilmis_adet(facts)
    if dil and (not adet or adet < SINIF_ESIGI * dil):
        adet = dil
    if adet and int(facts.get('cik') or 0) == BERKSHIRE_CIK:
        adet = adet * 1500 if adet < 5e6 else adet   # A eşdeğeri -> B adedi
    return adet


def hisse_adedi(facts, tarih):
    """Kapak sayfasındaki dolaşımdaki hisse sayısı (sınıflar toplanır), seyreltilmiş adetle sağlaması yapılmış."""
    dei = (((facts.get('facts') or {}).get('dei') or {})
           .get('EntityCommonStockSharesOutstanding') or {}).get('units', {}).get('shares')
    adet = None
    bilanco = son_bilanco_tarihi(facts)
    dei = [f for f in (dei or []) if _taze(f, bilanco)]
    if dei:
        son = max(f['end'] for f in dei)
        ayni = [f for f in dei if f['end'] == son]
        en_yeni = max(f.get('filed', '') for f in ayni)
        toplam = sum(f['val'] for f in ayni if f.get('filed', '') == en_yeni)
        if toplam > 0:
            adet = toplam
    if not adet:
        adet = anlik(facts, ETIKET['hisseDolasim'], tarih, 'shares')
    return hisse_dogrula(adet, facts)


def sirket_verisi_cikar(facts, sic=None):
    r = {'sirket': facts.get('entityName', '')}
    tarih = son_bilanco_tarihi(facts)
    if not tarih:
        r['not'] = 'Bilanço verisi bulunamadı'
        return r
    r['bilancoTarihi'] = tarih
    banka = finansal_mi(sic)

    satis, _, satis_kaynak = ttm(facts, ETIKET['satis'])
    if satis is None and banka:
        satis, _, satis_kaynak = ttm(facts, ETIKET['satisBanka'])
    net, _, net_kaynak = ttm(facts, ETIKET['netKar'])
    r['kaynak'] = net_kaynak or satis_kaynak
    r['satisTTM'] = satis
    r['netKarTTM'] = net
    r['ebitTTM'] = ttm(facts, ETIKET['ebit'])[0]
    if r['ebitTTM'] is None and not banka:
        # Esas faaliyet kârını raporlamayan şirketler: vergi öncesi kâr + faiz gideri
        vo = ttm(facts, ETIKET['vergiOncesi'])[0]
        if vo is not None:
            r['ebitTTM'] = vo + (ttm(facts, ETIKET['faiz'])[0] or 0)
    brut = ttm(facts, ETIKET['brutKar'])[0]
    if brut is None and satis is not None:
        maliyet = ttm(facts, ETIKET['satisMaliyeti'])[0]
        if maliyet is not None:
            brut = satis - maliyet
    r['brutKarTTM'] = brut
    r['amortismanTTM'] = ttm(facts, ETIKET['amortisman'])[0]
    r['inaTTM'] = ttm(facts, ETIKET['ina'])[0]
    r['capexTTM'] = ttm(facts, ETIKET['capex'])[0]
    r['faizTTM'] = ttm(facts, ETIKET['faiz'])[0]

    r['varlik'] = anlik(facts, ETIKET['varlik'], tarih)
    r['ozkaynak'] = anlik(facts, ETIKET['ozkaynak'], tarih)
    yuk = anlik(facts, ETIKET['yukumluluk'], tarih)
    oz_toplam = anlik(facts, ETIKET['ozkaynakToplam'], tarih)
    if yuk is None and r['varlik'] is not None and oz_toplam is not None:
        yuk = r['varlik'] - oz_toplam
    r['yukumluluk'] = yuk
    r['donenVarlik'] = anlik(facts, ETIKET['donen'], tarih)
    r['kvYukumluluk'] = anlik(facts, ETIKET['kvYuk'], tarih)
    r['nakit'] = topla(anlik(facts, ETIKET['nakit'], tarih), anlik(facts, ETIKET['kvYatirim'], tarih))
    b = borc(facts, tarih)
    r['borc'] = b if b is not None else 0
    r['gecmisKar'] = anlik(facts, ETIKET['gecmisKar'], tarih)
    r['hisseAdedi'] = hisse_adedi(facts, tarih)

    net_seri = yillik_seri(facts, ETIKET['netKar'])
    if net_seri:
        fy = net_seri[0]['end']
        fy1 = (date.fromisoformat(fy) - timedelta(days=365)).isoformat()

        def yil(etiketler, bitis, birim='USD'):
            return seriden_bul(yillik_seri(facts, etiketler, birim), bitis)

        def an(etiketler, bitis):
            return anlik(facts, etiketler, bitis)

        r['netKarFY'] = net_seri[0]['val']
        r['netKarFY1'] = seriden_bul(net_seri, fy1)
        r['satisFY'] = yil(ETIKET['satis'], fy)
        r['satisFY1'] = yil(ETIKET['satis'], fy1)
        if r['satisFY'] is None and banka:
            r['satisFY'] = yil(ETIKET['satisBanka'], fy)
            r['satisFY1'] = yil(ETIKET['satisBanka'], fy1)
        r['inaFY'] = yil(ETIKET['ina'], fy)
        r['varlikFY'] = an(ETIKET['varlik'], fy)
        r['varlikFY1'] = an(ETIKET['varlik'], fy1)
        r['uvBorcFY'] = an(ETIKET['borcUV'], fy)
        r['uvBorcFY1'] = an(ETIKET['borcUV'], fy1)
        if r['uvBorcFY'] is None and r['uvBorcFY1'] is None:
            r['uvBorcFY'] = an(ETIKET['borcToplamUV'], fy)
            r['uvBorcFY1'] = an(ETIKET['borcToplamUV'], fy1)
        r['donenFY'] = an(ETIKET['donen'], fy)
        r['donenFY1'] = an(ETIKET['donen'], fy1)
        r['kvYukFY'] = an(ETIKET['kvYuk'], fy)
        r['kvYukFY1'] = an(ETIKET['kvYuk'], fy1)
        r['hisseFY'] = yil(ETIKET['hisseSeyreltilmis'], fy, 'shares')
        r['hisseFY1'] = yil(ETIKET['hisseSeyreltilmis'], fy1, 'shares')
        b_fy, b_fy1 = yil(ETIKET['brutKar'], fy), yil(ETIKET['brutKar'], fy1)
        if b_fy is None and r['satisFY'] is not None:
            m = yil(ETIKET['satisMaliyeti'], fy)
            if m is not None:
                b_fy = r['satisFY'] - m
        if b_fy1 is None and r['satisFY1'] is not None:
            m1 = yil(ETIKET['satisMaliyeti'], fy1)
            if m1 is not None:
                b_fy1 = r['satisFY1'] - m1
        r['brutKarFY'], r['brutKarFY1'] = b_fy, b_fy1

    eksik = []
    if r.get('netKarTTM') is None:
        eksik.append('net kâr')
    if r.get('ozkaynak') is None:
        eksik.append('özkaynak')
    if not banka and r.get('ebitTTM') is None:
        eksik.append('esas faaliyet kârı')
    if not banka and r.get('inaTTM') is None:
        eksik.append('nakit akışı')
    r['not'] = ('Eksik veri: ' + ', '.join(eksik)) if eksik else ''
    return r


# ---------------------------------------------------------------- ağ
def indir(url, ua, hedef=None):
    """URL'yi indirir. hedef verilirse dosyaya yazar, yoksa içeriği döndürür."""
    req = urllib.request.Request(url, headers={'User-Agent': ua, 'Accept-Encoding': 'gzip'})
    for deneme in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as res:
                gz = res.headers.get('Content-Encoding') == 'gzip'
                if hedef:
                    with open(hedef, 'wb') as f:
                        while True:
                            parca = res.read(1 << 20)
                            if not parca:
                                break
                            f.write(parca)
                    return hedef
                veri = res.read()
                return gzip.decompress(veri) if gz else veri
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503) and deneme < 2:
                time.sleep(5 * (deneme + 1))
                continue
            raise RuntimeError(f'Yanıt kodu {e.code} ({url})')
        except urllib.error.URLError:
            if deneme < 2:
                time.sleep(5)
                continue
            raise
    return None


def get_json(url, ua):
    veri = indir(url, ua)
    return json.loads(veri) if veri else None


# ---------------------------------------------------------------- evren
SP500_URL = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
SP500_YEDEK_URL = 'https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv'
FACTS_ZIP = 'https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip'
ADAY_SAYISI = int(os.environ.get('ADAY_SAYISI', '500'))
TOPLAM_SINIR = int(os.environ.get('TOPLAM_SINIR', '1000'))  # sheet'teki Tarama sayfası 1000 satır alır


class _TabloOkuyucu(HTMLParser):
    """Wikipedia'daki id="constituents" tablosunun her satırının ilk hücresini okur."""

    def __init__(self):
        super().__init__()
        self.tablo_derinlik = 0   # hedef tablonun içindeyken > 0
        self.satir = None
        self.hucre = None
        self.semboller = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'table':
            if self.tablo_derinlik:
                self.tablo_derinlik += 1
            elif a.get('id') == 'constituents':
                self.tablo_derinlik = 1
        elif self.tablo_derinlik == 1:
            if tag == 'tr':
                self.satir = []
            elif tag in ('td', 'th') and self.satir is not None:
                self.hucre = [] if tag == 'td' else None

    def handle_endtag(self, tag):
        if tag == 'table' and self.tablo_derinlik:
            self.tablo_derinlik -= 1
        elif self.tablo_derinlik == 1:
            if tag == 'td' and self.hucre is not None and self.satir is not None:
                self.satir.append(''.join(self.hucre).strip())
                self.hucre = None
            elif tag == 'tr' and self.satir is not None:
                if self.satir:
                    self.semboller.append(self.satir[0])
                self.satir = None

    def handle_data(self, data):
        if self.hucre is not None:
            self.hucre.append(data)


def _gecerli(semboller):
    temiz = [s.strip().upper() for s in semboller if re.fullmatch(r'[A-Za-z][A-Za-z0-9.\-]{0,9}', s.strip())]
    return list(dict.fromkeys(temiz))


def _wikipedia(ua):
    html = indir(SP500_URL, ua).decode('utf-8')
    o = _TabloOkuyucu()
    o.feed(html)
    return _gecerli(o.semboller)


def _yedek_kaynak(ua):
    metin = indir(SP500_YEDEK_URL, ua).decode('utf-8')
    satirlar = list(csv.reader(io.StringIO(metin)))
    i = [h.strip().lower() for h in satirlar[0]].index('symbol')
    return _gecerli([r[i] for r in satirlar[1:] if len(r) > i])


def sp500_cek(ua):
    """S&P 500 üyelerini alır: önce Wikipedia, sonra yedek kaynak, en son depodaki kayıtlı liste."""
    for ad, kaynak in (('Wikipedia', _wikipedia), ('yedek kaynak', _yedek_kaynak)):
        try:
            semboller = kaynak(ua)
            if len(semboller) < 450:
                raise RuntimeError(f'beklenenden az sembol ({len(semboller)})')
            with open('sp500.txt', 'w', encoding='utf-8') as f:
                f.write('\n'.join(semboller) + '\n')
            print(f'S&P 500 listesi {ad} üzerinden alındı: {len(semboller)} sembol')
            return semboller
        except Exception as e:
            print(f'Uyarı: S&P 500 listesi {ad} üzerinden alınamadı ({e})')
    if os.path.exists('sp500.txt'):
        print('Depodaki kayıtlı S&P 500 listesi kullanılıyor.')
        with open('sp500.txt', encoding='utf-8') as f:
            return [x.strip() for x in f if x.strip()]
    sys.exit('S&P 500 listesi hiçbir kaynaktan alınamadı.')


def borsa_listesi(ua):
    """NYSE ve Nasdaq'ta işlem gören hisseler: {ticker: (cik, ad)} ve {cik: ilk ticker}."""
    j = get_json('https://www.sec.gov/files/company_tickers_exchange.json', ua)
    alanlar = j['fields']
    i_cik, i_ad, i_t, i_b = (alanlar.index(x) for x in ('cik', 'name', 'ticker', 'exchange'))
    ticker_harita, cik_ticker = {}, {}
    for r in j['data']:
        if r[i_b] not in ('NYSE', 'Nasdaq'):
            continue
        cik = str(r[i_cik]).zfill(10)
        t = str(r[i_t]).upper()
        ticker_harita[t] = (cik, r[i_ad])
        # Bir şirketin birden fazla sınıfı/tercihli hissesi olabilir: tire içermeyen ilk kodu tercih et
        if cik not in cik_ticker or ('-' in cik_ticker[cik] and '-' not in t):
            cik_ticker[cik] = t
    return ticker_harita, cik_ticker


def halka_acik_deger(facts):
    """Şirketin raporladığı halka açık hisse değeri (USD): piyasa değerine yakın bir büyüklük ölçüsü."""
    u = (((facts.get('facts') or {}).get('dei') or {}).get('EntityPublicFloat') or {}).get('units', {}).get('USD')
    if not u:
        return None
    son = max(u, key=lambda f: (f['end'], f.get('filed', '')))
    if son['end'] < (date.today() - timedelta(days=730)).isoformat():
        return None  # iki yıldan eski: şirket artık rapor vermiyor olabilir
    return son['val']


def zipten_oku(z, cik):
    try:
        with z.open(f'CIK{cik}.json') as f:
            return json.load(f)
    except KeyError:
        return None


def _ort(k, n, geri=0):
    """Son n günün ortalaması; geri > 0 ise o kadar gün önceki n günlük ortalama."""
    son = len(k) - geri
    return sum(k[son - n:son]) / n


def _yon(yukari, asagi):
    return 'Yükseliş' if yukari else 'Düşüş' if asagi else 'Yatay'


def trend_durumu(kapanis):
    """Günlük kapanışlardan üç vadeli trend (kısa, orta, uzun).
    Kısa (20 gün):  fiyat 20 günlük ortalamanın üzerinde VE 20 günlük ortalama 1 ay (21 işlem günü) öncesinden yüksek.
    Orta (50 gün):  fiyat 50 günlüğün üzerinde VE 20 günlük 50 günlüğün üzerinde.
    Uzun (200 gün): fiyat 200 günlüğün üzerinde VE 50 günlük 200 günlüğün üzerinde.
    Koşulların ikisi de tersine dönmüşse Düşüş, karışıksa Yatay; veri yetmezse boş."""
    k = [x for x in kapanis if x is not None and x == x and x > 0]
    son = k[-1] if k else None
    kisa = orta = uzun = ''
    if len(k) >= 41:
        o20, o20_once = _ort(k, 20), _ort(k, 20, 21)
        kisa = _yon(son > o20 and o20 > o20_once, son < o20 and o20 < o20_once)
    if len(k) >= 50:
        o20, o50 = _ort(k, 20), _ort(k, 50)
        orta = _yon(son > o50 and o20 > o50, son < o50 and o20 < o50)
    if len(k) >= 200:
        o50, o200 = _ort(k, 50), _ort(k, 200)
        uzun = _yon(son > o200 and o50 > o200, son < o200 and o50 < o200)
    return kisa, orta, uzun


def trend_hesapla(tickerlar):
    """Yahoo Finance'ten son ~15 ayın günlük kapanışları; yfinance kurulu değilse trend boş kalır."""
    try:
        import yfinance as yf
    except ImportError:
        print('Uyarı: yfinance kurulu değil, trend hesaplanmadı.')
        return {}
    sonuc = {}
    tickerlar = sorted(set(t for t in tickerlar if t))
    for i in range(0, len(tickerlar), 200):
        grup = tickerlar[i:i + 200]
        try:
            d = yf.download(grup, period='15mo', interval='1d', auto_adjust=True, group_by='column',
                            threads=True, progress=False)
            kap = d['Close'] if 'Close' in d.columns.get_level_values(0) else None
        except Exception as e:
            print(f'Uyarı: trend fiyatları alınamadı ({e})')
            continue
        if kap is None:
            continue
        if not hasattr(kap, 'columns'):
            kap = kap.to_frame(grup[0])
        for t in grup:
            if t in kap.columns:
                sonuc[t] = trend_durumu(list(kap[t].dropna()))
    print(f'Trend hesaplandı: {sum(1 for v in sonuc.values() if v[2])} hisse (uzun vade dolu)')
    return sonuc


def main():
    ua = os.environ.get('SEC_UA', '').strip()
    if '@' not in ua:
        sys.exit('SEC_UA tanımlı değil. GitHub > Settings > Secrets and variables > Actions > Variables '
                 'altına SEC_UA adıyla "Ad Soyad eposta@ornek.com" ekle.')

    ticker_harita, cik_ticker = borsa_listesi(ua)
    sp = sp500_cek(ua)

    ek = []
    if os.path.exists('tickers.txt'):
        with open('tickers.txt', encoding='utf-8') as f:
            ek = [t.strip().upper() for t in f if t.strip() and not t.startswith('#')]

    print('SEC toplu veri dosyası indiriliyor...')
    zip_yolu = indir(FACTS_ZIP, ua, os.path.join(tempfile.gettempdir(), 'companyfacts.zip'))
    z = zipfile.ZipFile(zip_yolu)

    # 1) S&P 500 üyeleri (aynı şirketin iki hisse sınıfı varsa tek satır)
    evren, gorulen = [], set()
    for t in sp:
        e = ticker_harita.get(t.replace('.', '-'))
        if e and e[0] not in gorulen:
            gorulen.add(e[0])
            evren.append((t.replace('.', '-'), e[0], 'S&P 500'))

    # tickers.txt'te olup endekste olmayanlar 3. adımda eklenecek; toplam TOPLAM_SINIR'ı aşmasın
    ek_cik, ek_bulunamayan = set(), set()
    for t in ek:
        e = ticker_harita.get(t.replace('.', '-'))
        if e and e[0] not in gorulen:
            ek_cik.add(e[0])
        elif not e:
            ek_bulunamayan.add(t)  # yine de (hata notuyla) bir satır yer kaplar
    aday_kota = max(0, min(ADAY_SAYISI, TOPLAM_SINIR - len(evren) - len(ek_cik) - len(ek_bulunamayan)))

    # 2) Endekse aday: endeks dışındaki, son 12 ayı kârlı en büyük şirketler
    adaylar = []
    for cik, t in cik_ticker.items():
        if cik in gorulen or cik in ek_cik:
            continue
        facts = zipten_oku(z, cik)
        if not facts or 'us-gaap' not in (facts.get('facts') or {}):
            continue
        deger = halka_acik_deger(facts)
        if not deger:
            continue
        net = ttm(facts, ETIKET['netKar'])[0]
        if net is None or net <= 0:
            continue  # S&P 500'e girmek için kâr şartı var
        adaylar.append((deger, t, cik))
    adaylar.sort(reverse=True)
    for deger, t, cik in adaylar[:aday_kota]:
        gorulen.add(cik)
        evren.append((t, cik, 'Aday'))
    print(f'Endeks dışı uygun şirket: {len(adaylar)}, seçilen: {min(len(adaylar), aday_kota)}')

    # 3) tickers.txt'teki ek hisseler (portföyündekiler gibi)
    for t in ek:
        e = ticker_harita.get(t.replace('.', '-'))
        if e and e[0] not in gorulen:
            gorulen.add(e[0])
            evren.append((t.replace('.', '-'), e[0], 'Ek liste'))
        elif not e and t in ek_bulunamayan:
            ek_bulunamayan.discard(t)
            evren.append((t, None, 'Ek liste'))

    simdi = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    satirlar = []
    for n, (t, cik, endeks) in enumerate(evren, 1):
        r = {}
        try:
            if not cik:
                raise RuntimeError('NYSE/Nasdaq listesinde bulunamadı (ETF veya yabancı hisse olabilir)')
            facts = zipten_oku(z, cik)
            if not facts:
                raise RuntimeError('SEC finansal verisi yok')
            sub = get_json(f'https://data.sec.gov/submissions/CIK{cik}.json', ua) or {}
            time.sleep(BEKLEME)
            sic = sub.get('sic', '')
            r = sirket_verisi_cikar(facts, sic)
            r['cik'] = cik
            r['sic'] = sic
            r['sektor'] = sub.get('sicDescription', '')
            r['sirket'] = sub.get('name') or r.get('sirket') or ticker_harita.get(t, ('', ''))[1]
            r['grup'] = 'Finansal' if finansal_mi(sic) else ('Diğer' if sic else '')
        except Exception as e:  # bir hissenin hatası diğerlerini durdurmasın
            r = {'not': f'Hata: {e}'}
            print(f'{t}: {e}')
        r['ticker'] = t
        r['guncelleme'] = simdi
        r['endeks'] = endeks
        satirlar.append(r)
        if n % 100 == 0:
            print(f'{n}/{len(evren)} işlendi')

    trendler = trend_hesapla([r['ticker'] for r in satirlar])
    for r in satirlar:
        r['trendKisa'], r['trendOrta'], r['trendUzun'] = trendler.get(r['ticker'], ('', '', ''))

    with open('veri.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(VERI_SUTUNLARI)
        for r in satirlar:
            w.writerow(['' if r.get(k) is None else r.get(k) for k in VERI_SUTUNLARI])
    hatali = sum(1 for r in satirlar if str(r.get('not', '')).startswith('Hata'))
    print(f'{len(satirlar)} hisse yazıldı, {hatali} hatalı.')


if __name__ == '__main__':
    main()
