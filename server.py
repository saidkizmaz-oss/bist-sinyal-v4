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

PORT = int(os.environ.get("PORT", 8765))
DB_PATH = "/data/bist.db" if os.path.isdir("/data") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "bist.db")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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
        sembol TEXT, tarih TEXT, sayisi INTEGER DEFAULT 0,
        PRIMARY KEY (sembol, tarih))""")
    con.commit()
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
    piyasa_acik = (10 * 60 + 0) <= saat <= (18 * 60 + 0)

    # KRİTERLER
    kriter1 = fiyat > vwap                            # VWAP uzerinde
    kriter2 = ema9 > ema21                            # EMA9 > EMA21 (kısa vade yukarı)
    kriter3 = 40 <= rsi <= 70                         # RSI uygun bolge (genisletildi)
    kriter4 = macd_hist > 0                           # MACD histogram pozitif
    kriter5 = hacim_carpan >= 0.8                     # Hacim aktif (3 bar ort)
    kriter6 = mumlar_yesil >= 1                       # Son mum yesil
    kriter8 = gunluk_yukari                           # Gunluk trend yukari
    kriter9 = piyasa_acik                             # Piyasa saatleri

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
        "hedef": round(fiyat * 1.01, 4),
        "stop": round(fiyat * 0.995, 4),
        "kriterler": {
            "vwap": kriter1, "ema_stack": kriter2, "rsi": kriter3,
            "macd": kriter4, "hacim": kriter5, "momentum": kriter6,
            "gunluk": kriter8, "saat": kriter9
        }
    }

# ── VERİ ÇEKME ───────────────────────────────────────────────
_cache = {}
_lock = threading.Lock()

def veri_cek(sembol):
    try:
        t = yf.Ticker(sembol + ".IS")
        df_15m = t.history(period="5d", interval="2m")
        df_1d  = t.history(period="3mo", interval="1d")
        if df_15m is None or len(df_15m) < 25:
            return None
        closes_15m  = [float(x) for x in df_15m["Close"].tolist()]
        volumes_15m = [int(x) for x in df_15m["Volume"].tolist()]
        closes_1d   = [float(x) for x in df_1d["Close"].tolist()] if df_1d is not None else []
        return closes_15m, volumes_15m, closes_1d
    except:
        return None

def veri_cek_demo(sembol):
    import random, math
    random.seed(abs(hash(sembol)) % 9999)
    base = random.uniform(10, 500)
    closes_15m, vols = [], []
    f = base
    for i in range(80):
        f *= random.uniform(0.995, 1.005)
        closes_15m.append(round(f, 4))
        vols.append(int(random.uniform(100000, 2000000)))
    # Son 3 mumu yükselt (sinyal olasılığı artırsın)
    for i in range(-3, 0):
        closes_15m[i] = closes_15m[i-1] * random.uniform(1.001, 1.004)
    closes_1d = [base * random.uniform(0.95, 1.05) for _ in range(60)]
    return closes_15m, vols, closes_1d

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

# ── SİNYAL KAYDET ────────────────────────────────────────────
def sinyal_kaydet(sembol, sonuc):
    now = datetime.utcnow() + timedelta(hours=3)
    tarih = now.strftime("%Y-%m-%d")
    saat  = now.strftime("%H:%M")

    # Günlük limit kontrolü (max 2 sinyal/hisse)
    con = get_db()
    row = con.execute("SELECT sayisi FROM gunluk_sinyal_sayisi WHERE sembol=? AND tarih=?", (sembol, tarih)).fetchone()
    sayisi = row[0] if row else 0
    if sayisi >= 2:
        con.close()
        return False

    con.execute("INSERT OR REPLACE INTO gunluk_sinyal_sayisi VALUES (?,?,?)", (sembol, tarih, sayisi + 1))
    con.execute("""INSERT INTO sinyaller (sembol, fiyat_giris, hedef, stop, rsi, hacim_carpan,
        ema20, ema50, tarih, saat, durum) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (sembol, sonuc["fiyat"], sonuc["hedef"], sonuc["stop"],
         sonuc["rsi"], sonuc["hacim_carpan"], sonuc["ema9"], sonuc["ema50"],
         tarih, saat, "BEKLIYOR"))
    con.commit()
    con.close()

    # Telegram bildirimi
    msg = (f"🟢 <b>{sembol}</b> — AL SİNYALİ\n"
           f"💰 Fiyat: {sonuc['fiyat']:.2f} TL\n"
           f"🎯 Hedef: {sonuc['hedef']:.2f} TL (+%1)\n"
           f"🛡 Stop: {sonuc['stop']:.2f} TL (-%%0.5)\n"
           f"📊 RSI: {sonuc['rsi']} | Hacim: x{sonuc['hacim_carpan']}\n"
           f"📈 15dk değişim: +{sonuc['degisim_15dk']}%\n"
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
son_sinyal = {}

def tara():
    global son_sinyal
    print(f"[{(datetime.utcnow() + timedelta(hours=3)).strftime('%H:%M:%S')}] Tarama başlıyor...")
    veri_sayisi = 0
    hata_sayisi = 0
    kriter_say = {f"k{i}": 0 for i in [1,2,3,4,5,6,8,9]}
    for sembol in BIST100:
        try:
            if VERI_MODU == "gercek":
                veri = veri_cek(sembol)
            else:
                veri = veri_cek_demo(sembol)

            if not veri:
                continue

            closes_15m, volumes_15m, closes_1d = veri
            veri_sayisi += 1

            sonuc = sinyal_kontrol(sembol, closes_15m, volumes_15m, closes_1d)

            if sonuc:
                k = sonuc.get("kriterler", {})
                if k.get("vwap"): kriter_say["k1"] += 1
                if k.get("ema_stack"): kriter_say["k2"] += 1
                if k.get("rsi"): kriter_say["k3"] += 1
                if k.get("macd"): kriter_say["k4"] += 1
                if k.get("hacim"): kriter_say["k5"] += 1
                if k.get("momentum"): kriter_say["k6"] += 1
                if k.get("gunluk"): kriter_say["k8"] += 1
                if k.get("saat"): kriter_say["k9"] += 1
                with _lock:
                    _cache[sembol] = {
                        "sembol": sembol,
                        "fiyat": sonuc["fiyat"],
                        "ema20": sonuc["ema9"],
                        "ema50": sonuc["ema50"],
                        "rsi": sonuc["rsi"],
                        "hacim_carpan": sonuc["hacim_carpan"],
                        "degisim_15dk": sonuc["degisim_15dk"],
                        "sinyal": sonuc["sinyal"],
                        "hedef": sonuc["hedef"],
                        "stop": sonuc["stop"],
                        "kriterler": sonuc["kriterler"],
                    }

                # Sinyal varsa kaydet (son 15dk içinde aynı hisseden gelmemişse)
                if sonuc["sinyal"]:
                    with _lock:
                        son = son_sinyal.get(sembol)
                        now = time.time()
                        if not son or (now - son) > 900:  # 15 dakika
                            son_sinyal[sembol] = now  # Önceden işaretle (duplicate önleme)
                            gonder = True
                        else:
                            gonder = False
                    if gonder:
                        if sinyal_kaydet(sembol, sonuc):
                            print(f"  🟢 SİNYAL: {sembol} @ {sonuc['fiyat']}")
        except Exception as e:
            hata_sayisi += 1
            if hata_sayisi <= 3:
                print(f"  HATA {sembol}: {e}")

    sonuc_guncelle()
    print(f"[{(datetime.utcnow() + timedelta(hours=3)).strftime('%H:%M:%S')}] Tarama bitti. Cache:{len(_cache)} | VWAP:{kriter_say['k1']} EMA:{kriter_say['k2']} RSI:{kriter_say['k3']} MACD:{kriter_say['k4']} HACIM:{kriter_say['k5']} MOM:{kriter_say['k6']} GUNLUK:{kriter_say['k8']} SAAT:{kriter_say['k9']}")

def tarama_dongusu():
    while True:
        tara()
        time.sleep(60)  # Her 1 dakika bekle (efektif ~3-4 dk)

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
                sinyaller = [v for v in _cache.values() if v.get("sinyal")]
                tumu = list(_cache.values())
            self.send_json({
                "sinyaller": sinyaller,
                "tarama_sayisi": len(tumu),
                "sinyal_sayisi": len(sinyaller),
                "zaman": (datetime.utcnow() + timedelta(hours=3)).strftime("%H:%M:%S"),
                "mod": VERI_MODU
            })

        elif path == "/api/sinyaller":
            tarih = params.get("tarih", [""])[0]
            durum = params.get("durum", [""])[0]
            sembol = params.get("sembol", [""])[0]
            limit = int(params.get("limit", ["100"])[0])

            q = "SELECT * FROM sinyaller WHERE 1=1"
            args = []
            if tarih: q += " AND tarih=?"; args.append(tarih)
            if durum: q += " AND durum=?"; args.append(durum)
            if sembol: q += " AND sembol=?"; args.append(sembol.upper())
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
