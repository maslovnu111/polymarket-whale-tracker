import requests
import os
import json
import time
from datetime import datetime, timezone

# Налаштування (беруться з секретів GitHub)
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
MIN_AMOUNT = float(os.environ.get('MIN_AMOUNT', '1000'))

STATE_FILE = 'last_check.json'
DATA_API = 'https://data-api.polymarket.com'
GAMMA_API = 'https://gamma-api.polymarket.com'


def load_state():
    """Завантажити час останньої перевірки"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    # Перший запуск: дивимось назад 5 хвилин
    return {'last_timestamp': int(time.time()) - 300}


def save_state(timestamp):
    """Зберегти час поточної перевірки"""
    with open(STATE_FILE, 'w') as f:
        json.dump({'last_timestamp': timestamp}, f)


def get_trades(since_timestamp):
    """Отримати угоди з Polymarket"""
    url = f"{DATA_API}/trades"
    params = {
        'limit': 100,
        'startTs': since_timestamp
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"Помилка отримання угод: {e}")
        return []


def get_market_info(condition_id):
    """Отримати назву ринку по condition_id"""
    try:
        url = f"{GAMMA_API}/markets"
        params = {'condition_id': condition_id, 'limit': 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            market = data[0]
            return {
                'question': market.get('question', 'Невідомий ринок'),
                'slug': market.get('slug', '')
            }
    except Exception:
        pass
    return {'question': f"Ринок {condition_id[:16]}...", 'slug': ''}


def send_telegram(message):
    """Надіслати повідомлення в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': False
        }, timeout=10)
        r.raise_for_status()
        print("Telegram повідомлення надіслано")
    except Exception as e:
        print(f"Помилка Telegram: {e}")


def calculate_usd(trade):
    """Розрахувати суму угоди в USD"""
    # Спробуємо різні варіанти полів (API може повертати по-різному)
    usd = trade.get('usdcSize') or trade.get('cashAmount')
    if usd:
        return float(usd)
    # Якщо прямого поля немає — рахуємо size * price
    size = float(trade.get('size', 0) or 0)
    price = float(trade.get('price', 0) or 0)
    return size * price


def main():
    state = load_state()
    since = state['last_timestamp']
    now = int(time.time())

    print(f"Перевірка угод з {datetime.fromtimestamp(since, tz=timezone.utc).strftime('%H:%M:%S UTC')}")

    trades = get_trades(since)
    print(f"Отримано угод: {len(trades)}")

    alerts_sent = 0

    for trade in trades:
        # Фільтр по часу (на випадок якщо API не фільтрує)
        trade_time = float(trade.get('timestamp', 0) or trade.get('match_time', 0) or 0)
        if trade_time and trade_time <= since:
            continue

        # Розрахунок суми
        usd_amount = calculate_usd(trade)
        if usd_amount < MIN_AMOUNT:
            continue

        # Дані угоди
        condition_id = trade.get('market', '')
        outcome = trade.get('outcome', 'N/A')
        side = trade.get('side', 'BUY')
        trader = trade.get('maker_address') or trade.get('trader', 'Unknown')

        # Скорочення адреси гаманця
        if trader and len(trader) > 12:
            trader_short = trader[:6] + '...' + trader[-4:]
        else:
            trader_short = trader or 'Unknown'

        # Назва ринку
        market_info = get_market_info(condition_id) if condition_id else {'question': 'N/A', 'slug': ''}
        title = market_info['question']

        # Посилання на ринок
        if market_info['slug']:
            market_url = f"https://polymarket.com/event/{market_info['slug']}"
        else:
            market_url = "https://polymarket.com"

        emoji_side = "🟢 КУПИВ" if side == "BUY" else "🔴 ПРОДАВ"

        msg = (
            f"🐋 <b>Велика ставка на Polymarket!</b>\n\n"
            f"📌 <b>Подія:</b> {title}\n"
            f"🎯 <b>Результат:</b> {outcome}\n"
            f"📊 <b>Дія:</b> {emoji_side}\n"
            f"💰 <b>Сума:</b> ${usd_amount:,.0f}\n"
            f"👤 <b>Гравець:</b> <code>{trader_short}</code>\n"
            f"🔗 <a href='{market_url}'>Відкрити ринок</a>"
        )

        send_telegram(msg)
        alerts_sent += 1
        print(f"Алерт: ${usd_amount:,.0f} — {title[:60]}")

        # Невелика пауза між повідомленнями щоб не перевантажити Telegram
        if alerts_sent > 1:
            time.sleep(0.5)

    # Зберегти час поточної перевірки
    save_state(now)
    print(f"Готово. Надіслано алертів: {alerts_sent}")


if __name__ == '__main__':
    main()
