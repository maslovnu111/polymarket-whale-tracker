"""Тести бота: усі сценарії, які трапляються на ринку Polymarket.

Мережі не торкаються — API і Telegram підмінені, годинник керований.
Запуск: python test_tracker.py
"""
import os
import sys
import json
import tempfile

os.environ.setdefault('TELEGRAM_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_CHAT_ID', '42')
os.environ.setdefault('MIN_AMOUNT', '1000000')
os.environ.setdefault('COMPONENT_MIN', '50000')
os.environ.setdefault('AGG_WINDOW_MINUTES', '60')

import tracker  # noqa: E402

NOW = 1_800_000_000
MIN = tracker.MIN_AMOUNT
WIN = tracker.AGG_WINDOW_SECONDS

_real_time = tracker.time
_failures = []


class FakeTime:
    """Годинник, який стоїть на місці, поки тест сам його не зрушить."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, _s):
        pass


def mk(ts, usd, wallet='0xwhale', asset='asset-yes', side='BUY',
       price=0.5, tx=None, title='Ринок', slug='event-slug',
       outcome='Yes', outcome_index='0', condition='0xcond', taker=True,
       name='Кит'):
    """Рядок угоди у форматі Polymarket Data API.

    usdcSize API не віддає (перевірено: 0 з 5027 рядків), тому суму бот
    рахує як size*price — так само робимо й тут.
    """
    return {
        'timestamp': ts,
        'size': usd / price,
        'price': price,
        'proxyWallet': wallet,
        'asset': asset,
        'side': side,
        'transactionHash': tx if tx is not None else f"0xtx{int(ts)}{int(usd)}",
        'title': title,
        'eventSlug': slug,
        'outcome': outcome,
        'outcomeIndex': outcome_index,
        'conditionId': condition,
        'name': name,
        '_taker': taker,
    }


def run(feed, now, state_file, taker_subset=None, with_digest=False,
        commands=None, incomplete=False):
    """Один запуск бота на синтетичній стрічці. Повертає надіслані тексти."""
    tracker.STATE_FILE = state_file
    tracker.time = FakeTime(now)
    sent = []

    def fake_send(msg):
        sent.append(msg)
        return True

    def fake_get(since, taker_only=False, side='BUY'):
        rows = [t for t in feed if str(t['side']).upper() == side]
        if taker_only:
            rows = [t for t in rows if t.get('_taker', True)] or rows[:1]
        return rows, not incomplete

    tracker.send_telegram = fake_send
    tracker.get_trades_since = fake_get
    tracker.maybe_daily_digest = (_real_digest if with_digest
                                  else (lambda *a, **k: False))
    tracker.handle_commands = (commands if commands is not None
                               else (lambda *a, **k: 0))
    try:
        tracker.main()
    finally:
        tracker.time = _real_time
        tracker.send_telegram = _real_send
        tracker.get_trades_since = _real_get
        tracker.maybe_daily_digest = _real_digest
        tracker.handle_commands = _real_commands
    return sent


_real_digest = tracker.maybe_daily_digest
_real_commands = tracker.handle_commands
_real_send = tracker.send_telegram
_real_get = tracker.get_trades_since


def fresh_state():
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    os.unlink(path)
    return path


def check(name, cond, detail=''):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        _failures.append(name)


def case(name):
    def deco(fn):
        print(f"\n{name}")
        try:
            fn()
        except Exception as e:
            import traceback
            print(f"  FAIL {name}: {e}")
            traceback.print_exc()
            _failures.append(name)
        return fn
    return deco


# ===========================================================================
# 1. Базові форми ставки кита
# ===========================================================================

@case("1. Одна велика купівля -> сигнал")
def _():
    st = fresh_state()
    sent = run([mk(NOW - 60, 1_500_000)], NOW, st)
    check("надіслано рівно 1", len(sent) == 1, sent)
    check("сума у тексті", '$1,500,000' in sent[0], sent[0][:120])
    check("це купівля", 'Велика ставка кита' in sent[0])


@case("2. Дроблення на шматки в межах вікна -> один сигнал на суму")
def _():
    st = fresh_state()
    feed = [mk(NOW - 3000 + i * 300, 210_000, tx=f"0xa{i}") for i in range(5)]
    sent = run(feed, NOW, st)
    check("надіслано 1", len(sent) == 1, len(sent))
    check("сума 1,050,000", '$1,050,000' in sent[0], sent[0][:200])
    check("видно 5 угод", '5 угод' in sent[0])


@case("3. Ті самі шматки, але розтягнуті ПОЗА вікном -> сигналу немає")
def _():
    st = fresh_state()
    # крок 50 хв: у будь-яке 60-хв вікно потрапляє максимум 2 шматки
    feed = [mk(NOW - 5 * 3000 + i * 3000, 210_000, tx=f"0xb{i}") for i in range(5)]
    sent = run(feed, NOW, st)
    check("сигналу немає", len(sent) == 0, sent)


@case("4. Накопичення через кілька запусків (стан переноситься)")
def _():
    st = fresh_state()
    all_sent = []
    # 4 запуски по 5 хв, у кожному по одному шматку $300k
    for i in range(4):
        t = NOW + i * 300
        feed = [mk(NOW + j * 300 - 60, 300_000, tx=f"0xc{j}") for j in range(i + 1)]
        all_sent += run(feed, t, st)
    check("рівно 1 сигнал за 4 запуски", len(all_sent) == 1, len(all_sent))
    check("сума 1,200,000", all_sent and '$1,200,000' in all_sent[0],
          all_sent[0][:200] if all_sent else '')


@case("5. Ескалація: позиція подвоїлась -> другий сигнал")
def _():
    st = fresh_state()
    s1 = run([mk(NOW - 600, 1_100_000, tx='0xd1')], NOW, st)
    s2 = run([mk(NOW - 600, 1_100_000, tx='0xd1'),
              mk(NOW + 240, 1_300_000, tx='0xd2')], NOW + 300, st)
    check("перший сигнал", len(s1) == 1, s1)
    check("другий сигнал (ескалація)", len(s2) == 1, s2)
    check("сказано «збільшив»", s2 and 'збільшив' in s2[0], s2[0][:120] if s2 else '')


@case("6. Зростання менше за ESCALATE_FACTOR -> дубля немає")
def _():
    st = fresh_state()
    run([mk(NOW - 600, 1_100_000, tx='0xe1')], NOW, st)
    s2 = run([mk(NOW - 600, 1_100_000, tx='0xe1'),
              mk(NOW + 240, 200_000, tx='0xe2')], NOW + 300, st)
    check("другого сигналу немає", len(s2) == 0, s2)


# ===========================================================================
# 2. Форми ринків Polymarket
# ===========================================================================

@case("7. Один кит на ДВА результати одного ринку -> не сумується")
def _():
    st = fresh_state()
    feed = [mk(NOW - 600, 600_000, asset='yes-token', outcome='Yes', outcome_index='0'),
            mk(NOW - 500, 600_000, asset='no-token', outcome='No', outcome_index='1')]
    sent = run(feed, NOW, st)
    check("сигналу немає (кожна позиція $600k)", len(sent) == 0, sent)


@case("8. Два РІЗНИХ кити на один результат -> не сумується")
def _():
    st = fresh_state()
    feed = [mk(NOW - 600, 600_000, wallet='0xaaa'),
            mk(NOW - 500, 600_000, wallet='0xbbb')]
    check("сигналу немає", len(run(feed, NOW, st)) == 0)


@case("9. Той самий кит купив і продав той самий актив -> не сумується")
def _():
    st = fresh_state()
    feed = [mk(NOW - 600, 600_000, side='BUY'),
            mk(NOW - 500, 600_000, side='SELL')]
    check("сигналу немає", len(run(feed, NOW, st)) == 0)


@case("10. Великий ПРОДАЖ -> окремий сигнал про вихід кита")
def _():
    st = fresh_state()
    sent = run([mk(NOW - 600, 2_400_000, side='SELL', outcome='Spain')], NOW, st)
    check("сигнал є", len(sent) == 1, sent)
    check("формулювання про скидання", sent and 'скидає' in sent[0],
          sent[0][:120] if sent else '')
    check("червона мітка", sent and '🔴' in sent[0])
    check("написано «Продано»", sent and 'Продано' in sent[0])


@case("11. Багатоваріантний ринок (не Yes/No): Spain / France")
def _():
    st = fresh_state()
    feed = [mk(NOW - 600, 1_200_000, asset='spain', outcome='Spain',
               title='Who wins the 2026 FIFA World Cup?', outcome_index='0')]
    sent = run(feed, NOW, st)
    check("сигнал є", len(sent) == 1)
    check("назва результату збережена", sent and 'Spain' in sent[0])


@case("12. Ринок Over/Under")
def _():
    st = fresh_state()
    feed = [mk(NOW - 600, 1_100_000, asset='over', outcome='Over',
               title='France vs. Spain: O/U 2.5', outcome_index='0')]
    sent = run(feed, NOW, st)
    check("сигнал є", len(sent) == 1)
    check("Over у тексті", sent and 'Over' in sent[0])


@case("13. outcomeIndex без поля outcome: 0=Yes, 1=No (перевірено на живих даних)")
def _():
    check("idx 0 -> Yes", tracker.outcome_of({'outcomeIndex': '0'}) == 'Yes',
          tracker.outcome_of({'outcomeIndex': '0'}))
    check("idx 1 -> No", tracker.outcome_of({'outcomeIndex': '1'}) == 'No',
          tracker.outcome_of({'outcomeIndex': '1'}))
    check("явний outcome має пріоритет",
          tracker.outcome_of({'outcome': 'Spain', 'outcomeIndex': '1'}) == 'Spain')
    check("нічого немає -> ?", tracker.outcome_of({}) == '?')


@case("14. Немає asset -> ключ по conditionId:outcomeIndex, позиції не зливаються")
def _():
    a = tracker.position_key({'proxyWallet': '0xw', 'asset': None,
                              'conditionId': '0xc', 'outcomeIndex': '0', 'side': 'BUY'})
    b = tracker.position_key({'proxyWallet': '0xw', 'asset': None,
                              'conditionId': '0xc', 'outcomeIndex': '1', 'side': 'BUY'})
    c = tracker.position_key({'proxyWallet': '0xw', 'asset': 'x', 'side': 'SELL'})
    d = tracker.position_key({'proxyWallet': '0xw', 'asset': 'x', 'side': 'BUY'})
    check("різні результати -> різні ключі", a != b, (a, b))
    check("різні сторони -> різні ключі", c != d, (c, d))
    check("регістр гаманця не важливий",
          tracker.position_key({'proxyWallet': '0xAB', 'asset': 'x', 'side': 'BUY'}) ==
          tracker.position_key({'proxyWallet': '0xab', 'asset': 'x', 'side': 'BUY'}))


@case("15. Немає eventSlug -> посилання не ламається")
def _():
    st = fresh_state()
    sent = run([mk(NOW - 600, 1_200_000, slug='')], NOW, st)
    check("сигнал є", len(sent) == 1)
    check("є запасне посилання", sent and 'https://polymarket.com' in sent[0])


# ===========================================================================
# 3. Особливості даних API
# ===========================================================================

@case("16. Та сама угода прийшла двічі (перечитування) -> рахується один раз")
def _():
    st = fresh_state()
    t = mk(NOW - 600, 1_200_000, tx='0xdup')
    sent = run([t, dict(t)], NOW, st)
    check("сигнал один", len(sent) == 1, len(sent))
    check("сума не задвоїлась", sent and '$1,200,000' in sent[0], sent[0][:200] if sent else '')


@case("17. Часткові виконання одного ордера (один tx, різні суми) -> сумуються")
def _():
    st = fresh_state()
    feed = [mk(NOW - 600, 400_000, tx='0xsame', price=0.4),
            mk(NOW - 599, 700_000, tx='0xsame', price=0.5)]
    sent = run(feed, NOW, st)
    check("сигнал є", len(sent) == 1, sent)
    check("сума 1,100,000", sent and '$1,100,000' in sent[0], sent[0][:200] if sent else '')


@case("18. Сума рахується як size*price, коли usdcSize відсутній")
def _():
    check("size*price", abs(tracker.calc_usd({'size': 160000, 'price': 0.9859429419})
                            - 157750.870704) < 0.01)
    check("usdcSize має пріоритет", tracker.calc_usd({'usdcSize': '5', 'size': 1, 'price': 1}) == 5.0)
    check("сміття -> 0", tracker.calc_usd({'size': 'x', 'price': None}) == 0.0)


@case("19. Крайні ціни: 0.1¢ і 99.9¢")
def _():
    st = fresh_state()
    s1 = run([mk(NOW - 600, 1_100_000, price=0.001, asset='longshot')], NOW, st)
    check("дешевий лонгшот проходить", len(s1) == 1, s1)
    st2 = fresh_state()
    s2 = run([mk(NOW - 600, 1_100_000, price=0.999, asset='almost-sure')], NOW, st2)
    check("майже вирішений ринок проходить", len(s2) == 1, s2)


@case("20. Межі вікна: рівно на межі і поза нею")
def _():
    st = fresh_state()
    lag = tracker.WINDOW_LAG_SECONDS
    # рівно window_end -> враховується; свіжіше -> ні (візьмемо наступного разу)
    s1 = run([mk(NOW - lag, 1_200_000, tx='0xedge1')], NOW, st)
    check("угода рівно на межі вікна врахована", len(s1) == 1, s1)
    st2 = fresh_state()
    s2 = run([mk(NOW - lag + 2, 1_200_000, tx='0xedge2')], NOW, st2)
    check("угода свіжіша за межу відкладена", len(s2) == 0, s2)


@case("21. Тип виконання: маркет vs лімітка, і чесне «не знаю»")
def _():
    st = fresh_state()
    feed = [mk(NOW - 600, 600_000, tx='0xm', taker=True),
            mk(NOW - 500, 600_000, tx='0xl', taker=False)]
    sent = run(feed, NOW, st)
    check("сигнал є", len(sent) == 1, sent)
    check("є мітка маркету", sent and '⚡ маркет' in sent[0], sent[0][:400] if sent else '')
    check("є мітка лімітки", sent and '📘 лімітка' in sent[0], sent[0][:400] if sent else '')
    # поза покриттям контрольної вибірки тип невідомий — і це чесніше за здогад
    check("старіше за покриття -> порожньо",
          tracker.classify_kind('u', 100, {'x'}, 200) == '')
    check("немає вибірки -> порожньо",
          tracker.classify_kind('u', 250, None, None) == '')
    check("у покритті і є в тейкерах -> market",
          tracker.classify_kind('u', 250, {'u'}, 200) == 'market')
    check("у покритті, немає в тейкерах -> limit",
          tracker.classify_kind('u', 250, {'x'}, 200) == 'limit')


# ===========================================================================
# 4. Стійкість: стан, помилки, Telegram
# ===========================================================================

@case("22. Порожня перша сторінка API -> стан не рухається")
def _():
    st = fresh_state()
    tracker.STATE_FILE = st
    tracker.time = FakeTime(NOW)
    tracker.send_telegram = lambda m: True
    tracker.maybe_daily_digest = lambda *a, **k: False
    tracker.handle_commands = lambda *a, **k: 0

    def boom(since, taker_only=False, side='BUY'):
        raise RuntimeError("API повернув порожню першу сторінку — підозріло")

    tracker.get_trades_since = boom
    try:
        tracker.main()
    finally:
        tracker.time = _real_time
        tracker.maybe_daily_digest = _real_digest
        tracker.handle_commands = _real_commands
    check("файл стану не створено", not os.path.exists(st))


@case("23. Неповне покриття -> межа вікна не просувається")
def _():
    st = fresh_state()
    run([mk(NOW - 600, 1_200_000)], NOW, st)          # створили стан
    before = json.load(open(st))['last_timestamp']
    run([mk(NOW + 200, 1_300_000, tx='0xz')], NOW + 300, st, incomplete=True)
    after = json.load(open(st))['last_timestamp']
    check("last_timestamp не зріс", after == before, (before, after))


@case("24. Старий формат стану мігрує (alerted: bool, немає side)")
def _():
    st = fresh_state()
    json.dump({'last_timestamp': NOW - 300, 'positions': {
        '0xwhale|asset-yes': {
            'wallet': '0xwhale', 'asset': 'asset-yes', 'title': 'Старий',
            'slug': 's', 'outcome': 'Yes', 'name': '', 'alerted': True,
            'trades': [{'ts': NOW - 400, 'usd': 1_200_000, 'price': 0.5,
                        'tx': '0xold', 'id': 'old-uid'}],
        }}}, open(st, 'w'))
    sent = run([], NOW, st)
    check("дубля по старій позиції немає", len(sent) == 0, sent)
    saved = json.load(open(st))['positions']
    pos = list(saved.values())[0]
    check("alerted -> alerted_usd", pos.get('alerted_usd') == 1_200_000, pos.get('alerted_usd'))
    check("side проставлено", pos.get('side') == 'BUY', pos.get('side'))
    check("ключа alerted більше немає", 'alerted' not in pos)


@case("25. Стан зберігає meta між запусками")
def _():
    st = fresh_state()
    run([mk(NOW - 600, 1_200_000)], NOW, st)
    meta = json.load(open(st)).get('meta', {})
    check("записано last_alert", bool(meta.get('last_alert')), meta)
    check("сума в last_alert", meta.get('last_alert', {}).get('usd') == 1_200_000)
    loaded = None
    tracker.STATE_FILE = st
    tracker.time = FakeTime(NOW + 60)
    try:
        loaded = tracker.load_state()
    finally:
        tracker.time = _real_time
    check("load_state віддає meta", bool(loaded['meta'].get('last_alert')))


@case("26. Telegram: 429 -> повтор, 400 -> plain text")
def _():
    calls = []

    def fake_http(url, params=None, payload=None, timeout=None):
        calls.append(payload)
        if len(calls) == 1:
            return 429, {'parameters': {'retry_after': 0}}
        return 200, {'ok': True}

    real_http, real_time = tracker.http_json, tracker.time
    tracker.http_json, tracker.time = fake_http, FakeTime(NOW)
    try:
        ok = tracker.send_telegram("привіт")
    finally:
        tracker.http_json, tracker.time = real_http, real_time
    check("429 -> зрештою надіслано", ok and len(calls) == 2, len(calls))

    calls2 = []

    def fake_http400(url, params=None, payload=None, timeout=None):
        calls2.append(payload)
        if len(calls2) == 1:
            return 400, {'description': "can't parse entities"}
        return 200, {'ok': True}

    tracker.http_json, tracker.time = fake_http400, FakeTime(NOW)
    try:
        ok2 = tracker.send_telegram("<b>жирний</b> &amp; текст")
    finally:
        tracker.http_json, tracker.time = real_http, real_time
    check("400 -> plain-text дійшов", ok2, ok2)
    check("розмітку прибрано", calls2[1]['text'] == "жирний & текст", calls2[1]['text'])
    check("parse_mode прибрано", 'parse_mode' not in calls2[1])


@case("27. Небезпечні символи в назві ринку екрануються")
def _():
    st = fresh_state()
    sent = run([mk(NOW - 600, 1_200_000, title='BTC <$95k? "тест" & <b>hack</b>')], NOW, st)
    check("сигнал є", len(sent) == 1)
    check("теги екрановані", sent and '<b>hack</b>' not in sent[0])
    check("є &lt;", sent and '&lt;' in sent[0], sent[0][:200] if sent else '')


@case("28. Дуже довга назва -> вкладаємось у ліміт Telegram")
def _():
    st = fresh_state()
    sent = run([mk(NOW - 600 - i, 200_000, tx=f"0xL{i}", title='Д' * 900)
                for i in range(8)], NOW, st)
    check("сигнал є", len(sent) == 1, len(sent))
    check("довжина <= 4096", sent and len(sent[0]) <= 4096, len(sent[0]) if sent else 0)
    check("посилання на подію збережено", sent and 'Відкрити подію' in sent[0])


@case("29. Щоденний підсумок: топ-3 угоди за добу")
def _():
    st = fresh_state()
    # DAILY_DIGEST_HOUR = 9 UTC; беремо момент після цієї години
    base = 1_800_000_000
    day = base - base % 86400 + 10 * 3600            # 10:00 UTC
    feed = [
        mk(day - 3600, 900_000, tx='0xd1', title='Найбільша подія', outcome='Yes'),
        mk(day - 7200, 600_000, tx='0xd2', title='Друга подія', side='SELL'),
        mk(day - 10800, 300_000, tx='0xd3', title='Третя подія'),
        mk(day - 14400, 100_000, tx='0xd4', title='Четверта подія'),
        mk(day - 200_000, 5_000_000, tx='0xd5', title='Позавчора — не рахується'),
    ]
    sent = run(feed, day, st, with_digest=True)
    check("підсумок надіслано", len(sent) == 1, len(sent))
    msg = sent[0] if sent else ''
    check("це підсумок доби", 'Підсумок доби' in msg, msg[:80])
    check("топ-1 є", '$900,000' in msg, msg[:400])
    check("топ-2 є", '$600,000' in msg)
    check("топ-3 є", '$300,000' in msg)
    check("четверта НЕ показана", '$100,000' not in msg)
    check("старіша за добу НЕ показана", '5,000,000' not in msg, msg[:400])
    check("продаж помічено", 'продаж' in msg)
    check("є лічильник за добу", 'Усього угод' in msg)
    check("є посилання на подію", 'polymarket.com/event' in msg)
    check("є посилання на трейд", 'polygonscan.com/tx' in msg)

    # вдруге того самого дня — не повторюється
    again = run(feed, day + 300, st, with_digest=True)
    check("вдруге за день не шле", len(again) == 0, again)
    # наступної доби — знову
    nxt = run([mk(day + 86400 - 600, 700_000, tx='0xd6')], day + 86400, st,
              with_digest=True)
    check("наступної доби шле знову", len(nxt) == 1, nxt)


@case("29б. Підсумок не йде до призначеної години і працює на порожній добі")
def _():
    st = fresh_state()
    base = 1_800_000_000
    early = base - base % 86400 + 3 * 3600           # 03:00 UTC < 9:00
    check("о 03:00 підсумку немає",
          len(run([mk(early - 600, 900_000, tx='0xe9')], early, st,
                  with_digest=True)) == 0)
    st2 = fresh_state()
    day = base - base % 86400 + 10 * 3600
    sent = run([mk(day - 200_000, 900_000, tx='0xe8')], day, st2, with_digest=True)
    check("порожня доба -> підсумок усе одно є", len(sent) == 1, len(sent))
    check("сказано, що угод не було",
          sent and 'не було жодної угоди' in sent[0], sent[0][:120] if sent else '')


@case("30. Команди Telegram: /status, /top, /help")
def _():
    st = fresh_state()
    run([mk(NOW - 600, 1_200_000)], NOW, st)
    state = json.load(open(st))
    positions = state['positions']
    meta = state['meta']

    replies = []
    real_send, real_http = tracker.send_telegram, tracker.http_json
    tracker.send_telegram = lambda m: (replies.append(m), True)[1]

    updates = [
        {'update_id': 1, 'message': {'chat': {'id': 42}, 'text': '/status'}},
        {'update_id': 2, 'message': {'chat': {'id': 42}, 'text': '/top'}},
        {'update_id': 3, 'message': {'chat': {'id': 42}, 'text': '/help'}},
        {'update_id': 4, 'message': {'chat': {'id': 999}, 'text': '/status'}},
        {'update_id': 5, 'message': {'chat': {'id': 42}, 'text': 'просто текст'}},
    ]
    tracker.http_json = lambda *a, **k: (200, {'ok': True, 'result': updates})
    try:
        n = tracker.handle_commands(meta, positions, [mk(NOW - 600, 1_200_000)], NOW, NOW - 300)
    finally:
        tracker.send_telegram, tracker.http_json = real_send, real_http

    check("оброблено 3 команди", n == 3, n)
    check("чужий чат проігноровано", len(replies) == 3, len(replies))
    check("/status містить налаштування", 'Стан бота' in replies[0], replies[0][:80])
    check("/status містить останній сигнал", 'Останній сигнал' in replies[0])
    check("/top показує позиції", 'Позиції у вікні' in replies[1], replies[1][:80])
    check("/help показує команди", '/status' in replies[2])
    check("offset просунуто", meta.get('tg_offset') == 6, meta.get('tg_offset'))


@case("31. /top і /status не падають на порожньому стані")
def _():
    top = tracker.build_top_message({}, NOW)
    status = tracker.build_status_message({}, [], {}, NOW, NOW - 300)
    check("/top на порожньому", 'немає жодної позиції' in top, top[:80])
    check("/status на порожньому", 'Стан бота' in status, status[:80])
    check("/status без сигналів", 'ще не було' in status)


@case("32. Купівлі і продажі не заважають одна одній в одному запуску")
def _():
    st = fresh_state()
    feed = [mk(NOW - 600, 1_200_000, wallet='0xbuyer', asset='yes', side='BUY'),
            mk(NOW - 500, 1_400_000, wallet='0xseller', asset='yes', side='SELL')]
    sent = run(feed, NOW, st)
    check("два окремі сигнали", len(sent) == 2, len(sent))
    joined = "\n".join(sent)
    check("є сигнал про набір", 'Велика ставка кита' in joined)
    check("є сигнал про вихід", 'скидає' in joined)


@case("33. Дублюючий тригер одразу після збереження -> миттєвий вихід")
def _():
    st = fresh_state()
    run([mk(NOW - 600, 1_200_000)], NOW, st)
    saved = json.load(open(st))['last_timestamp']
    sent = run([mk(NOW - 600, 1_200_000), mk(NOW + 5, 2_000_000, tx='0xnew')],
               NOW + 20, st)
    check("нічого не надіслано", len(sent) == 0, sent)
    check("стан не змінився", json.load(open(st))['last_timestamp'] == saved)


@case("34. Позиція вийшла з вікна, кит зайшов знову -> новий сигнал")
def _():
    st = fresh_state()
    s1 = run([mk(NOW - 600, 1_200_000, tx='0xr1')], NOW, st)
    # через 2 години стара угода поза вікном, нова — свіжа
    later = NOW + 2 * WIN
    s2 = run([mk(later - 600, 1_300_000, tx='0xr2')], later, st)
    check("перший сигнал", len(s1) == 1, s1)
    check("другий сигнал після нового заходу", len(s2) == 1, s2)



@case("35. Наздоганяння після простою: старі угоди не сумуються у одне вікно")
def _():
    st = fresh_state()
    # бот стояв 3 години; за цей час кит купував по $400k раз на годину
    json.dump({'last_timestamp': NOW - 3 * 3600, 'positions': {}, 'meta': {}},
              open(st, 'w'))
    feed = [mk(NOW - 3 * 3600 + 60, 400_000, tx='0xg1'),
            mk(NOW - 2 * 3600 + 60, 400_000, tx='0xg2'),
            mk(NOW - 600, 400_000, tx='0xg3')]
    sent = run(feed, NOW, st)
    check("три години НЕ склались у одну позицію", len(sent) == 0, sent)
    pos = list(json.load(open(st))['positions'].values())
    check("у вікні лишилась одна угода", pos and len(pos[0]['trades']) == 1,
          [len(p['trades']) for p in pos])


@case("36. Кит повернувся у ТОЙ САМИЙ ринок після паузи -> сигнал знову")
def _():
    st = fresh_state()
    s1 = run([mk(NOW - 600, 1_200_000, tx='0xh1')], NOW, st)
    mid = NOW + 2 * WIN
    s2 = run([mk(mid - 600, 1_300_000, tx='0xh2')], mid, st)
    late = mid + 2 * WIN
    s3 = run([mk(late - 600, 1_100_000, tx='0xh3')], late, st)
    check("сигнал 1", len(s1) == 1, s1)
    check("сигнал 2 (позиція встигла вийти з вікна)", len(s2) == 1, s2)
    check("сигнал 3", len(s3) == 1, s3)


print("=" * 70)
print(f"Провалено: {len(_failures)}")
for f in _failures:
    print(f"  - {f}")

@case("35. Наздоганяння після простою: старі угоди не сумуються у одне вікно")
def _():
    st = fresh_state()
    # бот стояв 3 години; за цей час кит купував по $400k раз на годину
    json.dump({'last_timestamp': NOW - 3 * 3600, 'positions': {}, 'meta': {}},
              open(st, 'w'))
    feed = [mk(NOW - 3 * 3600 + 60, 400_000, tx='0xg1'),
            mk(NOW - 2 * 3600 + 60, 400_000, tx='0xg2'),
            mk(NOW - 600, 400_000, tx='0xg3')]
    sent = run(feed, NOW, st)
    check("три години НЕ склались у одну позицію", len(sent) == 0, sent)
    pos = list(json.load(open(st))['positions'].values())
    check("у вікні лишилась одна угода", pos and len(pos[0]['trades']) == 1,
          [len(p['trades']) for p in pos])


@case("36. Кит повернувся у ТОЙ САМИЙ ринок після паузи -> сигнал знову")
def _():
    st = fresh_state()
    s1 = run([mk(NOW - 600, 1_200_000, tx='0xh1')], NOW, st)
    mid = NOW + 2 * WIN
    s2 = run([mk(mid - 600, 1_300_000, tx='0xh2')], mid, st)
    late = mid + 2 * WIN
    s3 = run([mk(late - 600, 1_100_000, tx='0xh3')], late, st)
    check("сигнал 1", len(s1) == 1, s1)
    check("сигнал 2 (позиція встигла вийти з вікна)", len(s2) == 1, s2)
    check("сигнал 3", len(s3) == 1, s3)


print("=" * 70)
sys.exit(1 if _failures else 0)
