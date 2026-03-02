"""
=============================================================
TANGENTWISE — Battery Input Cost Pipeline
=============================================================
Sources:
  - Google Sheets (manual)  : Lithium, Graphite
  - World Bank Pink Sheet   : Cobalt, Nickel, Copper, Manganese, Phosphate, Aluminum
  - yfinance                : Nickel, Copper, Aluminum, LIT ETF (lithium proxy)

Output:
  - data/materials.json     : Indexed price series (base = Jan 2022 = 100)
  - data/kwh_index.json     : Battery pack cost $/kWh over time (IEA seeded)

Run locally : python fetch_battery_data.py
Run on CI   : GitHub Actions (see .github/workflows/update_data.yml)
=============================================================
"""

import json
import os
import io
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────

OUTPUT_DIR = "data"
INDEX_BASE_DATE = "2022-01-01"   # Everything indexed to 100 at this date

GOOGLE_SHEET_LITHIUM  = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ45-wl_wNqjBFGKt2c9n9Vcx9n_rkbdaXnXS58Z62F-qzqExZdxMCJcUucxoW59rwayHfNHMBYjtxX/pub?gid=0&single=true&output=csv"
GOOGLE_SHEET_GRAPHITE = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ45-wl_wNqjBFGKt2c9n9Vcx9n_rkbdaXnXS58Z62F-qzqExZdxMCJcUucxoW59rwayHfNHMBYjtxX/pub?gid=2061809493&single=true&output=csv"

# World Bank Pink Sheet — stable direct download URL
PINK_SHEET_URL = "https://thedocs.worldbank.org/en/doc/5d903e848db1d1b83e0ec8f744e55570-0350012021/related/CMO-Historical-Data-Monthly.xlsx"

# yfinance tickers
YFINANCE_TICKERS = {
    "nickel":   "NKL=F",   # Nickel futures (CME)
    "copper":   "HG=F",    # Copper futures (COMEX, $/lb — we convert to $/tonne)
    "aluminum": "ALI=F",   # Aluminum futures
    "lit_etf":  "LIT",     # Global X Lithium & Battery Tech ETF (lithium proxy)
}

# Fallback tickers if primary fails
YFINANCE_FALLBACKS = {
    "nickel":   ["NIKL", "LNICKEL.L"],
}

# World Bank Pink Sheet column names (these match the sheet's actual headers)
PINK_SHEET_COLS = {
    "cobalt":    "Cobalt",
    "nickel":    "Nickel",
    "copper":    "Copper",
    "manganese": "Manganese ore",
    "aluminum":  "Aluminum",
    "phosphate": "Phosphate rock",
}

# ── IEA/BNEF seeded $/kWh data (published, does not change) ──────────────────
# Source: IEA Global EV Outlook + BloombergNEF Battery Price Survey
KWH_HISTORICAL = [
    {"year": 2013, "price_usd_kwh": 684},
    {"year": 2014, "price_usd_kwh": 588},
    {"year": 2015, "price_usd_kwh": 373},
    {"year": 2016, "price_usd_kwh": 273},
    {"year": 2017, "price_usd_kwh": 214},
    {"year": 2018, "price_usd_kwh": 176},
    {"year": 2019, "price_usd_kwh": 156},
    {"year": 2020, "price_usd_kwh": 137},
    {"year": 2021, "price_usd_kwh": 132},
    {"year": 2022, "price_usd_kwh": 151},   # spiked due to lithium prices
    {"year": 2023, "price_usd_kwh": 139},
    {"year": 2024, "price_usd_kwh": 115},   # IEA preliminary
]


# ── HELPERS ───────────────────────────────────────────────────────────────────

def make_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

def to_monthly_index(series: pd.Series, base_date: str) -> pd.Series:
    """
    Resample a price series to month-end, then index it so the value
    at base_date equals 100. Returns empty series if data is insufficient.
    """
    series = series.dropna()
    if series.empty:
        return pd.Series(dtype=float)

    series.index = pd.to_datetime(series.index)
    monthly = series.resample("ME").last().dropna()

    if monthly.empty:
        return pd.Series(dtype=float)

    base = pd.Timestamp(base_date)
    # Find closest month-end to base date
    indexer = monthly.index.get_indexer([base], method="nearest")
    if indexer[0] == -1 or len(monthly.index) == 0:
        # No data near base date — index to first available point
        base_val = monthly.iloc[0]
    elif base not in monthly.index:
        nearest = monthly.index[indexer[0]]
        base_val = monthly.loc[nearest]
    else:
        base_val = monthly.loc[base]

    if base_val == 0 or pd.isna(base_val):
        return monthly  # can't index, return raw

    return (monthly / base_val * 100).round(2)

def series_to_records(series: pd.Series, label: str) -> list:
    """Convert a named pandas Series to list of {date, value} dicts."""
    records = []
    for dt, val in series.items():
        if pd.isna(val):
            continue
        records.append({
            "date": dt.strftime("%Y-%m-%d"),
            "value": float(val)
        })
    return records


# ── SOURCE 1: GOOGLE SHEETS ───────────────────────────────────────────────────

def fetch_google_sheet(url: str, material_name: str) -> pd.Series:
    print(f"  Fetching {material_name} from Google Sheets...")
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = [c.strip().lower() for c in df.columns]
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        series = pd.to_numeric(df["price_usd"], errors="coerce")
        series.name = material_name
        print(f"    ✓ {len(series)} rows, {series.index[0].date()} → {series.index[-1].date()}")
        return series
    except Exception as e:
        print(f"    ✗ Failed: {e}")
        return pd.Series(name=material_name, dtype=float)


# ── SOURCE 2: WORLD BANK PINK SHEET ──────────────────────────────────────────

def fetch_pink_sheet() -> dict:
    """
    Download the World Bank Pink Sheet Excel file and extract
    monthly commodity price series. Returns dict of {material: pd.Series}.
    """
    print("  Fetching World Bank Pink Sheet...")
    try:
        resp = requests.get(PINK_SHEET_URL, timeout=30)
        resp.raise_for_status()

        # The Pink Sheet has multiple sheets — we want "Monthly Prices"
        xl = pd.ExcelFile(io.BytesIO(resp.content))
        sheet_name = [s for s in xl.sheet_names if "monthly" in s.lower()]
        if not sheet_name:
            sheet_name = xl.sheet_names[0]
        else:
            sheet_name = sheet_name[0]

        # Read with no header first to find where data starts
        raw = pd.read_excel(xl, sheet_name=sheet_name, header=None)

        # Find the row where dates start (look for a cell containing a year like 1960)
        date_row = None
        for i, row in raw.iterrows():
            for val in row:
                try:
                    if 1960 <= int(float(str(val))) <= 1970:
                        date_row = i
                        break
                except:
                    pass
            if date_row is not None:
                break

        if date_row is None:
            # Fallback: assume row 6
            date_row = 6

        df = pd.read_excel(xl, sheet_name=sheet_name, header=date_row)

        # First column is dates
        df = df.rename(columns={df.columns[0]: "date"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df.set_index("date").sort_index()

        # Print actual columns for debugging
        print(f"    Pink Sheet columns: {list(df.columns[:20])}")

        results = {}
        for material, col_name in PINK_SHEET_COLS.items():
            # Try multiple matching strategies
            col_lower = col_name.lower()
            matching = []
            # 1. Exact partial match
            matching = [c for c in df.columns if col_lower in str(c).lower()]
            # 2. First word match (e.g. "Cobalt" matches "Cobalt, cathode")
            if not matching:
                first_word = col_lower.split()[0]
                matching = [c for c in df.columns if str(c).lower().startswith(first_word)]
            # 3. Any word match
            if not matching:
                words = col_lower.split()
                matching = [c for c in df.columns if any(w in str(c).lower() for w in words)]

            if matching:
                s = pd.to_numeric(df[matching[0]], errors="coerce")
                s.name = material
                results[material] = s
                print(f"    ✓ {material}: matched '{matching[0]}', {s.dropna().__len__()} rows")
            else:
                print(f"    ✗ {material}: no match for '{col_name}'")
                results[material] = pd.Series(name=material, dtype=float)

        return results

    except Exception as e:
        print(f"    ✗ Pink Sheet failed: {e}")
        return {m: pd.Series(name=m, dtype=float) for m in PINK_SHEET_COLS}


# ── SOURCE 3: YFINANCE ────────────────────────────────────────────────────────

def fetch_yfinance() -> dict:
    """
    Pull commodity/ETF price history from Yahoo Finance.
    Returns dict of {material: pd.Series} with monthly close prices.
    """
    results = {}
    start_date = "2019-01-01"

    for material, ticker in YFINANCE_TICKERS.items():
        print(f"  Fetching {material} ({ticker}) from yfinance...")
        tickers_to_try = [ticker] + YFINANCE_FALLBACKS.get(material, [])
        success = False

        for t_symbol in tickers_to_try:
            try:
                t = yf.Ticker(t_symbol)
                hist = t.history(start=start_date, interval="1mo")
                if hist.empty:
                    hist = t.history(start=start_date, interval="1d")
                    if hist.empty:
                        raise ValueError("No data returned")
                    hist = hist["Close"].resample("ME").last()
                else:
                    hist = hist["Close"]

                hist.index = hist.index.tz_localize(None) if hist.index.tz else hist.index
                hist.name = material

                if material == "copper":
                    hist = hist * 2204.62

                print(f"    ✓ {t_symbol}: {len(hist)} months, latest: {hist.index[-1].date()} = {hist.iloc[-1]:.2f}")
                results[material] = hist
                success = True
                break

            except Exception as e:
                print(f"    ✗ {t_symbol} failed: {e}")
                continue

        if not success:
            print(f"    ✗ All tickers failed for {material}")
            results[material] = pd.Series(name=material, dtype=float)

    return results


# ── MERGE & INDEX ─────────────────────────────────────────────────────────────

def build_materials_json(all_series: dict) -> dict:
    """
    Takes dict of {material: pd.Series (raw prices)},
    indexes each to 100 at INDEX_BASE_DATE,
    returns structured JSON-ready dict.
    """
    materials_out = {}

    METADATA = {
        "lithium":   {"label": "Lithium Carbonate", "unit": "$/tonne",  "source": "Manual (SMM reference)"},
        "graphite":  {"label": "Flake Graphite",     "unit": "$/tonne",  "source": "Manual (Fastmarkets reference)"},
        "cobalt":    {"label": "Cobalt",             "unit": "$/tonne",  "source": "World Bank Pink Sheet"},
        "nickel":    {"label": "Nickel",             "unit": "$/tonne",  "source": "World Bank / yfinance"},
        "copper":    {"label": "Copper",             "unit": "$/tonne",  "source": "World Bank / yfinance"},
        "manganese": {"label": "Manganese Ore",      "unit": "$/dmtu",   "source": "World Bank Pink Sheet"},
        "aluminum":  {"label": "Aluminum",           "unit": "$/tonne",  "source": "World Bank / yfinance"},
        "phosphate": {"label": "Phosphate Rock",     "unit": "$/tonne",  "source": "World Bank Pink Sheet"},
        "lit_etf":   {"label": "LIT ETF (Li proxy)", "unit": "USD/share","source": "Yahoo Finance"},
    }

    for material, series in all_series.items():
        if series.empty:
            continue
        indexed = to_monthly_index(series, INDEX_BASE_DATE)
        meta = METADATA.get(material, {"label": material, "unit": "index", "source": "various"})
        materials_out[material] = {
            "label":       meta["label"],
            "unit":        meta["unit"],
            "source":      meta["source"],
            "index_base":  INDEX_BASE_DATE,
            "data":        series_to_records(indexed, material)
        }

    return {
        "updated":   datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "index_base": INDEX_BASE_DATE,
        "note": "All values indexed to 100 at base date. Enables cross-material comparison regardless of unit.",
        "materials": materials_out
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("\n=== TangentWise Battery Data Pipeline ===\n")
    make_output_dir()

    all_series = {}

    # 1. Google Sheets — Lithium & Graphite
    print("[ 1/3 ] Google Sheets")
    all_series["lithium"]  = fetch_google_sheet(GOOGLE_SHEET_LITHIUM,  "lithium")
    all_series["graphite"] = fetch_google_sheet(GOOGLE_SHEET_GRAPHITE, "graphite")

    # 2. World Bank Pink Sheet
    print("\n[ 2/3 ] World Bank Pink Sheet")
    pink = fetch_pink_sheet()
    all_series.update(pink)

    # 3. yfinance
    print("\n[ 3/3 ] Yahoo Finance")
    yf_data = fetch_yfinance()
    # Merge yfinance into Pink Sheet where Pink Sheet wins (more complete history)
    # but yfinance fills in recent months that Pink Sheet may lag on
    for material in ["nickel", "copper", "aluminum"]:
        if material in all_series and not all_series[material].empty and material in yf_data:
            combined = pd.concat([all_series[material], yf_data[material]])
            combined = combined[~combined.index.duplicated(keep="first")].sort_index()
            all_series[material] = combined
        elif material in yf_data:
            all_series[material] = yf_data[material]
    # LIT ETF is standalone
    all_series["lit_etf"] = yf_data.get("lit_etf", pd.Series(dtype=float))

    # Build and write materials.json
    print("\n[ Output ] Building materials.json...")
    materials_json = build_materials_json(all_series)
    out_path = os.path.join(OUTPUT_DIR, "materials.json")
    with open(out_path, "w") as f:
        json.dump(materials_json, f, indent=2)
    print(f"  ✓ Written: {out_path}")
    print(f"  Materials: {list(materials_json['materials'].keys())}")

    # Build and write kwh_index.json
    print("\n[ Output ] Building kwh_index.json...")
    kwh_json = {
        "updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "unit": "USD per kWh (pack level)",
        "source": "IEA Global EV Outlook / BloombergNEF Battery Price Survey",
        "note": "Annual averages. 2022 spike reflects lithium carbonate price surge. Recent years are preliminary estimates.",
        "data": KWH_HISTORICAL
    }
    kwh_path = os.path.join(OUTPUT_DIR, "kwh_index.json")
    with open(kwh_path, "w") as f:
        json.dump(kwh_json, f, indent=2)
    print(f"  ✓ Written: {kwh_path}")

    print("\n=== Done ===\n")


if __name__ == "__main__":
    main()
