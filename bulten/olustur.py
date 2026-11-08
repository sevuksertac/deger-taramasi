"""Haftalık haber bülteni: bulten/veri/TARIH.json dosyasından e-posta HTML'i üretir.

Kullanım:  python bulten/olustur.py bulten/veri/2026-11-09.json
Çıktı:     bulten/son.html  (Apps Script bunu okuyup e-postayla gönderir)
           bulten/arsiv/TARIH.html

JSON biçimi:
{
  "tarih": "2026-11-09",
  "genel": "Haftanın 2-3 cümlelik genel özeti",
  "sirketler": [
    {"ticker": "MA", "sirket": "Mastercard", "sira": 1, "puan": "84%", "endeks": "S&P 500",
     "yeni": false, "yon": "olumlu" | "olumsuz" | "notr" | "yok",
     "ozet": "2-3 cümle Türkçe özet",
     "kaynaklar": [{"baslik": "...", "url": "https://..."}]}
  ]
}
"yon": "yok" = o hafta kayda değer haber çıkmadı.
"""
import html
import json
import os
import sys

RENK = {'olumlu': ('#E2EFDA', '#375623', 'Olumlu'),
        'olumsuz': ('#FCE4D6', '#C00000', 'Olumsuz'),
        'notr': ('#EDEDED', '#404040', 'Nötr'),
        'yok': ('#FFFFFF', '#808080', 'Haber yok')}

AYLAR = ['Ocak', 'Şubat', 'Mart', 'Nisan', 'Mayıs', 'Haziran', 'Temmuz',
         'Ağustos', 'Eylül', 'Ekim', 'Kasım', 'Aralık']


def e(s):
    return html.escape(str(s if s is not None else ''))


def okunur_tarih(t):
    y, m, d = t.split('-')
    return f'{int(d)} {AYLAR[int(m) - 1]} {y}'


def kart(s):
    arka, yazi, etiket = RENK.get(s.get('yon', 'notr'), RENK['notr'])
    yeni = (' <span style="background:#FFC000;color:#000;border-radius:3px;padding:1px 5px;font-size:11px">'
            'YENİ GİRDİ</span>') if s.get('yeni') else ''
    kay = ' · '.join(f'<a href="{e(k["url"])}" style="color:#2F5597">{e(k["baslik"])}</a>'
                     for k in s.get('kaynaklar', []) if k.get('url'))
    return (
        '<tr><td style="padding:10px 12px;border-bottom:1px solid #DDD">'
        f'<div style="font-size:15px"><b>{e(s["ticker"])}</b> — {e(s["sirket"])}{yeni}'
        f' <span style="color:#777;font-size:12px">· Aday #{e(s.get("sira", ""))} · Puan {e(s.get("puan", ""))}'
        f' · {e(s.get("endeks", ""))}</span>'
        f' <span style="background:{arka};color:{yazi};border-radius:3px;padding:1px 6px;font-size:11px">{etiket}</span></div>'
        f'<div style="margin-top:5px;font-size:14px;line-height:1.45;color:#222">{e(s.get("ozet", ""))}</div>'
        + (f'<div style="margin-top:4px;font-size:12px;color:#555">Kaynak: {kay}</div>' if kay else '')
        + '</td></tr>')


def main(yol):
    with open(yol, encoding='utf-8') as f:
        b = json.load(f)
    tarih = b['tarih']
    sir = b['sirketler']
    yeniler = [s for s in sir if s.get('yeni')]
    digerleri = [s for s in sir if not s.get('yeni')]
    baslik = f'Haftalık haber bülteni — {okunur_tarih(tarih)}'
    if yeniler:
        baslik += f' ({len(yeniler)} yeni giriş)'

    h = ['<!DOCTYPE html><html><head><meta charset="utf-8">',
         f'<meta name="bulten-tarihi" content="{e(tarih)}">',
         f'<title>{e(baslik)}</title></head>',
         '<body style="margin:0;padding:0;background:#F4F4F4">',
         '<div style="max-width:720px;margin:0 auto;background:#FFF;font-family:Arial,Helvetica,sans-serif">',
         '<div style="background:#1F3864;color:#FFF;padding:16px 14px">',
         f'<div style="font-size:20px;font-weight:bold">{e(baslik)}</div>',
         '<div style="font-size:13px;opacity:.85;margin-top:3px">ABD değer taraması — aday listesinin ilk 25 hissesi'
         ' ve bu hafta listeye girenler</div></div>']
    if b.get('genel'):
        h.append(f'<div style="padding:12px 14px;font-size:14px;line-height:1.5;background:#F2F6FC">'
                 f'<b>Haftanın özeti:</b> {e(b["genel"])}</div>')
    tablo = '<table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse">'
    if yeniler:
        h.append('<div style="padding:12px 14px 4px;font-size:16px;font-weight:bold;color:#1F3864">'
                 'Bu hafta listeye yeni girenler</div>' + tablo + ''.join(kart(s) for s in yeniler) + '</table>')
    h.append('<div style="padding:12px 14px 4px;font-size:16px;font-weight:bold;color:#1F3864">'
             'İlk 25 aday</div>' + tablo + ''.join(kart(s) for s in digerleri) + '</table>')
    h.append('<div style="padding:12px 14px;font-size:11px;color:#888">Haberler Claude tarafından web aramasıyla '
             'toplanıp özetlenmiştir; önemli bir karardan önce kaynağı açıp kontrol et. Yatırım tavsiyesi değildir.'
             '</div></div></body></html>')
    cikti = ''.join(h)

    kok = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(kok, 'arsiv'), exist_ok=True)
    for p in (os.path.join(kok, 'son.html'), os.path.join(kok, 'arsiv', tarih + '.html')):
        with open(p, 'w', encoding='utf-8') as f:
            f.write(cikti)
    print(f'{baslik}: {len(sir)} şirket ({len(yeniler)} yeni) -> bulten/son.html')


if __name__ == '__main__':
    main(sys.argv[1])
