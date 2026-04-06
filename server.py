#!/usr/bin/env python3
"""
BIST %1 Sinyal Sistemi v4.0
"""
import sqlite3, json, os, threading, time, urllib.request, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta

try:
    import yfinance as yf
    VERI_MODU = "gercek"
except ImportError:
    VERI_MODU = "demo"

try:
    import anthropic as _anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

PORT = int(os.environ.get("PORT", 8765))
DB_PATH = "/data/bist.db" if os.path.isdir("/data") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "bist.db")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RAILWAY_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "satisfied-harmony-production-69fa.up.railway.app")

# Konuşma geçmişi (chat_id → mesaj listesi)
_konusma_gecmisi = {}
_ai_lock = threading.Lock()

BIST100 = [
    "THYAO","GARAN","AKBNK","YKBNK","EREGL","ASELS","SISE","KCHOL",
    "SAHOL","BIMAS","TCELL","TUPRS","PETKM","KRDMD","FROTO","TOASO",
    "ARCLK","TTKOM","ISCTR","VAKBN","HALKB","MGROS","OTKAR","PGSUS",
    "LOGO","ENKAI","EKGYO","TAVHL","AGHOL","DOHOL","ULKER","SASA",
    "ALARK","VESTL","AEFES","CCOLA","BRISA","AKSEN","ANACM","AVISA",
    "BAGFS","BANVT","BERA","BIZIM","BNTAS","BRYAT","BUCIM","BURCE",
    "CANTE","CEMAS","CIMSA","CLEBI","COKAS","DOAS","DEVA","DGATE",
    "ECILC","ECZYT","EGEEN","EMKEL","ENJSA","ERBOS","ERSU","EUPWR",
    "FENER","FLAP","FONET","FRIGO","GENIL","GENTS","GEREL","GEDZA",
    "GMTAS","GOLTS","GOZDE","GRSEL","GUBRF","HEKTS","INDES","IPEKE",
    "ISGYO","IZMDC","KARTN","KAYSE","KCHOL","KERVT","KLNMA","KNFRT",
    "KONYA","KORDS","KOZAA","KRDMD","KRONT","KTLEV","LIDFA","LKMNH",
    "MAVI","MERIT","MIATK","MIPAZ","MPARK","NETAS","NTHOL","ODAS",
    "ORGE","OYAKC","OZGYO","PAPIL","PARSN","PCILT","PEKMT","PENGD",
]
BIST100 = list(dict.fromkeys(BIST100))[:100]

# ── VERİTABANI ──────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sinyaller (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sembol TEXT, fiyat_giris REAL, hedef REAL, stop REAL,
        rsi REAL, hacim_carpan REAL, ema20 REAL, ema50 REAL,
        tarih TEXT, saat TEXT, durum TEXT DEFAULT 'BEKLIYOR',
        fiyat_cikis REAL, kar_zarar REAL, sonuc_saati TEXT,
        notlar TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS fiyat_cache (
        sembol TEXT PRIMARY KEY, fiyat REAL, onceki REAL,
        ema20 REAL, ema50 REAL, rsi REAL, hacim_carpan REAL,
        son_3_mum TEXT, degisim_15dk REAL, guncelleme TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS gunluk_sinyal_sayisi (
        sembol TEXT, tarih TEXT, strateji TEXT DEFAULT 'S1', sayisi INTEGER DEFAULT 0,
        PRIMARY KEY (sembol, tarih, strateji))""")
    con.commit()
    # Migration: strateji kolonu ekle
    try:
        c.execute("ALTER TABLE sinyaller ADD COLUMN strateji TEXT DEFAULT 'S1'")
        con.commit()
    except:
        pass
    con.close()

def get_db():
    return sqlite3.connect(DB_PATH)

# ── TEKNİK ANALİZ ────────────────────────────────────────────
def calc_ema(prices, period):
    if len(prices) < period:
        return [None] * len(prices)
    k = 2 / (period + 1)
    res = [None] * (period - 1)
    res.append(sum(prices[:period]) / period)
    for p in prices[period:]:
        res.append(p * k + res[-1] * (1 - k))
    return res

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period-1) + gains[i]) / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
    rs = avg_g / avg_l if avg_l else 100
    return round(100 - 100 / (1 + rs), 1)

def calc_vwap(closes, volumes):
    if len(closes) < 2:
        return closes[-1] if closes else 0
    total_pv = sum(c * v for c, v in zip(closes, volumes))
    total_v = sum(volumes)
    return total_pv / total_v if total_v else closes[-1]

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def calc_ssl(closes, highs, lows, period=10):
    if len(closes) < period + 2:
        return None, None
    ema_h = calc_ema(highs, period)
    ema_l = calc_ema(lows, period)
    hlv = [0] * len(closes)
    for i in range(period, len(closes)):
        if ema_h[i] is None or ema_l[i] is None:
            hlv[i] = hlv[i-1]
        elif closes[i] > ema_h[i]:
            hlv[i] = 1
        elif closes[i] < ema_l[i]:
            hlv[i] = -1
        else:
            hlv[i] = hlv[i-1]
    ssl_up, ssl_dn = [], []
    for i in range(len(closes)):
        if ema_h[i] is None or ema_l[i] is None:
            ssl_up.append(None); ssl_dn.append(None)
        elif hlv[i] < 0:
            ssl_up.append(ema_h[i]); ssl_dn.append(ema_l[i])
        else:
            ssl_up.append(ema_l[i]); ssl_dn.append(ema_h[i])
    return ssl_up, ssl_dn

def calc_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal:
        return 0
    e_fast = calc_ema(prices, fast)
    e_slow = calc_ema(prices, slow)
    macd_line = []
    for f, s in zip(e_fast, e_slow):
        if f and s:
            macd_line.append(f - s)
        else:
            macd_line.append(None)
    macd_vals = [v for v in macd_line if v is not None]
    if len(macd_vals) < signal:
        return 0
    signal_line = calc_ema(macd_vals, signal)
    sig_val = next((v for v in reversed(signal_line) if v), 0)
    macd_val = macd_vals[-1]
    return macd_val - sig_val  # histogram

def sinyal_kontrol(sembol, closes_15m, volumes_15m, closes_1d):
    if len(closes_15m) < 55 or len(volumes_15m) < 55:
        return None

    # EMA9, EMA21, EMA50
    e9  = calc_ema(closes_15m, 9)
    e21 = calc_ema(closes_15m, 21)
    e50 = calc_ema(closes_15m, 50)
    ema9  = next((v for v in reversed(e9)  if v), None)
    ema21 = next((v for v in reversed(e21) if v), None)
    ema50 = next((v for v in reversed(e50) if v), None)
    if not ema9 or not ema21 or not ema50:
        return None

    # RSI(14)
    rsi = calc_rsi(closes_15m)

    # VWAP (bugünkü veriler)
    vwap = calc_vwap(closes_15m[-30:], volumes_15m[-30:])

    # MACD histogram
    macd_hist = calc_macd(closes_15m)

    # Hacim (son 3 bar ortalaması / genel 20 bar ortalaması)
    vol_ort = sum(volumes_15m[-20:]) / 20
    vol_son3 = sum(volumes_15m[-3:]) / 3
    hacim_carpan = vol_son3 / vol_ort if vol_ort else 0

    # Son fiyat
    fiyat = closes_15m[-1]

    # 15dk degisim
    degisim_15dk = (closes_15m[-1] - closes_15m[-2]) / closes_15m[-2] * 100 if len(closes_15m) >= 2 else 0

    # Son 2 mum yesil
    mumlar_yesil = sum(1 for i in [-2, -1] if closes_15m[i] > closes_15m[i-1])

    # Gunluk trend
    gunluk_yukari = True
    if len(closes_1d) >= 50:
        e20_d = calc_ema(closes_1d, 20)
        e50_d = calc_ema(closes_1d, 50)
        gema20 = next((v for v in reversed(e20_d) if v), None)
        gema50 = next((v for v in reversed(e50_d) if v), None)
        if gema20 and gema50:
            gunluk_yukari = gema20 > gema50

    # SAAT KONTROLU
    # Türkiye saati (UTC+3) - Railway UTC'de çalışır
    turkey_now = datetime.utcnow() + timedelta(hours=3)
    saat = turkey_now.hour * 60 + turkey_now.minute
    # Açılış 30dk beklenir, 12:00-13:30 ölü bölge kapalı, kapanış 17:40
    pencere1 = (10 * 60 + 30) <= saat <= (12 * 60 + 0)   # 10:30-12:00
    pencere2 = (13 * 60 + 30) <= saat <= (17 * 60 + 40)  # 13:30-17:40
    piyasa_acik = pencere1 or pencere2

    # KRİTERLER
    kriter1 = fiyat > vwap                            # VWAP uzerinde
    kriter2 = ema9 > ema21 > ema50                    # EMA9 > EMA21 > EMA50 (güçlü trend)
    kriter3 = rsi >= 50                               # RSI 50 üstü (trend filtresi)
    kriter4 = macd_hist > 0                           # MACD histogram pozitif
    kriter5 = hacim_carpan >= 1.5                     # Güçlü hacim (1.5x)
    kriter6 = mumlar_yesil >= 2                       # Son 2 mum yesil
    kriter8 = gunluk_yukari                           # Gunluk trend yukari
    kriter9 = piyasa_acik                             # Piyasa saatleri (ölü bölge hariç)

    tumu = kriter1 and kriter2 and kriter3 and kriter4 and kriter5 and kriter6 and kriter8 and kriter9

    return {
        "sinyal": tumu,
        "fiyat": round(fiyat, 4),
        "vwap": round(vwap, 4),
        "ema9": round(ema9, 4),
        "ema21": round(ema21, 4),
        "ema50": round(ema50, 4),
        "rsi": rsi,
        "macd_hist": round(macd_hist, 4),
        "hacim_carpan": round(hacim_carpan, 2),
        "degisim_15dk": round(degisim_15dk, 2),
        "hedef": round(fiyat * 1.02, 4),
        "stop": round(fiyat * 0.99, 4),
        "kriterler": {
            "vwap": kriter1, "ema_stack": kriter2, "rsi": kriter3,
            "macd": kriter4, "hacim": kriter5, "momentum": kriter6,
            "gunluk": kriter8, "saat": kriter9
        }
    }

def sinyal_kontrol_ssl(sembol, closes, highs, lows, volumes, closes_1d, strateji):
    """SSL Hybrid sinyal — S2 (1h) ve S3 (4h) için"""
    min_bar = 60
    if len(closes) < min_bar or len(highs) < min_bar:
        return None

    # Baseline: EMA50
    ema50 = calc_ema(closes, 50)
    baseline = next((v for v in reversed(ema50) if v), None)
    if not baseline:
        return None

    # SSL Channel (period=10)
    ssl_up, ssl_dn = calc_ssl(closes, highs, lows, period=10)
    if ssl_up is None or len(ssl_up) < 2:
        return None
    prev_up, prev_dn = ssl_up[-2], ssl_dn[-2]
    curr_up, curr_dn = ssl_up[-1], ssl_dn[-1]
    if None in [prev_up, prev_dn, curr_up, curr_dn]:
        return None

    # Crossover: önceki bar down>up iken şimdi up>down
    ssl_crossover = (prev_up <= prev_dn) and (curr_up > curr_dn)

    # ATR stop
    atr = calc_atr(highs, lows, closes, period=14)
    fiyat = closes[-1]
    multiplier = 1.5 if strateji == 'S2' else 2.0
    atr_stop = round(fiyat - atr * multiplier, 4) if atr else round(fiyat * 0.985, 4)

    # RSI
    rsi = calc_rsi(closes)

    # Hacim
    vol_ort = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
    hacim_carpan = volumes[-1] / vol_ort if vol_ort else 0

    # Günlük trend
    gunluk_yukari = True
    if len(closes_1d) >= 50:
        e20_d = calc_ema(closes_1d, 20)
        e50_d = calc_ema(closes_1d, 50)
        gema20 = next((v for v in reversed(e20_d) if v), None)
        gema50 = next((v for v in reversed(e50_d) if v), None)
        if gema20 and gema50:
            gunluk_yukari = gema20 > gema50

    # Saat kontrolü
    turkey_now = datetime.utcnow() + timedelta(hours=3)
    saat = turkey_now.hour * 60 + turkey_now.minute
    pencere1 = (10 * 60 + 30) <= saat <= (12 * 60 + 0)
    pencere2 = (13 * 60 + 30) <= saat <= (17 * 60 + 40)
    piyasa_acik = pencere1 or pencere2

    k1 = fiyat > baseline
    k2 = ssl_crossover
    k3 = rsi >= 50
    k4 = hacim_carpan >= 1.2
    k5 = gunluk_yukari
    k6 = piyasa_acik

    tumu = k1 and k2 and k3 and k4 and k5 and k6

    hedef_pct = 1.03 if strateji == 'S2' else 1.04
    return {
        "sinyal": tumu,
        "fiyat": round(fiyat, 4),
        "rsi": rsi,
        "hacim_carpan": round(hacim_carpan, 2),
        "degisim_15dk": 0,
        "hedef": round(fiyat * hedef_pct, 4),
        "stop": atr_stop,
        "ema9": round(curr_up, 4),
        "ema50": round(baseline, 4),
        "kriterler": {
            "baseline": k1, "ssl_cross": k2, "rsi": k3,
            "hacim": k4, "gunluk": k5, "saat": k6
        }
    }

# ── VERİ ÇEKME ───────────────────────────────────────────────
_cache = {}
_lock = threading.Lock()

def _parse_ticker_df(df_all, ticker, tek):
    """MultiIndex yf.download çıktısından tek ticker DataFrame'ini çıkarır."""
    if tek:
        return df_all
    try:
        lvl0 = df_all.columns.get_level_values(0)
        if ticker not in lvl0:
            return None
        df = df_all[ticker].dropna(how="all")
        return df if len(df) > 0 else None
    except:
        return None

def veri_cek_toplu():
    """Tüm hisseleri yf.download() ile tek seferde çeker — rate limit'i önler."""
    try:
        semboller = [s + ".IS" for s in BIST100]
        tek = len(semboller) == 1

        print("  Toplu veri indiriliyor (15m)...")
        df_15m_all = yf.download(semboller, period="5d",  interval="15m", group_by="ticker", auto_adjust=True, progress=False)
        print("  Toplu veri indiriliyor (1h)...")
        df_1h_all  = yf.download(semboller, period="60d", interval="1h",  group_by="ticker", auto_adjust=True, progress=False)
        print("  Toplu veri indiriliyor (1d)...")
        df_1d_all  = yf.download(semboller, period="3mo", interval="1d",  group_by="ticker", auto_adjust=True, progress=False)

        sonuc = {}
        for sembol in BIST100:
            try:
                ticker = sembol + ".IS"
                df_15m = _parse_ticker_df(df_15m_all, ticker, tek)
                df_1h  = _parse_ticker_df(df_1h_all,  ticker, tek)
                df_1d  = _parse_ticker_df(df_1d_all,  ticker, tek)

                if df_15m is None or len(df_15m) < 25:
                    continue

                d1_closes = [float(x) for x in df_1d["Close"].tolist()] if df_1d is not None and len(df_1d) > 0 else []

                s1 = {
                    "closes":  [float(x) for x in df_15m["Close"].tolist()],
                    "volumes": [int(x)   for x in df_15m["Volume"].tolist()],
                }

                s2 = None
                if df_1h is not None and len(df_1h) >= 60:
                    s2 = {
                        "closes":  [float(x) for x in df_1h["Close"].tolist()],
                        "highs":   [float(x) for x in df_1h["High"].tolist()],
                        "lows":    [float(x) for x in df_1h["Low"].tolist()],
                        "volumes": [int(x)   for x in df_1h["Volume"].tolist()],
                    }

                s3 = None
                if df_1h is not None and len(df_1h) >= 32:
                    try:
                        df_4h = df_1h.copy()
                        if df_4h.index.tz:
                            df_4h.index = df_4h.index.tz_convert("UTC").tz_localize(None)
                        df_4h = df_4h.resample("4h").agg({
                            "Open": "first", "High": "max", "Low": "min",
                            "Close": "last", "Volume": "sum"
                        }).dropna()
                        if len(df_4h) >= 30:
                            s3 = {
                                "closes":  [float(x) for x in df_4h["Close"].tolist()],
                                "highs":   [float(x) for x in df_4h["High"].tolist()],
                                "lows":    [float(x) for x in df_4h["Low"].tolist()],
                                "volumes": [int(x)   for x in df_4h["Volume"].tolist()],
                            }
                    except:
                        pass

                sonuc[sembol] = {"s1": s1, "s2": s2, "s3": s3, "d1": d1_closes}
            except:
                pass

        return sonuc
    except Exception as e:
        print(f"  Toplu veri hatası: {e}")
        return {}

def veri_cek(sembol):
    """Tek hisse için yedek fonksiyon (artık kullanılmıyor)."""
    return None

def veri_cek_demo(sembol):
    import random
    random.seed(abs(hash(sembol)) % 9999)
    base = random.uniform(10, 500)

    def gen_bars(n, step=1.0):
        closes, highs, lows, vols = [], [], [], []
        f = base * step
        for _ in range(n):
            f *= random.uniform(0.995, 1.008)
            h = f * random.uniform(1.001, 1.005)
            l = f * random.uniform(0.995, 0.999)
            closes.append(round(f, 4))
            highs.append(round(h, 4))
            lows.append(round(l, 4))
            vols.append(int(random.uniform(100000, 2000000)))
        for i in range(-3, 0):
            closes[i] = closes[i-1] * random.uniform(1.001, 1.005)
            highs[i]  = closes[i] * 1.003
        return closes, highs, lows, vols

    c15, h15, l15, v15 = gen_bars(80)
    c1h, h1h, l1h, v1h = gen_bars(80, 1.0)
    c4h, h4h, l4h, v4h = gen_bars(60, 1.0)
    d1c = [base * random.uniform(0.95, 1.05) for _ in range(60)]

    return {
        "s1": {"closes": c15, "volumes": v15},
        "s2": {"closes": c1h, "highs": h1h, "lows": l1h, "volumes": v1h},
        "s3": {"closes": c4h, "highs": h4h, "lows": l4h, "volumes": v4h},
        "d1": d1c,
    }

# ── TELEGRAM ─────────────────────────────────────────────────
def telegram_gonder(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] {mesaj}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Telegram hatası: {e}")

def telegram_gonder_chat(mesaj, chat_id):
    if not TELEGRAM_TOKEN:
        return
    # Telegram mesaj limiti 4096 karakter, uzunsa böl
    for i in range(0, len(mesaj), 4000):
        parca = mesaj[i:i+4000]
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = json.dumps({"chat_id": chat_id, "text": parca, "parse_mode": "HTML"}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"Telegram chat hatası: {e}")

def telegram_webhook_ayarla():
    if not TELEGRAM_TOKEN:
        return
    try:
        webhook_url = f"https://{RAILWAY_URL}/telegram_webhook"
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
        data = json.dumps({"url": webhook_url, "drop_pending_updates": True}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        print(f"Telegram webhook ayarlandı: {webhook_url} → {result.get('description', '')}")
    except Exception as e:
        print(f"Webhook ayarlama hatası: {e}")

# ── AI ASİSTAN ────────────────────────────────────────────────
def ai_cevap(mesaj: str, chat_id: str):
    if not HAS_ANTHROPIC or not ANTHROPIC_API_KEY:
        telegram_gonder_chat("⚠️ AI devre dışı (ANTHROPIC_API_KEY eksik).", chat_id)
        return

    now = datetime.utcnow() + timedelta(hours=3)
    bugun = now.strftime("%Y-%m-%d")

    # Güncel cache bilgisi
    with _lock:
        sinyal_cache = list(_cache.values())
    aktif = [s for s in sinyal_cache if s.get("sinyal")]

    # DB: son sinyaller + istatistik
    con = get_db()
    son30 = con.execute(
        "SELECT sembol, fiyat_giris, hedef, stop, rsi, hacim_carpan, tarih, saat, durum, kar_zarar "
        "FROM sinyaller ORDER BY id DESC LIMIT 30"
    ).fetchall()
    bugun_rows = con.execute(
        "SELECT durum, COUNT(*), SUM(kar_zarar) FROM sinyaller WHERE tarih=? GROUP BY durum", (bugun,)
    ).fetchall()
    genel_rows = con.execute(
        "SELECT durum, COUNT(*), SUM(kar_zarar) FROM sinyaller GROUP BY durum"
    ).fetchall()
    con.close()

    bugun_ozet = {r[0]: (r[1], r[2] or 0) for r in bugun_rows}
    genel_ozet = {r[0]: (r[1], r[2] or 0) for r in genel_rows}

    sistem_prompt = f"""Sen BIST (Borsa İstanbul) uzmanı bir AI borsa asistanısın. Kullanıcı seninle Telegram'dan konuşuyor.

📅 Şu an: {now.strftime('%d.%m.%Y %H:%M')} (Türkiye saati)

🔴 CANLI AKTİF SİNYALLER ({len(aktif)} adet):
{chr(10).join([f"• {s['sembol']}: {s['fiyat']:.2f} TL | RSI:{s['rsi']} | Hacim:x{s['hacim_carpan']}" for s in aktif]) or '• Şu an aktif sinyal yok'}

📊 BUGÜN ({bugun}):
• KAR: {bugun_ozet.get('KAR', (0,0))[0]} sinyal ({bugun_ozet.get('KAR', (0,0))[1]:.0f} TL)
• ZARAR: {bugun_ozet.get('ZARAR', (0,0))[0]} sinyal ({bugun_ozet.get('ZARAR', (0,0))[1]:.0f} TL)
• BEKLIYOR: {bugun_ozet.get('BEKLIYOR', (0,0))[0]} sinyal

📈 GENEL İSTATİSTİK (tüm zamanlar):
• KAR: {genel_ozet.get('KAR', (0,0))[0]} sinyal ({genel_ozet.get('KAR', (0,0))[1]:.0f} TL)
• ZARAR: {genel_ozet.get('ZARAR', (0,0))[0]} sinyal ({genel_ozet.get('ZARAR', (0,0))[1]:.0f} TL)

📋 SON 30 SİNYAL:
{chr(10).join([f"• {r[0]} {r[6]} {r[7]}: Giriş {r[1]:.2f} TL | RSI:{r[4]:.0f} | {r[8]}{' | '+str(round(r[9],0))+' TL' if r[9] else ''}" for r in son30]) or '• Henüz sinyal yok'}

⚙️ SİSTEM KRİTERLERİ:
• VWAP üstü fiyat
• EMA9 > EMA21 > EMA50 (trend stack)
• RSI ≥ 50
• MACD histogram pozitif
• Hacim x1.5 ortalamanın üstü
• Son 2 mum yeşil
• Günlük trend yukarı
• Piyasa saatleri: 10:30–12:00 ve 13:30–17:40
• Kâr hedefi: %2 | Stop-loss: %1 | Max 2 sinyal/hisse/gün

Kullanıcı sana her türlü borsa sorusu sorabilir. Hisse analizi, yorum, strateji, teknik analiz — hepsini Türkçe, net ve akıcı cevapla. Emoji kullan. Gereksiz uzatma ama kapsamlı ol."""

    with _ai_lock:
        if chat_id not in _konusma_gecmisi:
            _konusma_gecmisi[chat_id] = []
        _konusma_gecmisi[chat_id].append({"role": "user", "content": mesaj})
        gecmis = list(_konusma_gecmisi[chat_id][-20:])  # Son 20 mesaj

    try:
        client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1500,
            system=sistem_prompt,
            messages=gecmis,
        )
        cevap = response.content[0].text

        with _ai_lock:
            _konusma_gecmisi[chat_id].append({"role": "assistant", "content": cevap})

        telegram_gonder_chat(cevap, chat_id)
    except Exception as e:
        print(f"AI hatası: {e}")
        telegram_gonder_chat(f"⚠️ AI hatası: {e}", chat_id)

# ── SİNYAL KAYDET ────────────────────────────────────────────
def sinyal_kaydet(sembol, sonuc, strateji='S1'):
    now = datetime.utcnow() + timedelta(hours=3)
    tarih = now.strftime("%Y-%m-%d")
    saat  = now.strftime("%H:%M")

    # Günlük limit: max 2 sinyal/hisse/strateji
    con = get_db()
    row = con.execute(
        "SELECT sayisi FROM gunluk_sinyal_sayisi WHERE sembol=? AND tarih=? AND strateji=?",
        (sembol, tarih, strateji)).fetchone()
    sayisi = row[0] if row else 0
    if sayisi >= 2:
        con.close()
        return False

    con.execute("INSERT OR REPLACE INTO gunluk_sinyal_sayisi VALUES (?,?,?,?)",
                (sembol, tarih, strateji, sayisi + 1))
    con.execute("""INSERT INTO sinyaller
        (sembol, fiyat_giris, hedef, stop, rsi, hacim_carpan, ema20, ema50,
         tarih, saat, durum, strateji)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sembol, sonuc["fiyat"], sonuc["hedef"], sonuc["stop"],
         sonuc["rsi"], sonuc["hacim_carpan"], sonuc.get("ema9", 0), sonuc.get("ema50", 0),
         tarih, saat, "BEKLIYOR", strateji))
    con.commit()
    con.close()

    etiket = {"S1": "📊 S1 · 15dk", "S2": "📈 S2 · 1sa", "S3": "🕐 S3 · 4sa"}.get(strateji, strateji)
    hedef_pct = {"S1": "+%2", "S2": "+%3", "S3": "+%4"}.get(strateji, "")
    msg = (f"🟢 <b>{sembol}</b> — AL SİNYALİ  [{etiket}]\n"
           f"💰 Fiyat: {sonuc['fiyat']:.2f} TL\n"
           f"🎯 Hedef: {sonuc['hedef']:.2f} TL ({hedef_pct})\n"
           f"🛡 Stop: {sonuc['stop']:.2f} TL\n"
           f"📊 RSI: {sonuc['rsi']} | Hacim: x{sonuc['hacim_carpan']}\n"
           f"🕐 {saat}")
    telegram_gonder(msg)
    return True

# ── SİNYAL SONUÇ TAKİBİ ─────────────────────────────────────
def sonuc_guncelle():
    con = get_db()
    bekleyenler = con.execute(
        "SELECT id, sembol, fiyat_giris, hedef, stop FROM sinyaller WHERE durum='BEKLIYOR'"
    ).fetchall()
    con.close()

    for row in bekleyenler:
        sid, sembol, giris, hedef, stop = row
        with _lock:
            c = _cache.get(sembol, {})
        fiyat = c.get("fiyat")
        if not fiyat:
            continue

        now = (datetime.utcnow() + timedelta(hours=3)).strftime("%H:%M")
        if fiyat >= hedef:
            kar = round((hedef - giris) / giris * 100000, 2)
            con = get_db()
            con.execute("UPDATE sinyaller SET durum='KAR', fiyat_cikis=?, kar_zarar=?, sonuc_saati=? WHERE id=?",
                (hedef, kar, now, sid))
            con.commit(); con.close()
            telegram_gonder(f"✅ <b>{sembol}</b> HEDEF TUTTU! +{kar:.0f} TL kâr 🎉")
        elif fiyat <= stop:
            zarar = round((stop - giris) / giris * 100000, 2)
            con = get_db()
            con.execute("UPDATE sinyaller SET durum='ZARAR', fiyat_cikis=?, kar_zarar=?, sonuc_saati=? WHERE id=?",
                (stop, zarar, now, sid))
            con.commit(); con.close()
            telegram_gonder(f"🔴 <b>{sembol}</b> STOP ÇALIŞTI! {zarar:.0f} TL zarar")

# ── TARAMA DÖNGÜSÜ ───────────────────────────────────────────
son_sinyal = {}  # key: "SEMBOL_S1" gibi

def tara():
    global son_sinyal
    now_str = (datetime.utcnow() + timedelta(hours=3)).strftime('%H:%M:%S')
    print(f"[{now_str}] Tarama başlıyor...")
    veri_sayisi = sinyal_sayisi = hata_sayisi = 0

    # Gerçek modda tüm hisseleri tek seferde toplu indir
    if VERI_MODU == "gercek":
        tum_veriler = veri_cek_toplu()
        print(f"  Toplu indirme tamamlandı: {len(tum_veriler)} hisse verisi alındı")
    else:
        tum_veriler = {}

    for sembol in BIST100:
        try:
            veri = tum_veriler.get(sembol) if VERI_MODU == "gercek" else veri_cek_demo(sembol)
            if not veri:
                continue
            veri_sayisi += 1

            d1 = veri["d1"]

            # ── S1: 15 dakika ──────────────────────────────
            s1 = veri.get("s1")
            if s1:
                sonuc1 = sinyal_kontrol(sembol, s1["closes"], s1["volumes"], d1)
                if sonuc1:
                    with _lock:
                        _cache[f"{sembol}_S1"] = {
                            "sembol": sembol, "strateji": "S1",
                            "fiyat": sonuc1["fiyat"], "rsi": sonuc1["rsi"],
                            "hacim_carpan": sonuc1["hacim_carpan"],
                            "degisim_15dk": sonuc1["degisim_15dk"],
                            "sinyal": sonuc1["sinyal"],
                            "hedef": sonuc1["hedef"], "stop": sonuc1["stop"],
                        }
                    if sonuc1["sinyal"]:
                        key = f"{sembol}_S1"
                        with _lock:
                            son = son_sinyal.get(key)
                            now_t = time.time()
                            gonder = not son or (now_t - son) > 900
                            if gonder: son_sinyal[key] = now_t
                        if gonder and sinyal_kaydet(sembol, sonuc1, "S1"):
                            print(f"  🟢 S1 SİNYAL: {sembol} @ {sonuc1['fiyat']}")
                            sinyal_sayisi += 1

            # ── S2: 1 saat ─────────────────────────────────
            s2 = veri.get("s2")
            if s2:
                sonuc2 = sinyal_kontrol_ssl(sembol, s2["closes"], s2["highs"], s2["lows"], s2["volumes"], d1, "S2")
                if sonuc2:
                    with _lock:
                        _cache[f"{sembol}_S2"] = {
                            "sembol": sembol, "strateji": "S2",
                            "fiyat": sonuc2["fiyat"], "rsi": sonuc2["rsi"],
                            "hacim_carpan": sonuc2["hacim_carpan"],
                            "degisim_15dk": 0, "sinyal": sonuc2["sinyal"],
                            "hedef": sonuc2["hedef"], "stop": sonuc2["stop"],
                        }
                    if sonuc2["sinyal"]:
                        key = f"{sembol}_S2"
                        with _lock:
                            son = son_sinyal.get(key)
                            now_t = time.time()
                            gonder = not son or (now_t - son) > 3600  # 1sa cooldown
                            if gonder: son_sinyal[key] = now_t
                        if gonder and sinyal_kaydet(sembol, sonuc2, "S2"):
                            print(f"  🔵 S2 SİNYAL: {sembol} @ {sonuc2['fiyat']}")
                            sinyal_sayisi += 1

            # ── S3: 4 saat ─────────────────────────────────
            s3 = veri.get("s3")
            if s3:
                sonuc3 = sinyal_kontrol_ssl(sembol, s3["closes"], s3["highs"], s3["lows"], s3["volumes"], d1, "S3")
                if sonuc3:
                    with _lock:
                        _cache[f"{sembol}_S3"] = {
                            "sembol": sembol, "strateji": "S3",
                            "fiyat": sonuc3["fiyat"], "rsi": sonuc3["rsi"],
                            "hacim_carpan": sonuc3["hacim_carpan"],
                            "degisim_15dk": 0, "sinyal": sonuc3["sinyal"],
                            "hedef": sonuc3["hedef"], "stop": sonuc3["stop"],
                        }
                    if sonuc3["sinyal"]:
                        key = f"{sembol}_S3"
                        with _lock:
                            son = son_sinyal.get(key)
                            now_t = time.time()
                            gonder = not son or (now_t - son) > 14400  # 4sa cooldown
                            if gonder: son_sinyal[key] = now_t
                        if gonder and sinyal_kaydet(sembol, sonuc3, "S3"):
                            print(f"  🟣 S3 SİNYAL: {sembol} @ {sonuc3['fiyat']}")
                            sinyal_sayisi += 1

        except Exception as e:
            hata_sayisi += 1
            if hata_sayisi <= 3:
                print(f"  HATA {sembol}: {e}")

    sonuc_guncelle()
    now_str2 = (datetime.utcnow() + timedelta(hours=3)).strftime('%H:%M:%S')
    print(f"[{now_str2}] Tarama bitti. Veri:{veri_sayisi} | Sinyal:{sinyal_sayisi} | Hata:{hata_sayisi}")

def tarama_dongusu():
    while True:
        tara()
        time.sleep(300)  # Her 5 dakikada bir tara

# ── HTTP SUNUCU ───────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/telegram_webhook":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                update = json.loads(body)
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = (message.get("text") or "").strip()
                if text and chat_id:
                    if text == "/start":
                        telegram_gonder_chat(
                            "👋 Merhaba! Ben BIST AI Asistanıyım 🤖📈\n\n"
                            "Sana şunları yapabilirim:\n"
                            "• Hisse analizi ve yorum\n"
                            "• Güncel sinyalleri açıklama\n"
                            "• Teknik analiz soruları\n"
                            "• Strateji ve risk değerlendirmesi\n"
                            "• Genel borsa yorumu\n\n"
                            "Ne sormak istiyorsun? ✍️", chat_id)
                    else:
                        threading.Thread(target=ai_cevap, args=(text, chat_id), daemon=True).start()
            except Exception as e:
                print(f"Webhook parse hatası: {e}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        elif p.path == "/api/ai":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
                mesaj = payload.get("mesaj", "").strip()
                chat_id = payload.get("chat_id", "web_kullanici")
                if not mesaj:
                    self.send_json({"hata": "mesaj boş"}, 400)
                    return
                # AI'yı çağır ve cevabı HTTP yanıtı olarak dön
                if not HAS_ANTHROPIC or not ANTHROPIC_API_KEY:
                    self.send_json({"cevap": "AI devre dışı (API anahtarı eksik)."})
                    return

                now = datetime.utcnow() + timedelta(hours=3)
                bugun = now.strftime("%Y-%m-%d")
                with _lock:
                    sinyal_cache = list(_cache.values())
                aktif = [s for s in sinyal_cache if s.get("sinyal")]

                con = get_db()
                son30 = con.execute(
                    "SELECT sembol, fiyat_giris, hedef, stop, rsi, hacim_carpan, tarih, saat, durum, kar_zarar "
                    "FROM sinyaller ORDER BY id DESC LIMIT 30"
                ).fetchall()
                bugun_rows = con.execute(
                    "SELECT durum, COUNT(*), SUM(kar_zarar) FROM sinyaller WHERE tarih=? GROUP BY durum", (bugun,)
                ).fetchall()
                genel_rows = con.execute(
                    "SELECT durum, COUNT(*), SUM(kar_zarar) FROM sinyaller GROUP BY durum"
                ).fetchall()
                con.close()

                bugun_ozet = {r[0]: (r[1], r[2] or 0) for r in bugun_rows}
                genel_ozet = {r[0]: (r[1], r[2] or 0) for r in genel_rows}

                sistem_prompt = f"""Sen BIST (Borsa İstanbul) uzmanı bir AI borsa asistanısın.

📅 Şu an: {now.strftime('%d.%m.%Y %H:%M')} (Türkiye saati)

🔴 AKTİF SİNYALLER ({len(aktif)} adet):
{chr(10).join([f"• {s['sembol']}: {s['fiyat']:.2f} TL | RSI:{s['rsi']} | Hacim:x{s['hacim_carpan']}" for s in aktif]) or '• Şu an aktif sinyal yok'}

📊 BUGÜN: KAR:{bugun_ozet.get('KAR',(0,0))[0]} | ZARAR:{bugun_ozet.get('ZARAR',(0,0))[0]} | BEKLIYOR:{bugun_ozet.get('BEKLIYOR',(0,0))[0]}
📈 GENEL: KAR:{genel_ozet.get('KAR',(0,0))[0]} sinyal | ZARAR:{genel_ozet.get('ZARAR',(0,0))[0]} sinyal

📋 SON 30 SİNYAL:
{chr(10).join([f"• {r[0]} {r[6]}: {r[1]:.2f} TL RSI:{r[4]:.0f} → {r[8]}{' '+str(round(r[9],0))+' TL' if r[9] else ''}" for r in son30]) or '• Henüz sinyal yok'}

Kullanıcı sana her türlü borsa sorusu sorabilir. Türkçe, net ve kapsamlı cevap ver. Emoji kullanabilirsin."""

                with _ai_lock:
                    if chat_id not in _konusma_gecmisi:
                        _konusma_gecmisi[chat_id] = []
                    _konusma_gecmisi[chat_id].append({"role": "user", "content": mesaj})
                    gecmis = list(_konusma_gecmisi[chat_id][-20:])

                client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                response = client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=1500,
                    system=sistem_prompt,
                    messages=gecmis,
                )
                cevap = response.content[0].text

                with _ai_lock:
                    _konusma_gecmisi[chat_id].append({"role": "assistant", "content": cevap})

                self.send_json({"cevap": cevap})
            except Exception as e:
                print(f"API AI hatası: {e}")
                self.send_json({"hata": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        p = urlparse(self.path)
        path = p.path
        params = parse_qs(p.query)

        if path in ("/", "/index.html"):
            self.serve_file()
            return
        elif path == "/manifest.json":
            self.serve_static("manifest.json", "application/json"); return
        elif path == "/sw.js":
            self.serve_static("sw.js", "application/javascript"); return
        elif path in ("/icon-192.png", "/icon-512.png"):
            self.serve_icon(); return

        elif path == "/api/canli":
            with _lock:
                tumu = list(_cache.values())
            sinyaller = [v for v in tumu if v.get("sinyal")]
            self.send_json({
                "sinyaller": sinyaller,
                "tarama_sayisi": len(set(v["sembol"] for v in tumu)),
                "sinyal_sayisi": len(sinyaller),
                "zaman": (datetime.utcnow() + timedelta(hours=3)).strftime("%H:%M:%S"),
                "mod": VERI_MODU
            })

        elif path == "/api/sinyaller":
            tarih = params.get("tarih", [""])[0]
            durum = params.get("durum", [""])[0]
            sembol = params.get("sembol", [""])[0]
            strateji = params.get("strateji", [""])[0]
            limit = int(params.get("limit", ["100"])[0])

            q = "SELECT * FROM sinyaller WHERE 1=1"
            args = []
            if tarih: q += " AND tarih=?"; args.append(tarih)
            if durum: q += " AND durum=?"; args.append(durum)
            if sembol: q += " AND sembol=?"; args.append(sembol.upper())
            if strateji: q += " AND strateji=?"; args.append(strateji.upper())
            q += " ORDER BY id DESC LIMIT ?"
            args.append(limit)

            con = get_db()
            rows = con.execute(q, args).fetchall()
            cols = ["id","sembol","fiyat_giris","hedef","stop","rsi","hacim_carpan",
                    "ema20","ema50","tarih","saat","durum","fiyat_cikis","kar_zarar","sonuc_saati","notlar"]
            sinyaller = [dict(zip(cols, r)) for r in rows]

            # Özet istatistikler
            kar_list   = [s["kar_zarar"] for s in sinyaller if s["durum"] == "KAR"]
            zarar_list = [s["kar_zarar"] for s in sinyaller if s["durum"] == "ZARAR"]
            toplam_kar = sum(kar_list)
            toplam_zarar = sum(zarar_list)
            basari_oran = len(kar_list) / (len(kar_list) + len(zarar_list)) * 100 if (kar_list or zarar_list) else 0

            con.close()
            self.send_json({
                "sinyaller": sinyaller,
                "ozet": {
                    "toplam": len(sinyaller),
                    "kar_sayisi": len(kar_list),
                    "zarar_sayisi": len(zarar_list),
                    "bekliyor": sum(1 for s in sinyaller if s["durum"] == "BEKLIYOR"),
                    "toplam_kar": round(toplam_kar, 2),
                    "toplam_zarar": round(toplam_zarar, 2),
                    "net": round(toplam_kar + toplam_zarar, 2),
                    "basari_oran": round(basari_oran, 1),
                }
            })

        elif path == "/api/ozet":
            con = get_db()
            bugun = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d")
            bugun_sinyaller = con.execute(
                "SELECT durum, kar_zarar FROM sinyaller WHERE tarih=?", (bugun,)).fetchall()
            con.close()
            kar   = [r[1] for r in bugun_sinyaller if r[0] == "KAR"]
            zarar = [r[1] for r in bugun_sinyaller if r[0] == "ZARAR"]
            self.send_json({
                "bugun_sinyal": len(bugun_sinyaller),
                "bugun_kar": len(kar),
                "bugun_zarar": len(zarar),
                "bugun_net": round(sum(kar) + sum(zarar), 2),
                "mod": VERI_MODU,
                "zaman": (datetime.utcnow() + timedelta(hours=3)).strftime("%H:%M:%S"),
            })

        elif path == "/api/telegram_ayarla":
            token = params.get("token", [""])[0]
            chat_id = params.get("chat_id", [""])[0]
            if token and chat_id:
                global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
                TELEGRAM_TOKEN = token
                TELEGRAM_CHAT_ID = chat_id
                telegram_gonder("✅ BIST Sinyal Sistemi bağlandı! Sinyaller buraya gelecek.")
                self.send_json({"ok": True})
            else:
                self.send_json({"hata": "token ve chat_id gerekli"})

        elif path == "/api/tara_simdi":
            threading.Thread(target=tara, daemon=True).start()
            self.send_json({"ok": True, "mesaj": "Tarama başlatıldı"})

        elif path == "/api/sifirla":
            con = get_db()
            con.execute("DELETE FROM sinyaller")
            con.execute("DELETE FROM gunluk_sinyal_sayisi")
            con.commit(); con.close()
            with _lock: _cache.clear()
            self.send_json({"ok": True, "mesaj": "Tüm sinyaller silindi"})

        else:
            self.send_response(404); self.end_headers()

    def serve_static(self, filename, content_type):
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, "static", filename)
        if not os.path.exists(path):
            path = os.path.join(base, filename)
        with open(path, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def serve_icon(self):
        import base64
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAABmJLR0QA/wD/AP+gvaeTAAAA"
            "GklEQVR42mNkYGBg+M9AgAEAAAD//wMABQAB/tRoowAAAABJRU5ErkJggg==")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", len(png))
        self.end_headers()
        self.wfile.write(png)

    def serve_file(self):
        base = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(base, "static", "index.html")
        if not os.path.exists(html_path):
            html_path = os.path.join(base, "index.html")
        with open(html_path, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

# ── BAŞLAT ────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  BIST %1 SİNYAL SİSTEMİ v4.0")
    print("=" * 50)
    init_db()

    # Telegram webhook ayarla
    threading.Thread(target=telegram_webhook_ayarla, daemon=True).start()

    # İlk taramayı hemen yap
    t1 = threading.Thread(target=tara, daemon=True)
    t1.start()

    # 15 dakikada bir tarama
    t2 = threading.Thread(target=tarama_dongusu, daemon=True)
    t2.start()

    print(f"Sunucu: http://0.0.0.0:{PORT}")
    try:
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("Kapandı.")
