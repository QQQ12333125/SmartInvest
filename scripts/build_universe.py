#!/usr/bin/env python3
"""Build a best-effort Taiwan/US stock and ETF universe from public directories."""
import csv
import io
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "universe.json"
UA = "SmartInvest-universe-builder/1.1 (GitHub Actions; public directory refresh)"


def get(url):
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,text/plain,*/*",
    })
    with urlopen(req, timeout=45) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


class TableParser(HTMLParser):
    """Extract table rows/cells without relying on fragile HTML regexes."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            value = " ".join("".join(self.cell).replace("\xa0", " ").split())
            self.row.append(value)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
            self.cell = None


def parse_tw_page(mode):
    # Use the official English-language ISIN directory; it exposes the same
    # listing tables but avoids encoding issues on the Traditional Chinese page.
    url = f"https://isin.twse.com.tw/isin/e_C_public.jsp?strMode={mode}"
    parser = TableParser()
    parser.feed(get(url))
    out = []
    section = ""
    for cells in parser.rows:
        if not cells:
            continue
        first = cells[0].strip()
        # Section headers are typically single-cell rows such as "Stocks" or "ETF".
        if len(cells) == 1:
            if first and len(first) < 100:
                section = first.lower()
            continue
        # The first column is e.g. "1101 TCC". Accept only normal stocks and ETFs;
        # skip warrants, bonds, certificates and other non-stock securities.
        match = re.match(r"^\s*([A-Z0-9]{4,6})\s+(.+?)\s*$", first, flags=re.I)
        if not match:
            continue
        symbol, name = match.group(1).upper(), match.group(2).strip()
        section_text = section + " " + " ".join(cells).lower()
        name_text = name.lower()
        is_etf = (
            "etf" in section_text
            or "exchange traded fund" in section_text
            or "exchange-traded fund" in section_text
            or "指數股票型" in section_text
        )
        is_stock_section = "stock" in section or "equities" in section
        is_etf_section = "etf" in section or "exchange traded fund" in section
        # Keep only ordinary listed equities and ETFs. Some ETF pages don't
        # label the section consistently, so also recognize ETF in row fields.
        if not (is_stock_section or is_etf_section or is_etf):
            continue
        # Avoid taking heading rows or non-equity products from a broad section.
        if re.search(r"\b(warrant|bond|debenture|certificate|futures|options|structured product)\b", name_text):
            continue
        kind = "ETF" if is_etf else "股票"
        out.append({
            "symbol": symbol,
            "name": name,
            "market": "tw",
            "type": kind,
            "exchange": "上市" if mode == 2 else "上櫃",
            "yahoo": symbol + (".TW" if mode == 2 else ".TWO"),
            "source": "TWSE ISIN securities directory",
        })
    return out


def parse_nasdaq_file(url, other=False):
    raw = get(url)
    lines = raw.splitlines()
    if not lines:
        return []
    headers = lines[0].split("|")
    out = []
    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"):
            continue
        vals = line.split("|")
        if len(vals) < 2:
            continue
        row = dict(zip(headers, vals))
        symbol = (row.get("Symbol") or row.get("ACT Symbol") or "").strip()
        name = (row.get("Security Name") or "").strip()
        if not symbol or not name or row.get("Test Issue", "N").strip().upper() == "Y":
            continue
        # Keep symbols usable in Yahoo Finance chart URLs. Remove directory placeholders.
        if symbol.lower() in {"symbol", "act symbol"}:
            continue
        etf_flag = row.get("ETF", "N").strip().upper() == "Y"
        if other:
            exchange_map = {
                "N": "NYSE",
                "A": "NYSE American",
                "P": "NYSE Arca",
                "Z": "Cboe BZX",
                "V": "IEX",
            }
            exchange = exchange_map.get((row.get("Exchange") or "").strip(), "US exchange")
        else:
            exchange = "NASDAQ"
        kind = "ETF" if etf_flag or re.search(r"\b(ETF|EXCHANGE.?TRADED FUND)\b", name, re.I) else "股票"
        out.append({
            "symbol": symbol,
            "name": name,
            "market": "us",
            "type": kind,
            "exchange": exchange,
            "yahoo": symbol.replace(".", "-"),
            "source": "Nasdaq Trader Symbol Directory",
        })
    return out


def main():
    assets = []
    errors = []
    for mode in (2, 4):
        try:
            rows = parse_tw_page(mode)
            print(f"Taiwan {'listed' if mode == 2 else 'OTC'}: {len(rows)} symbols")
            if len(rows) < 100:
                raise RuntimeError(f"Parsed only {len(rows)} Taiwan symbols; refusing suspicious result")
            assets.extend(rows)
        except Exception as exc:
            errors.append(f"Taiwan mode {mode}: {exc}")
    for url, other in (
        ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", False),
        ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", True),
    ):
        try:
            rows = parse_nasdaq_file(url, other)
            print(f"US directory {'other-listed' if other else 'Nasdaq'}: {len(rows)} symbols")
            if len(rows) < 1000:
                raise RuntimeError(f"Parsed only {len(rows)} US symbols; refusing suspicious result")
            assets.extend(rows)
        except Exception as exc:
            errors.append(f"US directory {url}: {exc}")

    # Do not publish an incomplete universe if any major source failed.
    if errors:
        raise SystemExit("Refusing to write universe.json because a source failed: " + "; ".join(errors))

    seen = set()
    clean = []
    for asset in assets:
        key = (asset["market"], asset["symbol"])
        if key in seen:
            continue
        seen.add(key)
        clean.append(asset)

    tw_count = sum(1 for asset in clean if asset["market"] == "tw")
    us_count = sum(1 for asset in clean if asset["market"] == "us")
    tw_etf = sum(1 for asset in clean if asset["market"] == "tw" and asset["type"] == "ETF")
    us_etf = sum(1 for asset in clean if asset["market"] == "us" and asset["type"] == "ETF")
    if tw_count < 200 or us_count < 1000:
        raise SystemExit(f"Suspicious market counts: Taiwan={tw_count}, US={us_count}; refusing to overwrite")

    payload = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(clean),
        "counts": {
            "taiwan": tw_count,
            "us": us_count,
            "taiwanEtf": tw_etf,
            "usEtf": us_etf,
        },
        "sourceErrors": errors,
        "assets": clean,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {len(clean)} symbols to {OUT}; source errors=0; "
        f"Taiwan={tw_count} (ETF={tw_etf}), US={us_count} (ETF={us_etf})"
    )


if __name__ == "__main__":
    main()
