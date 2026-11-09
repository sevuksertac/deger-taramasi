"""Veri denetimi: veri.csv'deki hisse adedi ve kârdan çıkan piyasa değeri ile F/K'yı Yahoo Finance ile karşılaştırır.
Büyük sapmalar (hisse adedi, sınıf, birim hataları) denetim/denetim.csv'ye yazılır. Elle veya haftalık çalışır."""
import csv
import os
import sys
import time

import yfinance as yf

ESIK = 0.25   # %25'ten büyük sapma şüpheli sayılır


def sayi(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    satirlar = list(csv.DictReader(open('veri.csv', encoding='utf-8')))
    sonuc = []
    for n, r in enumerate(satirlar, 1):
        t = r['ticker']
        try:
            bilgi = yf.Ticker(t).info or {}
        except Exception as e:
            bilgi = {}
            print(f'{t}: Yahoo okunamadı ({e})')
        fiyat = bilgi.get('currentPrice') or bilgi.get('regularMarketPrice') or bilgi.get('navPrice')
        adet = sayi(r.get('hisseAdedi'))
        net = sayi(r.get('netKarTTM'))
        y_pd = bilgi.get('marketCap')
        y_fk = bilgi.get('trailingPE')
        b_pd = fiyat * adet if fiyat and adet else None
        b_fk = b_pd / net if b_pd and net and net > 0 else None
        pd_oran = b_pd / y_pd if b_pd and y_pd else None
        fk_oran = b_fk / y_fk if b_fk and y_fk else None
        tur = bilgi.get('quoteType', '')
        sorun = []
        if tur and tur not in ('EQUITY',):
            sorun.append(f'şirket değil ({tur})')
        if pd_oran and abs(pd_oran - 1) > ESIK:
            sorun.append(f'piyasa değeri sapması x{pd_oran:.2f}')
        if fk_oran and abs(fk_oran - 1) > ESIK:
            sorun.append(f'F/K sapması x{fk_oran:.2f}')
        sonuc.append(dict(ticker=t, sirket=r['sirket'], endeks=r['endeks'], tur=tur, fiyat=fiyat,
                          bizim_pd_mn=round(b_pd / 1e6) if b_pd else '', yahoo_pd_mn=round(y_pd / 1e6) if y_pd else '',
                          pd_oran=round(pd_oran, 3) if pd_oran else '', bizim_fk=round(b_fk, 1) if b_fk else '',
                          yahoo_fk=round(y_fk, 1) if y_fk else '', fk_oran=round(fk_oran, 3) if fk_oran else '',
                          sorun='; '.join(sorun)))
        if n % 100 == 0:
            print(f'{n}/{len(satirlar)}')
        time.sleep(0.2)
    os.makedirs('denetim', exist_ok=True)
    with open('denetim/denetim.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(sonuc[0]))
        w.writeheader()
        w.writerows(sonuc)
    sorunlu = [s for s in sonuc if s['sorun']]
    print(f'{len(sonuc)} hisse denetlendi, {len(sorunlu)} sorunlu.')


if __name__ == '__main__':
    sys.exit(main())
