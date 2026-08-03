"""Тимчасова наскрізна діагностика бота.

Перевіряє КОЖЕН крок окремо на живих даних Polymarket:
  [1] чи серверний фільтр нічого не ховає (звірка з сирою стрічкою)
  [2] чи takerOnly=false реально додає мейкерські (лімітні) виконання
  [3] чи коректні суми (usdcSize vs size*price) і поля (asset/side/outcome)
  [4] чи немає задвоєння рядків на одну транзакцію
  [5] як далеко назад узагалі можна прогортати стрічку
  [6] РЕПЛЕЙ справжнього алгоритму бота по історії — скільки сповіщень
      він МАВ БИ надіслати щодня (головна перевірка)
  [7] чутливість до вікна, порога, способу групування, сторони угоди
"""
import json
import time
import urllib.request
import urllib.parse
from collections import defaultdict, Counter
from datetime import datetime, timezone

API = 'https://data-api.polymarket.com/trades'

MIN_AMOUNT = 1_000_000       # поріг сигналу (як у бота)
COMPONENT_MIN = 50_000       # поріг шматка (як у бота)
FILTER_AMOUNT = int(COMPONENT_MIN * 0.98)
AGG_WINDOW = 3600            # вікно агрегації, сек
ESCALATE = 2.0
TICK = 300                   # крок запуску бота, сек
PAGES = 8


def get(params, timeout=30):
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        'User-Agent': 'whale-verify', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ts_of(t):
    v = float(t.get('timestamp') or 0)
    return v / 1000 if v > 1e12 else v


def usd_of(t):
    for f in ('usdcSize', 'cashAmount', 'cash_amount'):
        if t.get(f):
            return float(t[f])
    return float(t.get('size') or 0) * float(t.get('price') or 0)


def uid(t):
    tx = t.get('transactionHash') or f"notx:{t.get('proxyWallet')}:{t.get('conditionId')}"
    return '|'.join(str(x) for x in (
        tx, t.get('asset') or t.get('outcomeIndex'), t.get('side'),
        t.get('size'), t.get('price'), t.get('timestamp')))


def day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%d.%m')


def fmt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%d.%m %H:%M')


def fetch(extra, pages=PAGES, label=''):
    """Гортає стрічку сторінками по 500, дедуплікує за uid."""
    out, seen = [], set()
    for p in range(pages):
        params = {'limit': 500, 'offset': p * 500}
        params.update(extra)
        try:
            data = get(params)
        except Exception as e:
            print(f"    [{label}] сторінка offset={p * 500}: {e}")
            break
        if not isinstance(data, list) or not data:
            break
        for t in data:
            u = uid(t)
            if u not in seen:
                seen.add(u)
                out.append(t)
        if len(data) < 500:
            break
        time.sleep(0.2)
    return out


NOW = time.time()
BASE = {'takerOnly': 'false', 'filterType': 'CASH', 'filterAmount': FILTER_AMOUNT}

print("=" * 78)
print(f"НАСКРІЗНА ПЕРЕВІРКА · {fmt(NOW)} UTC · поріг ${MIN_AMOUNT:,} · "
      f"шматок ${COMPONENT_MIN:,} · вікно {AGG_WINDOW // 60} хв")
print("=" * 78)

buys = fetch(dict(BASE, side='BUY'), label='BUY')
sells = fetch(dict(BASE, side='SELL'), label='SELL')
takers = fetch(dict(BASE, side='BUY', takerOnly='true'), label='BUY/taker')

for name, rows in (('BUY  (takerOnly=false)', buys),
                   ('SELL (takerOnly=false)', sells),
                   ('BUY  (takerOnly=true) ', takers)):
    if not rows:
        print(f"\n[0] {name}: ПОРОЖНЬО — запит не вдався!")
        continue
    tss = [ts_of(t) for t in rows]
    span = (max(tss) - min(tss)) / 86400
    print(f"\n[0] {name}: {len(rows):>5} рядків · {fmt(min(tss))}..{fmt(max(tss))} "
          f"· {span:.1f} діб · {len(rows) / max(span, .01):.0f}/добу")

# --------------------------------------------------------------- [1] фільтр
print("\n" + "-" * 78)
print("[1] ЧИ НЕ ХОВАЄ СЕРВЕРНИЙ ФІЛЬТР ЩОСЬ (звірка з сирою стрічкою)")
raw = fetch({'takerOnly': 'false'}, pages=6, label='raw')
if raw:
    r_ts = [ts_of(t) for t in raw]
    lo, hi = min(r_ts), max(r_ts)
    print(f"    сира стрічка: {len(raw)} рядків · {fmt(lo)}..{fmt(hi)} "
          f"· {(hi - lo) / 60:.1f} хв")
    # усі великі купівлі з сирої стрічки мають бути у нашій вибірці
    raw_big = [t for t in raw
               if str(t.get('side', '')).upper() == 'BUY' and usd_of(t) >= COMPONENT_MIN]
    ours = {uid(t) for t in buys}
    missed = [t for t in raw_big if uid(t) not in ours]
    print(f"    у сирій стрічці купівель >= ${COMPONENT_MIN:,}: {len(raw_big)}")
    print(f"    з них НЕМАЄ у нашій вибірці: {len(missed)}"
          + ("  <-- ПРОБЛЕМА" if missed else "  OK"))
    for t in missed[:5]:
        print(f"        {fmt(ts_of(t))} ${usd_of(t):,.0f} {str(t.get('title'))[:40]}")
    # і навпаки: чи не пролазить дрібнота (фільтр не за грошима?)
    small = [t for t in buys if usd_of(t) < FILTER_AMOUNT * 0.95]
    print(f"    у нашій вибірці рядків ДЕШЕВШЕ за ${FILTER_AMOUNT:,}: {len(small)}"
          + ("  <-- фільтр не грошовий!" if len(small) > len(buys) * 0.02 else "  OK"))
    if small:
        print(f"        напр. ${usd_of(small[0]):,.0f} "
              f"(size={small[0].get('size')} price={small[0].get('price')})")

# ------------------------------------------------------------ [2] takerOnly
print("\n" + "-" * 78)
print("[2] ЧИ takerOnly=false РЕАЛЬНО ДОДАЄ ЛІМІТНІ (мейкерські) ВИКОНАННЯ")
if buys and takers:
    t_lo, t_hi = min(ts_of(t) for t in takers), max(ts_of(t) for t in takers)
    b_lo, b_hi = min(ts_of(t) for t in buys), max(ts_of(t) for t in buys)
    lo, hi = max(t_lo, b_lo), min(t_hi, b_hi)          # спільний період
    B = {uid(t): t for t in buys if lo <= ts_of(t) <= hi}
    T = {uid(t): t for t in takers if lo <= ts_of(t) <= hi}
    only_b, only_t = set(B) - set(T), set(T) - set(B)
    print(f"    спільний період {fmt(lo)}..{fmt(hi)} ({(hi - lo) / 86400:.1f} діб)")
    print(f"    false: {len(B)} · true: {len(T)} · лише у false (лімітки): {len(only_b)} "
          f"· лише у true: {len(only_t)}")
    lim_vol = sum(usd_of(B[u]) for u in only_b)
    print(f"    обсяг, який видно ЛИШЕ через takerOnly=false: ${lim_vol:,.0f}")
    big_lim = sorted((usd_of(B[u]) for u in only_b), reverse=True)[:3]
    print(f"    найбільші з них: {[f'${x:,.0f}' for x in big_lim]}")
    if not only_b:
        print("    <-- takerOnly НЕ ВПЛИВАЄ: усі рядки однакові. Лімітки НЕ видно!")

# ---------------------------------------------------------------- [3] поля
print("\n" + "-" * 78)
print("[3] ЯКІСТЬ ПОЛІВ")
allrows = buys + sells
print(f"    side у відповіді: {dict(Counter(str(t.get('side')) for t in allrows))}")
no_asset = [t for t in allrows if not t.get('asset')]
print(f"    рядків без asset: {len(no_asset)}" + ("  <-- позиції зіллються!" if no_asset else "  OK"))
no_tx = [t for t in allrows if not t.get('transactionHash')]
print(f"    рядків без transactionHash: {len(no_tx)}")
no_slug = [t for t in allrows if not (t.get('eventSlug') or t.get('slug'))]
print(f"    рядків без eventSlug: {len(no_slug)}")
print(f"    outcomeIndex -> outcome: "
      f"{dict(Counter((str(t.get('outcomeIndex')), str(t.get('outcome'))) for t in allrows).most_common(6))}")
diff = []
for t in allrows:
    a = usd_of(t)
    b = float(t.get('size') or 0) * float(t.get('price') or 0)
    if b > 0 and abs(a - b) / b > 0.01:
        diff.append((a, b, t))
print(f"    usdcSize != size*price (>1%): {len(diff)}")
for a, b, t in diff[:3]:
    print(f"        usd={a:,.2f} vs size*price={b:,.2f} ({str(t.get('title'))[:32]})")
has_usdc = sum(1 for t in allrows if t.get('usdcSize'))
print(f"    рядків з полем usdcSize: {has_usdc}/{len(allrows)}")

# ----------------------------------------------------------- [4] задвоєння
print("\n" + "-" * 78)
print("[4] ЗАДВОЄННЯ (кілька рядків BUY на один tx+asset+гаманець)")
grp = defaultdict(list)
for t in buys:
    grp[(t.get('transactionHash'), t.get('asset'),
         (t.get('proxyWallet') or '').lower())].append(t)
dups = {k: v for k, v in grp.items() if len(v) > 1}
print(f"    груп із >1 рядком: {len(dups)} із {len(grp)}")
for k, v in list(dups.items())[:3]:
    print(f"        tx {str(k[0])[:14]}.. -> {len(v)} рядків, "
          f"суми {[f'${usd_of(x):,.0f}' for x in v]}")
print("    (кілька рядків на tx = часткові виконання одного ордера — їх треба сумувати)")

# -------------------------------------------------------------- [5] глибина
print("\n" + "-" * 78)
print("[5] ГЛИБИНА ГОРТАННЯ (де стрічка обривається)")
for off in (0, 1000, 2000, 3000, 3500, 4000, 5000):
    try:
        d = get(dict(BASE, side='BUY', limit=500, offset=off))
        n = len(d) if isinstance(d, list) else -1
        old = fmt(min(ts_of(t) for t in d)) if n > 0 else '-'
        print(f"    offset={off:>5}: {n:>3} рядків, найстаріший {old}")
        if n == 0:
            break
    except Exception as e:
        print(f"    offset={off:>5}: {str(e)[:60]}")
        break
    time.sleep(0.2)


# ------------------------------------------------------- [6] реплей бота
def replay(rows, key_fn, threshold=MIN_AMOUNT, window=AGG_WINDOW, tick=TICK):
    """Точна копія логіки tracker.py, прокручена по історії.

    Повертає список (ts, сума, ключ, назва, к-сть угод) — момент,
    коли бот надіслав би сповіщення.
    """
    rows = sorted(rows, key=ts_of)
    if not rows:
        return []
    start, end = ts_of(rows[0]), ts_of(rows[-1])
    positions = {}
    alerts = []
    i = 0
    prev = start
    t_end = start
    while t_end <= end:
        t_end += tick
        cutoff = t_end - window
        # 1) додаємо нові угоди
        while i < len(rows) and ts_of(rows[i]) <= t_end:
            t = rows[i]
            i += 1
            if ts_of(t) <= prev:
                continue
            k = key_fn(t)
            p = positions.setdefault(k, {'trades': [], 'alerted_usd': 0.0,
                                         'title': str(t.get('title'))[:40]})
            p['trades'].append((ts_of(t), usd_of(t)))
        prev = t_end
        # 2) прибираємо угоди поза вікном
        for k in list(positions):
            p = positions[k]
            p['trades'] = [x for x in p['trades'] if x[0] > cutoff]
            if not p['trades']:
                del positions[k]
        # 3) сигнал
        for k, p in positions.items():
            total = sum(x[1] for x in p['trades'])
            a = p['alerted_usd']
            if total >= threshold and (a <= 0 or total >= a * ESCALATE):
                p['alerted_usd'] = total
                alerts.append((t_end, total, k, p['title'], len(p['trades'])))
    return alerts


def by_asset(t):
    return ((t.get('proxyWallet') or '').lower(),
            t.get('asset') or f"{t.get('conditionId')}:{t.get('outcomeIndex')}")


def by_condition(t):
    return ((t.get('proxyWallet') or '').lower(), t.get('conditionId'))


def by_event(t):
    return ((t.get('proxyWallet') or '').lower(),
            t.get('eventSlug') or t.get('slug') or t.get('conditionId'))


print("\n" + "-" * 78)
print("[6] РЕПЛЕЙ АЛГОРИТМУ БОТА ПО РЕАЛЬНІЙ ІСТОРІЇ  <<< ГОЛОВНЕ")
alerts = replay(buys, by_asset)
span_days = (max(ts_of(t) for t in buys) - min(ts_of(t) for t in buys)) / 86400 if buys else 1
print(f"    сповіщень за {span_days:.1f} діб: {len(alerts)} "
      f"({len(alerts) / max(span_days, .01):.2f}/добу)")
hist = defaultdict(int)
for a in alerts:
    hist[day(a[0])] += 1
lo_ts = min(ts_of(t) for t in buys) if buys else NOW
d = lo_ts
seen_days = []
while d <= NOW:
    seen_days.append(day(d))
    d += 86400
print("    по днях (порожній рядок = того дня сигналів не було):")
for dd in seen_days:
    n = hist.get(dd, 0)
    print(f"      {dd}: {'█' * n}{'' if n else '·'} {n}")

for horizon in (3, 5, 7):
    n = len([a for a in alerts if NOW - a[0] <= horizon * 86400])
    print(f"    за останні {horizon} діб: {n}")

print("    останні 12 сигналів, які бот МАВ БИ надіслати:")
for ts, total, k, title, n in alerts[-12:]:
    print(f"      {fmt(ts)}  ${total:>12,.0f}  {n:>2} угод  {k[0][:10]}..  {title}")

# ------------------------------------------------------- [7] чутливість
print("\n" + "-" * 78)
print("[7] ЧУТЛИВІСТЬ (скільки сповіщень за останні 7 діб дали б інші налаштування)")


def recent(a_list, days=7):
    return len([a for a in a_list if NOW - a[0] <= days * 86400])


print("    а) вікно агрегації:")
for w in (60, 180, 360, 720, 1440):
    print(f"        {w:>4} хв: {recent(replay(buys, by_asset, window=w * 60))}")
print("    б) поріг сигналу:")
for th in (1_000_000, 750_000, 500_000, 300_000, 200_000):
    print(f"        ${th:>9,}: {recent(replay(buys, by_asset, threshold=th))}")
print("    в) спосіб групування (вікно 60 хв, поріг $1M):")
for nm, fn in (('гаманець+asset (зараз)', by_asset),
               ('гаманець+ринок', by_condition),
               ('гаманець+подія', by_event)):
    print(f"        {nm:<24}: {recent(replay(buys, fn))}")
print("    г) сторона угоди:")
print(f"        лише BUY (зараз)  : {recent(replay(buys, by_asset))}")
print(f"        лише SELL         : {recent(replay(sells, by_asset))}")
print(f"        BUY+SELL окремо   : "
      f"{recent(replay(buys, by_asset)) + recent(replay(sells, by_asset))}")
old_single = [t for t in buys + sells if usd_of(t) >= MIN_AMOUNT]
print(f"    д) СТАРИЙ алгоритм (одна угода >= ${MIN_AMOUNT:,}, будь-яка сторона): "
      f"{len(old_single)} усього, {len([t for t in old_single if NOW - ts_of(t) <= 7 * 86400])} за 7 діб")
for t in sorted(old_single, key=ts_of, reverse=True)[:8]:
    print(f"        {fmt(ts_of(t))} {t.get('side'):<4} ${usd_of(t):>12,.0f}  {str(t.get('title'))[:38]}")

print("\n" + "=" * 78)
