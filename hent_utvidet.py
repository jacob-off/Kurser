#!/usr/bin/env python3
"""Test: utvidet kursdata (trinn A). Skriver BARE kurser_utvidet.json og kurser_utvidet_rapport.json.

- Rører ikke kurser.json eller den ordinære arbeidsflyten.
- Kilde: Yahoo Finance via yfinance (forsinket, IKKE sanntid).
- Manglende, ugyldige og gyldige data holdes adskilt. Ingen verdier fylles inn eller gjettes.
- Ved kritisk testfeil skrives IKKE kurser_utvidet.json; bare rapporten (status UGYLDIG), og exit-kode 1.

Bruk:  python hent_utvidet.py            (full kjøring, krever nettilgang til Yahoo og GitHub raw)
       python hent_utvidet.py --selftest (logikktest med konstruerte data i minnet; publiserer ingenting)
"""
import json, math, os, re, sys, time, urllib.request
import datetime as dt
from zoneinfo import ZoneInfo

OSLO = ZoneInfo("Europe/Oslo")
SCHEMA_VERSION = 1
HISTORY_DAYS = 30            # ønsket antall handelsdager
MIN_ROWS = 25                # under dette feiler dekningstesten
FRESH_DAYS = 5               # nyeste dato maks så mange kalenderdager gammel
PROD_URL = "https://raw.githubusercontent.com/jacob-off/kurser/main/kurser.json"
OUT_DATA = "kurser_utvidet.json"
OUT_REPORT = "kurser_utvidet_rapport.json"
FINAL_AFTER = dt.time(16, 45)  # dagens bar regnes som endelig først etter dette (Oslo-tid)
PASS_RATE_CRITICAL = 0.85

# MÅ være lik produksjonslisten i .github/workflows/*.yml. Testen "univers_lik_produksjon" sjekker det.
TICKERS = ["OSEBX.OL", "OBX.OL",
    "EQNR.OL", "DNB.OL", "MOWI.OL", "TEL.OL", "YAR.OL", "NHY.OL", "ORK.OL", "SALM.OL",
    "AKRBP.OL", "KOG.OL", "GJF.OL", "STB.OL", "SRBNK.OL", "SUBC.OL", "FRO.OL", "AKER.OL",
    "BAKKA.OL", "LSG.OL", "TGS.OL", "VAR.OL", "NOD.OL", "NEL.OL", "HEX.OL", "TOM.OL",
    "SCHA.OL", "SCHB.OL", "ELK.OL", "ENTRA.OL", "AUSS.OL", "VEI.OL", "EPR.OL", "KID.OL",
    "PROT.OL", "MING.OL", "BWLPG.OL", "HAFNI.OL", "WAWI.OL", "BORR.OL", "ODL.OL", "AKSO.OL",
    "NAS.OL", "ATEA.OL", "CRAYN.OL", "KIT.OL", "BONHR.OL", "PLT.OL", "RECSI.OL", "AUTO.OL"]
CORE = ["OSEBX.OL", "EQNR.OL", "DNB.OL"]
CALENDAR_REF = "OSEBX.OL"
OSLO_EXCHANGES = {"OSL"}

# Alternative koder som BARE testes og dokumenteres. De tas ikke inn i universet.
# kontroll=True: kode som forventes å være et annet selskap/marked (negativ kontroll eller etterfølger).
ALTERNATIVER = {
    "SRBNK.OL": {"selskap": "SpareBank 1 Sør-Norge", "navn": ["sparebank 1 sor norge"],
                 "koder": [("SRBNK.OL", False), ("SRBANK.OL", False), ("SB1NO.OL", False), ("SOON.OL", True)]},
    "SCHA.OL": {"selskap": "Schibsted (A-aksje)", "navn": ["schibsted"],
                "koder": [("SCHA.OL", False), ("SCHB.OL", True)]},
    "SCHB.OL": {"selskap": "Schibsted (B-aksje)", "navn": ["schibsted"],
                "koder": [("SCHB.OL", False), ("SCHA.OL", True)]},
    "CRAYN.OL": {"selskap": "Crayon Group", "navn": ["crayon"],
                 "koder": [("CRAYN.OL", False), ("SWON.SW", True)]},
    "RECSI.OL": {"selskap": "REC Silicon", "navn": ["rec silicon"],
                 "koder": [("RECSI.OL", False)]},
}

# ---------------------------------------------------------------- rene funksjoner

def num(x):
    try:
        if x is None:
            return None
        f = float(x)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return None


def r4(x):
    return None if x is None else round(x, 4)


def is_final(date_str, now_oslo):
    """Dagens bar er ikke endelig før børsen er stengt og Yahoo har rukket å oppdatere."""
    today = now_oslo.date().isoformat()
    if date_str > today:
        return False
    if date_str == today and now_oslo.time() < FINAL_AFTER:
        return False
    return True


def validate_rows(raw_rows, now_oslo):
    """raw_rows: [{date, open, high, low, close, volume}] i kildens rekkefølge.
    Returnerer (gyldige, ugyldige, manglende_felt). Ingenting fylles inn."""
    valid, invalid, missing = [], [], []
    for r in raw_rows:
        d = r["date"]
        o, h, l, c, v = (num(r.get(k)) for k in ("open", "high", "low", "close", "volume"))
        reasons = []
        try:
            wd = dt.date.fromisoformat(d).weekday()
            if wd >= 5:
                reasons.append("helgedato")
        except Exception:
            reasons.append("ugyldig_datoformat")
        if c is None or c <= 0:
            reasons.append("close_mangler_eller_ikke_positiv")
        for name, val in (("open", o), ("high", h), ("low", l)):
            if val is not None and val <= 0:
                reasons.append(name + "_ikke_positiv")
        present = [x for x in (o, c) if x is not None and x > 0]
        if h is not None and l is not None and h < l:
            reasons.append("high_under_low")
        if h is not None and present and h < max(present) * (1 - 1e-9):
            reasons.append("high_under_open_eller_close")
        if l is not None and present and l > min(present) * (1 + 1e-9):
            reasons.append("low_over_open_eller_close")
        if v is not None and v < 0:
            reasons.append("negativt_volum")
        if reasons:
            invalid.append({"date": d, "reasons": reasons,
                            "raw": {"open": o, "high": h, "low": l, "close": c, "volume": v}})
            continue
        flags = []
        for name, val in (("open", o), ("high", h), ("low", l), ("volume", v)):
            if val is None:
                missing.append({"date": d, "field": name, "reason": "mangler fra kilden (ikke fylt inn)"})
        if v is not None and v == 0:
            flags.append("volum_0_fra_kilde")  # verdien beholdes uendret
        valid.append({
            "date": d, "open": r4(o), "high": r4(h), "low": r4(l), "close": r4(c),
            "volume": None if v is None else int(round(v)),
            "turnoverEstNok": None if v is None else round(c * v, 0),
            "final": is_final(d, now_oslo), "flags": flags})
    return valid, invalid, missing


def chronology_ok(dates):
    return all(a < b for a, b in zip(dates, dates[1:]))


def compare_with_prod(valid_rows, prod_closes, tol=0.0005):
    """Sammenlign sluttkurser for datoer som finnes i begge (kun endelige rader).
    Returnerer (antall_sammenlignet, avvik[])."""
    prod = {x["date"]: x["close"] for x in prod_closes}
    n, diffs = 0, []
    for r in valid_rows:
        if not r["final"] or r["date"] not in prod or prod[r["date"]] in (None, 0):
            continue
        n += 1
        rel = abs(r["close"] / prod[r["date"]] - 1)
        if rel > tol:
            diffs.append({"date": r["date"], "ny": r["close"], "produksjon": prod[r["date"]], "relAvvik": round(rel, 6)})
    return n, diffs


def run_ticker_tests(sym, meta, raw_dates, valid, invalid, cal_dates, prod_entry, prod_ok, now_oslo):
    """Returnerer dict testnavn -> {resultat: bestått|feilet|ikke_utført, detalj}."""
    T = {}

    def put(name, res, detail=""):
        T[name] = {"resultat": res, "detalj": detail}

    # 1 marked/valuta
    if not sym.endswith(".OL"):
        put("marked", "feilet", "ticker slutter ikke på .OL")
    elif not meta or not meta.get("exchange"):
        put("marked", "feilet", "børs ikke oppgitt av kilden; kan ikke bekrefte marked")
    elif meta["exchange"] not in OSLO_EXCHANGES:
        put("marked", "feilet", "børs=%s (forventet OSL)" % meta["exchange"])
    elif meta.get("currency") != "NOK":
        put("marked", "feilet", "valuta=%s (forventet NOK)" % meta.get("currency"))
    else:
        put("marked", "bestått", "børs=%s valuta=%s" % (meta["exchange"], meta["currency"]))
    # 2 kronologi i kildens rekkefølge, ingen duplikater
    if chronology_ok(raw_dates):
        put("kronologi", "bestått", "datoer strengt stigende i kildens rekkefølge")
    else:
        put("kronologi", "feilet", "datoer ikke strengt stigende eller duplikater")
    # 3 OHLC-konsistens og helg
    put("ohlc_konsistens", "bestått" if not invalid else "feilet",
        "" if not invalid else "%d ugyldige rader" % len(invalid))
    # 4 mot handelskalender (OSEBX)
    if sym == CALENDAR_REF:
        put("kalender", "bestått", "er selv kalenderreferansen")
    elif cal_dates is None:
        put("kalender", "ikke_utført", "kalenderreferanse (OSEBX) mangler")
    else:
        extra = [r["date"] for r in valid if r["date"] not in cal_dates]
        put("kalender", "bestått" if not extra else "feilet",
            "" if not extra else "datoer som ikke finnes i OSEBX: %s" % extra[:5])
    # 5 dekning
    put("dekning", "bestått" if len(valid) >= MIN_ROWS else "feilet",
        "%d gyldige rader (krav ≥ %d)" % (len(valid), MIN_ROWS))
    # 6 ferskhet
    if valid:
        age = (now_oslo.date() - dt.date.fromisoformat(valid[-1]["date"])).days
        put("ferskhet", "bestått" if age <= FRESH_DAYS else "feilet",
            "nyeste dato %s (%d dager gammel)" % (valid[-1]["date"], age))
    else:
        put("ferskhet", "feilet", "ingen gyldige rader")
    # 7 mot produksjonsfilen
    if not prod_ok:
        put("mot_produksjon", "ikke_utført", "produksjonsfilen kunne ikke leses")
    elif prod_entry is None:
        put("mot_produksjon", "ikke_utført", "ticker finnes ikke i produksjonsfilen (feilet der)")
    else:
        n, diffs = compare_with_prod(valid, prod_entry.get("dailyCloses", []))
        if n == 0:
            put("mot_produksjon", "ikke_utført", "ingen overlappende endelige datoer")
        else:
            put("mot_produksjon", "bestått" if not diffs else "feilet",
                "%d datoer sammenlignet" % n if not diffs else "avvik: %s" % diffs[:3])
    return T


def ticker_passes(tests):
    # ikke_utført godtas bare for mot_produksjon og kalender; markedsbekreftelse må være utført.
    for name, t in tests.items():
        if t["resultat"] == "feilet":
            return False
        if t["resultat"] == "ikke_utført" and name not in ("mot_produksjon", "kalender"):
            return False
    return True


def norm_name(s):
    s = (s or "").lower().replace("ø", "o").replace("å", "a").replace("æ", "ae")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def alt_verdict(info, forventet_navn, kontroll, now_oslo):
    if info.get("error") or not info.get("rows"):
        return "ingen_data"
    if info.get("exchange") not in OSLO_EXCHANGES:
        return "annet_marked(%s)" % info.get("exchange")
    if info.get("currency") != "NOK":
        return "annet_marked(valuta %s)" % info.get("currency")
    age = (now_oslo.date() - dt.date.fromisoformat(info["lastDate"])).days
    if age > 14:
        return "foreldet_kurs(%d dager; mulig avnotert)" % age
    nm = norm_name(info.get("name"))
    if not nm:
        return "navn_ikke_oppgitt(kan_ikke_bekrefte_selskap)"
    if any(k in nm for k in forventet_navn):
        return "fungerer_og_samsvarer"
    return "annet_selskap"


# ---------------------------------------------------------------- nettverk (importeres sent)

def http_json(url):
    req = urllib.request.Request(url + ("&" if "?" in url else "?") + "t=%d" % int(time.time()),
                                 headers={"User-Agent": "kurser-test/1"})
    with urllib.request.urlopen(req, timeout=25) as f:
        return json.load(f)


def fetch_raw(yf, sym, period="2mo"):
    """Returnerer dict: rows (kildens rekkefølge, hele perioden), meta, error."""
    last_err = None
    for attempt in range(2):
        try:
            tk = yf.Ticker(sym)
            d = tk.history(period=period, interval="1d", auto_adjust=False, actions=False)
            if d is None or d.empty:
                raise ValueError("ingen daglige data fra kilden")
            rows = []
            for idx, r in d.iterrows():
                rows.append({"date": str(idx.date()), "open": r.get("Open"), "high": r.get("High"),
                             "low": r.get("Low"), "close": r.get("Close"), "volume": r.get("Volume")})
            meta = {}
            try:
                hm = tk.history_metadata or {}
                meta = {"exchange": hm.get("exchangeName"), "fullExchange": hm.get("fullExchangeName"),
                        "currency": hm.get("currency"), "instrumentType": hm.get("instrumentType"),
                        "name": hm.get("longName") or hm.get("shortName"), "symbol": hm.get("symbol")}
            except Exception as e:  # metadata utilgjengelig
                meta = {"error": str(e)[:100]}
            return {"rows": rows, "meta": meta, "error": None}
        except Exception as e:
            last_err = str(e)[:160]
            time.sleep(1.5)
    return {"rows": [], "meta": {}, "error": last_err}


# ---------------------------------------------------------------- selvtest (konstruerte data, publiseres aldri)

def selftest():
    now = dt.datetime(2026, 10, 14, 13, 0, tzinfo=OSLO)
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    good = {"date": "2026-10-12", "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000}
    v, i, m = validate_rows([good], now)
    check(len(v) == 1 and not i and not m, "gyldig rad skal passere")
    check(v[0]["turnoverEstNok"] == 104000, "omsetning = close*volum")
    check(v[0]["final"] is True, "gammel dag er endelig")
    v, i, m = validate_rows([dict(good, high=103)], now)
    check(len(i) == 1 and "high_under_open_eller_close" in i[0]["reasons"], "high under close skal være ugyldig")
    v, i, m = validate_rows([dict(good, low=105)], now)
    check(len(i) == 1, "low over close skal være ugyldig")
    v, i, m = validate_rows([dict(good, date="2026-10-10")], now)  # lørdag
    check(len(i) == 1 and "helgedato" in i[0]["reasons"], "helgedato skal være ugyldig")
    v, i, m = validate_rows([dict(good, open=float("nan"))], now)
    check(len(v) == 1 and any(x["field"] == "open" for x in m), "NaN open = manglende felt, ikke ugyldig, ikke utfylt")
    check(v[0]["open"] is None, "manglende open forblir null (ikke gjettet)")
    v, i, m = validate_rows([dict(good, close=None)], now)
    check(len(i) == 1, "manglende close = ugyldig rad")
    v, i, m = validate_rows([dict(good, volume=0)], now)
    check(v[0]["volume"] == 0 and "volum_0_fra_kilde" in v[0]["flags"], "volum 0 beholdes og flagges")
    v, i, m = validate_rows([dict(good, date="2026-10-14")], now)  # i dag, før 16:45
    check(v[0]["final"] is False, "dagens bar før børsslutt er ikke endelig")
    check(chronology_ok(["2026-10-09", "2026-10-12"]), "stigende datoer ok")
    check(not chronology_ok(["2026-10-12", "2026-10-09"]), "synkende datoer skal feile")
    check(not chronology_ok(["2026-10-12", "2026-10-12"]), "duplikater skal feile")
    v, _, _ = validate_rows([good], now)
    n, d = compare_with_prod(v, [{"date": "2026-10-12", "close": 104.0}])
    check(n == 1 and not d, "lik produksjonskurs gir ingen avvik")
    n, d = compare_with_prod(v, [{"date": "2026-10-12", "close": 110.0}])
    check(n == 1 and len(d) == 1, "avvikende produksjonskurs skal fanges")
    meta_ok = {"exchange": "OSL", "currency": "NOK"}
    t = run_ticker_tests("EQNR.OL", meta_ok, ["2026-10-12"], v * 30, [], {"2026-10-12"}, None, True, now)
    check(t["marked"]["resultat"] == "bestått", "OSL/NOK skal bestå")
    t = run_ticker_tests("EQNR.OL", {"exchange": "NYQ", "currency": "USD"}, ["2026-10-12"], v, [], None, None, True, now)
    check(t["marked"]["resultat"] == "feilet", "feil marked skal feile")
    check(not ticker_passes(t), "ticker med feil marked består ikke")
    t = run_ticker_tests("EQNR.OL", {}, ["2026-10-12"], v, [], None, None, True, now)
    check(t["marked"]["resultat"] == "feilet", "manglende børsmetadata kan ikke bekreftes -> feilet")
    check(alt_verdict({"rows": [1], "exchange": "OSL", "currency": "NOK", "lastDate": "2026-10-13",
                       "name": "Crayon Group Holding ASA", "symbol": "CRAYN.OL"}, ["crayon"], False, now)
          == "fungerer_og_samsvarer", "navnesamsvar")
    check(alt_verdict({"rows": [1], "exchange": "OSL", "currency": "NOK", "lastDate": "2026-05-01",
                       "name": "Crayon", "symbol": "CRAYN.OL"}, ["crayon"], False, now).startswith("foreldet"),
          "gammel kurs = foreldet")
    return fails


# ---------------------------------------------------------------- hovedprogram

def main():
    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    now_oslo = now_utc.astimezone(OSLO)
    report = {"schemaVersion": SCHEMA_VERSION, "runAt": now_utc.isoformat(), "runAtOslo": now_oslo.isoformat(),
              "status": None, "selftest": None, "kritisk": {}, "teller": {}, "tickere": {}, "alternativer": {},
              "merknader": []}

    st = selftest()
    report["selftest"] = {"resultat": "bestått" if not st else "feilet", "feil": st,
                          "merknad": "Logikktest med konstruerte data i minnet; ingen av disse dataene publiseres."}
    if st:
        report["status"] = "UGYLDIG"
        report["merknader"].append("Selvtest feilet; ingen datafil skrevet.")
        json.dump(report, open(OUT_REPORT, "w"), ensure_ascii=False, indent=1)
        print("SELVTEST FEILET", st)
        sys.exit(1)
    if "--selftest" in sys.argv:
        print("SELVTEST OK")
        return

    import yfinance as yf
    report["yfinance"] = getattr(yf, "__version__", "ukjent")

    # produksjonsfilen (kun lesing)
    prod, prod_ok = {}, False
    try:
        prod_file = http_json(PROD_URL)
        prod = prod_file.get("quotes", {})
        prod_failed = prod_file.get("failed", {})
        prod_ok = bool(prod)
        report["produksjonsfil"] = {"generatedAt": prod_file.get("generatedAt"), "antallQuotes": len(prod),
                                    "antallFailed": len(prod_failed)}
    except Exception as e:
        prod_failed = {}
        report["produksjonsfil"] = {"feil": str(e)[:160]}

    # forrige utvidede fil (for videreføring av gyldige data ved feil)
    previous = {}
    if os.path.exists(OUT_DATA):
        try:
            previous = json.load(open(OUT_DATA)).get("quotes", {})
        except Exception:
            previous = {}

    # hent alt
    fetched = {}
    for sym in TICKERS:
        fetched[sym] = fetch_raw(yf, sym)
        time.sleep(0.4)

    # kalenderreferanse
    cal_dates = None
    if fetched[CALENDAR_REF]["rows"]:
        v, _, _ = validate_rows(fetched[CALENDAR_REF]["rows"], now_oslo)
        cal_dates = {r["date"] for r in v}

    quotes, failed, ticker_results = {}, {}, {}
    for sym in TICKERS:
        f = fetched[sym]
        fetched_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        if f["error"]:
            tests = {"henting": {"resultat": "feilet", "detalj": f["error"]}}
            valid, invalid, missing = [], [], []
        else:
            raw_dates = [r["date"] for r in f["rows"]]
            valid_all, invalid, missing = validate_rows(f["rows"], now_oslo)
            valid = valid_all[-HISTORY_DAYS:]
            keep = {r["date"] for r in valid}
            missing = [x for x in missing if x["date"] in keep]
            invalid = [x for x in invalid if x["date"] >= (valid[0]["date"] if valid else "0000")]
            tests = run_ticker_tests(sym, f["meta"], raw_dates, valid, invalid, cal_dates,
                                     prod.get(sym), prod_ok, now_oslo)
        passed = ticker_passes(tests)
        ticker_results[sym] = {"bestått": passed,
                               "feilede_tester": {k: v["detalj"] for k, v in tests.items() if v["resultat"] == "feilet"},
                               "ikke_utførte_tester": [k for k, v in tests.items() if v["resultat"] == "ikke_utført"]}
        missing_dates = []
        if sym != CALENDAR_REF and cal_dates and valid:
            lo = valid[0]["date"]
            missing_dates = sorted(d for d in cal_dates if d >= lo and d not in {r["date"] for r in valid})
        entry = {"status": None, "fetchedAt": fetched_at, "name": f["meta"].get("name"),
                 "exchange": f["meta"].get("exchange"), "currency": f["meta"].get("currency"),
                 "history": valid, "invalidRows": invalid, "missingFields": missing,
                 "missingDates": missing_dates, "tests": tests, "carriedForward": False, "error": f["error"]}
        if passed:
            entry["status"] = "gyldig" if not (missing or missing_dates) else "gyldig_med_mangler"
        else:
            entry["status"] = "feilet_tester" if not f["error"] else "feilet_henting"
            failed[sym] = f["error"] or "; ".join("%s: %s" % (k, v) for k, v in ticker_results[sym]["feilede_tester"].items())
            # ikke erstatt tidligere gyldige data med tomt: behold forrige gyldige historikk, tydelig merket
            old = previous.get(sym)
            if old and old.get("history") and old.get("status", "").startswith("gyldig") and not old.get("carriedForward"):
                entry["history"] = old["history"]
                entry["carriedForward"] = True
                entry["carriedFromFetchedAt"] = old.get("fetchedAt")
                entry["invalidRows"] = invalid  # dagens avviste rader, ikke de gamle
            else:
                entry["history"] = []  # ugyldige data publiseres ikke som historikk
        quotes[sym] = entry

    # tellere
    tested = len(TICKERS)
    passed_n = sum(1 for r in ticker_results.values() if r["bestått"])
    per_test = {}
    for sym, e in quotes.items():
        for name, t in e["tests"].items():
            per_test.setdefault(name, {"bestått": 0, "feilet": [], "ikke_utført": []})
            if t["resultat"] == "bestått":
                per_test[name]["bestått"] += 1
            elif t["resultat"] == "feilet":
                per_test[name]["feilet"].append(sym)
            else:
                per_test[name]["ikke_utført"].append(sym)
    report["teller"] = {"testet": tested, "bestått": passed_n, "feilet": tested - passed_n,
                        "perTest": per_test}
    report["tickere"] = ticker_results

    # kritiske tester
    univ_prod = set(prod) | set(prod_failed)
    kritisk = {
        "produksjonsfil_lesbar": prod_ok,
        "univers_lik_produksjon": (set(TICKERS) == univ_prod) if prod_ok else False,
        "kalenderreferanse_OSEBX_ok": ticker_results[CALENDAR_REF]["bestått"],
        "kjernetickere_bestått": all(ticker_results[c]["bestått"] for c in CORE),
        "bestått_andel_minst_%d_prosent" % int(PASS_RATE_CRITICAL * 100): passed_n / tested >= PASS_RATE_CRITICAL,
    }
    if prod_ok and set(TICKERS) != univ_prod:
        report["merknader"].append("Univers-avvik: kun i test=%s, kun i produksjon=%s" %
                                   (sorted(set(TICKERS) - univ_prod), sorted(univ_prod - set(TICKERS))))
    report["kritisk"] = kritisk
    all_critical_ok = all(kritisk.values())

    # alternative koder (kun dokumentasjon; endrer ikke universet)
    for orig, spec in ALTERNATIVER.items():
        res = {"selskap": spec["selskap"], "kandidater": [], "sok": None}
        for code, kontroll in spec["koder"]:
            raw = fetch_raw(yf, code, period="3mo")
            info = {"symbol": code, "error": raw["error"], "rows": raw["rows"], "exchange": raw["meta"].get("exchange"),
                    "currency": raw["meta"].get("currency"), "name": raw["meta"].get("name")}
            if raw["rows"]:
                info["lastDate"] = raw["rows"][-1]["date"]
                info["lastClose"] = r4(num(raw["rows"][-1]["close"]))
            verdict = alt_verdict(info, spec["navn"], kontroll, now_oslo)
            res["kandidater"].append({"kode": code, "kontrollkode": kontroll, "verdikt": verdict,
                                      "navnFraKilde": info["name"], "børs": info["exchange"], "valuta": info["currency"],
                                      "sisteDato": info.get("lastDate"), "sisteClose": info.get("lastClose"),
                                      "antallRader": len(raw["rows"]), "feil": raw["error"]})
            time.sleep(0.4)
        try:
            s = yf.Search(spec["selskap"].split(" (")[0], max_results=8)
            res["sok"] = [{"symbol": q.get("symbol"), "navn": q.get("shortname") or q.get("longname"),
                           "børs": q.get("exchDisp") or q.get("exchange")} for q in (s.quotes or [])]
        except Exception as e:
            res["sok"] = "ikke tilgjengelig: %s" % str(e)[:100]
        report["alternativer"][orig] = res
    report["alternativer_merknad"] = ("Kun dokumentasjon. Kodene er IKKE tatt inn i universet. 'fungerer_og_samsvarer' "
                                      "er en automatisk navnesjekk og må bekreftes manuelt før en kode eventuelt tas i bruk.")

    if not all_critical_ok:
        report["status"] = "UGYLDIG"
        report["merknader"].append("Kritiske tester feilet; %s er IKKE skrevet." % OUT_DATA)
        json.dump(report, open(OUT_REPORT, "w"), ensure_ascii=False, indent=1)
        print("UGYLDIG: kritiske tester feilet", {k: v for k, v in kritisk.items() if not v})
        sys.exit(1)

    status = "gyldig" if passed_n == tested else "delvis_gyldig"
    out = {
        "schemaVersion": SCHEMA_VERSION, "status": status,
        "generatedAt": now_utc.isoformat(), "generatedAtOslo": now_oslo.isoformat(),
        "source": {"navn": "Yahoo Finance via yfinance", "yfinanceVersjon": report["yfinance"],
                   "forsinkelse": "Forsinket data (Yahoo leverer normalt Oslo Børs med ca. 15 minutters forsinkelse). IKKE sanntid. "
                                  "Dagens bar er ufullstendig (final=false) til etter børsslutt.",
                   "justering": "auto_adjust=False. 'close' er Yahoos Close (justert for splitter, ikke for utbytte)."},
        "vindu": {"ønsketHandelsdager": HISTORY_DAYS, "minimumForGyldig": MIN_ROWS},
        "definisjoner": {"turnoverEstNok": "ANSLAG = close × volume (ikke børsens omsetning)",
                         "missingFields": "felt kilden ikke leverte; verdien er null og er ikke fylt inn",
                         "invalidRows": "rader som feilet kontroll; ikke med i history",
                         "missingDates": "handelsdager i OSEBX-kalenderen som mangler for tickeren",
                         "carriedForward": "true = forrige gyldige historikk beholdt fordi dagens henting feilet"},
        "tester": {"testet": tested, "bestått": passed_n, "feilet": tested - passed_n, "kritisk": kritisk},
        "quotes": quotes, "failed": failed}
    json.dump(out, open(OUT_DATA, "w"), ensure_ascii=False, indent=1)
    report["status"] = status
    json.dump(report, open(OUT_REPORT, "w"), ensure_ascii=False, indent=1)
    print(status, "- testet", tested, "bestått", passed_n, "feilet", tested - passed_n)


if __name__ == "__main__":
    main()
