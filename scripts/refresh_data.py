#!/usr/bin/env python3
"""盤中行情更新：讀 watchlist.md → yfinance 抓報價 → 更新 docs/data.json（news 保留原值）

穩健化重點（避免 GitHub Actions 上 Yahoo 封鎖 datacenter IP 導致更新失敗）：
- 改用批次 history/chart 端點（比 fast_info 的 quote 端點穩定很多）
- 每檔失敗自動重試、瀏覽器 UA session
- 只在抓到足夠檔數時才覆寫 data.json，避免把好資料洗掉
"""
import json, re, sys, time
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

TPE = timezone(timedelta(hours=8))
INDICES = ["^TWII", "^GSPC", "^VIX"]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def parse_watchlist(path="watchlist.md"):
    symbols, section = [], None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("##"):
            if "上市" in line: section = ".TW"
            elif "上櫃" in line: section = ".TWO"
            elif "美股" in line: section = "US"
            else: section = None
            continue
        m = re.match(r"^([0-9A-Za-z.\-]+)\s*\|", line)
        if m and section:
            code = m.group(1)
            symbols.append(code + section if section in (".TW", ".TWO") else code)
    return symbols


def _round(p):
    return round(p, 4 if p < 1 else 2)


def _quote_from_closes(sym, closes):
    """由收盤價序列計算 quote 欄位。closes 為由舊到新的浮點 list。"""
    closes = [c for c in closes if c is not None and c == c]  # 去除 NaN
    if len(closes) < 2:
        return None
    p, prev = closes[-1], closes[-2]
    last252 = closes[-252:]
    last50 = closes[-50:]
    last200 = closes[-200:]
    return {
        "s": sym,
        "p": _round(p),
        "c": round((p / prev - 1) * 100, 2) if prev else 0,
        "a50": round(sum(last50) / len(last50), 2) if last50 else None,
        "a200": round(sum(last200) / len(last200), 2) if last200 else None,
        "yh": round(max(last252), 2),
        "yl": round(min(last252), 2),
    }


def fetch_batch(symbols):
    """批次抓 1 年日線，回傳 {sym: quote}。失敗的檔案不會出現在結果中。"""
    out = {}
    try:
        df = yf.download(symbols, period="1y", interval="1d", auto_adjust=False,
                         group_by="ticker", threads=True, progress=False)
    except Exception as e:
        print("download error:", e, file=sys.stderr)
        return out
    for sym in symbols:
        try:
            col = df[sym]["Close"] if len(symbols) > 1 else df["Close"]
            q = _quote_from_closes(sym, list(col.values))
            if q:
                out[sym] = q
        except Exception as e:
            print(f"parse {sym}: {e}", file=sys.stderr)
    return out


def fetch_one(sym):
    """單檔重試：history 端點。"""
    for attempt in range(3):
        try:
            h = yf.Ticker(sym).history(period="1y", interval="1d", auto_adjust=False)
            if len(h):
                q = _quote_from_closes(sym, list(h["Close"].values))
                if q:
                    return q
        except Exception as e:
            print(f"retry {sym} #{attempt}: {e}", file=sys.stderr)
        time.sleep(1.5)
    return None


def _n(x):
    try: return int(str(x).replace(",", "").strip() or 0)
    except Exception: return 0


def twse_inst(codes):
    """證交所官方免費 API：個股三大法人買賣超（T86）＋大盤合計（BFI82U）。最近交易日回溯 7 天。"""
    H = {"User-Agent": UA}
    for i in range(7):
        d = (datetime.now(TPE) - timedelta(days=i)).strftime("%Y%m%d")
        try:
            r = requests.get(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d}&selectType=ALLBUT0999&response=json",
                             headers=H, timeout=25)
            j = r.json()
            if j.get("stat") != "OK" or not j.get("data"):
                continue
            by = {}
            for row in j["data"]:
                code = row[0].strip()
                if code in codes:
                    by[code] = {"f": _n(row[4]) // 1000, "t": _n(row[10]) // 1000, "all": _n(row[-1]) // 1000}
            inst = {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "byCode": by}
            try:
                r2 = requests.get(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={d}&type=day&response=json",
                                  headers=H, timeout=25)
                j2 = r2.json()
                if j2.get("data"):
                    total = j2["data"][-1]
                    inst["totalNetE"] = round(_n(total[3]) / 1e8, 1)
            except Exception as e:
                print("BFI82U", e, file=sys.stderr)
            return inst
        except Exception as e:
            print("T86", d, e, file=sys.stderr)
    return None




# ── 備援即時價（yfinance 被擋時仍能更新現價/漲跌）──
def fetch_tw_mis(tw_symbols):
    """證交所/櫃買官方盤中 API（mis.twse.com.tw）。含 ^TWII（tse_t00.tw）。回 {sym:(price,chgpct)}"""
    chs = []
    for s in tw_symbols:
        if s == "^TWII":
            chs.append("tse_t00.tw"); continue
        code, sfx = s.rsplit(".", 1)
        chs.append(("tse_" if sfx == "TW" else "otc_") + code + ".tw")
    out = {}
    S = requests.Session(); S.headers.update({"User-Agent": UA, "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
    try: S.get("https://mis.twse.com.tw/stock/index.jsp", timeout=15)
    except Exception: pass
    for i in range(0, len(chs), 20):
        batch = "|".join(chs[i:i+20])
        try:
            r = S.get(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={batch}&json=1&delay=0", timeout=20)
            for row in r.json().get("msgArray", []):
                code, ex = row.get("c"), row.get("ex")
                try: y = float(row.get("y"))
                except Exception: continue
                price = None
                try: price = float(row.get("z"))
                except Exception:
                    for k in ("b", "a", "o"):  # 無成交時退而求買/賣/開盤價
                        v = (row.get(k) or "").split("_")[0]
                        try: price = float(v); break
                        except Exception: pass
                if not price or not y: continue
                sym = "^TWII" if code == "t00" else code + (".TW" if ex == "tse" else ".TWO")
                out[sym] = (price, round((price / y - 1) * 100, 2))
        except Exception as e:
            print("MIS", e, file=sys.stderr)
        time.sleep(1)
    return out


STOOQ_MAP = {"^GSPC": "^spx", "^VIX": "^vix", "TWD=X": "usdtwd"}

def fetch_stooq(symbols):
    """Stooq 免費報價（美股/指數/匯率，延遲約15分）。回 {sym:(price,chgpct)}"""
    out = {}
    codes = {s: STOOQ_MAP.get(s, s.lower() + ".us") for s in symbols}
    try:
        lst = ",".join(codes.values())
        r = requests.get(f"https://stooq.com/q/l/?s={lst}&f=sd2t2ohlcv&e=csv",
                         headers={"User-Agent": UA}, timeout=25)
        live = {}
        for ln in r.text.strip().split("\n")[1:]:
            f = ln.split(",")
            if len(f) >= 7 and f[6] not in ("N/D", ""):
                try: live[f[0].lower()] = float(f[6])
                except Exception: pass
        d2 = datetime.now(TPE).strftime("%Y%m%d")
        d1 = (datetime.now(TPE) - timedelta(days=12)).strftime("%Y%m%d")
        for sym, code in codes.items():
            cur = live.get(code.lower())
            if cur is None: continue
            try:
                h = requests.get(f"https://stooq.com/q/d/l/?s={code}&d1={d1}&d2={d2}&i=d",
                                 headers={"User-Agent": UA}, timeout=25)
                closes = [float(x.split(",")[4]) for x in h.text.strip().split("\n")[1:] if x.count(",") >= 4]
                prev = closes[-2] if len(closes) >= 2 and abs(closes[-1] - cur) < 1e-9 else (closes[-1] if closes else None)
                if prev: out[sym] = (cur, round((cur / prev - 1) * 100, 2))
            except Exception as e:
                print("stooq-h", code, e, file=sys.stderr)
            time.sleep(0.5)
    except Exception as e:
        print("stooq", e, file=sys.stderr)
    return out


def main():
    data = json.load(open("docs/data.json", encoding="utf-8"))
    old_by = {q["s"]: q for q in data.get("quotes", [])}
    wl = parse_watchlist()
    symbols = wl + INDICES + ["TWD=X"]

    quotes_by_sym = fetch_batch(symbols)
    for sym in symbols:
        if sym not in quotes_by_sym:
            q = fetch_one(sym)
            if q:
                quotes_by_sym[sym] = q

    # ── 備援：yfinance 沒拿到的檔，用官方/Stooq 即時價＋舊結構層（均線/52週沿用上次）──
    missing = [s for s in symbols if s not in quotes_by_sym]
    fb_used = 0
    if missing:
        fast = {}
        tw_miss = [s for s in missing if s.endswith(".TW") or s.endswith(".TWO") or s == "^TWII"]
        if tw_miss:
            fast.update(fetch_tw_mis(tw_miss))
        other_miss = [s for s in missing if s not in tw_miss]
        if other_miss:
            fast.update(fetch_stooq(other_miss))
        for sym, (p, cpct) in fast.items():
            base = dict(old_by.get(sym if sym != "TWD=X" else "TWDUSD",
                                   {"a50": None, "a200": None, "yh": None, "yl": None}))
            base.update({"s": sym, "p": _round(p), "c": cpct})
            for k in ("a50", "a200", "yh", "yl"):
                base.setdefault(k, None)
            quotes_by_sym[sym] = base
            fb_used += 1

    quotes = [quotes_by_sym[s] for s in wl + INDICES if s in quotes_by_sym]

    fx = quotes_by_sym.get("TWD=X")
    if fx and fx.get("p"):
        if fx.get("a50"):
            quotes.append({"s": "TWDUSD", "p": round(1 / fx["p"], 8),
                           "c": round(-fx["c"], 2),
                           "a50": round(1 / fx["a50"], 8) if fx["a50"] else None,
                           "a200": round(1 / fx["a200"], 8) if fx["a200"] else None,
                           "yh": round(1 / fx["yl"], 6) if fx["yl"] else None,
                           "yl": round(1 / fx["yh"], 6) if fx["yh"] else None})
        else:
            base = dict(old_by.get("TWDUSD", {"a50": None, "a200": None, "yh": None, "yl": None}))
            base.update({"s": "TWDUSD", "p": round(1 / fx["p"], 8), "c": round(-fx["c"], 2)})
            quotes.append(base)

    need = max(5, (len(wl) + len(INDICES)) // 2)
    if len(quotes) < need:
        print(f"only {len(quotes)}/{len(wl)+len(INDICES)} quotes (<{need}), abort to keep good data", file=sys.stderr)
        sys.exit(1)

    data["quotes"] = quotes
    tw_codes = {s.split(".")[0] for s in wl if s.endswith(".TW")}
    inst = twse_inst(tw_codes)
    if inst:
        data["inst"] = inst
    tag = "盤中更新 Yahoo＋證交所數據" if not fb_used else f"盤中更新・含 {fb_used} 檔官方/Stooq 備援即時價"
    data["generatedAt"] = datetime.now(TPE).strftime("%Y-%m-%d %H:%M") + f"（台北時間・{tag}）"
    json.dump(data, open("docs/data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
