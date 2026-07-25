"""Тимчасова діагностика: чи бачить бот УСІ купівлі >= порогу.

Перевіряє на живих даних Polymarket:
  1. takerOnly=true (як зараз) vs takerOnly=false — скільки купівель ми НЕ бачимо.
  2. Чи не дублюються рядки при takerOnly=false (ризик подвійного рахунку).
  3. Чи справді filterAmount відсікає за сумою.
  4. Чи справді сортування — від нових до старих.
  5. Що саме бот просигналив би за останню годину в обох режимах.
Запускається вручну, у бойовому циклі не бере участі.
"""
import json
import urllib.request
import urllib.parse
import time
from collections import defaultdict
from datetime import datetime, timezone

API = 'https://data-api.polymarket.com/trades'
COMPONENT_MIN = 100000
FILTER_AMOUNT = int(COMPONENT_MIN * 0.98)   # 98 000 — як у бота
ALERT_MIN = 1000000
WINDOW = 3600


def get(taker_only, offset=0, limit=500):
    params = {
        'limit': limit, 'offset': offset,
        'takerOnly': 'true' if taker_only else 'false',
        'side': 'BUY', 'filterType': 'CASH', 'filterAmount': FILTER_AMOUNT,
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'User-Agent': 'verify', 'Accept': 'application/json'})
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


def fetch_all(taker_only, pages=3):
    out, seen = [], set()
    for p in range(pages):
        try:
            data = get(taker_only, offset=p * 500)
        except Exception as e:
            print(f"   помилка на сторінці {p}: {e}")
            break
        if not isinstance(data, list) or not data:
            break
        for t in data:
            if uid(t) not in seen:
                seen.add(uid(t)); out.append(t)
        if len(data) < 500:
            break
        time.sleep(0.3)
    return out


print("=" * 72)
print("ПЕРЕВІРКА ПОКРИТТЯ: чи бачить бот УСІ купівлі >= $%s" % f"{FILTER_AMOUNT:,}")
print("=" * 72)

taker = fetch_all(True)
both = fetch_all(False)
print(f"\n[1] Отримано рядків:")
print(f"    takerOnly=true  (як зараз у боті): {len(taker)}")
print(f"    takerOnly=false (maker + taker)  : {len(both)}")

t_ids = {uid(t) for t in taker}
missed = [t for t in both if uid(t) not in t_ids]
print(f"\n[2] Купівлі, яких бот ЗАРАЗ НЕ БАЧИТЬ (є лише при takerOnly=false): {len(missed)}")
if missed:
    missed.sort(key=usd_of, reverse=True)
    print("    Найбільші з пропущених:")
    for t in missed[:10]:
        print(f"      ${usd_of(t):>12,.0f}  {fmt(ts_of(t))}  {str(t.get('proxyWallet'))[:10]}..  "
              f"{str(t.get('title'))[:40]}")
    tot = sum(usd_of(t) for t in missed)
    print(f"    Сумарний обсяг пропущеного: ${tot:,.0f}")

# 3. Дублікати в режимі maker+taker (ризик подвійного рахунку)
by_tx = defaultdict(list)
for t in both:
    by_tx[(t.get('transactionHash'), (t.get('proxyWallet') or '').lower(), t.get('asset'))].append(t)
dups = {k: v for k, v in by_tx.items() if len(v) > 1}
print(f"\n[3] Ризик подвійного рахунку (той самий tx+гаманець+asset двічі): {len(dups)} груп")
for k, v in list(dups.items())[:5]:
    print(f"      tx={str(k[0])[:14]}.. рядків={len(v)} суми={[f'{usd_of(x):,.0f}' for x in v]}")

# 4. Чи працює фільтр за сумою і сортування
bad = [t for t in both if usd_of(t) < FILTER_AMOUNT * 0.999]
print(f"\n[4] Рядків нижче порогу фільтра: {len(bad)} (має бути 0)")
tss = [ts_of(t) for t in both]
desc = all(tss[i] >= tss[i + 1] for i in range(len(tss) - 1))
print(f"    Сортування від нових до старих: {'ТАК' if desc else 'НІ — ПРОБЛЕМА'}")
if tss:
    print(f"    Діапазон: {fmt(min(tss))} .. {fmt(max(tss))}")

# 5. Що бот просигналив би за останню годину в обох режимах
now = time.time()


def aggregate(trades, label):
    pos = defaultdict(list)
    for t in trades:
        if now - ts_of(t) <= WINDOW:
            pos[((t.get('proxyWallet') or '').lower(), t.get('asset'))].append(t)
    hits = [(k, sum(usd_of(x) for x in v), v) for k, v in pos.items()]
    hits = [h for h in hits if h[1] >= ALERT_MIN]
    hits.sort(key=lambda x: x[1], reverse=True)
    print(f"\n    [{label}] позицій у вікні 60 хв: {len(pos)} · "
          f"з них >= ${ALERT_MIN:,}: {len(hits)}")
    for k, total, v in hits[:5]:
        print(f"       ${total:>12,.0f}  {len(v)} угод  {str(k[0])[:10]}..  "
              f"{str(v[0].get('title'))[:40]}")
    return {k for k, _, _ in hits}


print(f"\n[5] Що бот просигналив би за останні 60 хв (поріг ${ALERT_MIN:,}):")
a = aggregate(taker, "takerOnly=true  — ПОТОЧНА поведінка")
b = aggregate(both, "takerOnly=false — ПОВНЕ покриття")
only_b = b - a
if only_b:
    print(f"\n    !!! {len(only_b)} позицій, які поточний бот ПРОПУСТИВ БИ")
print("\n" + "=" * 72)
