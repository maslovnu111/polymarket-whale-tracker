import requests
import os
import json
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Логіка: ловимо не лише одну велику угоду, а НАКОПИЧЕННЯ позиції китом.
# Трейдер часто заходить на $1M не разово (немає ліквідності / лімітка), а
# кількома угодами по ~$200k протягом ~години. Тому:
#   1. Просимо в сервера лише угоди-кандидати (BUY, сума >= COMPONENT_MIN, напр.
#      $100k) — їх небагато, вони завжди влазять у ліміт API.
#   2. Групуємо їх по (гаманець + конкретний результат ринку = asset) і сумуємо
#      за ковзне вікно AGG_WINDOW (60 хв).
#   3. Коли сума позиції одного трейдера >= MIN_AMOUNT ($1M) — шлемо сповіщення.
# Одна велика угода теж спрацьовує — це просто позиція з однієї угоди.
#
# Чому це надійно навіть у пік: за один запуск добираємо лише НОВІ угоди з
# моменту минулого запуску (~5 хв), а 60-хв суму тримаємо у стані між запусками.
# Щоб пробити ліміт ~3500 угод/запит, потрібно >3500 угод >= COMPONENT_MIN за
# 5 хв — обсяг, якого фізично не існує.
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

STATE_FILE = 'last_check.json'
DATA_API = 'https://data-api.polymarket.com'
POLY_EVENT = 'https://polymarket.com/event'
POLY_PROFILE = 'https://polymarket.com/profile'
POLYGONSCAN_TX = 'https://polygonscan.com/tx'


def _env_num(name, default, cast=float):
    """Читає число з env; порожнє/відсутнє/некоректне значення -> default.
    (Робить безпечним підключення порожніх repo variables у workflow.)"""
    v = os.environ.get(name)
    if v is None or str(v).strip() == '':
        return cast(default)
    try:
        return cast(v)
    except Exception:
        return cast(default)


# Поріг СИГНАЛУ — сумарна позиція трейдера, від якої шлемо сповіщення.
MIN_AMOUNT = _env_num('MIN_AMOUNT', 1000, float)
# Поріг ШМАТКА — мінімальна окрема угода, яку рахуємо як частину позиції.
# Не може бути більшим за MIN_AMOUNT (інакше пропустили б одиничну велику угоду).
COMPONENT_MIN = min(_env_num('COMPONENT_MIN', 100000, float), MIN_AMOUNT)
# Вікно агрегації: за який період підсумовуємо угоди одного трейдера (сек).
AGG_WINDOW_SECONDS = int(_env_num('AGG_WINDOW_MINUTES', 60, float) * 60)

# Серверний поріг трохи нижчий за COMPONENT_MIN — щоб не втратити пограничні
# шматки через округлення.
FILTER_MARGIN = _env_num('FILTER_MARGIN', 0.98, float)
FILTER_AMOUNT = max(0, int(COMPONENT_MIN * FILTER_MARGIN))

# Максимальна глибина наздоганяння пропущених запусків (год -> сек).
MAX_BACKFILL_SECONDS = int(_env_num('MAX_BACKFILL_HOURS', 24, float) * 3600)

PAGE_LIMIT = 500
# Запобіжник від нескінченного циклу (сторінок на запуск). Оскільки за раз
# добираємо лише ~5 хв угод-кандидатів, реально сторінка майже завжди 1.
MAX_PAGES = int(_env_num('MAX_PAGES', 200, int))
# Скільки детальних сповіщень максимум за один запуск (решта — у зведенні).
MAX_MESSAGES = int(_env_num('MAX_MESSAGES', 30, int))
REQUEST_RETRIES = 3
REQUEST_TIMEOUT = 20


# ------------------------------- стан --------------------------------------

def load_state():
    """Повертає {'last_timestamp': since, 'positions': {...}} з наздоганянням
    пропусків і «прогрівом» вікна, якщо позицій ще немає."""
    now = int(time.time())
    ts = 0
    positions = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            ts = int(data.get('last_timestamp', 0))
            positions = data.get('positions', {}) or {}
        except Exception as e:
            print(f"Не вдалося прочитати стан ({e}) — починаємо з чистого")
            ts, positions = 0, {}

    floor = now - MAX_BACKFILL_SECONDS
    if ts <= 0:
        since = now - AGG_WINDOW_SECONDS
        print(f"Стану немає — прогріваємо вікно на {AGG_WINDOW_SECONDS // 60} хв назад")
    elif ts < floor:
        gap_h = (now - ts) / 3600
        print(f"Простій ~{gap_h:.1f} год перевищує ліміт наздоганяння "
              f"({MAX_BACKFILL_SECONDS // 3600} год) — беремо лише останні "
              f"{MAX_BACKFILL_SECONDS // 3600} год")
        since = floor
    else:
        since = ts
        gap_min = (now - ts) / 60
        if gap_min > 6:
            print(f"Наздоганяємо пропуск ~{gap_min:.0f} хв з моменту останнього запуску")

    # Якщо позицій у стані немає (перший запуск / втрата стану / міграція) —
    # добираємо одразу цілим вікном, щоб агрегація була коректною з першого разу.
    if not positions:
        since = min(since, now - AGG_WINDOW_SECONDS)
        since = max(since, floor)

    return {'last_timestamp': since, 'positions': positions}


def save_state(timestamp, positions):
    with open(STATE_FILE, 'w') as f:
        json.dump({'last_timestamp': int(timestamp), 'positions': positions}, f)


# ------------------------------ утиліти ------------------------------------

def trade_ts(trade):
    """Час угоди у секундах (Unix). Нормалізуємо мілісекунди -> секунди."""
    try:
        ts = float(trade.get('timestamp') or 0)
        if ts > 1e12:
            ts /= 1000
        return ts
    except Exception:
        return 0.0


def trade_uid(trade):
    """Стабільний унікальний ідентифікатор угоди для дедуплікації."""
    parts = (
        trade.get('transactionHash'),
        trade.get('asset') or trade.get('outcomeIndex'),
        str(trade.get('side')),
        str(trade.get('size')),
        str(trade.get('price')),
        str(trade.get('timestamp')),
    )
    return '|'.join(str(p) for p in parts)


def calc_usd(trade):
    for field in ['usdcSize', 'cashAmount', 'cash_amount']:
        val = trade.get(field)
        if val:
            try:
                return float(val)
            except Exception:
                pass
    size = float(trade.get('size') or 0)
    price = float(trade.get('price') or 0)
    return size * price


def outcome_of(trade):
    o = trade.get('outcome')
    if o:
        return str(o)
    idx = str(trade.get('outcomeIndex', ''))
    if idx == '1':
        return 'Yes'
    if idx == '0':
        return 'No'
    return '?'


def position_key(trade):
    """Ключ позиції: гаманець + конкретний результат (asset).
    Так угоди одного трейдера на одну й ту саму ставку сумуються разом."""
    wallet = (trade.get('proxyWallet') or '').lower()
    asset = trade.get('asset')
    if not asset:
        asset = f"{trade.get('conditionId')}:{trade.get('outcomeIndex')}"
    return f"{wallet}|{asset}"


def format_time(ts):
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime('%d.%m %H:%M UTC')
    except Exception:
        return '?'


def short_wallet(wallet):
    return (wallet[:6] + '...' + wallet[-4:]) if len(wallet or '') > 10 else (wallet or '')


# ------------------------------- API ---------------------------------------

def _request_trades(offset):
    """Одна сторінка угод-кандидатів (BUY, CASH >= FILTER_AMOUNT) з ретраями."""
    params = {
        'limit': PAGE_LIMIT,
        'offset': offset,
        'takerOnly': 'true',            # одна угода = один рядок
        'side': 'BUY',                  # лише купівлі (набір позиції)
        'filterType': 'CASH',           # фільтр за грошовим обсягом (USDC)...
        'filterAmount': FILTER_AMOUNT,  # ...шматки >= цього порогу
    }
    last_err = None
    for attempt in range(REQUEST_RETRIES):
        try:
            r = requests.get(f"{DATA_API}/trades", params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return []
            return data
        except Exception as e:
            last_err = e
            if attempt < REQUEST_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"offset={offset}: не вдалося отримати угоди: {last_err}")


def get_trades_since(since_timestamp):
    """Тягне угоди-кандидати, новіші за since_timestamp (від нових до старих,
    із зупинкою на межі часу). Повертає (trades, complete)."""
    all_trades = []
    seen = set()
    offset = 0
    pages = 0
    reached_boundary = False
    complete = True

    while pages < MAX_PAGES:
        try:
            data = _request_trades(offset)
        except Exception as e:
            if offset == 0:
                raise  # повний провал — викликач не рухає стан
            print(f"⚠️ {e}. Зупиняємось, покриття неповне.")
            complete = False
            break

        if not data:
            reached_boundary = True
            break

        page_min_ts = None
        new_count = 0
        for t in data:
            ts = trade_ts(t)
            if page_min_ts is None or ts < page_min_ts:
                page_min_ts = ts
            uid = trade_uid(t)
            if uid in seen:
                continue
            seen.add(uid)
            all_trades.append(t)
            new_count += 1

        print(f"offset={offset}: {len(data)} угод-кандидатів (нових: {new_count}, всього: {len(all_trades)})")

        if page_min_ts is not None and page_min_ts <= since_timestamp:
            reached_boundary = True
            break
        if len(data) < PAGE_LIMIT:
            reached_boundary = True
            break

        offset += PAGE_LIMIT
        pages += 1
        time.sleep(0.3)

    if pages >= MAX_PAGES and not reached_boundary:
        complete = False
        print(f"⚠️ Досягнуто ліміт сторінок ({MAX_PAGES}) — вікно надто велике, "
              f"частину найстаріших угод могло бути пропущено")

    return all_trades, complete


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(REQUEST_RETRIES):
        try:
            r = requests.post(url, json={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }, timeout=10)
            if r.status_code == 429:
                retry_after = 1
                try:
                    retry_after = int(r.json().get('parameters', {}).get('retry_after', 1))
                except Exception:
                    pass
                print(f"Telegram 429 — чекаємо {retry_after}с")
                time.sleep(retry_after + 1)
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            print(f"Telegram помилка (спроба {attempt + 1}): {e}")
            if attempt < REQUEST_RETRIES - 1:
                time.sleep(2 ** attempt)
    return False


# --------------------------- формування сигналу -----------------------------

def build_position_message(pos, total, now_ts):
    title = pos.get('title') or 'Ринок'
    outcome = pos.get('outcome') or '?'
    slug = pos.get('slug') or ''
    event_url = f"{POLY_EVENT}/{slug}" if slug else 'https://polymarket.com'
    wallet = pos.get('wallet') or ''
    name = (pos.get('name') or '').strip()
    short = short_wallet(wallet)
    trader = f"{name} ({short})" if name else (short or 'Анонім')
    profile_url = f"{POLY_PROFILE}/{wallet}" if wallet else ''

    trades = pos.get('trades', [])
    count = len(trades)
    oldest = min((tr['ts'] for tr in trades), default=now_ts)
    span_min = max(0, (now_ts - oldest) / 60)

    # Середня ціна, зважена за обсягом.
    vol = sum(tr['usd'] for tr in trades) or 1
    avg_price = sum(tr.get('price', 0) * tr['usd'] for tr in trades) / vol

    # Рядки угод (найбільші перші), кожна з посиланням на транзакцію.
    rows = sorted(trades, key=lambda x: x['usd'], reverse=True)
    lines = []
    for tr in rows[:8]:
        cents = f"{tr.get('price', 0) * 100:.0f}¢" if tr.get('price') else '?'
        t_time = format_time(tr['ts'])
        tx = tr.get('tx') or ''
        base = f"🟢 <b>${tr['usd']:,.0f}</b> · {cents} · {t_time}"
        if tx:
            base += f" · <a href='{POLYGONSCAN_TX}/{tx}'>трейд</a>"
        lines.append(base)
    trades_text = "\n".join(lines)
    if count > 8:
        trades_text += f"\n<i>... і ще {count - 8} угод</i>"

    if count == 1:
        header = "🐋 <b>Велика ставка кита!</b>"
    else:
        header = f"🐋 <b>Кит набирає позицію · {count} угод за {span_min:.0f} хв</b>"

    trader_line = f"👤 <a href='{profile_url}'>{trader}</a>" if profile_url else f"👤 {trader}"

    return (
        f"{header}\n\n"
        f"📌 <b>{title}</b>\n"
        f"🎯 Ставка: <b>{outcome}</b> · середня ціна {avg_price * 100:.0f}¢\n"
        f"💰 <b>Позиція: ${total:,.0f}</b>" + (f" · {count} угод" if count > 1 else "") + "\n"
        f"{trader_line}\n\n"
        f"{trades_text}\n\n"
        f"🔗 <a href='{event_url}'>Відкрити подію</a>"
    )


# -------------------------------- main -------------------------------------

def main():
    state = load_state()
    since = state['last_timestamp']
    positions = state['positions']
    window_end = int(time.time())
    agg_cutoff = window_end - AGG_WINDOW_SECONDS

    print(f"Перевірка з {format_time(since)} · поріг сигналу ${MIN_AMOUNT:,.0f} · "
          f"шматок ≥ ${COMPONENT_MIN:,.0f} · вікно {AGG_WINDOW_SECONDS // 60} хв")

    try:
        trades, complete = get_trades_since(since)
    except Exception as e:
        print(f"❌ Помилка отримання угод: {e}. Стан не оновлюється, повторимо наступного разу.")
        return

    print(f"Отримано угод-кандидатів: {len(trades)}")

    # Діагностика: чи є взагалі кандидати у вікні, і чи не відкидаємо їх помилково.
    if trades:
        ts_all = [trade_ts(t) for t in trades]
        newest, oldest = max(ts_all), min(ts_all)
        in_window = sum(1 for ts in ts_all if since < ts <= window_end)
        future = sum(1 for ts in ts_all if ts > window_end)
        before = sum(1 for ts in ts_all if ts <= since)
        biggest = max(trades, key=calc_usd)
        print(f"Діапазон кандидатів: {format_time(oldest)} .. {format_time(newest)} "
              f"(це найновіші {len(trades)} купівель ≥ ${FILTER_AMOUNT:,.0f})")
        print(f"У вікні ({format_time(since)}..зараз): {in_window} · "
              f"до вікна: {before} · у майбутньому (розсинхрон годинника): {future}")
        print(f"Найбільша у вибірці: ${calc_usd(biggest):,.0f} о {format_time(trade_ts(biggest))}")
        if in_window == 0 and future == 0:
            print("→ У цьому вікні великих купівель не було (тихий період) — це нормально.")

    # 1) Додаємо нові угоди у відповідні позиції (гаманець + результат).
    added = 0
    for t in trades:
        ts = trade_ts(t)
        if ts <= since or ts > window_end:
            continue
        if str(t.get('side', 'BUY')).upper() != 'BUY':
            continue
        usd = calc_usd(t)
        if usd <= 0:
            continue

        key = position_key(t)
        pos = positions.get(key)
        if pos is None:
            pos = {
                'wallet': t.get('proxyWallet', ''),
                'asset': str(t.get('asset') or ''),
                'conditionId': t.get('conditionId', ''),
                'title': t.get('title') or t.get('question') or '',
                'slug': t.get('eventSlug') or t.get('slug') or '',
                'outcome': outcome_of(t),
                'name': (t.get('name') or t.get('pseudonym') or '').strip(),
                'alerted': False,
                'trades': [],
            }
            positions[key] = pos

        uid = trade_uid(t)
        if any(tr.get('id') == uid for tr in pos['trades']):
            continue  # вже враховано (перекриття наздоганяння)

        pos['trades'].append({
            'ts': ts,
            'usd': usd,
            'price': float(t.get('price') or 0),
            'tx': t.get('transactionHash') or '',
            'id': uid,
        })
        # Дозаповнюємо метадані, якщо раптом були порожні.
        if not pos.get('title'):
            pos['title'] = t.get('title') or t.get('question') or ''
        if not pos.get('slug'):
            pos['slug'] = t.get('eventSlug') or t.get('slug') or ''
        if not pos.get('name'):
            pos['name'] = (t.get('name') or t.get('pseudonym') or '').strip()
        added += 1

    print(f"Додано нових угод у позиції: {added}")

    # 2) Прибираємо угоди поза вікном; чистимо порожні; «озброюємось» знову,
    #    якщо позиція розсмокталась нижче порогу.
    for key in list(positions.keys()):
        pos = positions[key]
        pos['trades'] = [tr for tr in pos['trades'] if tr['ts'] > agg_cutoff]
        if not pos['trades']:
            del positions[key]
            continue
        total = sum(tr['usd'] for tr in pos['trades'])
        if pos.get('alerted') and total < MIN_AMOUNT:
            pos['alerted'] = False

    # 3) Позиції, що перетнули поріг і ще не просигналені.
    to_alert = []
    for key, pos in positions.items():
        total = sum(tr['usd'] for tr in pos['trades'])
        if total >= MIN_AMOUNT and not pos.get('alerted'):
            to_alert.append((pos, total))
    to_alert.sort(key=lambda x: x[1], reverse=True)
    print(f"Нових позицій ≥ ${MIN_AMOUNT:,.0f}: {len(to_alert)} · активних позицій у стані: {len(positions)}")

    sent = 0
    for pos, total in to_alert[:MAX_MESSAGES]:
        if send_telegram(build_position_message(pos, total, window_end)):
            pos['alerted'] = True
            sent += 1
            print(f"✅ {pos.get('title', '')[:50]} · {pos.get('outcome')} · "
                  f"${total:,.0f} за {len(pos['trades'])} угод")
        time.sleep(0.3)

    # Переповнення (дуже рідко): зведення, щоб нічого не «зникло тихо».
    overflow = len(to_alert) - MAX_MESSAGES
    if overflow > 0:
        extra = to_alert[MAX_MESSAGES:]
        extra_vol = sum(x[1] for x in extra)
        send_telegram(
            f"➕ <b>Ще {overflow} позицій ≥ ${MIN_AMOUNT:,.0f}</b>\n"
            f"💰 Сумарно: ${extra_vol:,.0f}\n"
            f"<i>(показано топ-{MAX_MESSAGES} за обсягом)</i>"
        )
        for pos, _total in extra:
            pos['alerted'] = True

    if not complete:
        print("⚠️ Покриття було неповним — рідкісний найстаріший «хвіст» вікна міг бути пропущений.")
    save_state(window_end, positions)
    print(f"✅ Готово. Сповіщень надіслано: {sent}")


if __name__ == '__main__':
    main()
