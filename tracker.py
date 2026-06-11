import requests
import os
import json
import time
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
MIN_AMOUNT = float(os.environ.get('MIN_AMOUNT', '1000'))

STATE_FILE = 'last_check.json'
DATA_API = 'https://data-api.polymarket.com'
GAMMA_API = 'https://gamma-api.polymarket.com'


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
            ts = data.get('last_timestamp', 0)
            # Якщо 0 або більше ніж година назад — ставимо 5 хвилин назад
            if ts == 0 or ts < (int(time.time()) - 3600):
                print("Timestamp скинуто до 5 хвилин назад (перший запуск або дуже старий)")
                return {'last_timestamp': int(time.time()) - 300}
            return data
    return {'last_timestamp': int(time.time()) - 300}


def save_state(timestamp):
    with open(STATE_FILE, 'w') as f:
        json.dump({'last_timestamp': timestamp}, f)


def get_all_trades(since_timestamp):
    """Отримати ВСІ угоди з пагінацією"""
    all_trades = []
    offset = 0
    limit = 500

    while True:
        url = f"{DATA_API}/trades"
        params = {
            'limit': limit,
            'offset': offset,
            'start': since_timestamp,  # правильна назва параметра
        }
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()

            if not isinstance(data, list) or len(data) == 0:
                print(f"Порожня відповідь на offset={offset}, зупиняємось")
                break

            all_trades.extend(data)
            print(f"Порція offset={offset}: {len(data)} угод (всього: {len(all_trades)})")

            if len(data) < limit:
                break  # остання сторінка

            offset += limit
            time.sleep(0.3)

        except Exception as e:
            print(f"Помилка отримання угод (offset={offset}): {e}")
            break

    return all_trades


def get_market_info(condition_id):
    """Отримати назву і slug ринку"""
    # Спробуємо кілька варіантів параметрів
    attempts = [
        {'conditionId': condition_id},
        {'condition_id': condition_id},
        {'conditionIds': condition_id},
    ]
    for params in attempts:
        try:
            r = requests.get(f"{GAMMA_API}/markets", params=params, timeout=10)
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                m = data[0]
                return {
                    'question': m.get('question') or m.get('title') or '',
                    'slug': m.get('slug') or m.get('marketSlug') or ''
                }
        except Exception as e:
            print(f"Gamma API помилка ({params}): {e}")
    return {'question': '', 'slug': ''}


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
        print("✅ Telegram надіслано")
    except Exception as e:
        print(f"❌ Telegram помилка: {e}")


def extract_usd(trade):
    """Спробувати різні поля для суми в USD"""
    for field in ['usdcSize', 'cashAmount', 'cash_amount', 'collateral_amount']:
        val = trade.get(field)
        if val:
            try:
                return float(val)
            except Exception:
                pass
    # Розрахунок: кількість токенів * ціна
    size = trade.get('size') or trade.get('tokensAmount') or 0
    price = trade.get('price') or 0
    try:
        return float(size) * float(price)
    except Exception:
        return 0


def extract_trader(trade):
    """Спробувати різні поля для адреси гаманця"""
    for field in ['maker_address', 'taker_address', 'trader', 'owner',
                  'transactorAddress', 'user', 'userId', 'proxyWallet']:
        val = trade.get(field)
        if val and str(val).startswith('0x') and len(str(val)) > 10:
            addr = str(val)
            return addr[:6] + '...' + addr[-4:]
    return None


def extract_condition_id(trade):
    """Спробувати різні поля для ID ринку"""
    for field in ['conditionId', 'condition_id', 'market', 'marketId',
                  'market_id', 'questionID']:
        val = trade.get(field)
        if val and len(str(val)) > 10:
            return str(val)
    return None


def extract_timestamp(trade):
    """Спробувати різні поля для часу"""
    for field in ['timestamp', 'match_time', 'created_at',
                  'transactionTime', 'blockTimestamp']:
        val = trade.get(field)
        if val:
            try:
                ts = float(val)
                # Якщо timestamp в мілісекундах — конвертуємо
                if ts > 1e12:
                    ts = ts / 1000
                return ts
            except Exception:
                pass
    return None


def main():
    state = load_state()
    since = state['last_timestamp']
    now = int(time.time())

    print(f"Перевірка угод з {datetime.fromtimestamp(since, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    trades = get_all_trades(since)
    print(f"Всього угод отримано: {len(trades)}")

    # Debug: показуємо поля першої угоди для діагностики
    if trades:
        print(f"📋 Поля першої угоди: {list(trades[0].keys())}")
        print(f"📋 Приклад: {json.dumps(trades[0], indent=2)[:500]}")

    alerts_sent = 0

    for trade in trades:
        # Фільтр по часу
        trade_time = extract_timestamp(trade)
        if trade_time and trade_time <= since:
            continue

        # Сума
        usd = extract_usd(trade)
        if usd < MIN_AMOUNT:
            continue

        # Дані
        condition_id = extract_condition_id(trade)
        side = str(trade.get('side', 'BUY')).upper()
        outcome = trade.get('outcome') or trade.get('outcomeIndex', '')

        # Outcome: 0/1 → No/Yes
        if str(outcome) == '0':
            outcome_text = 'No'
        elif str(outcome) == '1':
            outcome_text = 'Yes'
        else:
            outcome_text = str(outcome) if outcome else '?'

        trader = extract_trader(trade) or 'Анонім'
        emoji_side = "🟢 КУПИВ" if side == "BUY" else "🔴 ПРОДАВ"

        # Назва ринку
        market_info = {'question': '', 'slug': ''}
        if condition_id:
            market_info = get_market_info(condition_id)

        title = market_info['question']
        if not title:
            title = f"ID: {condition_id[:20]}..." if condition_id else 'N/A'

        # Посилання
        if market_info['slug']:
            market_url = f"https://polymarket.com/event/{market_info['slug']}"
        else:
            market_url = "https://polymarket.com"

        msg = (
            f"🐋 <b>Велика ставка на Polymarket!</b>\n\n"
            f"📌 <b>Подія:</b> {title}\n"
            f"🎯 <b>Ставка:</b> {outcome_text}\n"
            f"📊 <b>Дія:</b> {emoji_side}\n"
            f"💰 <b>Сума:</b> ${usd:,.0f}\n"
            f"👤 <b>Гравець:</b> <code>{trader}</code>\n"
            f"🔗 <a href='{market_url}'>Відкрити ринок</a>"
        )

        send_telegram(msg)
        alerts_sent += 1
        print(f"Алерт: ${usd:,.0f} — {title[:70]}")

        if alerts_sent % 5 == 0:
            time.sleep(1)

    save_state(now)
    print(f"✅ Готово. Надіслано алертів: {alerts_sent}")


if __name__ == '__main__':
    main()
