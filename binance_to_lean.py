#!/usr/bin/env python3
"""
Download Binance spot 1-minute klines from data.binance.vision and convert
them into QuantConnect LEAN crypto data (market = binance).

Outputs, for each SYMBOL (lowercased):
  data/crypto/binance/minute/{sym}/{YYYYMMDD}_trade.zip   (one per day)
       inner csv: {YYYYMMDD}_{sym}_minute_trade.csv
       rows:      ms_since_midnight,open,high,low,close,volume
  data/crypto/binance/hour/{sym}_trade.zip                (one file, whole history)
  data/crypto/binance/daily/{sym}_trade.zip               (one file, whole history)
       inner csv: {sym}.csv
       rows:      YYYYMMDD HH:MM,open,high,low,close,volume

Raw monthly/daily source zips are cached under ~/binance_cache so re-runs are
fast and resumable. Idempotent: safe to run repeatedly.

Usage:  python3 binance_to_lean.py [SYMBOL ...]
        (defaults to BTCUSDT ETHUSDT SUIUSDT TAOUSDT)
"""
import os, sys, re, time, zipfile, datetime as dt
import urllib.request, urllib.error, urllib.parse

DATA  = os.path.expanduser("~/quantconnect/workspace/data")
CACHE = os.path.expanduser("~/binance_cache")
S3    = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DL    = "https://data.binance.vision"
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SUIUSDT", "TAOUSDT"]
EPOCH = dt.datetime(1970, 1, 1)


def log(msg):
    print("[%s] %s" % (dt.datetime.utcnow().strftime("%H:%M:%S"), msg), flush=True)


def http_get(url, tries=5):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lean-loader/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError("GET failed %s: %s" % (url, last))


def s3_list(prefix):
    """Return list of object keys under prefix (handles pagination)."""
    keys, marker = [], ""
    while True:
        url = "%s?prefix=%s&marker=%s" % (S3, urllib.parse.quote(prefix), urllib.parse.quote(marker))
        xml = http_get(url)
        if xml is None:
            break
        xml = xml.decode()
        page = re.findall(r"<Key>([^<]+)</Key>", xml)
        keys.extend(page)
        if "<IsTruncated>true</IsTruncated>" in xml and page:
            marker = page[-1]
        else:
            break
    return keys


def cached_download(remote_url, cache_path):
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return cache_path
    data = http_get(remote_url)
    if data is None:
        return None
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, cache_path)
    return cache_path


def norm_ms(ts):
    ts = int(ts)
    # ms ~1.5e12 (13 digits); microseconds ~1.5e15 (16 digits) -> normalize to ms
    if ts >= 10 ** 14:
        ts //= 1000
    return ts


def iter_kline_rows(zip_path):
    """Yield (ts_ms, open, high, low, close, volume) strings, in file order."""
    with zipfile.ZipFile(zip_path) as z:
        name = z.namelist()[0]
        raw = z.read(name).decode()
    for line in raw.splitlines():
        if not line:
            continue
        p = line.split(",")
        if not p[0].lstrip("-").isdigit():   # skip any header row
            continue
        yield norm_ms(p[0]), p[1], p[2], p[3], p[4], p[5]


def write_minute_zip(sym, date_str, lines):
    outdir = os.path.join(DATA, "crypto", "binance", "minute", sym)
    os.makedirs(outdir, exist_ok=True)
    zpath = os.path.join(outdir, "%s_trade.zip" % date_str)
    inner = "%s_%s_minute_trade.csv" % (date_str, sym)
    tmp = zpath + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(inner, "\n".join(lines) + "\n")
    os.replace(tmp, zpath)


def write_bars_zip(sym, kind, rows):
    """kind in {'hour','daily'}; rows = list of (dt_str,o,h,l,c,v)."""
    outdir = os.path.join(DATA, "crypto", "binance", kind)
    os.makedirs(outdir, exist_ok=True)
    zpath = os.path.join(outdir, "%s_trade.zip" % sym)
    inner = "%s.csv" % sym
    body = "\n".join("%s,%s,%s,%s,%s,%s" % r for r in rows) + "\n"
    tmp = zpath + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(inner, body)
    os.replace(tmp, zpath)


def fmt(x):
    s = ("%.8f" % x).rstrip("0").rstrip(".")
    return s if s else "0"


def process_symbol(symbol):
    sym = symbol.lower()
    log("=== %s ===" % symbol)

    monthly_keys = sorted(k for k in s3_list("data/spot/monthly/klines/%s/1m/" % symbol)
                          if k.endswith(".zip"))
    if not monthly_keys:
        log("  no monthly data on Binance for %s; skipping" % symbol)
        return 0
    months = [re.search(r"-1m-(\d{4}-\d{2})\.zip$", k).group(1) for k in monthly_keys]
    last_month = months[-1]
    log("  %d monthly files: %s .. %s" % (len(monthly_keys), months[0], last_month))

    # daily fill files for months AFTER the last monthly file (recent data)
    now = dt.datetime.utcnow()
    fill_months, y, m = [], int(last_month[:4]), int(last_month[5:7])
    while True:
        m += 1
        if m > 12:
            m, y = 1, y + 1
        if (y, m) > (now.year, now.month):
            break
        fill_months.append("%04d-%02d" % (y, m))
    daily_keys = []
    for fm in fill_months:
        daily_keys += [k for k in s3_list("data/spot/daily/klines/%s/1m/%s-1m-%s-" % (symbol, symbol, fm))
                       if k.endswith(".zip")]
    daily_keys.sort()
    if daily_keys:
        log("  %d daily fill files for %s" % (len(daily_keys), fill_months))

    src = [("monthly", k) for k in monthly_keys] + [("daily", k) for k in daily_keys]

    hour_acc, day_acc, order_h, order_d = {}, {}, [], []
    state = {"date": None, "lines": [], "days": 0}

    def flush_day():
        if state["date"] and state["lines"]:
            write_minute_zip(sym, state["date"], state["lines"])
            state["days"] += 1
        state["date"], state["lines"] = None, []

    for kind, key in src:
        cpath = os.path.join(CACHE, symbol, os.path.basename(key))
        got = cached_download("%s/%s" % (DL, key), cpath)
        if got is None:
            log("  WARN missing %s" % key)
            continue
        try:
            for ts, o, h, l, c, v in iter_kline_rows(got):
                t = EPOCH + dt.timedelta(milliseconds=ts)
                date_str = t.strftime("%Y%m%d")
                ms_mid = (t.hour * 3600 + t.minute * 60 + t.second) * 1000 + t.microsecond // 1000
                if date_str != state["date"]:
                    flush_day()
                    state["date"] = date_str
                state["lines"].append("%d,%s,%s,%s,%s,%s" % (ms_mid, o, h, l, c, v))
                fo, fh, fl, fc, fv = float(o), float(h), float(l), float(c), float(v)
                hk = t.strftime("%Y%m%d %H:00")
                a = hour_acc.get(hk)
                if a is None:
                    hour_acc[hk] = [fo, fh, fl, fc, fv]; order_h.append(hk)
                else:
                    a[1] = max(a[1], fh); a[2] = min(a[2], fl); a[3] = fc; a[4] += fv
                dk = t.strftime("%Y%m%d 00:00")
                a = day_acc.get(dk)
                if a is None:
                    day_acc[dk] = [fo, fh, fl, fc, fv]; order_d.append(dk)
                else:
                    a[1] = max(a[1], fh); a[2] = min(a[2], fl); a[3] = fc; a[4] += fv
        except Exception as e:
            log("  ERROR processing %s: %s" % (key, e))
    flush_day()

    hrows = [(k, fmt(v[0]), fmt(v[1]), fmt(v[2]), fmt(v[3]), fmt(v[4]))
             for k, v in ((k, hour_acc[k]) for k in order_h)]
    drows = [(k, fmt(v[0]), fmt(v[1]), fmt(v[2]), fmt(v[3]), fmt(v[4]))
             for k, v in ((k, day_acc[k]) for k in order_d)]
    write_bars_zip(sym, "hour", hrows)
    write_bars_zip(sym, "daily", drows)
    log("  DONE %s: %d day-zips, %d hour-bars, %d daily-bars" %
        (symbol, state["days"], len(hrows), len(drows)))
    return state["days"]


def main():
    symbols = [s.upper() for s in sys.argv[1:]] or DEFAULT_SYMBOLS
    log("Symbols: %s" % symbols)
    log("Data dir: %s" % DATA)
    total = 0
    for s in symbols:
        total += process_symbol(s)
    log("ALL DONE. total day-zips written: %d" % total)


if __name__ == "__main__":
    main()
