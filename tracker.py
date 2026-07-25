import os
import re
import json
import time
import html
import urllib.request
import urllib.error
from urllib.parse import quote, urlencode
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
#      Повторно та сама позиція сигналить лише коли зросла у ESCALATE_FACTOR
#      разів від уже просигналеної суми (захист від дублів на межі порогу).
# Одна велика угода теж спрацьовує — це просто позиція з однієї угоди.
#
# Чому це надійно навіть у пік: за один запуск добираємо лише НОВІ угоди з
# моменту минулого запуску (~5 хв), а 60-хв суму тримаємо у стані між запусками.
# Щоб пробити ліміт ~3500 угод/запит, потрібно >3500 угод >= COMPONENT_MIN за
# 5 хв — обсяг, якого фізично не існує.
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit("❌ Не задано TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (Secrets репозиторію)")

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
        print(f"⚠️ {name}={v!r} — не число, використовую default {default}")
        return cast(default)


def _clamp(val, lo, hi, name):
    if val < lo or val > hi:
        fixed = min(max(val, lo), hi)
        print(f"⚠️ {name}={val} поза межами [{lo}..{hi}] — використовую {fixed}")
        return fixed
    return val


# Поріг СИГНАЛУ — сумарна позиція трейдера, від якої шлемо сповіщення.
MIN_AMOUNT = _clamp(_env_num('MIN_AMOUNT', 1_000_000, float), 1_000, 1e12, 'MIN_AMOUNT')
# Поріг ШМАТКА — мінімальна окрема угода, яку рахуємо як частину позиції.
# Не вище MIN_AMOUNT (інакше пропустили б одиничну велику угоду) і не нижче
# $1000 (інакше кандидатів стане забагато і впремося в ліміт API).
COMPONENT_MIN = _clamp(_env_num('COMPONENT_MIN', 100_000, float), 1_000, MIN_AMOUNT, 'COMPONENT_MIN')
# Вікно агрегації: за який період підсумовуємо угоди одного трейдера (сек).
AGG_WINDOW_SECONDS = int(_clamp(_env_num('AGG_WINDOW_MINUTES', 60, float), 5, 24 * 60, 'AGG_WINDOW_MINUTES') * 60)
# Повторний сигнал по вже просигналеній позиції — лише якщо вона зросла у
# стільки разів (захист від дублів, коли сума коливається біля порогу).
ESCALATE_FACTOR = _clamp(_env_num('ESCALATE_FACTOR', 2.0, float), 1.2, 100, 'ESCALATE_FACTOR')

# Серверний поріг трохи нижчий за COMPONENT_MIN — щоб не втратити пограничні
# шматки через округлення.
FILTER_MARGIN = _clamp(_env_num('FILTER_MARGIN', 0.98, float), 0.5, 1.0, 'FILTER_MARGIN')
FILTER_AMOUNT = max(1, int(COMPONENT_MIN * FILTER_MARGIN))

# Максимальна глибина наздоганяння пропущених запусків (год -> сек).
MAX_BACKFILL_SECONDS = int(_env_num('MAX_BACKFILL_HOURS', 24, float) * 3600)
MAX_BACKFILL_SECONDS = max(MAX_BACKFILL_SECONDS, AGG_WINDOW_SECONDS)

# Як часто ОБОВ'ЯЗКОВО просувати збережений стан, навіть якщо нічого не
# сталося (сек). Між цими «серцебиттями» тихий запуск не чіпає файл стану —
# отже workflow не робить коміт. Це прибирає ~5-хвилинну комітну «молотилку»
# (репозиторій уже має тисячі комітів «Update last check») і разом з нею —
# гонки при push. Нічого не губиться: незмінений last_timestamp означає, що
# наступний запуск просто перечитає те саме вікно, а дублі виключені
# (угоди — за uid, сповіщення — за alerted_usd).
STATE_HEARTBEAT_SECONDS = int(_clamp(_env_num('STATE_HEARTBEAT_MINUTES', 30, float),
                                     1, 240, 'STATE_HEARTBEAT_MINUTES') * 60)
# Захист від дублюючих тригерів (зовнішній планувальник + розклад GitHub +
# ручний запуск): якщо стан збережено щойно, повторний запуск нічого не дасть.
MIN_RUN_INTERVAL_SECONDS = int(_clamp(_env_num('MIN_RUN_INTERVAL_SECONDS', 45, float),
                                      0, 240, 'MIN_RUN_INTERVAL_SECONDS'))

# Бюджет часу на вибірку угод (сек). Джоба вбивається таймаутом за 4 хв, і
# смерть посеред вибірки — найгірший сценарій: сповіщення не надсилаються
# взагалі, стан не зберігається, наступний запуск повторює те саме вікно і
# може обірватись знову. Тому зупиняємось САМІ й обробляємо те, що встигли:
# сигнали йдуть, межа вікна не просувається, наступний запуск доробить.
FETCH_BUDGET_SECONDS = int(_clamp(_env_num('FETCH_BUDGET_SECONDS', 90, float),
                                  10, 180, 'FETCH_BUDGET_SECONDS'))

PAGE_LIMIT = 500
# Запобіжник від нескінченного циклу (сторінок на запуск). Оскільки за раз
# добираємо лише ~5 хв угод-кандидатів, реально сторінка майже завжди 1.
MAX_PAGES = max(1, int(_env_num('MAX_PAGES', 200, int)))
# Скільки детальних сповіщень максимум за один запуск (решта — у зведенні).
MAX_MESSAGES = max(1, int(_env_num('MAX_MESSAGES', 30, int)))
# Стеля кількості позицій у стані (страховка від патологічного розростання).
MAX_POSITIONS = 3000
REQUEST_RETRIES = 3
REQUEST_TIMEOUT = 20


# ------------------------------- стан --------------------------------------

def load_state():
    """Повертає {'last_timestamp': since, 'stored_ts': ts, 'positions': {...}}
    з наздоганянням пропусків. `stored_ts` — те, що реально лежить у файлі
    (0 = стану немає); `since` — звідки читати цього разу."""
    now = int(time.time())
    ts = 0
    positions = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            ts = int(data.get('last_timestamp', 0))
            positions = data.get('positions', {}) or {}
            if not isinstance(positions, dict):
                positions = {}
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
        if gap_min > (STATE_HEARTBEAT_SECONDS / 60 + 6):
            print(f"Наздоганяємо пропуск ~{gap_min:.0f} хв з моменту останнього запуску")

    return {'last_timestamp': since, 'stored_ts': ts, 'positions': positions}


def save_state(timestamp, positions):
    with open(STATE_FILE, 'w') as f:
        json.dump({'last_timestamp': int(timestamp), 'positions': positions}, f)


# ------------------------------ утиліти ------------------------------------

def esc(s):
    """Екранування для Telegram HTML: <, >, &, лапки. Без цього назва ринку
    на кшталт 'BTC <$95k?' ламає парсинг і сповіщення губиться."""
    return html.escape(str(s), quote=True)


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
    """Стабільний унікальний ідентифікатор угоди для дедуплікації.

    Хеш транзакції у реальних даних є завжди; якщо його раптом немає, самих
    лише суми/ціни/часу замало — дві різні угоди могли б злитися в одну і
    частина позиції загубилася б. Тому підставляємо гаманець і ринок.
    Для угод із хешем формат не змінюється — старі записи стану лишаються
    чинними і нічого не задвоюється."""
    tx = trade.get('transactionHash')
    if not tx:
        tx = f"notx:{trade.get('proxyWallet')}:{trade.get('conditionId')}"
    parts = (
        tx,
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
    try:
        size = float(trade.get('size') or 0)
        price = float(trade.get('price') or 0)
        return size * price
    except Exception:
        return 0.0


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


def pos_total(pos):
    return sum(tr['usd'] for tr in pos.get('trades', []))


def format_time(ts):
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime('%d.%m %H:%M UTC')
    except Exception:
        return '?'


def short_wallet(wallet):
    return (wallet[:6] + '...' + wallet[-4:]) if len(wallet or '') > 10 else (wallet or '')


# ------------------------------- HTTP --------------------------------------

def http_json(url, params=None, payload=None, timeout=None):
    """GET (з params) або POST JSON (payload). Повертає (status, data).

    Свідомо на стандартній бібліотеці, без `requests`: інакше кожен запуск
    мусив би тягнути пакет з PyPI — зайва мережева операція 288 разів на добу
    і ще одна причина, через яку запуск може впасти.
    """
    if params:
        url = f"{url}?{urlencode(params)}"
    data = None
    headers = {'User-Agent': 'polymarket-whale-tracker', 'Accept': 'application/json'}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout or REQUEST_TIMEOUT) as r:
            status, body = r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        # 4xx/5xx: тіло теж потрібне (429 -> retry_after, 400 -> опис помилки)
        status, body = e.code, e.read().decode('utf-8', 'replace')
    try:
        return status, json.loads(body)
    except Exception:
        return status, body


# ------------------------------- API ---------------------------------------

def _request_trades(offset, taker_only=False):
    """Одна сторінка угод-кандидатів (BUY, CASH >= FILTER_AMOUNT) з ретраями.
    Не-список у відповіді — це помилка (а не «угод немає»), інакше можна
    мовчки просунути стан і назавжди втратити вікно."""
    params = {
        'limit': PAGE_LIMIT,
        'offset': offset,
        # За замовчуванням API віддає ЛИШЕ ордери тейкера («Flag that determines
        # whether to return only taker orders. Defaults to true»), тобто ховає
        # всі виконання лімітних ордерів, де кит — мейкер. А кит саме так і
        # заходить на великі суми: ставить лімітку, бо в книзі немає ліквідності
        # на разовий ринковий ордер. Заміри на живих даних: при true не було
        # видно навіть одиничних купівель на $1.99M і $1.65M.
        # Подвійного рахунку false не створює: фільтр side=BUY лишає один рядок
        # на кожне виконання з боку покупця, а кілька рядків на один tx — це
        # часткові виконання одного ордера, які й треба підсумувати.
        # taker_only=True використовується ОКРЕМО — щоб визначити, яка угода
        # була ринковою, а яка лімітною (у відповіді немає такого поля).
        'takerOnly': 'true' if taker_only else 'false',
        'side': 'BUY',                  # лише купівлі (набір позиції)
        'filterType': 'CASH',           # фільтр за грошовим обсягом (USDC)...
        'filterAmount': FILTER_AMOUNT,  # ...шматки >= цього порогу
    }
    last_err = None
    for attempt in range(REQUEST_RETRIES):
        try:
            status, data = http_json(f"{DATA_API}/trades", params=params)
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {str(data)[:200]}")
            if not isinstance(data, list):
                raise ValueError(f"неочікувана відповідь API: {str(data)[:200]}")
            return data
        except Exception as e:
            last_err = e
            if attempt < REQUEST_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"offset={offset}: не вдалося отримати угоди: {last_err}")


def get_trades_since(since_timestamp, taker_only=False):
    """Тягне угоди-кандидати, новіші за since_timestamp (від нових до старих,
    із зупинкою на межі часу). Повертає (trades, complete)."""
    all_trades = []
    seen = set()
    offset = 0
    pages = 0
    reached_boundary = False
    complete = True
    started = time.time()

    while pages < MAX_PAGES:
        if pages and (time.time() - started) > FETCH_BUDGET_SECONDS:
            complete = False
            print(f"⚠️ Вибірка триває довше за {FETCH_BUDGET_SECONDS}с — зупиняємось самі, "
                  f"щоб не потрапити під таймаут джоби. Обробимо {len(all_trades)} угод, "
                  f"решту добере наступний запуск.")
            break
        try:
            data = _request_trades(offset, taker_only)
        except Exception as e:
            if offset == 0:
                raise  # повний провал — викликач не рухає стан
            print(f"⚠️ {e}. Зупиняємось, покриття неповне.")
            complete = False
            break

        if not data:
            if offset == 0:
                # Угоди ≥ $98k існують завжди — порожня ПЕРША сторінка означає
                # збій API. Не рухаємо стан, наступний запуск наздожене.
                raise RuntimeError("API повернув порожню першу сторінку — підозріло")
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


TELEGRAM_MAX_CHARS = 4096


def clip_message(message, limit=TELEGRAM_MAX_CHARS - 120):
    """Обрізає надто довге повідомлення, щоб Telegram не відхилив його з 400.

    Типове сповіщення ~1800 символів, але аномально довга назва ринку може
    вийти за ліміт 4096 — і сигнал тоді просто не дійде. Ріжемо ЛИШЕ по межах
    рядків: кожен рядок самодостатній за розміткою, тож теги не розриваються.
    Останній рядок (посилання на подію) зберігаємо завжди.
    """
    if len(message) <= limit:
        return message
    lines = message.split('\n')
    tail = lines[-1]
    marker = '<i>…повідомлення скорочено</i>'
    used = len(tail) + len(marker) + 2
    kept = []
    for ln in lines[:-1]:
        if used + len(ln) + 1 > limit:
            break
        kept.append(ln)
        used += len(ln) + 1
    return '\n'.join(kept + [marker, tail])


def send_telegram(message):
    message = clip_message(message)
    """Надсилає HTML-повідомлення. 429 — чекаємо і повторюємо; 400 (постійна
    помилка розмітки) — надсилаємо plain-text без розмітки, щоб сигнал не
    загубився; мережеві збої — ретраї з backoff."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(REQUEST_RETRIES):
        try:
            status, data = http_json(url, payload={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }, timeout=10)
            if status == 429:
                retry_after = 1
                try:
                    retry_after = int(data.get('parameters', {}).get('retry_after', 1))
                except Exception:
                    pass
                print(f"Telegram 429 — чекаємо {retry_after}с")
                time.sleep(retry_after + 1)
                continue
            if status == 400:
                # Постійна помилка (найчастіше розмітка) — ретраїти марно.
                desc = data.get('description', '') if isinstance(data, dict) else str(data)[:200]
                print(f"Telegram 400 ({desc}) — пробую без розмітки")
                plain = html.unescape(re.sub(r'<[^>]+>', '', message))
                status2, data2 = http_json(url, payload={
                    'chat_id': TELEGRAM_CHAT_ID,
                    'text': plain,
                    'disable_web_page_preview': True,
                }, timeout=10)
                if status2 == 200:
                    return True
                print(f"Telegram plain-text теж відхилено: {status2} {str(data2)[:200]}")
                return False
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {str(data)[:200]}")
            return True
        except Exception as e:
            print(f"Telegram помилка (спроба {attempt + 1}): {e}")
            if attempt < REQUEST_RETRIES - 1:
                time.sleep(2 ** attempt)
    return False


# --------------------------- формування сигналу -----------------------------

# Як підписувати тип кожної угоди у сповіщенні.
KIND_LABEL = {'market': '⚡ маркет', 'limit': '📘 лімітка'}


def build_position_message(pos, total, now_ts, prev_alerted_usd=0):
    title = esc(pos.get('title') or 'Ринок')
    outcome = esc(pos.get('outcome') or '?')
    slug = pos.get('slug') or ''
    event_url = f"{POLY_EVENT}/{quote(str(slug), safe='-_/')}" if slug else 'https://polymarket.com'
    wallet = pos.get('wallet') or ''
    name = (pos.get('name') or '').strip()
    short = short_wallet(wallet)
    trader = esc(f"{name} ({short})" if name else (short or 'Анонім'))
    profile_url = f"{POLY_PROFILE}/{quote(str(wallet), safe='')}" if wallet else ''

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
        tx = str(tr.get('tx') or '')
        kind = KIND_LABEL.get(tr.get('kind'), '')
        base = f"🟢 <b>${tr['usd']:,.0f}</b> · {cents}"
        if kind:
            base += f" · {kind}"
        base += f" · {t_time}"
        if tx:
            base += f" · <a href=\"{POLYGONSCAN_TX}/{quote(tx, safe='')}\">трейд</a>"
        lines.append(base)
    trades_text = "\n".join(lines)
    if count > 8:
        trades_text += f"\n<i>... і ще {count - 8} угод</i>"

    # Підсумок: яка частина позиції набрана ринковими ордерами, яка лімітками.
    by_market = sum(tr['usd'] for tr in trades if tr.get('kind') == 'market')
    by_limit = sum(tr['usd'] for tr in trades if tr.get('kind') == 'limit')
    mix_parts = []
    if by_market:
        mix_parts.append(f"⚡ маркет ${by_market:,.0f}")
    if by_limit:
        mix_parts.append(f"📘 лімітки ${by_limit:,.0f}")
    mix_line = f"🧩 Набрано: {' · '.join(mix_parts)}\n" if mix_parts else ""

    if prev_alerted_usd > 0:
        header = (f"🐋 <b>Кит збільшив позицію до ${total:,.0f}</b> "
                  f"(було ${prev_alerted_usd:,.0f})")
    elif count == 1:
        header = "🐋 <b>Велика ставка кита!</b>"
    else:
        header = f"🐋 <b>Кит набирає позицію · {count} угод за {span_min:.0f} хв</b>"

    trader_line = (f"👤 <a href=\"{esc(profile_url)}\">{trader}</a>"
                   if profile_url else f"👤 {trader}")

    return (
        f"{header}\n\n"
        f"📌 <b>{title}</b>\n"
        f"🎯 Ставка: <b>{outcome}</b> · середня ціна {avg_price * 100:.0f}¢\n"
        f"💰 <b>Позиція: ${total:,.0f}</b>" + (f" · {count} угод" if count > 1 else "") + "\n"
        f"{mix_line}"
        f"{trader_line}\n\n"
        f"{trades_text}\n\n"
        f"🔗 <a href=\"{esc(event_url)}\">Відкрити подію</a>"
    )


# -------------------------------- main -------------------------------------

def main():
    state = load_state()
    since = state['last_timestamp']
    stored_ts = state['stored_ts']
    positions = state['positions']
    window_end = int(time.time())
    agg_cutoff = window_end - AGG_WINDOW_SECONDS

    # Дублюючий тригер (зовнішній планувальник + розклад GitHub + ручний
    # запуск) — стан збережено щойно, робити нічого. Виходимо одразу, щоб не
    # займати concurrency-групу і не смикати API.
    if 0 < stored_ts and (window_end - stored_ts) < MIN_RUN_INTERVAL_SECONDS:
        print(f"Стан оновлено {window_end - stored_ts}с тому "
              f"(< {MIN_RUN_INTERVAL_SECONDS}с) — дублюючий запуск, виходимо")
        return

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

    # Визначаємо тип кожної угоди: ринкова (тейкер) чи лімітка (мейкер).
    # У відповіді API такого поля немає, тож беремо ту саму вибірку з
    # takerOnly=true: що потрапило туди — ринкове, решта — лімітки.
    # Робимо це ЛИШЕ якщо у вікні взагалі є нові угоди, щоб тихі запуски
    # не витрачали зайвий запит. Якщо не вдалося — не біда: сповіщення
    # піде без мітки типу, а не зникне.
    taker_uids = None
    if any(since < trade_ts(t) <= window_end for t in trades):
        try:
            taker_trades, _tc = get_trades_since(since, taker_only=True)
            taker_uids = {trade_uid(t) for t in taker_trades}
            print(f"Тип угод визначено (ринкових у вибірці: {len(taker_uids)})")
        except Exception as e:
            print(f"⚠️ Не вдалося визначити тип угод ({e}) — сповіщення буде без міток")

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
                'alerted_usd': 0,
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
            # market = ринковий ордер (тейкер), limit = лімітка (мейкер),
            # '' = визначити не вдалося.
            'kind': '' if taker_uids is None else ('market' if uid in taker_uids else 'limit'),
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

    # 2) Прибираємо угоди поза вікном; чистимо порожні позиції; міграція
    #    старого формату (alerted: bool -> alerted_usd: сума на момент сигналу).
    #    ВАЖЛИВО: alerted_usd НЕ скидається, коли сума тимчасово падає нижче
    #    порогу (старі угоди виходять з вікна) — інакше одна нова угода знову
    #    піднімала б суму над порогом і летів би дубль-сигнал про ту саму
    #    позицію. Нова «серія» починається лише коли позиція повністю зникла.
    stale_alerts = 0
    for key in list(positions.keys()):
        pos = positions[key]
        pos['trades'] = [tr for tr in pos['trades'] if tr.get('ts', 0) > agg_cutoff]
        if not pos['trades']:
            del positions[key]
            continue
        if 'alerted_usd' not in pos:
            pos['alerted_usd'] = pos_total(pos) if pos.pop('alerted', False) else 0
        pos.pop('alerted', None)
        # Сповіщення записане за НИЖЧОГО порога (напр. під час тестів) більше
        # не має сенсу: за нинішнім порогом воно б не відбулося. Такі позначки
        # лише тримають стан «просигналеним» і змушують коміт на кожен запуск.
        if 0 < float(pos.get('alerted_usd') or 0) < MIN_AMOUNT:
            pos['alerted_usd'] = 0
            stale_alerts += 1

    if stale_alerts:
        print(f"Очищено застарілих позначок про сповіщення: {stale_alerts} "
              f"(записані за нижчого порога)")

    # Страховка від патологічного розростання стану.
    if len(positions) > MAX_POSITIONS:
        keep = sorted(positions.items(), key=lambda kv: pos_total(kv[1]), reverse=True)
        dropped = len(positions) - MAX_POSITIONS
        positions = dict(keep[:MAX_POSITIONS])
        print(f"⚠️ Позицій забагато — лишаю топ-{MAX_POSITIONS} за обсягом, відкинуто {dropped}")

    # 3) Позиції для сигналу: перетнули поріг уперше, або зросли у
    #    ESCALATE_FACTOR разів від уже просигналеної суми.
    to_alert = []
    for key, pos in positions.items():
        total = pos_total(pos)
        prev = float(pos.get('alerted_usd') or 0)
        if total >= MIN_AMOUNT and (prev <= 0 or total >= prev * ESCALATE_FACTOR):
            to_alert.append((pos, total, prev))
    to_alert.sort(key=lambda x: x[1], reverse=True)
    print(f"Нових позицій ≥ ${MIN_AMOUNT:,.0f}: {len(to_alert)} · активних позицій у стані: {len(positions)}")

    sent = 0
    for pos, total, prev in to_alert[:MAX_MESSAGES]:
        if send_telegram(build_position_message(pos, total, window_end, prev)):
            pos['alerted_usd'] = total
            sent += 1
            print(f"✅ {pos.get('title', '')[:50]} · {pos.get('outcome')} · "
                  f"${total:,.0f} за {len(pos['trades'])} угод"
                  + (f" (ескалація з ${prev:,.0f})" if prev > 0 else ""))
        time.sleep(0.3)

    # Переповнення (дуже рідко): зведення, щоб нічого не «зникло тихо».
    overflow = len(to_alert) - MAX_MESSAGES
    if overflow > 0:
        extra = to_alert[MAX_MESSAGES:]
        extra_vol = sum(x[1] for x in extra)
        # Позначати позиції просигналеними можна ЛИШЕ якщо зведення дійшло.
        # Інакше вони мовчки випали б назавжди: до сповіщення не потрапили і в
        # to_alert більше не повертаються.
        if send_telegram(
            f"➕ <b>Ще {overflow} позицій ≥ ${MIN_AMOUNT:,.0f}</b>\n"
            f"💰 Сумарно: ${extra_vol:,.0f}\n"
            f"<i>(показано топ-{MAX_MESSAGES} за обсягом)</i>"
        ):
            for pos, total, _prev in extra:
                pos['alerted_usd'] = total
        else:
            print(f"⚠️ Зведення про {overflow} позицій не надіслано — "
                  f"не позначаємо їх, спробуємо наступного запуску")

    # --- Чи треба взагалі чіпати файл стану? ---
    # Обов'язково зберігаємо, якщо: стану ще немає; щойно надіслали сповіщення
    # або в стані є вже просигналені позиції (це єдине, що НЕ відновлюється
    # перечитуванням — інакше сповіщення продублюється); або настав час
    # «серцебиття». В усіх інших випадках файл не чіпаємо — тоді workflow не
    # робить коміт, а наступний запуск просто перечитає те саме вікно з того
    # самого last_timestamp і відновить позиції з угод.
    has_alerted = any(float(p.get('alerted_usd') or 0) > 0 for p in positions.values())
    heartbeat_due = (window_end - stored_ts) >= STATE_HEARTBEAT_SECONDS
    # stale_alerts: чистка сталася лише в пам'яті — без запису файл лишався б
    # засміченим назавжди, і кожен запуск чистив би те саме наново.
    must_save = ((stored_ts <= 0) or sent > 0 or has_alerted
                 or heartbeat_due or stale_alerts > 0)

    if not complete:
        # Неповне покриття: не просуваємо межу вікна, наступний запуск доробить.
        if must_save:
            save_state(stored_ts if stored_ts > 0 else since, positions)
        print("⚠️ Покриття було неповним — стан не просунуто, наступний запуск доробить вікно.")
    elif must_save:
        save_state(window_end, positions)
        why = ("сповіщення" if sent > 0 else
               "є просигналені позиції" if has_alerted else
               "серцебиття" if heartbeat_due else
               "чистка застарілих позначок" if stale_alerts else "перший запуск")
        print(f"Стан збережено ({why})")
    else:
        quiet_min = (window_end - stored_ts) / 60
        print(f"Стан не змінюємо (тихо {quiet_min:.0f} хв, наступне збереження "
              f"через ~{(STATE_HEARTBEAT_SECONDS - (window_end - stored_ts)) / 60:.0f} хв) "
              f"— коміт не потрібен")

    print(f"✅ Готово. Сповіщень надіслано: {sent}")


if __name__ == '__main__':
    main()
