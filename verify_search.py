"""Тимчасова діагностика: чи не ховає наш запит великі угоди?

Порівнюємо СИРУ стрічку (без фільтрів, як робив старий бот) із нашим
поточним запитом. Якщо у сирій є угоди >= порогу, яких немає в нашій —
значить фільтр щось відрізає, і бот справді пропускає ставки.
"""
import json
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone

API = 'https://data-api.polymarket.com/trades'
BIG = 1_000_000          # поріг сигналу
COMPONENT = 50_000       # поточний поріг шматка
PAGES = 7                # 7 * 500 = 3500 — стеля offset


def get(params):
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'User-Agent': 'v', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def ts_of(t):
    v = float(t.get('timestamp') or 0)
    return v / 1000 if v > 1e12 else v


def usd_of(t):
    for f in ('usdcSize', 'cashAmount'):
        if t.get(f):
            return float(t[f])
    return float(t.get('size') or 0) * float(t.get('price') or 0)


def uid(t):
    return '|'.join(str(t.get(k)) for k in
                    ('transactionHash', 'asset', 'side', 'size', 'price', 'timestamp'))


def fmt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%d.%m %H:%M')


def fetch(extra, pages=PAGES, label=''):
    out, seen = [], set()
    for p in range(pages):
        params = {'limit': 500, 'offset': p * 500}
        params.update(extra)
        try:
            data = get(params)
        except Exception as e:
            print(f"   [{label}] сторінка {p}: {e}")
            break
        if not isinstance(data, list) or not data:
            break
        for t in data:
            if uid(t) not in seen:
                seen.add(uid(t)); out.append(t)
        if len(data) < 500:
            break
        time.sleep(0.25)
    return out


print("=" * 72)
print("ЧИ НЕ ХОВАЄ НАШ ЗАПИТ ВЕЛИКІ УГОДИ?")
print("=" * 72)

# 1. СИРА стрічка — без жодних фільтрів (як робив старий бот)
raw = fetch({}, label='raw')
raw_ts = [ts_of(t) for t in raw]
print(f"\n[1] СИРА стрічка (без фільтрів): {len(raw)} угод")
if raw_ts:
    print(f"    період: {fmt(min(raw_ts))} .. {fmt(max(raw_ts))} "
          f"({(max(raw_ts)-min(raw_ts))/3600:.1f} год)")
raw_big = [t for t in raw if usd_of(t) >= BIG]
by_side = defaultdict(int)
for t in raw_big:
    by_side[str(t.get('side'))] += 1
print(f"    угод >= ${BIG:,}: {len(raw_big)}  за сторонами: {dict(by_side)}")
for t in sorted(raw_big, key=usd_of, reverse=True)[:8]:
    print(f"      ${usd_of(t):>12,.0f} {str(t.get('side')):<5} {fmt(ts_of(t))} "
          f"{str(t.get('title'))[:40]}")

# 2. НАШ поточний запит
ours = fetch({'takerOnly': 'false', 'side': 'BUY', 'filterType': 'CASH',
              'filterAmount': int(COMPONENT * 0.98)}, label='ours')
ours_ts = [ts_of(t) for t in ours]
print(f"\n[2] НАШ запит (side=BUY, CASH>={int(COMPONENT*0.98):,}): {len(ours)} угод")
if ours_ts:
    print(f"    період: {fmt(min(ours_ts))} .. {fmt(max(ours_ts))} "
          f"({(max(ours_ts)-min(ours_ts))/86400:.1f} діб)")
ours_big = [t for t in ours if usd_of(t) >= BIG]
print(f"    з них >= ${BIG:,}: {len(ours_big)}")

# 3. Чи є у сирій великі КУПІВЛІ, яких немає в нашій (у спільному періоді)?
if raw_ts and ours_ts:
    lo, hi = max(min(raw_ts), min(ours_ts)), min(max(raw_ts), max(ours_ts))
    ours_ids = {uid(t) for t in ours}
    raw_buy_big = [t for t in raw
                   if usd_of(t) >= COMPONENT * 0.98
                   and str(t.get('side')).upper() == 'BUY'
                   and lo <= ts_of(t) <= hi]
    missing = [t for t in raw_buy_big if uid(t) not in ours_ids]
    print(f"\n[3] Спільний період: {fmt(lo)} .. {fmt(hi)}")
    print(f"    великих КУПІВЕЛЬ у сирій стрічці: {len(raw_buy_big)}")
    print(f"    З НИХ НЕМАЄ В НАШІЙ ВИБІРЦІ: {len(missing)}")
    for t in sorted(missing, key=usd_of, reverse=True)[:8]:
        print(f"      ${usd_of(t):>12,.0f} {fmt(ts_of(t))} {str(t.get('title'))[:40]}")

# 4. Скільки великих угод ми ігноруємо через side=BUY
raw_sell_big = [t for t in raw if usd_of(t) >= BIG and str(t.get('side')).upper() == 'SELL']
print(f"\n[4] Продажів >= ${BIG:,} у сирій стрічці: {len(raw_sell_big)} "
      f"(їх ми свідомо не рахуємо)")

# 5. Темп великих одиничних угод — щоб оцінити, чи мовчання нормальне
if ours_ts:
    span_days = (max(ours_ts) - min(ours_ts)) / 86400 or 1
    print(f"\n[5] Темп у НАШІЙ вибірці за {span_days:.1f} діб:")
    for th in (BIG, 500_000, 250_000, 100_000):
        n = sum(1 for t in ours if usd_of(t) >= th)
        print(f"      купівель >= ${th:>9,}: {n:>4}  ({n/span_days:.2f} на добу)")

# 6. Скільки ПОЗИЦІЙ (гаманець+результат, вікно 60 хв) досягли б порогу
win = 3600
pos = defaultdict(list)
for t in ours:
    pos[((t.get('proxyWallet') or '').lower(), t.get('asset'))].append(t)
hits = []
for k, v in pos.items():
    v.sort(key=ts_of)
    for i in range(len(v)):
        s = 0
        for j in range(i, len(v)):
            if ts_of(v[j]) - ts_of(v[i]) > win:
                break
            s += usd_of(v[j])
        if s >= BIG:
            hits.append((k, s, fmt(ts_of(v[i])), str(v[i].get('title'))[:40]))
            break
print(f"\n[6] Позицій (гаманець+результат) >= ${BIG:,} у вікні 60 хв: {len(hits)}")
for k, s, when, title in sorted(hits, key=lambda x: -x[1])[:10]:
    print(f"      ${s:>12,.0f}  {when}  {str(k[0])[:10]}..  {title}")
print("\n" + "=" * 72)
