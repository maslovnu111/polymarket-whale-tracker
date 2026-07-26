"""Тимчасова перевірка: чи справді /trades підтримує параметр `start`
(нижня межа часу)? Якщо так — фільтрація на сервері краща за нашу.
Запускається вручну, стан не чіпає."""
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

API = 'https://data-api.polymarket.com/trades'
FILTER_AMOUNT = 9800


def get(params):
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'User-Agent': 'verify', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def ts_of(t):
    v = float(t.get('timestamp') or 0)
    return v / 1000 if v > 1e12 else v


def fmt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%d.%m %H:%M:%S')


base = {'limit': 500, 'offset': 0, 'takerOnly': 'false', 'side': 'BUY',
        'filterType': 'CASH', 'filterAmount': FILTER_AMOUNT}
now = int(time.time())
since = now - 600           # 10 хвилин тому

print("=" * 70)
print("ЧИ ПРАЦЮЄ ПАРАМЕТР `start` НА /trades?")
print("=" * 70)

for label, extra in [("БЕЗ start", {}),
                     ("start=10 хв тому", {'start': since}),
                     ("startTs=10 хв тому", {'startTs': since}),
                     ("from=10 хв тому", {'from': since})]:
    p = dict(base); p.update(extra)
    try:
        data = get(p)
    except Exception as e:
        print(f"\n[{label}] помилка: {e}")
        continue
    if not isinstance(data, list) or not data:
        print(f"\n[{label}] порожня/неочікувана відповідь: {str(data)[:120]}")
        continue
    tss = [ts_of(t) for t in data]
    older = sum(1 for t in tss if t < since)
    print(f"\n[{label}]")
    print(f"   рядків: {len(data)}")
    print(f"   діапазон: {fmt(min(tss))} .. {fmt(max(tss))}")
    print(f"   старіших за межу (10 хв тому): {older} з {len(data)}")
    if extra:
        verdict = ("ПРАЦЮЄ — сервер відсік старі" if older == 0
                   else "ІГНОРУЄТЬСЯ — повернув і старі теж")
        print(f"   ВИСНОВОК: {verdict}")

print("\n" + "=" * 70)
