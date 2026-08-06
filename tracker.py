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

# Чи стежити також за ПРОДАЖАМИ (вихід кита з позиції).
# Первісна версія бота сигналила будь-яку угоду ≥ порога незалежно від
# сторони, і великі продажі туди потрапляли. Коли зʼявився фільтр side=BUY,
# вони зникли — а це реальні події (напр. 19-20.07: вихід зі Spain WC на
# $2.40M, $1.53M, $1.31M). Тому повертаємо їх окремим типом сигналу.
TRACK_SELLS = str(os.environ.get('TRACK_SELLS', '1')).strip().lower() not in ('0', 'false', 'no', 'off')
SIDES = ('BUY', 'SELL') if TRACK_SELLS else ('BUY',)

# Щоденний підсумок: топ-3 найбільші угоди за добу. Раз на добу, о цій
# годині UTC. Заразом показує, що бот живий — тиша на ринку інакше не
# відрізняється від поламаного бота (саме через це виникло питання
# «чому 6 днів немає сповіщень?»).
DAILY_DIGEST = str(os.environ.get('DAILY_DIGEST', '1')).strip().lower() not in ('0', 'false', 'no', 'off')
DAILY_DIGEST_HOUR = int(_clamp(_env_num('DAILY_DIGEST_HOUR', 9, float),
                               0, 23, 'DAILY_DIGEST_HOUR'))
# Скільки угод показувати у підсумку.
DIGEST_TOP = int(_clamp(_env_num('DIGEST_TOP', 3, float), 1, 10, 'DIGEST_TOP'))
# Чи відповідати на команди в Telegram (/status, /top, /help).
ENABLE_COMMANDS = str(os.environ.get('ENABLE_COMMANDS', '1')).strip().lower() not in ('0', 'false', 'no', 'off')

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

# Наскільки межа вікна відстає від «зараз» (сек).
#
# Обов'язково > 0. Часові мітки угод мають точність до СЕКУНДИ, а запит до API
# відбувається вже після того, як ми зафіксували window_end. Якщо взяти
# window_end = зараз, то угода, що сталася в ту саму секунду, але на частку
# секунди пізніше за наш запит, у відповідь не потрапить — а наступний запуск
# відкине її як стару (ts <= since). Вона зникає НАЗАВЖДИ.
# Замір: ~0.2% часу «сліпі», тобто при порозі $10k це ~3 втрачені угоди на добу.
# Відставання на кілька секунд закриває і це, і невелику затримку індексації
# на боці API. Ціна — сповіщення пізніше на ці ж кілька секунд.
WINDOW_LAG_SECONDS = int(_clamp(_env_num('WINDOW_LAG_SECONDS', 5, float),
                                1, 120, 'WINDOW_LAG_SECONDS'))

# Наскільки перечитувати НАЗАД за межу минулого запуску (сек).
#
# Відставання межі (вище) рятує від округлення до секунди, але не від затримки
# індексації на боці API: якщо угода зʼявляється в /trades пізніше, ніж ми
# зробили запит, наступний запуск відкине її як стару (ts <= since) — назавжди.
# Тому щоразу перечитуємо ще трохи назад. Задвоєння це створити НЕ може: кожна
# угода має uid, і вже враховані лежать у стані позиції — повторні просто
# відсіюються. Коштує це нічого: та сама одна сторінка.
RESCAN_OVERLAP_SECONDS = int(_clamp(_env_num('RESCAN_OVERLAP_SECONDS', 180, float),
                                    0, 3600, 'RESCAN_OVERLAP_SECONDS'))

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
    """Повертає {'last_timestamp': since, 'stored_ts': ts, 'positions': {...},
    'meta': {...}} з наздоганянням пропусків. `stored_ts` — те, що реально
    лежить у файлі (0 = стану немає); `since` — звідки читати цього разу.

    `meta` тримає дрібниці, які не відновлюються перечитуванням API:
    коли був останній сигнал, коли востаннє писали «я живий», і на якому
    update_id зупинилися при читанні команд Telegram."""
    now = int(time.time())
    ts = 0
    positions = {}
    meta = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            ts = int(data.get('last_timestamp', 0))
            positions = data.get('positions', {}) or {}
            if not isinstance(positions, dict):
                positions = {}
            meta = data.get('meta', {}) or {}
            if not isinstance(meta, dict):
                meta = {}
        except Exception as e:
            print(f"Не вдалося прочитати стан ({e}) — починаємо з чистого")
            ts, positions, meta = 0, {}, {}

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

    return {'last_timestamp': since, 'stored_ts': ts, 'positions': positions, 'meta': meta}


def save_state(timestamp, positions, meta=None):
    with open(STATE_FILE, 'w') as f:
        json.dump({'last_timestamp': int(timestamp), 'positions': positions,
                   'meta': meta or {}}, f)


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
    """Назва результату. Поле `outcome` є практично завжди і містить не лише
    Yes/No, а й «Spain», «Over», «Under» тощо — тому воно й пріоритетне.
    Запасний варіант — за індексом. Замір на 2384 живих рядках:
    outcomeIndex 0 -> 'Yes', 1 -> 'No' (раніше тут було навпаки)."""
    o = trade.get('outcome')
    if o:
        return str(o)
    idx = str(trade.get('outcomeIndex', ''))
    if idx == '0':
        return 'Yes'
    if idx == '1':
        return 'No'
    return '?'


def trade_side(trade):
    s = str(trade.get('side') or 'BUY').upper()
    return s if s in ('BUY', 'SELL') else 'BUY'


def position_key(trade):
    """Ключ позиції: гаманець + конкретний результат (asset) + сторона.

    Сторона обов'язкова в ключі: інакше кит, який за годину купив на $600k і
    продав на $600k того самого активу, виглядав би як позиція на $1.2M.
    Набір і скидання позиції — різні події, і сигналимо ми їх окремо."""
    wallet = (trade.get('proxyWallet') or '').lower()
    asset = trade.get('asset')
    if not asset:
        asset = f"{trade.get('conditionId')}:{trade.get('outcomeIndex')}"
    return f"{wallet}|{asset}|{trade_side(trade)}"


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

def _request_trades(offset, taker_only=False, side='BUY'):
    """Одна сторінка угод-кандидатів (CASH >= FILTER_AMOUNT) з ретраями.
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
        'side': side,                   # BUY = набір позиції, SELL = вихід
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


def get_trades_since(since_timestamp, taker_only=False, side='BUY'):
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
            data = _request_trades(offset, taker_only, side)
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

        print(f"{side}{'/taker' if taker_only else ''} offset={offset}: {len(data)} "
              f"угод-кандидатів (нових: {new_count}, всього: {len(all_trades)})")

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


def classify_kind(uid, ts, taker_uids, taker_from):
    """market = ринковий ордер (тейкер), limit = лімітка (мейкер),
    '' = визначити не вдалося.

    Перевіряємо лише НИЖНЮ межу покриття контрольної вибірки. Вона гортається
    тими самими сторінками і назад може сягати не так далеко, як основна —
    для старішої угоди її відсутність у тейкерах нічого не означає, і без цієї
    перевірки ринковий ордер отримав би мітку «лімітка».
    Верхня межа не потрібна: тейкерський запит виконується ПІСЛЯ основного,
    тож усе, що встигло потрапити в основну вибірку, він уже бачить.
    """
    if taker_uids is None or taker_from is None or ts < taker_from:
        return ''
    return 'market' if uid in taker_uids else 'limit'


def build_position_message(pos, total, now_ts, prev_alerted_usd=0):
    is_sell = str(pos.get('side') or 'BUY').upper() == 'SELL'
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
        base = f"{'🔴' if is_sell else '🟢'} <b>${tr['usd']:,.0f}</b> · {cents}"
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
        verb = "збільшив продаж до" if is_sell else "збільшив позицію до"
        header = (f"{'🔻' if is_sell else '🐋'} <b>Кит {verb} ${total:,.0f}</b> "
                  f"(було ${prev_alerted_usd:,.0f})")
    elif is_sell:
        header = ("🔻 <b>Кит скидає позицію!</b>" if count == 1 else
                  f"🔻 <b>Кит виходить з позиції · {count} угод за {span_min:.0f} хв</b>")
    elif count == 1:
        header = "🐋 <b>Велика ставка кита!</b>"
    else:
        header = f"🐋 <b>Кит набирає позицію · {count} угод за {span_min:.0f} хв</b>"

    trader_line = (f"👤 <a href=\"{esc(profile_url)}\">{trader}</a>"
                   if profile_url else f"👤 {trader}")

    return (
        f"{header}\n\n"
        f"📌 <b>{title}</b>\n"
        f"🎯 {'Продає' if is_sell else 'Ставка'}: <b>{outcome}</b> · "
        f"середня ціна {avg_price * 100:.0f}¢\n"
        f"💰 <b>{'Продано' if is_sell else 'Позиція'}: ${total:,.0f}</b>"
        + (f" · {count} угод" if count > 1 else "") + "\n"
        f"{mix_line}"
        f"{trader_line}\n\n"
        f"{trades_text}\n\n"
        f"🔗 <a href=\"{esc(event_url)}\">Відкрити подію</a>"
    )


# --------------------------- звіт про стан ----------------------------------

def describe_settings():
    return (f"поріг сигналу <b>${MIN_AMOUNT:,.0f}</b> · шматок від "
            f"<b>${COMPONENT_MIN:,.0f}</b> · вікно <b>{AGG_WINDOW_SECONDS // 60} хв</b> · "
            f"сторони: <b>{'купівлі + продажі' if TRACK_SELLS else 'лише купівлі'}</b>")


def build_status_message(positions, trades, meta, window_end, since):
    """Відповідь на /status: чи живий бот, що він зараз бачить і коли
    востаннє сигналив. Саме та інформація, якої бракує під час тиші."""
    lines = ["📊 <b>Стан бота</b>", "", f"⚙️ {describe_settings()}",
             f"🕒 Останній запуск: {format_time(window_end)} "
             f"(вікно з {format_time(since)})"]

    last = meta.get('last_alert') or {}
    if last.get('ts'):
        ago_h = max(0, (window_end - float(last['ts']))) / 3600
        lines.append(f"🔔 Останній сигнал: {format_time(last['ts'])} "
                     f"({ago_h:.0f} год тому) · ${float(last.get('usd') or 0):,.0f} · "
                     f"{esc(str(last.get('title') or '')[:60])}")
    else:
        lines.append("🔔 Сигналів у цьому стані ще не було")

    if positions:
        top = sorted(positions.values(), key=pos_total, reverse=True)[:3]
        lines.append(f"\n📈 Позицій у вікні: <b>{len(positions)}</b>. Найбільші:")
        for p in top:
            total = pos_total(p)
            mark = '🔴' if str(p.get('side')) == 'SELL' else '🟢'
            lines.append(f"  {mark} ${total:,.0f} ({len(p['trades'])} угод) · "
                         f"{esc(str(p.get('title') or '')[:44])} · "
                         f"{esc(str(p.get('outcome') or ''))}")
    else:
        lines.append(f"\n📈 У вікні {AGG_WINDOW_SECONDS // 60} хв зараз жодної позиції "
                     f"≥ ${COMPONENT_MIN:,.0f}")

    if trades:
        day_ago = window_end - 86400
        recent = [t for t in trades if trade_ts(t) > day_ago]
        lines.append(f"\n🔎 У вибірці API: {len(trades)} угод ≥ ${FILTER_AMOUNT:,.0f} "
                     f"({format_time(min(trade_ts(t) for t in trades))} .. "
                     f"{format_time(max(trade_ts(t) for t in trades))})")
        lines.append(f"   за останню добу з них: {len(recent)}")
        biggest = max(trades, key=calc_usd)
        lines.append(f"   найбільша: ${calc_usd(biggest):,.0f} "
                     f"({format_time(trade_ts(biggest))}) · "
                     f"{esc(str(biggest.get('title') or '')[:44])}")
        if recent:
            b24 = max(recent, key=calc_usd)
            lines.append(f"   найбільша за добу: ${calc_usd(b24):,.0f} · "
                         f"{esc(str(b24.get('title') or '')[:44])}")
    return "\n".join(lines)


def build_top_message(positions, window_end):
    """Відповідь на /top: усі позиції у вікні, навіть ті, що не дотягли
    до порогу. Видно, що бот бачить гроші, просто вони менші за поріг."""
    if not positions:
        return (f"📭 У вікні {AGG_WINDOW_SECONDS // 60} хв немає жодної позиції "
                f"≥ ${COMPONENT_MIN:,.0f}.\n{describe_settings()}")
    rows = sorted(positions.values(), key=pos_total, reverse=True)[:10]
    lines = [f"🏆 <b>Позиції у вікні {AGG_WINDOW_SECONDS // 60} хв</b> "
             f"(поріг сигналу ${MIN_AMOUNT:,.0f})", ""]
    for i, p in enumerate(rows, 1):
        total = pos_total(p)
        mark = '🔴' if str(p.get('side')) == 'SELL' else '🟢'
        pct = total / MIN_AMOUNT * 100
        lines.append(f"{i}. {mark} <b>${total:,.0f}</b> ({pct:.0f}% порога) · "
                     f"{len(p['trades'])} угод")
        lines.append(f"    {esc(str(p.get('title') or '')[:52])} · "
                     f"{esc(str(p.get('outcome') or ''))}")
    return "\n".join(lines)


HELP_TEXT = ("🤖 <b>Команди</b>\n\n"
             "/status — чи живий бот, налаштування, що він зараз бачить\n"
             "/top — топ позицій у поточному вікні (навіть нижче порога)\n"
             "/help — ця довідка\n\n"
             "<i>Відповідь приходить під час найближчого запуску (до 5 хв).</i>")


def handle_commands(meta, positions, trades, window_end, since):
    """Читає нові повідомлення в Telegram і відповідає на команди.

    Опитування (getUpdates) робимо раз на запуск — окремий процес для
    long-polling тут тримати ніде. Ціна питання: відповідь до 5 хв.
    `tg_offset` у стані гарантує, що одну команду не обробимо двічі.
    """
    if not ENABLE_COMMANDS:
        return 0
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {'timeout': 0, 'limit': 20, 'allowed_updates': '["message"]'}
    offset = int(meta.get('tg_offset') or 0)
    if offset:
        params['offset'] = offset
    try:
        status, data = http_json(url, params=params, timeout=10)
    except Exception as e:
        print(f"⚠️ Не вдалося прочитати команди ({e})")
        return 0
    if status != 200 or not isinstance(data, dict) or not data.get('ok'):
        # 409 = у бота стоїть webhook; опитування тоді неможливе, і це не збій.
        print(f"⚠️ getUpdates: {status} {str(data)[:120]}")
        return 0

    handled = 0
    for upd in data.get('result') or []:
        try:
            meta['tg_offset'] = int(upd.get('update_id', 0)) + 1
        except Exception:
            continue
        msg = upd.get('message') or {}
        # Відповідаємо лише у власний чат — щоб бот не обслуговував чужих.
        if str((msg.get('chat') or {}).get('id')) != str(TELEGRAM_CHAT_ID):
            continue
        text = str(msg.get('text') or '').strip().lower().split('@')[0]
        if text.startswith('/status'):
            send_telegram(build_status_message(positions, trades, meta, window_end, since))
        elif text.startswith('/top'):
            send_telegram(build_top_message(positions, window_end))
        elif text.startswith('/help') or text.startswith('/start'):
            send_telegram(HELP_TEXT)
        else:
            continue
        handled += 1
        print(f"↩️ Відповів на команду {text}")
    return handled


def build_digest_message(trades, window_end):
    """Щоденний підсумок: найбільші угоди за останні 24 години."""
    day_ago = window_end - 86400
    recent = [t for t in trades if day_ago < trade_ts(t) <= window_end]
    date = datetime.fromtimestamp(window_end, tz=timezone.utc).strftime('%d.%m')

    lines = [f"📅 <b>Підсумок доби · {date}</b>", ""]
    if not recent:
        lines.append(f"За 24 години не було жодної угоди ≥ ${COMPONENT_MIN:,.0f}.")
        lines.append(f"\n⚙️ {describe_settings()}")
        lines.append("<i>/status — подробиці, /top — позиції у вікні</i>")
        return "\n".join(lines)

    top = sorted(recent, key=calc_usd, reverse=True)[:DIGEST_TOP]
    lines.append(f"🏆 <b>Найбільші угоди за 24 год:</b>")
    for i, t in enumerate(top, 1):
        usd = calc_usd(t)
        is_sell = trade_side(t) == 'SELL'
        price = float(t.get('price') or 0)
        wallet = t.get('proxyWallet') or ''
        name = (t.get('name') or t.get('pseudonym') or '').strip()
        trader = esc(f"{name} ({short_wallet(wallet)})" if name
                     else (short_wallet(wallet) or 'Анонім'))
        slug = t.get('eventSlug') or t.get('slug') or ''
        tx = str(t.get('transactionHash') or '')

        head = (f"{i}. {'🔴' if is_sell else '🟢'} <b>${usd:,.0f}</b>"
                f"{' · продаж' if is_sell else ''} · {price * 100:.0f}¢ · "
                f"{format_time(trade_ts(t))}")
        title = esc(str(t.get('title') or 'Ринок'))
        if slug:
            title = (f"<a href=\"{POLY_EVENT}/{quote(str(slug), safe='-_/')}\">"
                     f"{title}</a>")
        links = []
        if wallet:
            links.append(f"<a href=\"{POLY_PROFILE}/{quote(str(wallet), safe='')}\">"
                         f"{trader}</a>")
        else:
            links.append(trader)
        if tx:
            links.append(f"<a href=\"{POLYGONSCAN_TX}/{quote(tx, safe='')}\">трейд</a>")

        lines.append(head)
        lines.append(f"    📌 {title} · 🎯 {esc(outcome_of(t))}")
        lines.append(f"    👤 {' · '.join(links)}")

    buys = [t for t in recent if trade_side(t) == 'BUY']
    sells = [t for t in recent if trade_side(t) == 'SELL']
    lines.append(f"\n📊 Усього угод ≥ ${COMPONENT_MIN:,.0f} за добу: <b>{len(recent)}</b> "
                 f"({len(buys)} купівель, {len(sells)} продажів) · "
                 f"обсяг ${sum(calc_usd(t) for t in recent):,.0f}")
    lines.append("<i>/status — подробиці, /top — позиції у вікні</i>")
    return "\n".join(lines)


def maybe_daily_digest(meta, trades, window_end):
    """Раз на добу (о DAILY_DIGEST_HOUR UTC) — підсумок із найбільшими угодами.

    Прив'язка до КАЛЕНДАРНОЇ доби, а не до «24 години з минулого разу»:
    інакше час підсумку щодня повзе вперед на тривалість запуску.
    """
    if not DAILY_DIGEST:
        return False
    now = datetime.fromtimestamp(window_end, tz=timezone.utc)
    today = now.strftime('%Y-%m-%d')
    if meta.get('last_digest_day') == today or now.hour < DAILY_DIGEST_HOUR:
        return False
    if send_telegram(build_digest_message(trades, window_end)):
        meta['last_digest_day'] = today
        print(f"📨 Надіслано підсумок доби за {today}")
        return True
    return False


# -------------------------------- main -------------------------------------

def main():
    state = load_state()
    since = state['last_timestamp']
    stored_ts = state['stored_ts']
    positions = state['positions']
    meta = state['meta']
    # Межа вікна свідомо відстає від «зараз» — див. WINDOW_LAG_SECONDS.
    window_end = int(time.time()) - WINDOW_LAG_SECONDS
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

    # Перечитуємо трохи назад за since — страховка від затримки індексації
    # на боці API. Дублі неможливі: угоди дедуплікуються за uid.
    scan_from = max(0, since - RESCAN_OVERLAP_SECONDS)

    # Купівлі — основа сигналу. Якщо їх не вдалося отримати, робити нічого:
    # виходимо без збереження стану, наступний запуск повторить те саме вікно.
    try:
        trades, complete = get_trades_since(scan_from, side='BUY')
    except Exception as e:
        print(f"❌ Помилка отримання угод: {e}. Стан не оновлюється, повторимо наступного разу.")
        return

    # Продажі — окремий запит. Їх провал не має знищувати вже здобуті купівлі:
    # позначаємо покриття неповним, і наступний запуск перечитає вікно.
    if TRACK_SELLS:
        try:
            sell_trades, sell_complete = get_trades_since(scan_from, side='SELL')
            trades += sell_trades
            complete = complete and sell_complete
        except Exception as e:
            print(f"⚠️ Не вдалося отримати продажі ({e}) — покриття неповне")
            complete = False

    print(f"Отримано угод-кандидатів: {len(trades)} (сторони: {', '.join(SIDES)})")

    # Діагностика: чи є взагалі кандидати у вікні, і чи не відкидаємо їх помилково.
    if trades:
        ts_all = [trade_ts(t) for t in trades]
        newest, oldest = max(ts_all), min(ts_all)
        in_window = sum(1 for ts in ts_all if since < ts <= window_end)
        in_overlap = sum(1 for ts in ts_all if scan_from < ts <= since)
        future = sum(1 for ts in ts_all if ts > window_end)
        before = sum(1 for ts in ts_all if ts <= since)
        biggest = max(trades, key=calc_usd)
        print(f"Діапазон кандидатів: {format_time(oldest)} .. {format_time(newest)} "
              f"(це найновіші {len(trades)} купівель ≥ ${FILTER_AMOUNT:,.0f})")
        print(f"У вікні ({format_time(since)}..зараз): {in_window} · "
              f"у перекритті (перечитано про запас): {in_overlap} · "
              f"до вікна: {before} · свіжіші за межу вікна (візьмемо наступного разу): {future}")
        print(f"Найбільша у вибірці: ${calc_usd(biggest):,.0f} о {format_time(trade_ts(biggest))}")
        if in_window == 0 and future == 0:
            print("→ У цьому вікні великих купівель не було (тихий період) — це нормально.")

    # Визначаємо тип кожної угоди: ринкова (тейкер) чи лімітка (мейкер).
    # У відповіді API такого поля немає, тож беремо ту саму вибірку з
    # takerOnly=true: що потрапило туди — ринкове, решта — лімітки.
    # Робимо це ЛИШЕ якщо у вікні взагалі є нові угоди, щоб тихі запуски
    # не витрачали зайвий запит. Якщо не вдалося — не біда: сповіщення
    # піде без мітки типу, а не зникне.
    # Класифікуємо ЛИШЕ той період, який контрольна вибірка реально покриває:
    # вона гортається тими самими сторінками і може сягати не так далеко назад.
    # Для угоди поза її діапазоном відсутність у ній нічого не означає — і без
    # цієї перевірки ринковий ордер отримав би мітку «лімітка».
    taker_uids = None
    taker_from = None
    fresh_sides = {trade_side(t) for t in trades if scan_from < trade_ts(t) <= window_end}
    if fresh_sides:
        try:
            taker_uids = set()
            spans = []
            for s in sorted(fresh_sides):
                tk, tk_complete = get_trades_since(scan_from, taker_only=True, side=s)
                if not tk or not tk_complete:
                    raise RuntimeError(f"вибірка {s}/taker неповна")
                taker_uids |= {trade_uid(t) for t in tk}
                spans.append((min(trade_ts(t) for t in tk),))
            taker_from = max(sp[0] for sp in spans)
            print(f"Тип угод визначено (ринкових у вибірці: {len(taker_uids)}, "
                  f"покриття з {format_time(taker_from)})")
        except Exception as e:
            taker_uids = None
            print(f"⚠️ Не вдалося визначити тип угод ({e}) — сповіщення буде без міток")

    # 1) СПОЧАТКУ прибираємо угоди поза вікном, і аж потім додаємо нові.
    #
    #    Порядок тут критичний. Позиція вважається завершеною, коли всі її
    #    угоди вийшли з вікна — тоді вона видаляється разом із позначкою
    #    alerted_usd, і наступний захід кита в той самий ринок сигналить
    #    заново. Якщо ж спершу додати нову угоду, позиція ніколи не буває
    #    порожньою: стара позначка живе далі й глушить сигнал доти, доки
    #    кит не подвоїть суму. Кит, що зайшов на $1.2M, пішов на дві години
    #    і повернувся на $1.3M, у такому разі не сигналив узагалі.
    #
    #    Тут же — міграція старого формату (alerted: bool -> alerted_usd).
    #    ВАЖЛИВО: alerted_usd НЕ скидається, коли сума тимчасово падає нижче
    #    порогу, а частина угод ще у вікні — інакше одна нова угода знову
    #    піднімала б суму над порогом і летів би дубль про ту саму позицію.
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
        # Записи, зроблені до появи продажів, сторони не мають — це купівлі.
        pos.setdefault('side', 'BUY')
        # Сповіщення записане за НИЖЧОГО порога (напр. під час тестів) більше
        # не має сенсу: за нинішнім порогом воно б не відбулося. Такі позначки
        # лише тримають стан «просигналеним» і змушують коміт на кожен запуск.
        if 0 < float(pos.get('alerted_usd') or 0) < MIN_AMOUNT:
            pos['alerted_usd'] = 0
            stale_alerts += 1

    if stale_alerts:
        print(f"Очищено застарілих позначок про сповіщення: {stale_alerts} "
              f"(записані за нижчого порога)")

    # 2) Додаємо нові угоди у відповідні позиції (гаманець + результат + сторона).
    added = 0
    for t in trades:
        ts = trade_ts(t)
        if ts <= scan_from or ts > window_end:
            continue
        # Чистка вікна відбулася ВИЩЕ, до цього циклу, тож угоду, старішу за
        # вікно агрегації, вже ніхто не прибере. А вона тут цілком можлива:
        # після довгого простою scan_from сягає далеко за agg_cutoff, і без
        # цієї перевірки наздоганяючий запуск підсумував би кількагодинну
        # історію як одну годинну позицію.
        if ts <= agg_cutoff:
            continue
        if trade_side(t) not in SIDES:
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
                'side': trade_side(t),
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
            'kind': classify_kind(uid, ts, taker_uids, taker_from),
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
            meta['last_alert'] = {
                'ts': window_end, 'usd': total,
                'title': (pos.get('title') or '')[:80],
                'side': pos.get('side', 'BUY'),
            }
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
    # Щоденний підсумок із найбільшими угодами за добу. Йде незалежно від
    # сигналів: це не «я живий», а окремий регулярний зріз ринку.
    digest_sent = maybe_daily_digest(meta, trades, window_end)

    # Команди з Telegram (/status, /top). Читаємо в кінці — щоб відповідь
    # містила вже актуальні позиції цього запуску.
    commands = handle_commands(meta, positions, trades, window_end, since)

    has_alerted = any(float(p.get('alerted_usd') or 0) > 0 for p in positions.values())
    heartbeat_due = (window_end - stored_ts) >= STATE_HEARTBEAT_SECONDS
    # stale_alerts: чистка сталася лише в пам'яті — без запису файл лишався б
    # засміченим назавжди, і кожен запуск чистив би те саме наново.
    # commands/digest: обидва пишуть у meta; без збереження бот відповів би на
    # ту саму команду ще раз і слав би підсумок щоп'ять хвилин.
    must_save = ((stored_ts <= 0) or sent > 0 or has_alerted
                 or heartbeat_due or stale_alerts > 0 or commands > 0 or digest_sent)

    if not complete:
        # Неповне покриття: не просуваємо межу вікна, наступний запуск доробить.
        if must_save:
            save_state(stored_ts if stored_ts > 0 else since, positions, meta)
        print("⚠️ Покриття було неповним — стан не просунуто, наступний запуск доробить вікно.")
    elif must_save:
        save_state(window_end, positions, meta)
        why = ("сповіщення" if sent > 0 else
               "команда в Telegram" if commands else
               "підсумок доби" if digest_sent else
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
