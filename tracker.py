import requests
import os
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
MIN_AMOUNT = float(os.environ.get('MIN_AMOUNT', '1000'))

STATE_FILE = 'last_check.json'
DATA_API = 'https://data-api.polymarket.com'

# Ендпоінт /trades НЕ має фільтра за часом і не віддає більше ~3500 угод
# (offset обмежений). Тому не намагаємось зчитати весь потік — просимо сервер
# одразу віддати ЛИШЕ великі угоди (filterType=CASH + filterAmount = мінімум $).
# Так великі угоди завжди влазять у ліміт, скільки б дрібних не було навколо.
# Поріг на сервері трохи нижчий за MIN_AMOUNT, щоб не втратити пограничні угоди
# через можливу різницю в округленні; точний відсів робимо вже в коді.
FILTER_MARGIN = float(os.environ.get('FILTER_MARGIN', '0.98'))
FILTER_AMOUNT = max(0, int(MIN_AMOUNT * FILTER_MARGIN))

# Скільки дивитись назад при ПЕРШОМУ запуску (немає збереженого стану), сек
LOOKBACK_SECONDS = int(os.environ.get('LOOKBACK_SECONDS', '300'))
# Максимальна глибина наздоганяння пропущених запусків, сек (за замовчуванням 24 год).
# Якщо GitHub пропустив/затримав запуски — добираємо всі угоди з моменту останнього
# запуску, але не глибше цієї межі (щоб після довгого простою не залити старим).
MAX_BACKFILL_SECONDS = int(os.environ.get('MAX_BACKFILL_SECONDS', str(24 * 3600)))

PAGE_LIMIT = 500
# Запобіжник від нескінченного циклу. Оскільки тягнемо лише ВЕЛИКІ угоди,
# їх мало — сторінок майже завжди буде 1. 200 — з величезним запасом.
MAX_PAGES = int(os.environ.get('MAX_PAGES', '200'))
# Скільки повідомлень максимум слати за один запуск (решта — у зведенні)
MAX_MESSAGES = int(os.environ.get('MAX_MESSAGES', '30'))
REQUEST_RETRIES = 3
REQUEST_TIMEOUT = 20


def load_state():
    now = int(time.time())
    ts = 0
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            ts = int(data.get('last_timestamp', 0))
        except Exception as e:
            print(f"Не вдалося прочитати стан ({e}) — почнемо з {LOOKBACK_SECONDS // 60} хв назад")
            ts = 0

    if ts <= 0:
        # Немає збереженого стану (перший запуск або пошкоджений файл).
        since = now - LOOKBACK_SECONDS
        print(f"Стану немає — дивимось {LOOKBACK_SECONDS // 60} хв назад")
        return {'last_timestamp': since}

    floor = now - MAX_BACKFILL_SECONDS
    if ts < floor:
        # Простій довший за ліміт наздоганяння — беремо лише останні MAX_BACKFILL.
        gap_h = (now - ts) / 3600
        print(f"Простій ~{gap_h:.1f} год перевищує ліміт наздоганяння "
              f"({MAX_BACKFILL_SECONDS // 3600} год) — добираємо лише останні "
              f"{MAX_BACKFILL_SECONDS // 3600} год")
        return {'last_timestamp': floor}

    # Нормальний випадок: чесно наздоганяємо ВСЕ з моменту останнього запуску,
    # навіть якщо GitHub затримав/пропустив кілька слотів — нічого не губимо.
    gap_min = (now - ts) / 60
    if gap_min > 6:
        print(f"Наздоганяємо пропуск ~{gap_min:.0f} хв з моменту останнього запуску")
    return {'last_timestamp': ts}


def save_state(timestamp):
    with open(STATE_FILE, 'w') as f:
        json.dump({'last_timestamp': int(timestamp)}, f)


def trade_ts(trade):
    """Час угоди у секундах (Unix). Нормалізуємо мілісекунди -> секунди."""
    try:
        ts = float(trade.get('timestamp') or 0)
        if ts > 1e12:
            ts /= 1000
        return ts
    except Exception:
        return 0.0


def trade_key(trade):
    """Унікальний ключ угоди для дедуплікації між сторінками."""
    return (
        trade.get('transactionHash'),
        trade.get('asset') or trade.get('outcomeIndex'),
        str(trade.get('side')),
        str(trade.get('size')),
        str(trade.get('price')),
        str(trade.get('timestamp')),
    )


def _request_trades(offset):
    """Один запит сторінки великих угод з ретраями. Кидає виняток при повній невдачі."""
    params = {
        'limit': PAGE_LIMIT,
        'offset': offset,
        'takerOnly': 'true',       # одна угода = один рядок (без дублів maker/taker)
        'filterType': 'CASH',      # фільтруємо за грошовим обсягом (USDC)...
        'filterAmount': FILTER_AMOUNT,  # ...повертаємо лише угоди з сумою >= цього
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
    """
    Тягне УСІ великі угоди (>= FILTER_AMOUNT), новіші за since_timestamp.

    Сервер уже віддає лише великі угоди, тож їх мало. Вони йдуть від найновіших
    до найстаріших — щойно на сторінці з'являється угода з часом <= since, все
    далі вже старе, зупиняємось. Оскільки великих угод небагато, межу за часом
    майже завжди знаходимо на першій сторінці, до жодної стелі offset не доходить.

    Повертає (trades, complete): complete=False, якщо покриття неповне
    (впёрлись у MAX_PAGES або мережева помилка/offset-ліміт на високому offset).
    """
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
                # Повний провал фетчу — хай викликач не рухає стан і повторить.
                raise
            # Помилка на високому offset (напр. offset-ліміт API) — не фатально:
            # усі найновіші великі угоди ми вже зібрали.
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
            key = trade_key(t)
            if key in seen:
                continue
            seen.add(key)
            all_trades.append(t)
            new_count += 1

        print(f"offset={offset}: {len(data)} великих угод (нових: {new_count}, всього: {len(all_trades)})")

        # Дійшли до угод, старіших за межу — далі все вже старе.
        if page_min_ts is not None and page_min_ts <= since_timestamp:
            reached_boundary = True
            break
        # Остання сторінка (даних більше немає).
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
                'disable_web_page_preview': False
            }, timeout=10)
            if r.status_code == 429:
                # Telegram rate-limit: чекаємо стільки, скільки просить.
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


def format_trader(trade):
    """Ім'я + скорочена адреса, або просто адреса"""
    name = trade.get('name') or trade.get('pseudonym') or ''
    wallet = trade.get('proxyWallet', '')
    short = (wallet[:6] + '...' + wallet[-4:]) if len(wallet) > 10 else wallet

    if name.strip():
        return f"{name.strip()} ({short})"
    return short or 'Анонім'


def format_time(ts):
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime('%d.%m %H:%M UTC')
    except Exception:
        return '?'


def calc_usd(trade):
    # Пробуємо готове поле, якщо немає — рахуємо самі
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


def main():
    state = load_state()
    since = state['last_timestamp']
    window_end = int(time.time())

    print(f"Перевірка з {datetime.fromtimestamp(since, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    try:
        trades, complete = get_trades_since(since)
    except Exception as e:
        # Не рухаємо стан — наступний запуск повторить те саме вікно.
        print(f"❌ Помилка отримання угод: {e}. Стан не оновлюється, повторимо наступного разу.")
        return

    print(f"Всього великих угод отримано: {len(trades)}")

    # Показуємо, за який реально період API віддав ці угоди (для розуміння —
    # /trades не має фільтра за часом, тож період не фіксований).
    ts_list = [trade_ts(t) for t in trades if trade_ts(t) > 0]
    if ts_list:
        oldest, newest = min(ts_list), max(ts_list)
        span_min = (newest - oldest) / 60
        print(f"Період цих угод: від {format_time(oldest)} до {format_time(newest)} "
              f"(за ~{span_min:.0f} хв / ~{span_min / 60:.1f} год)")

    # Відбираємо тільки угоди у вікні (since, window_end] і точно >= MIN_AMOUNT
    # (сервер фільтрував за трохи нижчим порогом — тут робимо точний відсів).
    big_trades = []
    for trade in trades:
        ts = trade_ts(trade)
        if ts <= since or ts > window_end:
            continue
        usd = calc_usd(trade)
        if usd < MIN_AMOUNT:
            continue
        big_trades.append({**trade, '_usd': usd, '_ts': ts})

    print(f"Великих угод (≥${MIN_AMOUNT:,.0f}): {len(big_trades)}")

    if not big_trades:
        save_state(window_end)
        print("Немає нових великих угод")
        return

    # Групуємо по ринку — одне повідомлення на ринок
    by_market = defaultdict(list)
    for t in big_trades:
        cid = t.get('conditionId', 'unknown')
        by_market[cid].append(t)

    print(f"Ринків з активністю: {len(by_market)}")

    # Сортуємо ринки за загальним обсягом — найбільші зверху (важливо при ліміті).
    markets = sorted(
        by_market.items(),
        key=lambda kv: sum(t['_usd'] for t in kv[1]),
        reverse=True,
    )

    sent = 0
    for cid, market_trades in markets[:MAX_MESSAGES]:
        # Назва і посилання беруться прямо з угоди (не потрібен окремий запит)
        first = market_trades[0]
        title = first.get('title') or first.get('question') or f"ID: {cid[:20]}..."
        slug = first.get('eventSlug') or first.get('slug') or ''
        market_url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"

        total_usd = sum(t['_usd'] for t in market_trades)
        count = len(market_trades)

        # Сортуємо по сумі — найбільші зверху
        market_trades.sort(key=lambda x: x['_usd'], reverse=True)

        lines = []
        for t in market_trades[:8]:
            side = str(t.get('side', 'BUY')).upper()
            outcome = t.get('outcome') or (
                'Yes' if str(t.get('outcomeIndex', '')) == '1'
                else 'No' if str(t.get('outcomeIndex', '')) == '0'
                else '?'
            )
            emoji = "🟢" if side == "BUY" else "🔴"
            trader = format_trader(t)
            trade_time = format_time(t['_ts'])
            usd = t['_usd']

            lines.append(f"{emoji} <b>${usd:,.0f}</b> · {outcome} · {trader} · {trade_time}")

        trades_text = "\n".join(lines)
        if count > 8:
            trades_text += f"\n<i>... і ще {count - 8} угод</i>"

        header = "🐋 <b>Велика ставка!</b>" if count == 1 else f"🐋 <b>Активність китів · {count} угод</b>"

        msg = (
            f"{header}\n\n"
            f"📌 <b>{title}</b>\n\n"
            f"{trades_text}\n\n"
            f"💰 <b>Загальний обсяг: ${total_usd:,.0f}</b>\n"
            f"🔗 <a href='{market_url}'>Відкрити ринок</a>"
        )

        if send_telegram(msg):
            sent += 1
            print(f"✅ {title[:60]} — {count} угод, ${total_usd:,.0f}")

        time.sleep(0.3)

    # Якщо ринків більше за ліміт — одне зведення, щоб нічого не «зникло тихо».
    overflow = len(markets) - MAX_MESSAGES
    if overflow > 0:
        extra = markets[MAX_MESSAGES:]
        extra_volume = sum(sum(t['_usd'] for t in mt) for _, mt in extra)
        send_telegram(
            f"➕ <b>Ще {overflow} ринків з великою активністю</b>\n"
            f"💰 Сумарний обсяг: ${extra_volume:,.0f}\n"
            f"<i>(показано топ-{MAX_MESSAGES} за обсягом)</i>"
        )

    # Просуваємо стан до кінця вікна. Навіть при неповному покритті рухаємось
    # вперед — інакше вікно розросталось би й ми повторно слали б сповіщення.
    # Повний провал фетчу оброблено вище (return без збереження стану).
    if not complete:
        print("⚠️ Покриття було неповним — рідкісний найстаріший «хвіст» вікна міг бути пропущений.")
    save_state(window_end)
    print(f"✅ Готово. Повідомлень надіслано: {sent} (по {len(by_market)} ринках)")


if __name__ == '__main__':
    main()
