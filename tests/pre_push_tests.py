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

def file_check(path, name, pattern, hint=""):
    c = _content(path)
    if c is None:
        check(name, False, f"Filen saknas: {path}")
    else:
        check(name, has(c, pattern), hint or f"Mönster saknas: {pattern!r}")

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
# faglar-observatorer.html
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{HEAD}faglar-observatorer.html{RST}")

OBS = "faglar-observatorer.html"

for fn in ["fetchData", "applyFilters", "toggleDetail", "toggleSpList",
           "buildDetail", "renderAll", "renderPagination", "goPage"]:
    file_check(OBS, f"Funktion {fn}() definierad", rf"function {fn}\s*\(")

for eid in ["searchInput", "tableBody", "pagination", "emptyState"]:
    file_check(OBS, f"Element #{eid} finns", rf'id="{eid}"')

# URL-param reporter
file_check(OBS, "Hanterar ?reporter= URL-param",
           r"get\(['\"]reporter['\"]\)",
           "URLSearchParams .get('reporter') saknas")
file_check(OBS, "openRow sätts från reporter-param",
           r"openRow\s*=\s*_reporterParam",
           "openRow = _reporterParam saknas")
file_check(OBS, "searchInput fylls från reporter-param",
           r"searchInput.*value\s*=\s*_reporterParam|value\s*=\s*_reporterParam.*searchInput",
           "searchInput.value = _reporterParam saknas")
file_check(OBS, "Artlistan öppnas automatiskt (toggleSpList kallas)",
           r"toggleSpList\s*\(_reporterParam",
           "toggleSpList(_reporterParam, ...) saknas i URL-param-hantering")

# Listnings-ID:n
file_check(OBS, "sp-list ID-mönster",  r"sp-list-\$\{safeId\}")
file_check(OBS, "sub-list ID-mönster", r"sub-list-\$\{safeId\}")
file_check(OBS, "hyb-list ID-mönster", r"hyb-list-\$\{safeId\}")

# Alfabetisk sortering
file_check(OBS, "Alfabetisk sortering med sv-locale",
           r"localeCompare\([^)]*'sv'[^)]*\)",
           "localeCompare(..., 'sv') saknas i sortering")

# sub/hyb sektioner i buildDetail
file_check(OBS, "buildDetail: sub-sektion visas om r.sub > 0",
           r"r\.sub\s*>\s*0",
           "sub-sektion saknas i buildDetail")
file_check(OBS, "buildDetail: hyb-sektion visas om r.hyb > 0",
           r"r\.hyb\s*>\s*0",
           "hyb-sektion saknas i buildDetail")

# Favoritfunktion
file_check(OBS, "Favoritfunktion bevarad", r"favorites")

# Default favoriter
file_check(OBS, "onlyFavs är true som default",
           r"let\s+onlyFavs\s*=\s*true",
           "onlyFavs ska vara true som standard")
file_check(OBS, "favToggle har active-klass som default",
           r'class="fav-toggle active"',
           "favToggle-knappen saknar active-klass i HTML")
file_check(OBS, "Tom favoritlista ger hjälpmeddelande",
           r"Inga favoriter",
           "Meddelande för tom favoritlista saknas")

# Footer
file_check(OBS, "Footer: Version 4.2", r"Version 4\.2")
file_check(OBS, "Footer: Datahämtning-span", r'id="footerDatahamtning"')
file_check(OBS, "Footer: hämtar /api/meta", r"/api/meta")

# ══════════════════════════════════════════════════════════════════════════════
# faglar-vasterbotten.html
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{HEAD}faglar-vasterbotten.html{RST}")

VB = "faglar-vasterbotten.html"

for fn in ["escHtml", "filteredRows", "rowHtml", "fmtTidCell"]:
    file_check(VB, f"Funktion {fn}() definierad", rf"function {fn}\s*\(")

# Rapportörslänk
file_check(VB, "Rapportörnamn är länk till observatörssidan",
           r"faglar-observatorer\.html\?reporter=",
           "Länk till faglar-observatorer.html?reporter= saknas")
file_check(VB, "Reporter-namn URL-encodas",
           r"encodeURIComponent\s*\(\s*row\.reporter\s*\)",
           "encodeURIComponent(row.reporter) saknas")
file_check(VB, "Klick-propagation stoppas på rapportörslänk",
           r"event\.stopPropagation\s*\(\s*\)",
           "event.stopPropagation() saknas på rapportörslänken")

# Kartknapp
file_check(VB, "Kartknapp finns", r"col-map|mapBtn")

# Dagslistans kolumner
for col in ["col-art", "col-antal", "col-huvud", "col-rap", "col-tid"]:
    file_check(VB, f"Kolumn {col} finns", col)

# Footer
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
