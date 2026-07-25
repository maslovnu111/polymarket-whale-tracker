"""Тимчасова діагностика повноти покриття (живі дані Polymarket).

Перевіряє:
  1. Скільки купівель ховає takerOnly=true — у СПІЛЬНОМУ часовому вікні
     (щоб порівняння було чесним, а не артефактом різної глибини вибірки).
  2. Чи не створює takerOnly=false подвійного рахунку: чи немає в межах одного
     tx+гаманець+asset «агрегованого» рядка, що дорівнює сумі інших.
  3. Чи справді filterAmount відсікає за сумою і чи сортування — від нових.
Запускається вручну, у бойовому циклі не бере участі.
"""
import json
import urllib.request
import urllib.parse
import time
from collections import defaultdict
from datetime import datetime, timezone

API = 'https://data-api.polymarket.com/trades'
FILTER_AMOUNT = 98000
PAGES = 4


def get(taker_only, offset, limit=500):
    params = {
        'limit': limit, 'offset': offset,
        'takerOnly': 'true' if taker_only else 'false',
        'side': 'BUY', 'filterType': 'CASH', 'filterAmount': FILTER_AMOUNT,
    }
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}",
                                 headers={'User-Agent': 'verify', 'Accept': 'application/json'})
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


def fetch(taker_only):
    out, seen = [], set()
    for p in range(PAGES):
        try:
            data = get(taker_only, p * 500)
        except Exception as e:
            print(f"   помилка сторінки {p}: {e}")
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


print("=" * 74)
print(f"ПОВНОТА ПОКРИТТЯ: купівлі >= ${FILTER_AMOUNT:,}")
print("=" * 74)

taker = fetch(True)
both = fetch(False)
print(f"\nОтримано: takerOnly=true -> {len(taker)} рядків · takerOnly=false -> {len(both)} рядків")

# --- Чесне порівняння: лише спільне часове вікно ---
if taker and both:
    lo = max(min(ts_of(t) for t in taker), min(ts_of(t) for t in both))
    hi = min(max(ts_of(t) for t in taker), max(ts_of(t) for t in both))
    tk = [t for t in taker if lo <= ts_of(t) <= hi]
    bt = [t for t in both if lo <= ts_of(t) <= hi]
    print(f"Спільне вікно: {fmt(lo)} .. {fmt(hi)}")
    print(f"  у цьому вікні: takerOnly=true -> {len(tk)} · takerOnly=false -> {len(bt)}")

    ids = {uid(t) for t in tk}
    missed = [t for t in bt if uid(t) not in ids]
    print(f"\n[1] ПРОПУЩЕНО поточним ботом у спільному вікні: {len(missed)} купівель")
    if missed:
        missed.sort(key=usd_of, reverse=True)
        big = [t for t in missed if usd_of(t) >= 1000000]
        print(f"    з них одиничних >= $1,000,000: {len(big)}")
        for t in missed[:8]:
            print(f"      ${usd_of(t):>12,.0f}  {fmt(ts_of(t))}  {str(t.get('proxyWallet'))[:10]}..  "
                  f"{str(t.get('title'))[:38]}")
        print(f"    сумарний пропущений обсяг: ${sum(usd_of(t) for t in missed):,.0f}")

    # Зворотна перевірка: чи є щось у taker, чого немає в both
    bids = {uid(t) for t in bt}
    only_taker = [t for t in tk if uid(t) not in bids]
    print(f"\n[1b] Є лише при takerOnly=true (не має бути багато): {len(only_taker)}")

# --- Чи не буде подвійного рахунку при takerOnly=false ---
groups = defaultdict(list)
for t in both:
    groups[(t.get('transactionHash'), (t.get('proxyWallet') or '').lower(), t.get('asset'))].append(t)
multi = {k: v for k, v in groups.items() if len(v) > 1}
aggregate_dupes = []
for k, v in multi.items():
    sums = [usd_of(x) for x in v]
    for i, s in enumerate(sums):
        rest = sum(sums[:i] + sums[i + 1:])
        if rest > 0 and abs(s - rest) / max(s, rest) < 0.01:
            aggregate_dupes.append((k, sums))
            break
print(f"\n[2] ПОДВІЙНИЙ РАХУНОК при takerOnly=false:")
print(f"    груп з кількома рядками на один tx: {len(multi)}")
print(f"    з них підозра «агрегат + його ж частини»: {len(aggregate_dupes)}")
taker_uids = {uid(t) for t in taker}
TRANSFER_SINGLE = '0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62'
TRANSFER_BATCH = '0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb'


def onchain_received(tx_hash, wallet, asset_id):
    """Скільки токенів asset_id реально отримав wallet у цій транзакції (ERC-1155)."""
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getTransactionReceipt',
                       'params': [tx_hash]}).encode()
    req = urllib.request.Request('https://polygon-rpc.com', data=body,
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'verify'})
    with urllib.request.urlopen(req, timeout=30) as r:
        rc = json.loads(r.read().decode()).get('result') or {}
    w = wallet.lower().replace('0x', '').rjust(64, '0')
    total = 0
    for log in rc.get('logs', []):
        topics = log.get('topics', [])
        if not topics:
            continue
        data = log.get('data', '0x')[2:]
        if topics[0].lower() == TRANSFER_SINGLE and len(topics) >= 4:
            if topics[3][2:].lower() != w:
                continue
            tid, val = int(data[0:64], 16), int(data[64:128], 16)
            if str(tid) == str(asset_id):
                total += val
        elif topics[0].lower() == TRANSFER_BATCH and len(topics) >= 4:
            if topics[3][2:].lower() != w:
                continue
            # data: offset ids, offset vals, len ids, ids..., len vals, vals...
            words = [data[i:i + 64] for i in range(0, len(data), 64)]
            try:
                n = int(words[2], 16)
                ids = [int(words[3 + i], 16) for i in range(n)]
                vals = [int(words[4 + n + i], 16) for i in range(n)]
                for tid, val in zip(ids, vals):
                    if str(tid) == str(asset_id):
                        total += val
            except Exception:
                pass
    return total


if aggregate_dupes:
    for k, s in aggregate_dupes[:3]:
        tx, wallet, asset = k
        print(f"\n      РОЗБІР tx={tx}")
        print(f"      гаманець={wallet}")
        for x in multi[k]:
            print(f"        ${usd_of(x):>11,.0f} size={x.get('size')} price={x.get('price')} "
                  f"ts={int(ts_of(x))} у_taker_вибірці={'ТАК' if uid(x) in taker_uids else 'НІ'}")
        rows_sum = sum(float(x.get('size') or 0) for x in multi[k])
        largest = max(float(x.get('size') or 0) for x in multi[k])
        try:
            got = onchain_received(tx, wallet, asset)
            print(f"\n        ОНЧЕЙН отримано токенів: {got:,}")
            print(f"        сума всіх рядків:        {rows_sum:,.0f}")
            print(f"        найбільший рядок:        {largest:,.0f}")
            if abs(got - rows_sum) <= max(1, rows_sum * 0.001):
                print("        ВИСНОВОК: рядки НЕ дублюються — сумувати ПРАВИЛЬНО")
            elif abs(got - largest) <= max(1, largest * 0.001):
                print("        ВИСНОВОК: це АГРЕГАТ + його частини — буде подвійний рахунок!")
            else:
                print("        ВИСНОВОК: не збігається ні з чим — потрібен ручний розбір")
        except Exception as e:
            print(f"        ончейн-перевірка не вдалася: {e}")
else:
    print("    -> дублів немає: це часткові виконання, їх ТРЕБА сумувати")
    for k, v in list(multi.items())[:3]:
        print(f"      приклад tx={str(k[0])[:16]}.. частини="
              f"{[f'{usd_of(x):,.0f}' for x in v]} разом=${sum(usd_of(x) for x in v):,.0f}")

# --- Санітарні перевірки ---
low = [t for t in both if usd_of(t) < FILTER_AMOUNT * 0.999]
tss = [ts_of(t) for t in both]
desc = all(tss[i] >= tss[i + 1] for i in range(len(tss) - 1))
print(f"\n[3] Рядків нижче порогу: {len(low)} (має бути 0) · "
      f"сортування від нових: {'ТАК' if desc else 'НІ'}")
print("=" * 74)
