#!/usr/bin/env python3
"""
Pre-push testsuite – Birds-appen
=================================
Kör automatiskt via .git/hooks/pre-push innan varje 'git push'.
Kan också köras manuellt:  python tests/pre_push_tests.py

Returnerar:
  exit 0  – alla tester godkända, push tillåts
  exit 1  – ett eller flera tester misslyckades, push blockeras
"""

import sys
import re
import subprocess
import os
import io

# Säkerställ UTF-8 output på Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── Färger ────────────────────────────────────────────────────────────────────
OK   = "\033[92m[OK]\033[0m"
ERR  = "\033[91m[FEL]\033[0m"
HEAD = "\033[1;34m"
RST  = "\033[0m"

failures = []
warnings = []

def _content(path):
    """Läs fil relativt repo-root, returnera sträng eller None."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    full = os.path.join(root, path.replace('/', os.sep))
    try:
        with open(full, encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return None

def check(name, ok, hint=""):
    if ok:
        print(f"  {OK} {name}")
    else:
        msg = f"  {ERR} {name}"
        if hint:
            msg += f"\n       → {hint}"
        print(msg)
        failures.append(name)

def has(content, pattern, flags=0):
    """True om regex-mönster finns i content."""
    return bool(re.search(pattern, content or "", flags))

def file_check(path, name, pattern, hint="", negate=False):
    c = _content(path)
    if c is None:
        # Fil saknas – OK om negate=True (filen ska inte finnas), fel annars
        check(name, negate, hint or f"Filen saknas: {path}")
    else:
        found = has(c, pattern)
        ok    = (not found) if negate else found
        check(name, ok, hint or (f"Mönster får ej finnas: {pattern!r}" if negate else f"Mönster saknas: {pattern!r}"))

# ══════════════════════════════════════════════════════════════════════════════
# proxy.py
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{HEAD}proxy.py{RST}")

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
proxy_path = os.path.join(root, "proxy.py")

# Syntax-kontroll
res = subprocess.run(
    [sys.executable, "-m", "py_compile", proxy_path],
    capture_output=True, text=True
)
check("Python-syntax OK", res.returncode == 0, res.stderr.strip())

# Viktiga funktioner
for fn in [
    "_merge_se_records",
    "_build_compact_se",
    "_se_build_one_pass",
    "_gbif_rank",
    "_looks_hybrid",
    "_apply_rank_corrections",
    "_se_rep_empty",
    "_build_species_se",
    "_load_se_obs_r2",
]:
    file_check("proxy.py", f"Funktion {fn}() definierad", rf"def {fn}\s*\(")

# Hybriddetektering – båda varianterna måste finnas
file_check("proxy.py", "Hybrid: kontrollerar '×' (Unicode)",  r"[×]",
           "Saknar Unicode-multiply-tecken i hybridcheck")
file_check("proxy.py", "Hybrid: kontrollerar ' x ' (bokstav)", r"' x '",
           "Saknar mellanrum-x-mellanrum i hybridcheck")

# Namnsammanslagningar
file_check("proxy.py", "_SV_NAME_MERGES definierad",  r"_SV_NAME_MERGES\s*=\s*\{")
file_check("proxy.py", "gråkråka i namnsammanslagningar", r"gråkråka")
file_check("proxy.py", "svartkråka i namnsammanslagningar", r"svartkråka")
file_check("proxy.py", "svname:-prefix för sammanslagningsnyckel", r"svname:")

# Rank-logik: bara SUBSPECIES → sub, inte FORM
file_check("proxy.py", "Bara SUBSPECIES ger 'sub'",
           r"rank\s*==\s*['\"]SUBSPECIES['\"]",
           "Kontrollen 'rank == SUBSPECIES' saknas")

# Grupptaxa (genus/familj) ska INTE räknas som art
file_check("proxy.py", "Grupptaxa GENUS ger 'grp'",
           r"['\"]GENUS['\"]",
           "'GENUS' saknas i rank-checks – grupptaxa filtreras inte")
file_check("proxy.py", "Grupptaxa hoppas över i artspårning",
           r"not\s+is_group",
           "is_group-check saknas i _merge_se_records – grupptaxa räknas som art")

# sub/hyb-fält i reporter-struct
file_check("proxy.py", "sub_ids i reporter-struct",  r"'sub_ids'\s*:\s*set\(\)")
file_check("proxy.py", "hyb_ids i reporter-struct",  r"'hyb_ids'\s*:\s*set\(\)")
file_check("proxy.py", "sub_obs i reporter-struct",  r"'sub_obs'\s*:")
file_check("proxy.py", "hyb_obs i reporter-struct",  r"'hyb_obs'\s*:")

# Kompakt format inkluderar sub/hyb
file_check("proxy.py", "Kompakt format: sub-räknare med", r"'sub'\s*:\s*(rep|d)\.")
file_check("proxy.py", "Kompakt format: hyb-räknare med",  r"'hyb'\s*:\s*(rep|d)\.")
file_check("proxy.py", "Kompakt format: subsp-lista med", r"'subsp'\s*:")
file_check("proxy.py", "Kompakt format: hybsp-lista med",  r"'hybsp'\s*:")

# Observer-filer ska INTE raderas – kommentaren i koden dokumenterar detta
file_check("proxy.py", "Observer-R2-filer raderas inte (kommentar finns)",
           r"observers_se.*kvar|Lämna observers_se|radera.*inte.*observers_se",
           "Saknar kommentar om att observers_se_*.json inte ska raderas")

# Minnesoptimering: historiska stats-år lagras inte i RAM
file_check("proxy.py", "_stats_r2_complete definierad",
           r"_stats_r2_complete\s*=\s*set\(\)",
           "_stats_r2_complete-set saknas – historiska år hålls i RAM")
file_check("proxy.py", "Historiska år hoppar över om de finns i R2",
           r"_stats_r2_complete",
           "Ingen kontroll av _stats_r2_complete i stats-loopen")
file_check("proxy.py", "Historiska år sparas direkt via _r2_put (ej _save_cache)",
           r"_r2_put\(.*stats_cache_.*cache_key",
           "Direktsparning till R2 för historiska år saknas")

# Paginering i /api/observations
file_check("proxy.py", "/api/observations paginerar (MAX_OBS definieras)",
           r"MAX_OBS\s*=\s*\d+",
           "MAX_OBS-konstant saknas – /api/observations paginerar inte")
file_check("proxy.py", "/api/observations returnerar 'truncated'-fält",
           r"'truncated'",
           "truncated-fält saknas i /api/observations-svar")

# ══════════════════════════════════════════════════════════════════════════════
# cloudflare-worker/src/index.js
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{HEAD}cloudflare-worker/src/index.js{RST}")

CW = "cloudflare-worker/src/index.js"

for fn in ["buildObserverResult", "proxyToRailway"]:
    file_check(CW, f"Funktion {fn}() definierad", rf"function {fn}\s*\(")

for route in ["/api/observer_stats", "/api/observer_species", "/api/statistics", "/api/meta"]:
    file_check(CW, f"Route {route} hanteras", re.escape(route))

file_check(CW, "sub-räknare returneras",  r"sub\s*:\s*d\.sub\b")
file_check(CW, "hyb-räknare returneras",  r"hyb\s*:\s*d\.hyb\b")
file_check(CW, "subsp-lista returneras",  r"subsp\s*:")
file_check(CW, "hybsp-lista returneras",  r"hybsp\s*:")
file_check(CW, "CORS-headers satta",      r"Access-Control-Allow-Origin")
file_check(CW, "Bakåtkompatibilitet: gammalt sp/pl-format", r"Array\.isArray.*d\.sp")

# ══════════════════════════════════════════════════════════════════════════════
# faglar-vasterbotten.html
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{HEAD}faglar-vasterbotten.html{RST}")

VB = "faglar-vasterbotten.html"

for fn in ["escHtml", "filteredRows", "rowHtml", "fmtTidCell"]:
    file_check(VB, f"Funktion {fn}() definierad", rf"function {fn}\s*\(")

# Rapportörnamn visas som ren text (ej länk till borttagen observatörssida)
file_check(VB, "Rapportörnamn är ej länk till observatörssidan",
           r"(?<!href=['\"])faglar-observatorer",
           "faglar-observatorer.html finns kvar som länk – ta bort den",
           negate=True)

# Kartknapp
file_check(VB, "Kartknapp finns", r"col-map|mapBtn")

# Dagslistans kolumner
for col in ["col-art", "col-antal", "col-huvud", "col-rap", "col-tid"]:
    file_check(VB, f"Kolumn {col} finns", col)

file_check(VB, "Footer: Version 4.2",       r"Version 4\.2")
file_check(VB, "Footer: Datahämtning-span", r'id="footerDatahamtning"')
file_check(VB, "Footer: hämtar /api/meta",  r"/api/meta")

# ══════════════════════════════════════════════════════════════════════════════
# faglar-statistik.html + faglar-hackning.html – footer
# ══════════════════════════════════════════════════════════════════════════════
for fname, label in [("faglar-statistik.html", "Statistiksidan"),
                     ("faglar-hackning.html",  "Häckningssidan")]:
    print(f"\n{HEAD}{fname}{RST}")
    file_check(fname, f"{label}: Version 4.2",       r"Version 4\.2")
    file_check(fname, f"{label}: Datahämtning-span", r'id="footerDatahamtning"')
    file_check(fname, f"{label}: hämtar /api/meta",  r"/api/meta")

# ══════════════════════════════════════════════════════════════════════════════
# Testfiler – existerar och har rätt struktur
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{HEAD}Testfiler – existerar och är korrekt strukturerade{RST}")

for tf in [
    "tests/test_proxy_logic.py",
    "tests/test_proxy_api.py",
    "tests/test_worker_logic.mjs",
    "tests/test_vasterbotten_logic.mjs",
    "tests/test_statistik_logic.mjs",
    "tests/test_hackning_logic.mjs",
]:
    file_check(tf, f"{tf} finns", r".",
               f"Testfilen {tf} saknas")

# observatörssidan är borttagen – kontrollera att testfilen INTE finns kvar
file_check("tests/test_observatorer_logic.mjs",
           "Obs-testfil borttagen (faglar-observatorer.html raderad)",
           r".", negate=True)

file_check("tests/test_proxy_api.py",           "API-test: TestObservations-klass",  r"class TestObservations")
file_check("tests/test_proxy_api.py",           "API-test: TestStatistics-klass",    r"class TestStatistics")
file_check("tests/test_proxy_api.py",           "API-test: TestBreeding-klass",      r"class TestBreeding")
file_check("tests/test_proxy_api.py",           "API-test: TestMemoryProfile-klass", r"class TestMemoryProfile")
file_check("tests/test_vasterbotten_logic.mjs", "VB-test: normalizeSosResults",     r"normalizeSosResults")
file_check("tests/test_vasterbotten_logic.mjs", "VB-test: rlBadge",                 r"rlBadge")
file_check("tests/test_statistik_logic.mjs",    "Stat-test: updateKpi",             r"updateKpi")
file_check("tests/test_statistik_logic.mjs",    "Stat-test: _muniMonthly",          r"_muniMonthly")
file_check("tests/test_hackning_logic.mjs",     "Hack-test: actCat",                r"actCat")
file_check("tests/test_hackning_logic.mjs",     "Hack-test: renderBreeding",        r"renderBreeding")

# ══════════════════════════════════════════════════════════════════════════════
# Sammanfattning
# ══════════════════════════════════════════════════════════════════════════════
total = len(failures)
print(f"\n{'═'*52}")
if total:
    print(f"{ERR} {total} test(er) misslyckades – push blockeras:\n")
    for f in failures:
        print(f"   • {f}")
    print()
    sys.exit(1)
else:
    print(f"{OK} Alla tester godkända – push tillåts.")
    sys.exit(0)
