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


def main():
    data = json.load(open("docs/data.json", encoding="utf-8"))
    wl = parse_watchlist()
    symbols = wl + INDICES + ["TWD=X"]

    quotes_by_sym = fetch_batch(symbols)
    # 補抓批次沒拿到的檔（單檔重試）
    for sym in symbols:
        if sym not in quotes_by_sym:
            q = fetch_one(sym)
            if q:
                quotes_by_sym[sym] = q

    quotes = [quotes_by_sym[s] for s in wl + INDICES if s in quotes_by_sym]

    # 美元兌台幣：yahoo TWD=X 是 USD/TWD，轉成 TWDUSD
    fx = quotes_by_sym.get("TWD=X")
    if fx and fx["p"]:
        quotes.append({"s": "TWDUSD", "p": round(1 / fx["p"], 8),
                       "c": round(-fx["c"], 2),
                       "a50": round(1 / fx["a50"], 8) if fx["a50"] else None,
                       "a200": round(1 / fx["a200"], 8) if fx["a200"] else None,
                       "yh": round(1 / fx["yl"], 6) if fx["yl"] else None,
                       "yl": round(1 / fx["yh"], 6) if fx["yh"] else None})

    # 安全門檻：至少要抓到一半以上的目標檔，否則保留舊資料不覆寫
    need = max(5, (len(wl) + len(INDICES)) // 2)
    if len(quotes) < need:
        print(f"only {len(quotes)}/{len(wl)+len(INDICES)} quotes (<{need}), abort to keep good data", file=sys.stderr)
        sys.exit(1)

    data["quotes"] = quotes
    tw_codes = {s.split(".")[0] for s in wl if s.endswith(".TW")}
    inst = twse_inst(tw_codes)
    if inst:
        data["inst"] = inst
    data["generatedAt"] = datetime.now(TPE).strftime("%Y-%m-%d %H:%M") + "（台北時間・盤中更新 Yahoo＋證交所數據）"
    json.dump(data, open("docs/data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"updated {len(quotes)} quotes")


if __name__ == "__main__":
    main()
