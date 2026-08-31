"""
HMR Precise Alert - alert H-15 menit + catatan dampak (terpisah dari sistem mingguan)
"""

import os
import json
import datetime as dt

import requests

FF_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_PATH = os.path.join(os.path.dirname(__file__), "state_precise.json")

TELEGRAM_BOT_TOKEN = os.environ.get("HMR_TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("HMR_TG_CHAT_ID", "")

IMPACT_FILTER = {"High", "Medium"}
PRE_ALERT_MINUTES = 15
RESULT_CHECK_DELAY_MINUTES = 3

XAU_BIAS_NOTES = {
    "non-farm employment change": ("direct", "Data NFP kuat -> USD cenderung menguat -> XAUUSD tertekan."),
    "unemployment rate": ("inverse", "Unemployment naik dari ekspektasi -> USD melemah -> XAUUSD berpotensi naik."),
    "cpi": ("direct", "Inflasi (CPI) di atas forecast -> USD naik -> XAUUSD tertekan."),
    "core cpi": ("direct", "Core CPI di atas forecast -> USD naik -> XAUUSD tertekan."),
    "ppi": ("direct", "PPI tinggi -> efek serupa CPI ke USD/XAU."),
    "gdp": ("direct", "GDP di atas forecast -> USD naik -> XAUUSD tertekan."),
    "retail sales": ("direct", "Retail sales kuat -> USD naik -> XAUUSD tertekan."),
    "pce price index": ("direct", "PCE tinggi -> USD naik -> XAUUSD tertekan."),
    "ism manufacturing pmi": ("direct", "PMI di atas forecast -> USD naik -> XAUUSD tertekan."),
    "unemployment claims": ("inverse", "Jobless claims naik -> USD melemah -> XAUUSD berpotensi naik."),
    "average hourly earnings": ("direct", "Upah naik di atas forecast -> USD naik -> XAUUSD tertekan."),
}

NON_NUMERIC_EVENTS = ("fomc", "federal funds rate", "rate decision", "press conference", "speaks", "speech")


def get_bias_note(title: str) -> str:
    t = title.lower()
    for key, (_corr, note) in XAU_BIAS_NOTES.items():
        if key in t:
            return note
    return "Dampak ke XAUUSD tergantung besar deviasi actual vs forecast dan sentimen pasar saat itu."


def compute_directional_call(title: str, forecast: str, actual: str) -> str:
    t = title.lower()
    if any(k in t for k in NON_NUMERIC_EVENTS):
        return "⚠️ Event statement/kebijakan - dampak tergantung isi statement, bukan cuma angka."

    correlation = None
    for key, (corr, _note) in XAU_BIAS_NOTES.items():
        if key in t:
            correlation = corr
            break
    if correlation is None:
        return "Belum ada mapping arah - cek manual deviasi actual vs forecast."

    try:
        f_val = float(str(forecast).replace("%", "").replace("K", "").replace(",", ""))
        a_val = float(str(actual).replace("%", "").replace("K", "").replace(",", ""))
    except (ValueError, TypeError):
        return "Actual/forecast tidak berupa angka - cek manual."

    if a_val == f_val:
        return "➖ Actual sesuai forecast - dampak minim."

    beat = a_val > f_val
    xau_bearish = beat if correlation == "direct" else not beat

    return "🔻 Kemungkinan XAUUSD BEARISH" if xau_bearish else "🔺 Kemungkinan XAUUSD BULLISH"


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def fetch_calendar() -> list:
    resp = requests.get(FF_FEED_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Token/chat_id kosong:\n", text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        if not r.ok:
            print(f"[ERROR] Telegram gagal: {r.status_code} {r.text}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[ERROR] Telegram exception: {e}")
        return False


def main() -> None:
    state = load_state()
    raw_events = fetch_calendar()
    now = dt.datetime.now(dt.timezone.utc)

    for raw in raw_events:
        if raw.get("country") != "USD" or raw.get("impact") not in IMPACT_FILTER:
            continue

        raw_date = raw.get("date")
        if not raw_date:
            continue
        try:
            event_time = dt.datetime.fromisoformat(raw_date)
        except ValueError:
            continue

        eid = f"{raw.get('title')}_{event_time.isoformat()}"
        entry = state.get(eid, {
            "title": raw.get("title"),
            "impact": raw.get("impact"),
            "forecast": raw.get("forecast", ""),
            "previous": raw.get("previous", ""),
            "actual": raw.get("actual", ""),
            "pre_alert_sent": False,
            "result_alert_sent": False,
        })
        entry["forecast"] = raw.get("forecast", entry["forecast"])
        entry["previous"] = raw.get("previous", entry["previous"])
        entry["actual"] = raw.get("actual", entry["actual"])
        state[eid] = entry

        event_time_utc = event_time.astimezone(dt.timezone.utc)
        minutes_to_event = (event_time_utc - now).total_seconds() / 60.0
        minutes_since_event = -minutes_to_event

        if not entry["pre_alert_sent"] and -10 <= minutes_to_event <= PRE_ALERT_MINUTES:
            bias = get_bias_note(entry["title"])
            wib = event_time_utc + dt.timedelta(hours=7)
            timing = f"H-{int(minutes_to_event)} menit" if minutes_to_event >= 0 else f"(terlambat {int(-minutes_to_event)} menit)"
            msg = (
                f"⏰ <b>[{entry['impact']}] {timing}: {entry['title']}</b>\n"
                f"Jadwal: {wib.strftime('%Y-%m-%d %H:%M')} WIB\n"
                f"Forecast: {entry['forecast'] or '-'} | Previous: {entry['previous'] or '-'}\n\n"
                f"📌 {bias}"
            )
            if send_telegram(msg):
                entry["pre_alert_sent"] = True

        if not entry["result_alert_sent"] and minutes_since_event >= RESULT_CHECK_DELAY_MINUTES and entry["actual"]:
            call = compute_directional_call(entry["title"], entry["forecast"], entry["actual"])
            msg = (
                f"✅ <b>[{entry['impact']}] Hasil: {entry['title']}</b>\n"
                f"Actual: {entry['actual']} | Forecast: {entry['forecast'] or '-'} | Previous: {entry['previous'] or '-'}\n\n"
                f"{call}"
            )
            if send_telegram(msg):
                entry["result_alert_sent"] = True

    cutoff = now - dt.timedelta(days=10)
    state = {k: v for k, v in state.items() if dt.datetime.fromisoformat(v.get("_time", now.isoformat())) > cutoff} if False else state

    save_state(state)
    print(f"Done. {len(state)} event tersimpan.")


if __name__ == "__main__":
    main()
