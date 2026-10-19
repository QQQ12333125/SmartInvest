#!/usr/bin/env python3
"""Build a best-effort symbol universe from public exchange directories."""
import csv, io, json, re, sys, time
from pathlib import Path
from urllib.request import Request, urlopen
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "universe.json"
UA = "SmartInvest-universe-builder/1.0 (GitHub Actions; public directory refresh)"

def get(url):
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,text/plain,*/*"})
    with urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8-sig", errors="replace")

def parse_tw_page(mode):
    # Taiwan Stock Exchange ISIN public directory: mode 2 listed, mode 4 OTC.
    url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
    html = get(url)
    # Use Python's standard library to avoid dependencies in Actions.
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I|re.S)
    out=[]
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.I|re.S)
        if len(cells) < 5: continue
        vals=[]
        for cell in cells:
            cell = re.sub(r"<br\s*/?>", " ", cell, flags=re.I)
            cell = re.sub(r"<[^>]+>", "", cell)
            import html as htmlmod
            vals.append(re.sub(r"\s+", " ", htmlmod.unescape(cell)).strip())
        code_name = vals[0].split()
        if len(code_name) < 2: continue
        symbol, name = code_name[0], " ".join(code_name[1:])
        market_label = vals[3] if len(vals)>3 else ""
        security_type = vals[4] if len(vals)>4 else ""
        # Exclude non-tradable/non-equity items where recognizable; preserve ETFs and listed equity-like securities.
        if not re.fullmatch(r"[0-9A-Z]{4,6}", symbol, flags=re.I): continue
        if symbol.lower() in {"security", "code"}: continue
        text = (name + " " + security_type).lower()
        kind = "ETF" if ("etf" in text or "指數股票型" in text or "指數型基金" in text) else "股票"
        out.append({"symbol":symbol,"name":name,"market":"tw","type":kind,"exchange":"上市" if mode==2 else "上櫃","yahoo":symbol+('.TW' if mode==2 else '.TWO'),"source":"TWSE ISIN securities directory"})
    return out

def parse_nasdaq_file(url, other=False):
    raw=get(url); lines=raw.splitlines(); out=[]
    if not lines: return out
    headers=lines[0].split('|')
    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"): continue
        vals=line.split('|')
        if len(vals)<2: continue
        row=dict(zip(headers, vals))
        symbol=(row.get('Symbol') or row.get('ACT Symbol') or '').strip()
        name=(row.get('Security Name') or '').strip()
        if not symbol or not name: continue
        if row.get('Test Issue','N')=='Y': continue
        if other:
            etf=row.get('ETF','N').strip().upper()=='Y'
            exchange_map={'N':'NYSE','A':'NYSE American','P':'NYSE Arca','Z':'Cboe BZX','V':'IEX'}
            exch=exchange_map.get((row.get('Exchange') or '').strip(), 'US exchange')
        else:
            etf=(row.get('ETF','N').strip().upper()=='Y') or bool(re.search(r'\b(ETF|EXCHANGE.?TRADED FUND)\b', name, re.I))
            exch='NASDAQ'
        # Exclude obvious warrants/rights/units/when-issued where directory naming identifies them;
        # keep ADRs, share classes, preferred securities if present, since directory is broad.
        if symbol in {"Symbol", "ACT Symbol"}: continue
        out.append({"symbol":symbol,"name":name,"market":"us","type":"ETF" if etf else "股票","exchange":exch,"yahoo":symbol.replace('.', '-') ,"source":"Nasdaq Trader Symbol Directory"})
    return out

def main():
    assets=[]; errors=[]
    for mode in (2,4):
        try: assets.extend(parse_tw_page(mode))
        except Exception as e: errors.append(f"Taiwan mode {mode}: {e}")
    for url,other in (("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",False),("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",True)):
        try: assets.extend(parse_nasdaq_file(url,other))
        except Exception as e: errors.append(f"US directory {url}: {e}")
    # Deduplicate; prefer the first source row.
    seen=set(); clean=[]
    for a in assets:
        key=(a['market'],a['symbol'])
        if key in seen: continue
        seen.add(key); clean.append(a)
    if len(clean)<1000:
        raise SystemExit(f"Refusing to overwrite universe.json with suspiciously small universe ({len(clean)} rows). Errors: {'; '.join(errors)}")
    payload={"generatedAt":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"count":len(clean),"sourceErrors":errors,"assets":clean}
    OUT.write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print(f"Wrote {len(clean)} symbols to {OUT}; source errors={len(errors)}")
    if errors: print("Warnings:","; ".join(errors))
if __name__=='__main__': main()
