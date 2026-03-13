"""
Fågelobservationer Västerbotten – Lokal API-proxy
==================================================
Stödjer tre autentiseringslägen:
  1. Prenumerationsnyckel (API-nyckel) – enklast, testar automatiskt
  2. Prenumerationsnyckel + SLU-inloggning – för skyddade arter
  3. Ingen nyckel + SLU-inloggning – om portalen ännu ej besökts

Krav:  pip install flask flask-cors requests
Starta: python proxy.py
"""

import sys as _sys, io as _io
if _sys.stdout and hasattr(_sys.stdout, 'buffer') and \
        (_sys.stdout.encoding or '').lower() not in ('utf-8', 'utf8'):
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')

from flask import Flask, request, jsonify, send_from_directory
import os as _os
from flask_cors import CORS
import requests
import re
import secrets
from urllib.parse import urljoin
import threading as _threading
import json as _json
import time as _time
from datetime import datetime as _dt, date as _date_type
from collections import defaultdict as _defaultdict

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── Konstanter ─────────────────────────────────────────────────────────────
APP_VERSION   = "2.4"          # Uppdatera vid varje deploy
_SERVER_START = _dt.now()      # Tidpunkt då servern startades

SLU_AUTH_URL  = "https://useradmin-auth.slu.se/connect/authorize"
SOS_API_BASE  = "https://api.artdatabanken.se/species-observation-system/v1"
CLIENT_ID     = "Artportalen"
REDIRECT_URI  = "https://www.artportalen.se/authentication/callback"
SCOPE         = "openid email profile SOS.Observations.Protected"

AVES_TAXON_ID           = 4000104
VASTERBOTTEN_FEATURE_ID = "24"

# Obligatoriska headers för SOS API
SOS_EXTRA_HEADERS = {
    "X-Api-Version":      "1.5",
    "X-Requesting-System": "Faglar-Vasterbotten",
}

# ── Session-state ───────────────────────────────────────────────────────────
_session = {
    "access_token":     None,
    "subscription_key": "",
    "username":         None,
    "auth_mode":        None,   # 'sub_key_only' | 'bearer' | 'bearer+sub_key'
}


# ── Hjälp: testa om prenumerationsnyckel fungerar ensam ────────────────────
def _test_sub_key(sub_key: str) -> bool:
    """Gör ett lättviktsanrop mot SOS API – returnerar True om nyckeln godkänns."""
    try:
        # Minimalt sökfilter – bara för att validera nyckeln
        body = {
            "taxon": {"ids": [AVES_TAXON_ID], "includeUnderlyingTaxa": False},
            "date":  {"startDate": "2024-01-01", "endDate": "2024-01-01",
                      "dateFilterType": "OverlappingStartDateAndEndDate"},
            "geographics": {"areas": [{"areaType": "County",
                                        "featureId": VASTERBOTTEN_FEATURE_ID}]},
        }
        resp = requests.post(
            f"{SOS_API_BASE}/Observations/Search",
            headers={
                "Ocp-Apim-Subscription-Key": sub_key,
                "Content-Type": "application/json",
                "Accept":       "application/json",
                **SOS_EXTRA_HEADERS,
            },
            json=body,
            params={"skip": 0, "take": 1},
            timeout=15,
        )
        print(f"  API-nyckeltest: HTTP {resp.status_code} – {resp.text[:120]}")
        return resp.status_code < 400
    except Exception as e:
        print(f"  API-nyckeltest: nätverksfel – {e}")
        return False


# ── Hjälp: SLU-inloggning via formulärflöde ────────────────────────────────
def _slu_login_flow(username: str, password: str) -> str:
    """
    Följer SLU:s OAuth2 implicit-flöde (form_post).
    Returnerar access_token-strängen.
    """
    web = requests.Session()
    web.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    })

    auth_resp = web.get(
        SLU_AUTH_URL,
        params={
            "client_id":     CLIENT_ID,
            "scope":         SCOPE,
            "response_type": "token id_token",
            "response_mode": "form_post",
            "redirect_uri":  REDIRECT_URI,
            "state":         secrets.token_urlsafe(16),
            "nonce":         secrets.token_urlsafe(16),
        },
        timeout=20,
    )
    auth_resp.raise_for_status()
    print(f"  SLU-login: hämtade inloggningssida ({auth_resp.status_code})")

    # Hitta formulärets action-URL
    action_m = re.search(
        r'<form[^>]+action=["\']([^"\']+)["\']', auth_resp.text, re.IGNORECASE
    )
    if not action_m:
        raise Exception("Hittade inte inloggningsformuläret på SLU:s sida.")

    form_action = action_m.group(1)
    if not form_action.startswith("http"):
        form_action = urljoin(auth_resp.url, form_action)

    # Samla formulärfält
    form_data = {}
    for m in re.finditer(r"<input([^>]*)>", auth_resp.text, re.IGNORECASE):
        attrs     = m.group(1)
        name_m    = re.search(r'name=["\']([^"\']+)["\']',  attrs)
        value_m   = re.search(r'value=["\']([^"\']*)["\']', attrs)
        type_m    = re.search(r'type=["\']([^"\']+)["\']',  attrs)
        if not name_m:
            continue
        name       = name_m.group(1)
        value      = value_m.group(1) if value_m else ""
        field_type = type_m.group(1).lower() if type_m else "text"

        if field_type == "email" or "email" in name.lower():
            form_data[name] = username
        elif field_type == "password" or "password" in name.lower():
            form_data[name] = password
        elif field_type not in ("submit", "button", "image", "reset"):
            form_data[name] = value

    # Fallback för vanliga IdentityServer4-fältnamn
    if not any(k.lower() in ("email", "username") for k in form_data):
        form_data["Username"] = username
        form_data["Email"]    = username
    if not any("password" in k.lower() for k in form_data):
        form_data["Password"] = password

    print(f"  SLU-login: skickar formulär till {form_action}")
    login_resp = web.post(
        form_action, data=form_data, allow_redirects=True, timeout=25
    )
    print(f"  SLU-login: formulärsvar {login_resp.status_code}, "
          f"innehåller access_token: {'access_token' in login_resp.text}")

    if "access_token" not in login_resp.text:
        err_m = re.search(
            r'(?:class|id)=["\'][^"\']*(?:error|alert|danger|validation)[^"\']*["\'][^>]*>'
            r'\s*([^<]{6,120})',
            login_resp.text, re.IGNORECASE,
        )
        msg = err_m.group(1).strip() if err_m else "Kontrollera e-post och lösenord."
        raise Exception(f"Inloggning misslyckades – {msg}")

    token_m = re.search(
        r'<input[^>]+name=["\']access_token["\'][^>]+value=["\']([^"\']+)["\']'
        r'|<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']access_token["\']',
        login_resp.text, re.IGNORECASE,
    )
    if not token_m:
        raise Exception("Inloggning lyckades men access_token hittades inte i svaret.")

    return (token_m.group(1) or token_m.group(2)).strip()


# ── Hjälp: auth-headers beroende på läge ───────────────────────────────────
def _auth_headers():
    h = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        **SOS_EXTRA_HEADERS,
    }
    if _session["access_token"]:
        h["Authorization"] = f"Bearer {_session['access_token']}"
    if _session["subscription_key"]:
        h["Ocp-Apim-Subscription-Key"] = _session["subscription_key"]
    return h


# ── Statiska filer ──────────────────────────────────────────────────────────

_BASE_DIR   = _os.path.dirname(_os.path.abspath(__file__))
_CACHE_FILE = _os.path.join(_BASE_DIR, 'stats_cache.json')
_FIRST_YEAR = 2022

_stats_cache    = {}   # { "2025": { aggregerat } }
_build_progress = {}   # { "2025": { status, fetched, total } }
_stats_lock     = _threading.Lock()

@app.route("/")
def index():
    return send_from_directory(_BASE_DIR, "faglar-vasterbotten.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(_BASE_DIR, filename)

# ── API-endpoints ───────────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    return jsonify({
        "running":   True,
        "logged_in": bool(_session["access_token"] or _session["subscription_key"]),
        "username":  _session["username"],
        "auth_mode": _session["auth_mode"],
    })


@app.route("/api/version")
def version():
    return jsonify({
        "version":    APP_VERSION,
        "started_at": _SERVER_START.isoformat(),
    })


@app.route("/api/login", methods=["POST"])
def login():
    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    sub_key  = (data.get("subscriptionKey") or "").strip()

    if not sub_key and not username:
        return jsonify({"error": "Ange antingen prenumerationsnyckel eller e-post + lösenord."}), 400

    # ── Alternativ 1: Prenumerationsnyckel ensam ──
    if sub_key and not username:
        print(f"  Testar prenumerationsnyckel (utan inloggning)…")
        if _test_sub_key(sub_key):
            _session.update({
                "access_token":     None,
                "subscription_key": sub_key,
                "username":         "API-nyckel",
                "auth_mode":        "sub_key_only",
            })
            print("  ✓ Prenumerationsnyckel godkänd")
            return jsonify({"success": True, "username": "API-nyckel", "mode": "sub_key_only"})
        return jsonify({"error": "Prenumerationsnyckeln godkändes inte av SOS API."}), 401

    # ── Alternativ 2: Prenumerationsnyckel + SLU-inloggning ──
    if sub_key and username:
        print(f"  Testar prenumerationsnyckel + inloggning…")
        if _test_sub_key(sub_key):
            # Nyckel fungerar – försök även hämta Bearer token
            try:
                token = _slu_login_flow(username, password)
                _session.update({
                    "access_token":     token,
                    "subscription_key": sub_key,
                    "username":         username,
                    "auth_mode":        "bearer+sub_key",
                })
                print(f"  ✓ Inloggad med SLU + API-nyckel: {username}")
                return jsonify({"success": True, "username": username, "mode": "bearer+sub_key"})
            except Exception:
                # Bearer misslyckades men nyckel fungerar – kör med nyckel
                _session.update({
                    "access_token":     None,
                    "subscription_key": sub_key,
                    "username":         username or "API-nyckel",
                    "auth_mode":        "sub_key_only",
                })
                print("  ✓ Prenumerationsnyckel godkänd (Bearer-login misslyckades)")
                return jsonify({"success": True, "username": _session["username"], "mode": "sub_key_only"})
        # Nyckel funkar ej – prova bara Bearer
        if username and password:
            try:
                token = _slu_login_flow(username, password)
                _session.update({
                    "access_token":     token,
                    "subscription_key": sub_key,
                    "username":         username,
                    "auth_mode":        "bearer+sub_key",
                })
                print(f"  ✓ Inloggad med SLU: {username}")
                return jsonify({"success": True, "username": username, "mode": "bearer+sub_key"})
            except Exception as e:
                return jsonify({"error": str(e)}), 401
        return jsonify({"error": "Varken prenumerationsnyckel eller inloggning fungerade."}), 401

    # ── Alternativ 3: Bara SLU-inloggning (ingen nyckel) ──
    if not password:
        return jsonify({"error": "Ange lösenord."}), 400
    try:
        token = _slu_login_flow(username, password)
    except requests.RequestException as e:
        return jsonify({"error": f"Nätverksfel: {e}"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 401

    _session.update({
        "access_token":     token,
        "subscription_key": "",
        "username":         username,
        "auth_mode":        "bearer",
    })
    print(f"  ✓ Inloggad med SLU: {username}")
    return jsonify({"success": True, "username": username, "mode": "bearer"})


@app.route("/api/logout", methods=["POST"])
def logout():
    _session.update({"access_token": None, "subscription_key": "", "username": None, "auth_mode": None})
    return jsonify({"success": True})


@app.route("/api/observations")
def get_observations():
    if not _session["access_token"] and not _session["subscription_key"]:
        return jsonify({"error": "Inte inloggad."}), 401

    date = (request.args.get("date") or "").strip()
    if not date:
        return jsonify({"error": "Parametern 'date' saknas (YYYY-MM-DD)."}), 400

    # Geografiskt filter – default: Västerbottens län
    feature_id = (request.args.get("featureId") or VASTERBOTTEN_FEATURE_ID).strip()
    area_type  = (request.args.get("areaType")  or "County").strip()
    # Säkerställ att areaType är ett av de tillåtna värdena
    if area_type not in ("County", "Municipality", "Province", "Parish"):
        area_type = "County"

    print(f"  Region: areaType={area_type}, featureId={feature_id}")

    body = {
        "taxon": {"ids": [AVES_TAXON_ID], "includeUnderlyingTaxa": True},
        "date": {
            "startDate": date, "endDate": date,
            "dateFilterType": "OverlappingStartDateAndEndDate",
        },
        "geographics": {
            "areas": [{"areaType": area_type, "featureId": feature_id}],
        },
    }

    try:
        resp = requests.post(
            f"{SOS_API_BASE}/Observations/Search",
            headers=_auth_headers(),
            json=body,
            params={"skip": 0, "take": 1000},
            timeout=30,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"Nätverksfel: {e}"}), 503

    print(f"  Observations/Search {date}: HTTP {resp.status_code}")
    if not resp.ok:
        print(f"  Feldetaljer: {resp.text[:800]}")

    if resp.status_code == 401:
        _session["access_token"] = None
        return jsonify({"error": "Sessionen har gått ut – logga in igen."}), 401

    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:600]
        return jsonify({"error": f"SOS API svarade {resp.status_code}", "detail": detail}), resp.status_code

    return jsonify(resp.json())


@app.route("/api/debug/token")
def debug_token():
    return jsonify({
        "auth_mode":      _session["auth_mode"],
        "has_bearer":     bool(_session["access_token"]),
        "has_sub_key":    bool(_session["subscription_key"]),
        "token_prefix":   (_session["access_token"] or "")[:40] + "…" if _session["access_token"] else None,
    })


# ── Rödlistecache (i minnet under körtiden) ─────────────────────────────────
_rl_cache = {}   # taxonId (int) → {"redListCategory": "NT"|"VU"|…|None, "source": str}


@app.route("/api/taxon/redlist", methods=["POST"])
def taxon_redlist():
    """
    Returnerar rödlistestatus för en lista taxa.
    Body: [{"id": 102998, "name": "Turdus merula"}, ...]
    Försöker SOS Taxon-API med befintlig prenumerationsnyckel,
    faller tillbaka på GBIF IUCN om det misslyckas.
    """
    if not _session["access_token"] and not _session["subscription_key"]:
        return jsonify({"error": "Inte inloggad."}), 401

    taxa = request.get_json(silent=True) or []
    if not taxa:
        return jsonify({}), 200

    result = {}
    uncached = [t for t in taxa if isinstance(t.get("id"), int) and t["id"] not in _rl_cache]

    # ── Hämta saknade via SOS Taxon API ──
    if uncached:
        sos_data = _fetch_sos_taxa([t["id"] for t in uncached])
        if sos_data:
            _rl_cache.update(sos_data)

    # ── GBIF-fallback för de som fortfarande saknar kategori ──
    for t in taxa:
        tid  = t.get("id")
        name = t.get("name", "")
        if isinstance(tid, int) and tid not in _rl_cache and name:
            cat = _gbif_iucn(name)
            _rl_cache[tid] = {"redListCategory": cat, "source": "gbif_iucn"}

    # ── Bygg svar ──
    for t in taxa:
        tid = t.get("id")
        if isinstance(tid, int):
            result[str(tid)] = _rl_cache.get(tid, {"redListCategory": None})

    return jsonify(result)


def _fetch_sos_taxa(ids):
    """
    Försöker hämta rödlistestatus via flera möjliga Artdatabanken-endpoints.
    Returnerar dict {taxonId: {redListCategory, source}} eller {}.
    """
    if not ids:
        return {}

    # ── Försök 1: SOS API /Taxon/Search ──
    for method, url, kw in [
        ("POST", f"{SOS_API_BASE}/Taxon/Search",
         {"json": {"ids": ids, "take": len(ids)}}),
        ("GET",  f"{SOS_API_BASE}/Taxon",
         {"params": {"ids": ",".join(str(i) for i in ids)}}),
    ]:
        try:
            resp = requests.request(
                method, url, headers=_auth_headers(), timeout=12, **kw
            )
            print(f"  SOS Taxon {method} {url}: HTTP {resp.status_code}")
            if resp.ok:
                data  = resp.json()
                taxa  = data.get("taxa") or data.get("records") or data.get("results") or []
                if isinstance(data, list):
                    taxa = data
                out   = {}
                for t in taxa:
                    tid = t.get("id") or t.get("taxonId")
                    if tid is None:
                        continue
                    attrs = t.get("attributes") or {}
                    rl = (attrs.get("redListCategory")
                          or attrs.get("redlistCategory")
                          or t.get("redListCategory")
                          or None)
                    out[int(tid)] = {"redListCategory": rl, "source": "sos"}
                if out:
                    print(f"  SOS Taxon: hittade {len(out)} taxa")
                    return out
        except Exception as e:
            print(f"  SOS Taxon {method} misslyckades: {e}")

    # ── Försök 2: Artfakta – hämta HTML server-side och sök JSON-LD ──
    import re as _re
    out = {}
    for tid in ids[:20]:   # Begränsa antalet HTTP-anrop
        cat = _scrape_artfakta_redlist(tid)
        if cat:
            out[tid] = {"redListCategory": cat, "source": "artfakta"}
    if out:
        print(f"  Artfakta scraping: hittade {len(out)} taxa")
    return out


def _scrape_artfakta_redlist(taxon_id):
    """
    Hämtar rödlistekategori från Artfaktas HTML-sida för ett Dyntaxa-ID.
    Söker i JSON-LD, meta-taggar och HTML-text.
    """
    import re as _re
    try:
        r = requests.get(
            f"https://artfakta.artdatabanken.se/taxon/{taxon_id}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            timeout=12,
            allow_redirects=True,
        )
        if not r.ok:
            return None
        html = r.text

        # Sök efter rödlistekategori i JSON-LD eller meta-taggar
        for pattern in [
            r'"redListCategory"\s*:\s*"([A-Z]{2,3})"',
            r'"conservationStatus"\s*:\s*"([A-Z]{2,3})"',
            r'r[oö]dliste?kategori[^"]*"([A-Z]{2,3})"',
            r'\b(CR|EN|VU|NT|DD|RE)\b',
        ]:
            m = _re.search(pattern, html, _re.IGNORECASE)
            if m:
                cat = m.group(1).upper()
                if cat in ("CR", "EN", "VU", "NT", "DD", "RE"):
                    return cat
        return None
    except Exception:
        return None


def _gbif_iucn(scientific_name):
    """Hämtar global IUCN-kategori via GBIF (sista fallback – ej svensk rödlista)."""
    try:
        r1 = requests.get(
            "https://api.gbif.org/v1/species/match",
            params={"name": scientific_name, "kingdom": "Animalia", "class": "Aves"},
            timeout=8,
        )
        if not r1.ok:
            return None
        d1  = r1.json()
        key = d1.get("usageKey") or d1.get("speciesKey")
        if not key or d1.get("matchType") == "NONE":
            return None
        r2 = requests.get(
            f"https://api.gbif.org/v1/species/{key}/iucnRedListCategory",
            timeout=8,
        )
        if not r2.ok:
            return None
        d2  = r2.json()
        cat = d2.get("category") or d2.get("code")
        return cat if cat and cat not in ("NE", "NA", "EX", "LC") else None
    except Exception:
        return None


@app.route("/api/debug/observation")
def debug_observation():
    """
    Returnerar en detaljerad vy av alla fält i de första 5 observationerna.
    Listar varje fältväg (taxon.sex, occurrence.lifeStage etc.) och dess värde.
    """
    if not _session["access_token"] and not _session["subscription_key"]:
        return jsonify({"error": "Inte inloggad."}), 401

    from datetime import date as _date
    today = _date.today().isoformat()
    body = {
        "taxon": {"ids": [AVES_TAXON_ID], "includeUnderlyingTaxa": True},
        "date":  {"startDate": today, "endDate": today,
                  "dateFilterType": "OverlappingStartDateAndEndDate"},
        "geographics": {"areas": [{"areaType": "County",
                                    "featureId": VASTERBOTTEN_FEATURE_ID}]},
    }
    try:
        resp = requests.post(
            f"{SOS_API_BASE}/Observations/Search",
            headers=_auth_headers(), json=body,
            params={"skip": 0, "take": 5}, timeout=20,
        )
        data = resp.json()
        records = data.get("records") or data.get("observations") or data.get("results") or []

        def flatten(obj, prefix=""):
            """Rekursivt plattar ut ett dict till en lista av (fältsökväg, värde)-par."""
            result = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    path = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, (dict, list)):
                        result.extend(flatten(v, path))
                    else:
                        result.append((path, v))
            elif isinstance(obj, list):
                for i, item in enumerate(obj[:3]):  # max 3 list-element
                    result.extend(flatten(item, f"{prefix}[{i}]"))
            return result

        # Aggregera alla unika fältsökvägar och exempel-värden över alla 5 obs
        field_map = {}
        for rec in records:
            for path, val in flatten(rec):
                if path not in field_map:
                    field_map[path] = val  # första icke-None-värdet vinner
                elif field_map[path] in (None, "", [], {}) and val not in (None, "", [], {}):
                    field_map[path] = val

        # Sortera per sökväg och bygg lista med sektion-grupperingar
        sections = {}
        for path, val in sorted(field_map.items()):
            top = path.split(".")[0]
            sections.setdefault(top, {})[path] = val

        return jsonify({
            "observation_count": len(records),
            "date": today,
            "field_summary": sections,
            "raw_first": records[0] if records else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# Statistik-cache – aggregering, hämtning och bakgrundstråd
# ══════════════════════════════════════════════════════════════════════════════

def _get_rl_category(taxon_obj):
    """Extraherar rödlistekategori från taxon-objekt."""
    attrs = taxon_obj.get('attributes') or taxon_obj.get('Attributes') or {}
    for field in ('redListCategory', 'redlistCategory', 'RedListCategory'):
        val = (attrs.get(field) or taxon_obj.get(field) or '')
        if val.upper() in ('CR', 'EN', 'VU', 'NT', 'DD', 'RE'):
            return val.upper()
    return None


def _aggregate_observations(records, rl_override=None):
    """Aggregerar en lista råobservationer till statistik-dict.
    rl_override: {taxon_id (int): 'NT'|'VU'|'EN'|'CR'} – extra rödlistestatus
                 som används om taxon-objektet saknar attributet (t.ex. vid API-nyckelauth).
    """
    species   = _defaultdict(lambda: {
        'obs': 0, 'ind': 0, 'sv': '', 'sci': '', 'key': None,
        'rl': None, 'last_date': '', 'last_rep': ''
    })
    reporters = _defaultdict(lambda: {'obs': 0, 'species': set()})
    monthly   = [0] * 12
    total_ind = 0

    # Per-månads-spårning (1-indexerat)
    monthly_sp  = _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'ind': 0}))
    monthly_rep = _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'species': set()}))

    # Per-kommun-spårning (featureId som nyckel)
    muni_sp  = _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'ind': 0}))
    muni_rep = _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'species': set()}))

    for rec in records:
        taxon    = rec.get('taxon')     or rec.get('Taxon')    or {}
        occ      = rec.get('occurrence') or rec.get('Occurrence') or {}
        event    = rec.get('event')     or rec.get('Event')    or {}
        location = rec.get('location')  or rec.get('Location') or {}
        muni_fid = (location.get('municipality') or {}).get('featureId') or ''

        key = taxon.get('id') or taxon.get('taxonId') or taxon.get('dyntaxaId')
        if not key:
            continue
        key = int(key)

        sv_name  = taxon.get('vernacularName') or taxon.get('commonName') or ''
        sci_name = taxon.get('scientificName') or ''
        count    = int(occ.get('individualCount') or occ.get('quantity') or 1)
        reporter = (occ.get('reportedBy') or occ.get('observer') or '').strip()
        start_dt = event.get('startDate') or event.get('startDayOfYear') or ''
        rl_cat   = _get_rl_category(taxon) or (rl_override or {}).get(key)

        # Månad (0-indexerad för monthly-array, 1-indexerad för per-månads-dict)
        month_0 = None
        if start_dt and len(start_dt) >= 7:
            try:
                month_0 = int(start_dt[5:7]) - 1
            except ValueError:
                pass

        sp = species[key]
        sp['obs'] += 1
        sp['ind'] += count
        if sv_name:  sp['sv']  = sv_name
        if sci_name: sp['sci'] = sci_name
        sp['key'] = key
        if rl_cat:
            sp['rl'] = rl_cat
        if start_dt[:10] > sp['last_date']:
            sp['last_date'] = start_dt[:10]
            sp['last_rep']  = reporter

        if reporter:
            reporters[reporter]['obs'] += 1
            reporters[reporter]['species'].add(key)

        if month_0 is not None:
            monthly[month_0] += 1
            m1 = month_0 + 1
            monthly_sp[m1][key]['obs'] += 1
            monthly_sp[m1][key]['ind'] += count
            if reporter:
                monthly_rep[m1][reporter]['obs'] += 1
                monthly_rep[m1][reporter]['species'].add(key)

        if muni_fid:
            muni_sp[muni_fid][key]['obs'] += 1
            muni_sp[muni_fid][key]['ind'] += count
            if reporter:
                muni_rep[muni_fid][reporter]['obs'] += 1
                muni_rep[muni_fid][reporter]['species'].add(key)

        total_ind += count

    top_sp = sorted(
        [{'sv': v['sv'], 'sci': v['sci'], 'key': v['key'],
          'obs': v['obs'], 'ind': v['ind']} for v in species.values()],
        key=lambda x: x['obs'], reverse=True
    )[:20]

    top_rap = sorted(
        [{'name': k, 'arter': len(v['species']), 'obs': v['obs']}
         for k, v in reporters.items()],
        key=lambda x: x['arter'], reverse=True
    )[:20]

    # Bygg per-månads topplista (top 20 per månad)
    month_species   = {}
    month_reporters = {}
    for m in range(1, 13):
        ms = monthly_sp.get(m, {})
        month_species[m] = sorted(
            [{'key': k, 'sv': species[k]['sv'], 'sci': species[k]['sci'],
              'obs': v['obs'], 'ind': v['ind']}
             for k, v in ms.items() if k in species],
            key=lambda x: x['obs'], reverse=True
        )[:20]
        mr = monthly_rep.get(m, {})
        month_reporters[m] = sorted(
            [{'name': nm, 'obs': v['obs'], 'arter': len(v['species'])}
             for nm, v in mr.items()],
            key=lambda x: x['arter'], reverse=True
        )[:20]

    # Bygg per-kommun topplista
    muni_species   = {}
    muni_reporters = {}
    for fid, ms in muni_sp.items():
        muni_species[fid] = sorted(
            [{'key': k, 'sv': species[k]['sv'], 'sci': species[k]['sci'],
              'obs': v['obs'], 'ind': v['ind']}
             for k, v in ms.items() if k in species],
            key=lambda x: x['obs'], reverse=True
        )[:20]
    for fid, mr in muni_rep.items():
        muni_reporters[fid] = sorted(
            [{'name': nm, 'obs': v['obs'], 'arter': len(v['species'])}
             for nm, v in mr.items()],
            key=lambda x: x['arter'], reverse=True
        )[:20]

    return {
        'kpi':             {'arter': len(species), 'obs': sum(v['obs'] for v in species.values()),
                            'ind': total_ind, 'reporters': len(reporters)},
        'monthly':         monthly,
        'top_species':     top_sp,
        'top_reporters':   top_rap,
        'month_species':   month_species,
        'month_reporters': month_reporters,
        'muni_species':    muni_species,
        'muni_reporters':  muni_reporters,
    }


def _fetch_year_stats(year):
    """Hämtar och aggregerar alla observationer för ett år via paginering."""
    if not _session['access_token'] and not _session['subscription_key']:
        return None

    all_records, skip, take, total = [], 0, 1000, None
    year_key = str(year)
    body = {
        'taxon':       {'ids': [AVES_TAXON_ID], 'includeUnderlyingTaxa': True},
        'date':        {'startDate': f'{year}-01-01', 'endDate': f'{year}-12-31',
                        'dateFilterType': 'OverlappingStartDateAndEndDate'},
        'geographics': {'areas': [{'areaType': 'County', 'featureId': VASTERBOTTEN_FEATURE_ID}]},
    }

    while True:
        try:
            resp = requests.post(
                f'{SOS_API_BASE}/Observations/Search',
                headers=_auth_headers(), json=body,
                params={'skip': skip, 'take': take}, timeout=40,
            )
        except requests.RequestException as e:
            print(f'  Stats ({year}): nätverksfel – {e}')
            break
        if not resp.ok:
            print(f'  Stats ({year}): HTTP {resp.status_code}')
            break

        data    = resp.json()
        records = data.get('records') or data.get('observations') or data.get('results') or []
        if total is None:
            total = int(data.get('totalCount') or data.get('total') or 0)
        all_records.extend(records)

        with _stats_lock:
            _build_progress[year_key] = {
                'status': 'building', 'fetched': len(all_records), 'total': total
            }

        skip += take
        if not records or (total and skip >= total):
            break
        _time.sleep(0.4)

    if not all_records:
        return None

    # ── Hämta rödlistestatus via SOS Taxon-API (max 60 s, ingen Artfakta-scraping) ──
    unique_ids = list({
        int(r.get('taxon', {}).get('id') or r.get('taxon', {}).get('taxonId') or 0)
        for r in all_records
        if r.get('taxon', {}).get('id') or r.get('taxon', {}).get('taxonId')
    } - {0})

    rl_override = {}
    if unique_ids:
        print(f'  Stats ({year}): rödlistekoll för {len(unique_ids)} arter…')
        t0 = _time.time()
        for i in range(0, len(unique_ids), 50):
            if _time.time() - t0 > 60:
                print(f'  Stats ({year}): rödlistekoll avbruten (>60 s)')
                break
            batch = unique_ids[i:i + 50]
            for method, url, kw in [
                ('POST', f'{SOS_API_BASE}/Taxon/Search',
                 {'json': {'ids': batch, 'take': len(batch)}}),
                ('GET',  f'{SOS_API_BASE}/Taxon',
                 {'params': {'ids': ','.join(str(x) for x in batch)}}),
            ]:
                try:
                    r = requests.request(
                        method, url, headers=_auth_headers(), timeout=8, **kw
                    )
                    if r.ok:
                        data = r.json()
                        taxa = (data.get('taxa') or data.get('records')
                                or data.get('results') or [])
                        if isinstance(data, list):
                            taxa = data
                        for t in taxa:
                            tid = t.get('id') or t.get('taxonId')
                            if not tid:
                                continue
                            attrs = t.get('attributes') or {}
                            rl = (attrs.get('redListCategory')
                                  or attrs.get('redlistCategory')
                                  or t.get('redListCategory') or '')
                            if rl.upper() in ('CR', 'EN', 'VU', 'NT'):
                                rl_override[int(tid)] = rl.upper()
                        break  # lyckades – hoppa över nästa metod
                except Exception:
                    pass
            _time.sleep(0.2)
        elapsed = _time.time() - t0
        print(f'  Stats ({year}): {len(rl_override)} rödlistade (SOS API, {elapsed:.0f}s)')

    result = _aggregate_observations(all_records, rl_override)
    result.update({'year': year, 'cached_at': _dt.now().isoformat(),
                   'total_fetched': len(all_records)})
    return result


def _stats_builder():
    """Bakgrundstråd: laddar cache från fil, hämtar saknade år, uppdaterar dagligen."""
    global _stats_cache

    # Ladda befintlig cache
    if _os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, 'r', encoding='utf-8') as f:
                loaded = _json.load(f)
            with _stats_lock:
                _stats_cache = loaded
            print(f'  Stats: cache laddad ({len(loaded)} år)')
        except Exception as e:
            print(f'  Stats: kunde inte läsa cache – {e}')

    while True:
        # Vänta på autentisering (max 5 min)
        for _ in range(60):
            if _session['access_token'] or _session['subscription_key']:
                break
            _time.sleep(5)

        if not _session['access_token'] and not _session['subscription_key']:
            print('  Stats: ingen autentisering – försöker igen om 10 min')
            _time.sleep(600)
            continue

        current_year = _date_type.today().year

        for year in range(_FIRST_YEAR, current_year + 1):
            year_key = str(year)
            with _stats_lock:
                cached = _stats_cache.get(year_key)

            # Historiska år: hämta bara en gång
            if cached and year < current_year:
                continue

            # Innevarande år: hoppa över om cache är < 24 h gammal
            if cached and year == current_year:
                try:
                    age = (_dt.now() - _dt.fromisoformat(cached['cached_at'])).total_seconds()
                    if age < 86400:
                        continue
                except Exception:
                    pass

            print(f'  Stats: hämtar {year}…')
            with _stats_lock:
                _build_progress[year_key] = {'status': 'building', 'fetched': 0, 'total': 0}

            result = _fetch_year_stats(year)
            if result:
                with _stats_lock:
                    _stats_cache[year_key] = result
                    _build_progress[year_key] = {'status': 'ready'}
                try:
                    with open(_CACHE_FILE, 'w', encoding='utf-8') as f:
                        _json.dump(_stats_cache, f, ensure_ascii=False, indent=2)
                    print(f'  Stats: {year} klar – '
                          f'{result["kpi"]["obs"]} obs, {result["kpi"]["arter"]} arter')
                except Exception as e:
                    print(f'  Stats: kunde inte spara cache – {e}')
            else:
                with _stats_lock:
                    _build_progress[year_key] = {'status': 'error'}
                print(f'  Stats: misslyckades för {year}')

        # Sov till nästa dag kl 03:00
        from datetime import timedelta as _td
        now   = _dt.now()
        next3 = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= next3:
            next3 += _td(days=1)
        sleep_s = (next3 - now).total_seconds()
        print(f'  Stats: nästa uppdatering om {sleep_s / 3600:.1f}h')
        _time.sleep(sleep_s)


@app.route('/api/statistics')
def get_statistics():
    year = request.args.get('year', str(_date_type.today().year))
    with _stats_lock:
        data     = _stats_cache.get(year)
        progress = _build_progress.get(year, {})

    if data:
        return jsonify({'status': 'ready', 'data': data})
    # Cachad data returneras alltid, men bygge kräver autentisering
    if not _session['access_token'] and not _session['subscription_key']:
        return jsonify({'status': 'unauthenticated'}), 401
    if progress.get('status') == 'building':
        return jsonify({'status': 'building',
                        'fetched': progress.get('fetched', 0),
                        'total':   progress.get('total', 0)}), 202
    return jsonify({'status': 'pending'}), 202


@app.route('/api/statistics/years')
def statistics_years():
    """Returnerar vilka år som finns i cachen."""
    with _stats_lock:
        years = {y: {'cached_at': d.get('cached_at'), 'kpi': d.get('kpi')}
                 for y, d in _stats_cache.items()}
    return jsonify(years)


# ── Auto-inloggning vid uppstart (körs oavsett om Flask eller Gunicorn används) ──
_auto_key   = _os.environ.get("SOS_SUBSCRIPTION_KEY", "").strip()
_auto_email = _os.environ.get("SLU_EMAIL", "").strip()
_auto_pass  = _os.environ.get("SLU_PASSWORD", "").strip()

if _auto_key:
    print("  Testar prenumerationsnyckel från miljövariabel…")
    if _test_sub_key(_auto_key):
        _session.update({
            "access_token":     None,
            "subscription_key": _auto_key,
            "username":         "API-nyckel (auto)",
            "auth_mode":        "sub_key_only",
        })
        print("  ✓ API-nyckel godkänd")
    else:
        print("  ✗ API-nyckel fungerade ej")

if _auto_email and _auto_pass:
    print(f"  Loggar in med SLU-konto: {_auto_email}…")
    try:
        token = _slu_login_flow(_auto_email, _auto_pass)
        _session.update({
            "access_token":     token,
            "subscription_key": _session["subscription_key"],
            "username":         _auto_email,
            "auth_mode":        "bearer+sub_key" if _session["subscription_key"] else "bearer",
        })
        print(f"  ✓ SLU-inloggning lyckades: {_auto_email}")
    except Exception as e:
        print(f"  ✗ SLU-inloggning misslyckades: {e}")


# ── Starta statistik-bakgrundstråd ──────────────────────────────────────────
_stats_thread = _threading.Thread(target=_stats_builder, daemon=True, name='stats-builder')
_stats_thread.start()

# ── Startup (endast vid direktkörning lokalt) ────────────────────────────────
if __name__ == "__main__":
    import sys, io
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    port = int(_os.environ.get("PORT", 5050))
    print(f"Startar på 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)