"""
HMR News Alert - CLOUD version (single-run, untuk GitHub Actions)
==================================================================
Beda dari versi PC-lokal: script ini jalan SEKALI per invocation (bukan loop
tak berhenti), karena GitHub Actions men-trigger workflow tiap N menit lewat
cron lalu container dimatikan lagi. State (event mana yang sudah di-alert)
disimpan di file JSON `state.json` yang di-commit balik ke repo tiap run.

Alur tiap kali dijalankan:
  1. Fetch feed ForexFactory minggu ini
  2. Update/merge ke state.json (event baru masuk, actual value ter-update)
  3. Cek semua event yang belum di-pre-alert & sudah masuk window H-30 -> kirim
  4. Cek semua event yang belum di-result-alert & actual-nya sudah terisi -> kirim
  5. Simpan state.json (di-commit oleh workflow, bukan oleh script ini)

ENV VARS yang dibutuhkan (diisi lewat GitHub Actions Secrets):
  HMR_TG_TOKEN     - token bot Telegram
  HMR_TG_CHAT_ID   - chat id Telegram
"""

import os
import json
import datetime as dt
from typing import Optional

import requests

FF_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

TELEGRAM_BOT_TOKEN = os.environ.get("HMR_TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("HMR_TG_CHAT_ID", "")

IMPACT_FILTER = {"High"}
CURRENCY_FILTER = {"USD"}
PRE_ALERT_MINUTES = 30
RESULT_CHECK_DELAY_MINUTES = 3

XAU_BIAS_NOTES = {
    "non-farm employment change": ("direct", "Data NFP kuat -> USD cenderung menguat -> XAUUSD tertekan."),
    "unemployment rate": ("inverse", "Unemployment naik dari ekspektasi -> USD melemah -> XAUUSD berpotensi naik."),
    "cpi": ("direct", "Inflasi (CPI) di atas forecast -> The Fed lebih hawkish -> USD naik -> XAUUSD tertekan."),
    "core cpi": ("direct", "Core CPI di atas forecast -> USD naik -> XAUUSD tertekan."),
    "ppi": ("direct", "PPI tinggi -> leading indicator inflasi -> efek serupa CPI ke USD/XAU."),
    "gdp": ("direct", "GDP di atas forecast -> USD naik -> XAUUSD tertekan (efek biasanya moderat)."),
    "retail sales": ("direct", "Retail sales kuat -> USD naik -> XAUUSD tertekan."),
    "pce price index": ("direct", "PCE (indikator inflasi favorit The Fed) tinggi -> USD naik -> XAUUSD tertekan."),
    "ism manufacturing pmi": ("direct", "PMI di atas forecast -> USD naik -> XAUUSD tertekan."),
    "unemployment claims": ("inverse", "Initial jobless claims naik -> USD melemah -> XAUUSD berpotensi naik."),
}

# Event yang biasanya bukan angka "actual vs forecast" biasa (rate decision,
# statement) - tetap dikasih catatan tapi nggak dipaksakan hitung bearish/bullish otomatis.
NON_NUMERIC_EVENTS = ("fomc", "federal funds rate", "rate decision", "press conference", "speaks", "speech")


def compute_directional_call(title: str, forecast: str, actual: str) -> str:
    """Hasilkan kesimpulan BEARISH/BULLISH/netral untuk XAUUSD berdasarkan
    actual vs forecast + arah korelasi event tsb terhadap USD."""
    t = title.lower()
    if any(k in t for k in NON_NUMERIC_EVENTS):
        return "⚠️ Event kebijakan/statement - dampak tergantung isi statement (hawkish/dovish), bukan cuma angka."

    correlation = None
    for key, (corr, _note) in XAU_BIAS_NOTES.items():
        if key in t:
            correlation = corr
            break
    if correlation is None:
        return "Belum ada mapping arah untuk event ini - cek manual deviasi actual vs forecast."

    try:
        f_val = float(str(forecast).replace("%", "").replace("K", "").replace(",", ""))
        a_val = float(str(actual).replace("%", "").replace("K", "").replace(",", ""))
    except (ValueError, TypeError):
        return "Actual/forecast tidak berupa angka - cek manual."

    if a_val == f_val:
        return "➖ Actual sesuai forecast - dampak ke XAUUSD kemungkinan minim."

    beat = a_val > f_val
    if correlation == "direct":
        xau_bearish = beat
    else:
        xau_bearish = not beat

    if xau_bearish:
        return "🔻 Kemungkinan XAUUSD BEARISH (tekanan turun)"
    else:
        return "🔺 Kemungkinan XAUUSD BULLISH (potensi naik)"


def get_bias_note(title: str) -> str:
    t = title.lower()
    for key, (_corr, note) in XAU_BIAS_NOTES.items():
        if key in t:
            return note
    return "Dampak ke XAUUSD tergantung besar deviasi actual vs forecast dan sentimen risk-on/risk-off saat itu."


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


def parse_event_time(raw: dict) -> Optional[dt.datetime]:
    raw_date = raw.get("date")
    if not raw_date:
        return None
    try:
        return dt.datetime.fromisoformat(raw_date)
    except ValueError:
        return None


def make_event_id(raw: dict, event_time: dt.datetime) -> str:
    return f"{raw.get('country')}_{raw.get('title')}_{event_time.isoformat()}"


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Token/chat_id kosong, pesan hanya dicetak:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    if not r.ok:
        print(f"[ERROR] Telegram gagal: {r.status_code} {r.text}")


def main() -> None:
    state = load_state()
    raw_events = fetch_calendar()
    now = dt.datetime.now(dt.timezone.utc)

    for raw in raw_events:
        if raw.get("impact") not in IMPACT_FILTER:
            continue
        if raw.get("country") not in CURRENCY_FILTER:
            continue
        event_time = parse_event_time(raw)
        if event_time is None:
            continue

        eid = make_event_id(raw, event_time)
        entry = state.get(eid, {
            "title": raw.get("title"),
            "country": raw.get("country"),
            "event_time": event_time.isoformat(),
            "forecast": raw.get("forecast", ""),
            "previous": raw.get("previous", ""),
            "actual": raw.get("actual", ""),
            "pre_alert_sent": False,
            "result_alert_sent": False,
        })
        # refresh data terbaru (forecast/actual bisa berubah dari run sebelumnya)
        entry["forecast"] = raw.get("forecast", entry["forecast"])
        entry["previous"] = raw.get("previous", entry["previous"])
        entry["actual"] = raw.get("actual", entry["actual"])
        state[eid] = entry

        minutes_to_event = (event_time - now).total_seconds() / 60.0
        minutes_since_event = -minutes_to_event

        # --- Pre-alert H-30 menit ---
        if not entry["pre_alert_sent"] and 0 <= minutes_to_event <= PRE_ALERT_MINUTES:
            bias = get_bias_note(entry["title"])
            event_time_wib = event_time.astimezone(dt.timezone.utc) + dt.timedelta(hours=7)
            msg = (
                f"⏰ <b>H-{int(minutes_to_event)} menit: {entry['title']} ({entry['country']})</b>\n"
                f"Jadwal: {event_time_wib.strftime('%Y-%m-%d %H:%M')} WIB\n"
                f"Forecast: {entry['forecast'] or '-'} | Previous: {entry['previous'] or '-'}\n\n"
                f"📌 Catatan XAUUSD: {bias}"
            )
            send_telegram(msg)
            entry["pre_alert_sent"] = True

        # --- Result alert setelah rilis ---
        if (
            not entry["result_alert_sent"]
            and minutes_since_event >= RESULT_CHECK_DELAY_MINUTES
            and entry["actual"]
        ):
            surprise = "(tidak bisa dibandingkan otomatis, cek manual)"
            try:
                f_val = float(str(entry["forecast"]).replace("%", "").replace("K", "").replace(",", ""))
                a_val = float(str(entry["actual"]).replace("%", "").replace("K", "").replace(",", ""))
                if a_val > f_val:
                    surprise = "📈 Actual BEAT forecast"
                elif a_val < f_val:
                    surprise = "📉 Actual MISS forecast"
                else:
                    surprise = "➖ Actual sesuai forecast"
            except (ValueError, TypeError):
                pass

            directional_call = compute_directional_call(entry["title"], entry["forecast"], entry["actual"])

            msg = (
                f"✅ <b>Hasil: {entry['title']} ({entry['country']})</b>\n"
                f"Actual: {entry['actual']} | Forecast: {entry['forecast'] or '-'} | Previous: {entry['previous'] or '-'}\n"
                f"{surprise}\n\n"
                f"{directional_call}"
            )
            send_telegram(msg)
            entry["result_alert_sent"] = True

    # buang event lama (>10 hari) biar state.json nggak membengkak terus
    cutoff = now - dt.timedelta(days=10)
    state = {
        k: v for k, v in state.items()
        if dt.datetime.fromisoformat(v["event_time"]) > cutoff
    }

    save_state(state)
    print(f"Done. {len(state)} event tersimpan di state.")


if __name__ == "__main__":
    main()
