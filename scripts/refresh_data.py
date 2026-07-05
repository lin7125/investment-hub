#!/usr/bin/env python3
"""盤中行情更新：讀 watchlist.md → yfinance 抓報價 → 更新 docs/data.json（news 保留原值）"""
import json, re, sys
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

TPE = timezone(timedelta(hours=8))
INDICES = ["^TWII", "^GSPC", "^VIX"]

def parse_watchlist(path="watchlist.md"):
    symbols = []
    section = None
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

def fetch(sym):
    try:
        t = yf.Ticker(sym)
        fi = t.fast_info
        p = fi.last_price
        prev = fi.previous_close
        if p is None: return None
        return {
            "s": sym,
            "p": round(p, 4 if p < 1 else 2),
            "c": round((p / prev - 1) * 100, 2) if prev else 0,
            "a50": round(fi.fifty_day_average, 2) if fi.fifty_day_average else None,
            "a200": round(fi.two_hundred_day_average, 2) if fi.two_hundred_day_average else None,
            "yh": round(fi.year_high, 2) if fi.year_high else None,
            "yl": round(fi.year_low, 2) if fi.year_low else None,
        }
    except Exception as e:
        print(f"skip {sym}: {e}", file=sys.stderr)
        return None

def _n(x):
    try: return int(str(x).replace(",", "").strip() or 0)
    except Exception: return 0

def twse_inst(codes):
    """證交所官方免費 API：個股三大法人買賣超（T86）＋大盤合計（BFI82U）。最近交易日回溯 7 天。"""
    H = {"User-Agent": "Mozilla/5.0"}
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
                    # 欄位：4=外陸資買賣超股數(不含外資自營) 10=投信買賣超股數 末欄=三大法人買賣超股數
                    by[code] = {"f": _n(row[4]) // 1000, "t": _n(row[10]) // 1000, "all": _n(row[-1]) // 1000}
            inst = {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "byCode": by}
            try:
                r2 = requests.get(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={d}&type=day&response=json",
                                  headers=H, timeout=25)
                j2 = r2.json()
                if j2.get("data"):
                    total = j2["data"][-1]  # 合計列：買進金額/賣出金額/買賣差額
                    inst["totalNetE"] = round(_n(total[3]) / 1e8, 1)  # 億元
            except Exception as e:
                print("BFI82U", e, file=sys.stderr)
            return inst
        except Exception as e:
            print("T86", d, e, file=sys.stderr)
    return None

def main():
    data = json.load(open("docs/data.json", encoding="utf-8"))
    quotes = []
    for sym in parse_watchlist() + INDICES:
        q = fetch(sym)
        if q: quotes.append(q)
    # 美元兌台幣：yahoo TWD=X 是 USD/TWD，轉成 TWDUSD
    fx = fetch("TWD=X")
    if fx and fx["p"]:
        quotes.append({"s": "TWDUSD", "p": round(1 / fx["p"], 8),
                       "c": round(-fx["c"], 2),
                       "a50": round(1 / fx["a50"], 8) if fx["a50"] else None,
                       "a200": round(1 / fx["a200"], 8) if fx["a200"] else None,
                       "yh": round(1 / fx["yl"], 6) if fx["yl"] else None,
                       "yl": round(1 / fx["yh"], 6) if fx["yh"] else None})
    if len(quotes) < 5:
        print("too few quotes, abort to avoid clobbering", file=sys.stderr)
        sys.exit(1)
    data["quotes"] = quotes
    tw_codes = {s.split(".")[0] for s in parse_watchlist() if s.endswith(".TW")}
    inst = twse_inst(tw_codes)
    if inst:
        data["inst"] = inst
    data["generatedAt"] = datetime.now(TPE).strftime("%Y-%m-%d %H:%M") + "（台北時間・盤中更新 Yahoo＋證交所數據）"
    json.dump(data, open("docs/data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"updated {len(quotes)} quotes")

if __name__ == "__main__":
    main()
