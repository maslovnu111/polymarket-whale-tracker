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


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
            ts = data.get('last_timestamp', 0)
            if ts == 0 or ts < (int(time.time()) - 3600):
                print("Timestamp скинуто до 5 хвилин назад")
                return {'last_timestamp': int(time.time()) - 300}
            return data
    return {'last_timestamp': int(time.time()) - 300}


def save_state(timestamp):
    with open(STATE_FILE, 'w') as f:
        json.dump({'last_timestamp': timestamp}, f)


def get_all_trades(since_timestamp):
    all_trades = []
    offset = 0
    limit = 500
    MAX_OFFSET = 3000  # API не приймає більше — зупиняємось тут

    while offset <= MAX_OFFSET:
        params = {
            'limit': limit,
            'offset': offset,
            'start': since_timestamp,
        }
        try:
            r = requests.get(f"{DATA_API}/trades", params=params, timeout=20)
            r.raise_for_status()
            data = r.json()

            if not isinstance(data, list) or len(data) == 0:
                break

            all_trades.extend(data)
            print(f"offset={offset}: {len(data)} угод (всього: {len(all_trades)})")

            if len(data) < limit:
                break  # остання сторінка

            offset += limit
            time.sleep(0.3)

        except Exception as e:
            print(f"Помилка (offset={offset}): {e}")
            break

    return all_trades


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': False
        }, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram помилка: {e}")
        return False


def format_trader(trade):
    """Ім'я + скорочена адреса, або просто адреса"""
    name = trade.get('name') or trade.get('pseudonym') or ''
    wallet = trade.get('proxyWallet', '')
    short = (wallet[:6] + '...' + wallet[-4:]) if len(wallet) > 10 else wallet

    if name.strip():
        return f"{name.strip()} ({short})"
    return short or 'Анонім'


def format_time(ts_raw):
    try:
        ts = float(ts_raw)
        if ts > 1e12:
            ts /= 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
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
    now = int(time.time())

    print(f"Перевірка з {datetime.fromtimestamp(since, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    trades = get_all_trades(since)
    print(f"Всього угод отримано: {len(trades)}")

    # Відбираємо тільки нові і великі
    big_trades = []
    for trade in trades:
        try:
            trade_ts = float(trade.get('timestamp') or 0)
            if trade_ts > 1e12:
                trade_ts /= 1000
        except Exception:
            trade_ts = 0

        if trade_ts and trade_ts <= since:
            continue

        usd = calc_usd(trade)
        if usd < MIN_AMOUNT:
            continue

        big_trades.append({**trade, '_usd': usd, '_ts': trade_ts})

    print(f"Великих угод (≥${MIN_AMOUNT:,.0f}): {len(big_trades)}")

    if not big_trades:
        save_state(now)
        print("Немає нових великих угод")
        return

    # Групуємо по ринку — одне повідомлення на ринок
    by_market = defaultdict(list)
    for t in big_trades:
        cid = t.get('conditionId', 'unknown')
        by_market[cid].append(t)

    print(f"Ринків з активністю: {len(by_market)}")
    sent = 0

    for cid, market_trades in by_market.items():
        # Назва і посилання беруться прямо з угоди (не потрібен окремий запит)
        first = market_trades[0]
        title = first.get('title') or first.get('question') or f"ID: {cid[:20]}..."
        slug = first.get('slug') or first.get('eventSlug') or ''
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

    save_state(now)
    print(f"✅ Готово. Повідомлень надіслано: {sent} (по {len(by_market)} ринках)")


if __name__ == '__main__':
    main()
