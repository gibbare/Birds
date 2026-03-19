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
from datetime import datetime as _dt, date as _date_type, timedelta as _timedelta
import calendar as _calendar
from collections import defaultdict as _defaultdict, deque as _deque

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── Konstanter ─────────────────────────────────────────────────────────────
APP_VERSION   = "3.1"          # Uppdatera vid varje deploy
_SERVER_START = _dt.now()      # Tidpunkt då servern startades

# ── Fellogg (cirkulär buffer, max 500 poster) ────────────────────────────────
_error_log: _deque = _deque(maxlen=500)

def _log_error(msg: str) -> None:
    """Lägg till ett felmeddelande i felloggen."""
    now = _dt.now()
    _error_log.append({
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M:%S'),
        'msg':  str(msg)[:400],
    })

SLU_AUTH_URL  = "https://useradmin-auth.slu.se/connect/authorize"
SOS_API_BASE  = "https://api.artdatabanken.se/species-observation-system/v1"
CLIENT_ID     = "Artportalen"
REDIRECT_URI  = "https://www.artportalen.se/authentication/callback"
SCOPE         = "openid email profile SOS.Observations.Protected"

AVES_TAXON_ID           = 4000104
VASTERBOTTEN_FEATURE_ID = "24"
DEFAULT_COUNTY_ID       = VASTERBOTTEN_FEATURE_ID

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
_CACHE_FILE = _os.path.join(_BASE_DIR, 'stats_cache.json')  # gammal samlad fil (bakåtkompatibilitet)
_FIRST_YEAR = 2022

def _cache_file_for(county_id, year):
    """Returnerar sökväg till per-år cachefil: stats_cache_24_2026.json"""
    return _os.path.join(_BASE_DIR, f'stats_cache_{county_id}_{year}.json')

_stats_cache    = {}   # { "24_2025": { aggregerat } }
_build_progress = {}   # { "24_2025": { status, fetched, total } }
_building       = set()  # cache-nycklar under pågående bygge
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
    # SOS API vill ha kommuner utan ledande nollor (0380 → 380, 2480 → 2480)
    try:
        feature_id = str(int(feature_id))
    except ValueError:
        pass

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


@app.route("/api/obs_map")
def obs_map():
    """Observationer med koordinater för kartvisning – filtrerar på art eller rapportör."""
    if not _session["access_token"] and not _session["subscription_key"]:
        return jsonify({"error": "Inte inloggad."}), 401

    year     = (request.args.get("year")     or str(_date_type.today().year)).strip()
    month    = int(request.args.get("month") or 0)
    county   = (request.args.get("county")   or VASTERBOTTEN_FEATURE_ID).strip()
    region   = (request.args.get("region")   or "").strip()
    taxon_id = (request.args.get("taxonId")  or "").strip()
    reporter = (request.args.get("reporter") or "").strip()

    # Om varken taxon_id eller reporter anges returneras ett urval av obs för området
    area_overview = not taxon_id and not reporter

    if month:
        last_day   = _calendar.monthrange(int(year), month)[1]
        start_date = f"{year}-{month:02d}-01"
        end_date   = f"{year}-{month:02d}-{last_day:02d}"
    else:
        start_date = f"{year}-01-01"
        end_date   = f"{year}-12-31"

    area_type  = "Municipality" if region else "County"
    feature_id = region if region else county
    try:
        feature_id = str(int(feature_id))
    except ValueError:
        pass

    body = {
        "taxon": {"ids": [AVES_TAXON_ID], "includeUnderlyingTaxa": True},
        "date": {
            "startDate": start_date,
            "endDate":   end_date,
            "dateFilterType": "OverlappingStartDateAndEndDate",
        },
        "geographics": {
            "areas": [{"areaType": area_type, "featureId": feature_id}],
        },
    }
    if taxon_id:
        body["taxon"] = {"ids": [int(taxon_id)], "includeUnderlyingTaxa": True}

    out  = []
    take = 1000
    max_pages = 5 if area_overview else 60  # översikt: max 5 000 obs, annars 60 000
    for page in range(max_pages):
        try:
            resp = requests.post(
                f"{SOS_API_BASE}/Observations/Search",
                headers=_auth_headers(),
                json=body,
                params={"skip": page * take, "take": take},
                timeout=30,
            )
        except requests.RequestException as e:
            _log_error(f"obs_map page {page}: {e}")
            break

        if resp.status_code == 401:
            _session["access_token"] = None
            return jsonify({"error": "Sessionen har gått ut."}), 401

        if not resp.ok:
            _log_error(f"obs_map HTTP {resp.status_code}: {resp.text[:200]}")
            break

        records = resp.json().get("records") or []
        for rec in records:
            loc   = rec.get("location")   or {}
            occ   = rec.get("occurrence") or {}
            taxon = rec.get("taxon")      or {}
            event = rec.get("event")      or {}
            lat   = loc.get("decimalLatitude")
            lon   = loc.get("decimalLongitude")
            if lat is None or lon is None:
                continue
            # Samma fältprioritet som statistikcachen använder
            rep = (occ.get("reportedBy") or occ.get("observer") or
                   occ.get("recordedBy") or "")
            if reporter and reporter.lower() not in rep.lower():
                continue
            out.append({
                "lat":      lat,
                "lon":      lon,
                "sv":       taxon.get("vernacularName")    or "",
                "sci":      taxon.get("scientificName")    or "",
                "date":     event.get("startDate")         or "",
                "reporter": rep,
                "cnt":      occ.get("organismQuantityInt") or 1,
            })

        if len(records) < take:
            break

    print(f"  obs_map: {len(out)} obs (taxon={taxon_id or '-'} reporter={reporter or '-'})")
    return jsonify({"total": len(out), "observations": out})


@app.route("/api/breeding")
def get_breeding():
    """Häckningsobservationer med koordinater för vald region och år."""
    if not _session["access_token"] and not _session["subscription_key"]:
        return jsonify({"error": "Inte inloggad."}), 401

    year         = (request.args.get("year")         or str(_date_type.today().year)).strip()
    county       = (request.args.get("county")       or VASTERBOTTEN_FEATURE_ID).strip()
    municipality = (request.args.get("municipality") or "").strip()
    try:
        min_act = max(1, int(request.args.get("minActivity") or 1))
    except ValueError:
        min_act = 1

    area_type  = "Municipality" if municipality else "County"
    feature_id = municipality if municipality else county
    try:
        feature_id = str(int(feature_id))
    except ValueError:
        pass

    # ── Cache-kontroll ──────────────────────────────────────────────────────
    cache_path   = f"breeding_cache_{feature_id}_{year}_{min_act}.json"
    today        = str(_date_type.today())
    current_year = str(_date_type.today().year)

    if _os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as _cf:
                _cached = _json.load(_cf)
            # Innevarande år: cacha en dag; äldre år: cachas permanent
            if year != current_year or _cached.get('cached_date') == today:
                print(f"  breeding: cache-träff {cache_path}")
                return jsonify(_cached['payload'])
        except Exception as _ce:
            _log_error(f"breeding cache read: {_ce}")

    # SOS returnerar inte häckningsnivå i svaret – bestäm kategori via tre anrop:
    #   Tier A+B+C : birdNestActivityLimit >= 1
    #   Tier B+C   : birdNestActivityLimit >= 5
    #   Tier C     : birdNestActivityLimit >= 13
    # Kategori per obs = lägsta tröskeln som INTE inkluderar den (mängdlära).

    base_body = {
        "taxon": {"ids": [AVES_TAXON_ID], "includeUnderlyingTaxa": True},
        "date": {
            "startDate": f"{year}-01-01",
            "endDate":   f"{year}-12-31",
            "dateFilterType": "OverlappingStartDateAndEndDate",
        },
        "geographics": {
            "areas": [{"areaType": area_type, "featureId": feature_id}],
        },
    }

    def _fetch_tier(limit):
        """Hämta alla obs med birdNestActivityLimit >= limit.
           Returnerar dict occurrenceId → obs."""
        body = dict(base_body)
        body["birdNestActivityLimit"] = limit
        result = {}
        take   = 1000
        for page in range(50):
            skip = page * take
            if skip + take > 50000:
                break
            try:
                r = requests.post(
                    f"{SOS_API_BASE}/Observations/Search",
                    headers=_auth_headers(),
                    json=body,
                    params={"skip": skip, "take": take},
                    timeout=30,
                )
            except requests.RequestException as e:
                _log_error(f"breeding tier{limit} p{page}: {e}")
                break
            if not r.ok:
                _log_error(f"breeding tier{limit} HTTP {r.status_code}: {r.text[:200]}")
                break
            data    = r.json()
            records = data if isinstance(data, list) else data.get("records", [])
            for obs in records:
                oid = (obs.get("occurrence") or {}).get("occurrenceId") or ""
                if oid:
                    result[oid] = obs
            if len(records) < take or len(result) >= 5000:
                break
        return result

    # birdNestActivityLimit fungerar som ett maxvärde i SOS API:
    #   limit=13 → alla (A+B+C), limit=5 → B+C, limit=1 → C (säker)
    # min_act från UI: 13=Alla, 5=B+C, 1=Säker(C)
    if min_act <= 1:
        tier_abc = _fetch_tier(1)   # bara C
        tier_bc  = tier_abc
        tier_c   = tier_abc
    elif min_act <= 5:
        tier_abc = _fetch_tier(5)   # B+C
        tier_bc  = tier_abc
        tier_c   = _fetch_tier(1)
    else:
        tier_abc = _fetch_tier(13)  # A+B+C
        tier_bc  = _fetch_tier(5)
        tier_c   = _fetch_tier(1)

    out = []
    for oid, obs in tier_abc.items():
        loc   = obs.get("location")   or {}
        occ   = obs.get("occurrence") or {}
        taxon = obs.get("taxon")      or {}
        event = obs.get("event")      or {}

        lat = loc.get("decimalLatitude")
        lon = loc.get("decimalLongitude")
        if lat is None or lon is None:
            continue

        # Bestäm kategori via mängdtillhörighet
        # tier_c (limit=1) = säkrast = C, tier_bc (limit=5) = B+C, tier_abc (limit=13) = allt
        if oid in tier_c:
            act = 13   # Säker häckning (C)
        elif oid in tier_bc:
            act = 5    # Sannolik häckning (B)
        else:
            act = 1    # Möjlig häckning (A)

        out.append({
            "lat":      lat,
            "lon":      lon,
            "sv":       taxon.get("vernacularName")    or "",
            "sci":      taxon.get("scientificName")    or "",
            "key":      taxon.get("id"),
            "act":      act,
            "cnt":      occ.get("organismQuantityInt") or 1,
            "site":     loc.get("locality")            or "",
            "date":     event.get("startDate")         or "",
            "reporter": occ.get("recordedBy") or occ.get("reportedBy") or "",
        })

    print(f"  breeding: abc={len(tier_abc)} bc={len(tier_bc)} "
          f"c={len(tier_c)} out={len(out)}")

    payload = {
        "total":       len(out),
        "truncated":   len(tier_abc) >= 5000,
        "observations": out,
    }

    # ── Spara till cache ────────────────────────────────────────────────────
    try:
        with open(cache_path, 'w', encoding='utf-8') as _cf:
            _json.dump({"cached_date": today, "payload": payload}, _cf,
                       ensure_ascii=False)
    except Exception as _ce:
        _log_error(f"breeding cache write: {_ce}")

    return jsonify(payload)


@app.route("/api/breeding/probe")
def breeding_probe():
    """Hämtar 5 råobservationer och returnerar fältnamnen i occurrence+location.
       Används för att diagnostisera vilket fältnamn häckningsaktiviteten har."""
    if not _session["access_token"] and not _session["subscription_key"]:
        return jsonify({"error": "Inte inloggad."}), 401

    # Hämta en handfull obs under häckningstid utan filter
    body = {
        "taxon": {"ids": [AVES_TAXON_ID], "includeUnderlyingTaxa": True},
        "date": {"startDate": "2025-06-01", "endDate": "2025-06-30",
                 "dateFilterType": "OverlappingStartDateAndEndDate"},
        "geographics": {"areas": [{"areaType": "County", "featureId": "24"}]},
    }
    try:
        r = requests.post(f"{SOS_API_BASE}/Observations/Search",
                          headers=_auth_headers(), json=body,
                          params={"skip": 0, "take": 20}, timeout=20)
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 503
    if not r.ok:
        return jsonify({"error": f"HTTP {r.status_code}", "detail": r.text[:400]}), r.status_code

    data    = r.json()
    records = data if isinstance(data, list) else data.get("records", [])

    samples = []
    for obs in records[:5]:
        occ = obs.get("occurrence") or {}
        loc = obs.get("location")   or {}
        # Visa alla fält – filtrera inga
        samples.append({
            "occurrence": occ,
            "location_keys": list(loc.keys()),
        })

    # Testa SAMMA anrop MED häckningsfilter och returnera HELA occurrence-objektet
    body_f = dict(body)
    body_f["birdNestActivityLimit"] = 1
    try:
        r2 = requests.post(f"{SOS_API_BASE}/Observations/Search",
                           headers=_auth_headers(), json=body_f,
                           params={"skip": 0, "take": 10}, timeout=20)
        if r2.ok:
            recs2   = r2.json() if isinstance(r2.json(), list) else r2.json().get("records", [])
            with_samples = []
            for obs in recs2[:5]:
                occ = obs.get("occurrence") or {}
                with_samples.append({
                    "occurrence_keys":   list(occ.keys()),
                    "occurrence_full":   occ,          # HELA objektet
                })
            filter_result = {
                "status":  r2.status_code,
                "count":   len(recs2),
                "samples": with_samples,
            }
        else:
            filter_result = {"status": r2.status_code, "error": r2.text[:400]}
    except Exception as e:
        filter_result = {"error": str(e)}

    return jsonify({
        "without_filter_sample": samples,
        "with_filter_test":      filter_result,
    })


@app.route("/api/reporter_list")
def reporter_list():
    """Returnerar lista på alla rapportörsnamn från statistikcachen (för autocomplete)."""
    county_id = (request.args.get("county_id") or DEFAULT_COUNTY_ID).strip()
    year      = (request.args.get("year") or str(_date_type.today().year)).strip()
    cache_key = f"{county_id}_{year}"
    with _stats_lock:
        data = _stats_cache.get(cache_key)
    if not data:
        return jsonify([])
    reporters = data.get('top_reporters', [])
    return jsonify([r['name'] for r in reporters])


@app.route("/api/reporter_stats")
def reporter_stats():
    """Returnerar detaljerad statistik för en enskild rapportör från statistikcachen."""
    county_id = (request.args.get("county_id") or DEFAULT_COUNTY_ID).strip()
    year      = (request.args.get("year") or str(_date_type.today().year)).strip()
    name      = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name krävs"}), 400
    cache_key = f"{county_id}_{year}"
    with _stats_lock:
        payload = _stats_cache.get(cache_key)
    if not payload:
        return jsonify({"error": "cache_missing"}), 404
    try:
        details  = payload.get('reporter_details', {})
        # Exakt match, annars case-insensitivt
        match = details.get(name) or next(
            (v for k, v in details.items() if k.lower() == name.lower()), None)
        match_name = name if name in details else next(
            (k for k in details if k.lower() == name.lower()), name)
        if not match:
            return jsonify({"error": "not_found"}), 404
        top_rap  = payload.get('top_reporters', [])
        rap_info = next((r for r in top_rap if r['name'] == match_name), {})
        return jsonify({
            'name':    match_name,
            'obs':     rap_info.get('obs', sum(match['monthly'])),
            'arter':   len(match['species']),
            'dagar':   match['dagar'],
            'since':   match['since'],
            'lastObs': match['lastObs'],
            'monthly': match['monthly'],
            'places':  match['places'],
            'species': match['species'],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reporter_debug")
def reporter_debug():
    """Debugar statistikcachens innehåll för ett visst county+år."""
    county_id = (request.args.get("county_id") or DEFAULT_COUNTY_ID).strip()
    year      = (request.args.get("year") or str(_date_type.today().year)).strip()
    cache_key = f"{county_id}_{year}"
    with _stats_lock:
        payload = _stats_cache.get(cache_key)
    if not payload:
        return jsonify({"in_memory": False, "cache_key": cache_key})
    details = payload.get('reporter_details', {})
    top_rap = payload.get('top_reporters', [])
    return jsonify({
        "in_memory":           True,
        "cache_key":           cache_key,
        "has_reporter_details": bool(details),
        "reporter_details_count": len(details),
        "top_reporters_count": len(top_rap),
        "sample_detail_keys":  list(details.keys())[:5],
        "sample_top_reporters": [r['name'] for r in top_rap[:5]],
    })


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


def _new_agg_state():
    """Skapar ett tomt aggregeringstillstånd för inkrementell bearbetning."""
    return {
        'species':        _defaultdict(lambda: {
                              'obs': 0, 'ind': 0, 'sv': '', 'sci': '', 'key': None,
                              'rl': None, 'last_date': '', 'last_rep': ''}),
        'reporters':      _defaultdict(lambda: {'obs': 0, 'species': set()}),
        'monthly':        [0] * 12,
        'total_ind':      0,
        'monthly_sp':     _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'ind': 0})),
        'monthly_rep':    _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'species': set()})),
        'muni_sp':        _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'ind': 0})),
        'muni_rep':       _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'species': set()})),
        'muni_month_sp':  _defaultdict(lambda: _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'ind': 0}))),
        'muni_month_rep': _defaultdict(lambda: _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'species': set()}))),
        'taxon_ids':      set(),
        'muni_names':     {},   # { featureId: kommunnamn } direkt från SOS API
        # Per-rapportör detaljdata (byggs upp under aggregeringen)
        'rep_monthly':    _defaultdict(lambda: [0] * 12),   # name → [12 månader]
        'rep_species':    _defaultdict(lambda: _defaultdict(lambda: {'obs': 0, 'sv': '', 'sci': ''})),
        'rep_places':     _defaultdict(lambda: _defaultdict(int)),  # name → locality → count
        'rep_days':       _defaultdict(set),                # name → set of date-strings
    }


def _agg_add_records(state, records):
    """Lägger till en sida av observationer i ett pågående aggregeringstillstånd."""
    species     = state['species']
    reporters   = state['reporters']
    monthly     = state['monthly']
    monthly_sp  = state['monthly_sp']
    monthly_rep = state['monthly_rep']
    muni_sp     = state['muni_sp']
    muni_rep    = state['muni_rep']
    muni_month_sp  = state['muni_month_sp']
    muni_month_rep = state['muni_month_rep']

    for rec in records:
        taxon    = rec.get('taxon')      or rec.get('Taxon')      or {}
        occ      = rec.get('occurrence') or rec.get('Occurrence') or {}
        event    = rec.get('event')      or rec.get('Event')      or {}
        location = rec.get('location')   or rec.get('Location')   or {}
        muni_obj = location.get('municipality') or {}
        muni_fid  = muni_obj.get('featureId') or ''
        muni_name = muni_obj.get('name') or ''
        if muni_fid and muni_name and muni_fid not in state['muni_names']:
            state['muni_names'][muni_fid] = muni_name

        key = taxon.get('id') or taxon.get('taxonId') or taxon.get('dyntaxaId')
        if not key:
            continue
        key = int(key)
        state['taxon_ids'].add(key)

        sv_name  = taxon.get('vernacularName') or taxon.get('commonName') or ''
        sci_name = taxon.get('scientificName') or ''
        count    = int(occ.get('individualCount') or occ.get('quantity') or 1)
        reporter = (occ.get('reportedBy') or occ.get('observer') or '').strip()
        start_dt = event.get('startDate') or event.get('startDayOfYear') or ''
        rl_cat   = _get_rl_category(taxon)

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

        locality = (location.get('locality') or location.get('name') or '').strip()

        if reporter:
            reporters[reporter]['obs'] += 1
            reporters[reporter]['species'].add(key)
            # Per-rapportör detaljdata
            if month_0 is not None:
                state['rep_monthly'][reporter][month_0] += 1
            state['rep_species'][reporter][key]['obs'] += 1
            if sv_name:  state['rep_species'][reporter][key]['sv']  = sv_name
            if sci_name: state['rep_species'][reporter][key]['sci'] = sci_name
            if locality:
                state['rep_places'][reporter][locality] += 1
            if start_dt and len(start_dt) >= 10:
                state['rep_days'][reporter].add(start_dt[:10])

        state['total_ind'] += count

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
            if month_0 is not None:
                m1 = month_0 + 1
                muni_month_sp[muni_fid][m1][key]['obs'] += 1
                muni_month_sp[muni_fid][m1][key]['ind'] += count
                if reporter:
                    muni_month_rep[muni_fid][m1][reporter]['obs'] += 1
                    muni_month_rep[muni_fid][m1][reporter]['species'].add(key)


def _agg_finalize(state):
    """Sorterar och formaterar aggregeringstillståndet till ett resultat-dict."""
    species     = state['species']
    reporters   = state['reporters']
    monthly_sp  = state['monthly_sp']
    monthly_rep = state['monthly_rep']
    muni_sp     = state['muni_sp']
    muni_rep    = state['muni_rep']
    muni_month_sp  = state['muni_month_sp']
    muni_month_rep = state['muni_month_rep']

    top_sp = sorted(
        [{'sv': v['sv'], 'sci': v['sci'], 'key': v['key'],
          'obs': v['obs'], 'ind': v['ind']} for v in species.values()],
        key=lambda x: x['obs'], reverse=True
    )

    top_rap = sorted(
        [{'name': k, 'arter': len(v['species']), 'obs': v['obs']}
         for k, v in reporters.items()],
        key=lambda x: x['arter'], reverse=True
    )

    month_species   = {}
    month_reporters = {}
    for m in range(1, 13):
        ms = monthly_sp.get(m, {})
        month_species[m] = sorted(
            [{'key': k, 'sv': species[k]['sv'], 'sci': species[k]['sci'],
              'obs': v['obs'], 'ind': v['ind']}
             for k, v in ms.items() if k in species],
            key=lambda x: x['obs'], reverse=True
        )
        mr = monthly_rep.get(m, {})
        month_reporters[m] = sorted(
            [{'name': nm, 'obs': v['obs'], 'arter': len(v['species'])}
             for nm, v in mr.items()],
            key=lambda x: x['arter'], reverse=True
        )

    muni_species   = {}
    muni_reporters = {}
    for fid, ms in muni_sp.items():
        muni_species[fid] = sorted(
            [{'key': k, 'sv': species[k]['sv'], 'sci': species[k]['sci'],
              'obs': v['obs'], 'ind': v['ind']}
             for k, v in ms.items() if k in species],
            key=lambda x: x['obs'], reverse=True
        )
    for fid, mr in muni_rep.items():
        muni_reporters[fid] = sorted(
            [{'name': nm, 'obs': v['obs'], 'arter': len(v['species'])}
             for nm, v in mr.items()],
            key=lambda x: x['arter'], reverse=True
        )

    muni_month_species   = {}
    muni_month_reporters = {}
    for fid, months in muni_month_sp.items():
        muni_month_species[fid] = {}
        for m, ms in months.items():
            muni_month_species[fid][m] = sorted(
                [{'key': k, 'sv': species[k]['sv'], 'sci': species[k]['sci'],
                  'obs': v['obs'], 'ind': v['ind']}
                 for k, v in ms.items() if k in species],
                key=lambda x: x['obs'], reverse=True
            )
    for fid, months in muni_month_rep.items():
        muni_month_reporters[fid] = {}
        for m, mr in months.items():
            muni_month_reporters[fid][m] = sorted(
                [{'name': nm, 'obs': v['obs'], 'arter': len(v['species'])}
                 for nm, v in mr.items()],
                key=lambda x: x['arter'], reverse=True
            )

    # Per-rapportör detaljdata
    reporter_details = {}
    for rep_name, rep_sp in state['rep_species'].items():
        days = state['rep_days'][rep_name]
        places_sorted = sorted(
            [{'name': k, 'obs': v} for k, v in state['rep_places'][rep_name].items()],
            key=lambda x: -x['obs']
        )[:15]
        species_sorted = sorted(
            [{'taxon': str(k), 'sv': v['sv'], 'sci': v['sci'], 'obs': v['obs']}
             for k, v in rep_sp.items()],
            key=lambda x: -x['obs']
        )
        reporter_details[rep_name] = {
            'monthly':  state['rep_monthly'][rep_name],
            'species':  species_sorted,
            'places':   places_sorted,
            'dagar':    len(days),
            'lastObs':  max(days) if days else '',
            'since':    min(days)[:4] if days else '',
        }

    return {
        'kpi':                  {'arter': len(species),
                                 'obs':   sum(v['obs'] for v in species.values()),
                                 'ind':   state['total_ind'],
                                 'reporters': len(reporters)},
        'monthly':              state['monthly'],
        'top_species':          top_sp,
        'top_reporters':        top_rap,
        'month_species':        month_species,
        'month_reporters':      month_reporters,
        'muni_names':           state['muni_names'],
        'muni_species':         muni_species,
        'muni_reporters':       muni_reporters,
        'muni_month_species':   muni_month_species,
        'muni_month_reporters': muni_month_reporters,
        'reporter_details':     reporter_details,
    }


def _fetch_year_stats(year, county_id=None, max_month=12):
    """Hämtar och aggregerar alla observationer för ett år via månadsvis paginering.
    SOS API tillåter max skip+take=50 000 per fråga, så vi delar upp per månad
    (och per vecka för månader med > 49 000 observationer, t.ex. stora län).
    max_month: hämta bara t.o.m. denna månad (används för innevarande år)."""
    if county_id is None:
        county_id = DEFAULT_COUNTY_ID
    if not _session['access_token'] and not _session['subscription_key']:
        return None

    cache_key   = f"{county_id}_{year}"
    state       = _new_agg_state()
    fetched     = 0
    take        = 1000

    with _stats_lock:
        _build_progress[cache_key] = {'status': 'building', 'fetched': 0, 'total': 0}

    for month in range(1, min(max_month, 12) + 1):
        last_day = _calendar.monthrange(year, month)[1]
        m_start  = f'{year}-{month:02d}-01'
        m_end    = f'{year}-{month:02d}-{last_day:02d}'

        # Probe: kontrollera hur många obs som finns denna månad (med retry)
        month_total = 0
        for attempt in range(3):
            try:
                probe_body = {
                    'taxon':       {'ids': [AVES_TAXON_ID], 'includeUnderlyingTaxa': True},
                    'date':        {'startDate': m_start, 'endDate': m_end,
                                    'dateFilterType': 'OverlappingStartDateAndEndDate'},
                    'geographics': {'areas': [{'areaType': 'County', 'featureId': county_id}]},
                }
                probe = requests.post(
                    f'{SOS_API_BASE}/Observations/Search',
                    headers=_auth_headers(), json=probe_body,
                    params={'skip': 0, 'take': 1}, timeout=30,
                )
                if probe.ok:
                    month_total = int(probe.json().get('totalCount') or 0)
                    break
                else:
                    print(f'  Stats ({cache_key}): probe {month:02d} HTTP {probe.status_code}, försök {attempt+1}/3')
                    _log_error(f'Stats {cache_key}: probe {month:02d} HTTP {probe.status_code}')
                    _time.sleep(5 * (attempt + 1))
            except Exception as e:
                print(f'  Stats ({cache_key}): probe {month:02d} fel – {e}, försök {attempt+1}/3')
                _log_error(f'Stats {cache_key}: probe {month:02d} – {e}')
                _time.sleep(5 * (attempt + 1))

        _time.sleep(1)  # paus mellan månader för att undvika rate-limiting

        # Dela upp i veckofönster om månaden > 49 000 obs (undviker 50K-gränsen)
        if month_total > 49000:
            windows = []
            d = _date_type(year, month, 1)
            end_d = _date_type(year, month, last_day)
            while d <= end_d:
                w_end = min(d + _timedelta(days=6), end_d)
                windows.append((d.isoformat(), w_end.isoformat()))
                d = w_end + _timedelta(days=1)
        else:
            windows = [(m_start, m_end)]

        for w_start, w_end in windows:
            body = {
                'taxon':       {'ids': [AVES_TAXON_ID], 'includeUnderlyingTaxa': True},
                'date':        {'startDate': w_start, 'endDate': w_end,
                                'dateFilterType': 'OverlappingStartDateAndEndDate'},
                'geographics': {'areas': [{'areaType': 'County', 'featureId': county_id}]},
            }
            skip      = 0
            win_total = None
            while True:
                # Hämta en sida med retry
                resp = None
                for attempt in range(3):
                    try:
                        resp = requests.post(
                            f'{SOS_API_BASE}/Observations/Search',
                            headers=_auth_headers(), json=body,
                            params={'skip': skip, 'take': take}, timeout=60,
                        )
                        if resp.ok:
                            break
                        print(f'  Stats ({cache_key}): HTTP {resp.status_code} skip={skip}, försök {attempt+1}/3')
                        _log_error(f'Stats {cache_key}: HTTP {resp.status_code} skip={skip}')
                        _time.sleep(10 * (attempt + 1))
                    except requests.RequestException as e:
                        print(f'  Stats ({cache_key}): nätverksfel – {e}, försök {attempt+1}/3')
                        _log_error(f'Stats {cache_key}: nätverksfel – {e}')
                        _time.sleep(10 * (attempt + 1))
                        resp = None

                if resp is None or not resp.ok:
                    print(f'  Stats ({cache_key}): ger upp för {w_start}–{w_end} skip={skip}')
                    _log_error(f'Stats {cache_key}: ger upp {w_start}–{w_end} skip={skip}')
                    break

                try:
                    data    = resp.json()
                    records = data.get('records') or data.get('observations') or data.get('results') or []
                    if win_total is None:
                        win_total = int(data.get('totalCount') or 0)
                    _agg_add_records(state, records)
                    fetched += len(records)
                except Exception as e:
                    print(f'  Stats ({cache_key}): fel vid aggregering – {e}')
                    _log_error(f'Stats {cache_key}: aggregeringsfel – {e}')
                    break

                with _stats_lock:
                    _build_progress[cache_key] = {
                        'status': 'building', 'fetched': fetched, 'total': 0,
                    }

                skip += take
                if not records or skip >= (win_total or 1):
                    break
                _time.sleep(0.5)  # något längre paus mellan sidor

    if fetched == 0:
        return None

    result = _agg_finalize(state)
    result.update({'year': year, 'county_id': county_id,
                   'cached_at': _dt.now().isoformat(), 'total_fetched': fetched})
    return result


def _save_cache(cache_key=None):
    """Sparar cachefil(er).  Om cache_key anges sparas bara den posten (snabbt).
    Annars sparas alla poster (används vid start/migrering).
    Varje county+år sparas i en egen fil: stats_cache_24_2026.json"""
    with _stats_lock:
        snapshot = {cache_key: _stats_cache[cache_key]} if cache_key and cache_key in _stats_cache \
                   else dict(_stats_cache)
    for ck, data in snapshot.items():
        try:
            parts = ck.split('_', 1)
            if len(parts) == 2:
                filepath = _cache_file_for(parts[0], parts[1])
            else:
                filepath = _CACHE_FILE  # fallback
            with open(filepath, 'w', encoding='utf-8') as f:
                _json.dump({ck: data}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f'  Stats: kunde inte spara {ck} – {e}')
            _log_error(f'Stats: kunde inte spara {ck} – {e}')


def _trigger_on_demand(year, county_id):
    """Startar bakgrundsbygge för en specifik county+year om det inte redan byggs."""
    cache_key = f"{county_id}_{year}"
    with _stats_lock:
        if cache_key in _stats_cache:
            return
        if cache_key in _building:
            return
        _building.add(cache_key)
        _build_progress[cache_key] = {'status': 'building', 'fetched': 0, 'total': 0}

    def _build():
        today = _date_type.today()
        max_month = today.month if year == today.year else 12
        result = _fetch_year_stats(year, county_id, max_month=max_month)
        with _stats_lock:
            _building.discard(cache_key)
            if result:
                _stats_cache[cache_key] = result
                _build_progress[cache_key] = {'status': 'ready'}
            else:
                _build_progress[cache_key] = {'status': 'error'}
        if result:
            _save_cache(cache_key)
            print(f'  Stats: {cache_key} klar – '
                  f'{result["kpi"]["obs"]} obs, {result["kpi"]["arter"]} arter')
        else:
            print(f'  Stats: misslyckades för {cache_key}')
            _log_error(f'Stats: misslyckades för {cache_key}')

    _threading.Thread(target=_build, daemon=True, name=f'stats-{cache_key}').start()


def _stats_builder():
    """Bakgrundstråd: laddar cache, hämtar saknade år för defaultlän (nyaste år först),
    uppdaterar innevarande år var 6:e timme."""
    global _stats_cache

    # ── Ladda per-år-filer (stats_cache_24_2026.json etc.) ──────────────────
    import glob as _glob
    loaded = {}
    per_year_files = sorted(_glob.glob(_os.path.join(_BASE_DIR, 'stats_cache_*_*.json')))
    for filepath in per_year_files:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = _json.load(f)
            loaded.update(data)
        except Exception as e:
            print(f'  Stats: kunde inte läsa {filepath} – {e}')

    # Fallback: gammal samlad fil (migrering)
    if not loaded and _os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, 'r', encoding='utf-8') as f:
                old = _json.load(f)
            for k, v in old.items():
                if k.isdigit():
                    loaded[f"{DEFAULT_COUNTY_ID}_{k}"] = v
                else:
                    loaded[k] = v
            print(f'  Stats: migrerade gammal cache ({len(loaded)} poster)')
        except Exception as e:
            print(f'  Stats: kunde inte läsa gammal cache – {e}')

    if loaded:
        with _stats_lock:
            _stats_cache = loaded
        print(f'  Stats: cache laddad ({len(loaded)} poster)')

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

        today        = _date_type.today()
        current_year = today.year
        current_month = today.month

        # ── Iterera nyaste år FÖRST (current_year → _FIRST_YEAR) ────────────
        for year in range(current_year, _FIRST_YEAR - 1, -1):
            cache_key = f"{DEFAULT_COUNTY_ID}_{year}"

            with _stats_lock:
                cached   = _stats_cache.get(cache_key)
                building = cache_key in _building

            if building:
                continue

            # Historiska år: hämta bara en gång – men kontrollera att cachen är komplett
            if cached and year < current_year:
                monthly = cached.get('monthly', [])
                months_with_data = sum(1 for m in (monthly or []) if m > 0)
                if months_with_data >= 6:
                    continue  # Ser komplett ut, hoppa över
                print(f'  Stats: {cache_key} verkar ofullständig ({months_with_data}/12 månader) – hämtar om')

            # Innevarande år: uppdatera om cachen är > 6 h gammal
            if cached and year == current_year:
                try:
                    age_h = (_dt.now() - _dt.fromisoformat(cached['cached_at'])).total_seconds() / 3600
                    if age_h < 6:
                        continue
                except Exception:
                    pass

            max_month = current_month if year == current_year else 12
            print(f'  Stats: hämtar {cache_key} (månader 1–{max_month})…')
            with _stats_lock:
                _build_progress[cache_key] = {'status': 'building', 'fetched': 0, 'total': 0}

            result = _fetch_year_stats(year, DEFAULT_COUNTY_ID, max_month=max_month)
            if result:
                with _stats_lock:
                    _stats_cache[cache_key] = result
                    _build_progress[cache_key] = {'status': 'ready'}
                _save_cache(cache_key)   # spara bara detta år direkt
                print(f'  Stats: {cache_key} klar – '
                      f'{result["kpi"]["obs"]} obs, {result["kpi"]["arter"]} arter')
            else:
                with _stats_lock:
                    _build_progress[cache_key] = {'status': 'error'}
                print(f'  Stats: misslyckades för {cache_key}')

        # Sov 1 timme (vaknar och kontrollerar om innevarande år behöver uppdateras)
        _time.sleep(3600)


@app.route('/api/statistics')
def get_statistics():
    year      = request.args.get('year', str(_date_type.today().year))
    county    = request.args.get('county', DEFAULT_COUNTY_ID)
    cache_key = f"{county}_{year}"

    with _stats_lock:
        data     = _stats_cache.get(cache_key)
        progress = _build_progress.get(cache_key, {})

    if data:
        return jsonify({'status': 'ready', 'data': data})
    if not _session['access_token'] and not _session['subscription_key']:
        return jsonify({'status': 'unauthenticated'}), 401
    if progress.get('status') == 'building':
        return jsonify({'status': 'building',
                        'fetched': progress.get('fetched', 0),
                        'total':   progress.get('total', 0)}), 202
    # Starta on-demand-bygge för ej cachade county/år-kombinationer
    _trigger_on_demand(int(year), county)
    return jsonify({'status': 'building', 'fetched': 0, 'total': 0}), 202


@app.route('/api/statistics/years')
def statistics_years():
    """Returnerar vilka år som finns i cachen, filtrerat per county."""
    county = request.args.get('county', DEFAULT_COUNTY_ID)
    prefix = f"{county}_"
    with _stats_lock:
        years = {
            k[len(prefix):]: {'cached_at': d.get('cached_at'), 'kpi': d.get('kpi')}
            for k, d in _stats_cache.items()
            if k.startswith(prefix)
        }
    return jsonify(years)


@app.route('/logs')
def error_logs():
    """Visar felloggen som en enkel HTML-sida."""
    entries = list(reversed(list(_error_log)))
    rows = ''.join(
        f'<tr><td>{e["date"]}</td><td>{e["time"]}</td><td>{e["msg"]}</td></tr>'
        for e in entries
    )
    table = (
        f'<table><thead><tr><th>Datum</th><th>Tid</th><th>Meddelande</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        if entries else '<p class="empty">Inga fel loggade sedan serverstart.</p>'
    )
    html = f"""<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <title>Fellogg – Fågelobservationer v{APP_VERSION}</title>
  <style>
    body {{ font-family: monospace; background: #111; color: #ccc; padding: 1.5rem; margin: 0; }}
    h1   {{ color: #e77; font-size: 1.1rem; margin-bottom: 0.3rem; }}
    .meta {{ color: #555; font-size: 0.75rem; margin-bottom: 1.2rem; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 0.8rem; }}
    th    {{ background: #2a2a2a; color: #999; padding: 5px 10px; text-align: left;
             border-bottom: 1px solid #333; }}
    td    {{ padding: 4px 10px; border-bottom: 1px solid #1e1e1e; vertical-align: top; }}
    td:nth-child(1), td:nth-child(2) {{ white-space: nowrap; color: #666; }}
    td:nth-child(3) {{ color: #ffd; word-break: break-all; }}
    .empty {{ color: #555; font-style: italic; }}
  </style>
</head>
<body>
  <a href="javascript:history.back()" style="display:inline-block;margin-bottom:1.2rem;color:#888;font-size:0.8rem;text-decoration:none;">← Tillbaka</a>
  <h1>Fellogg – {len(entries)} poster (max 500, nyaste först)</h1>
  <div class="meta">Serverstart: {_SERVER_START.strftime('%Y-%m-%d %H:%M:%S')} &nbsp;·&nbsp; Version {APP_VERSION}</div>
  {table}
</body>
</html>"""
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


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


# ── Umami besöksstatistik ────────────────────────────────────────────────────
_UMAMI_WEBSITE_ID  = '38c468e5-8142-479d-bc5d-62ab6904d5e8'
_UMAMI_BASE        = 'https://cloud.umami.is'
_umami_token_cache = {'token': None, 'expires': 0}  # enkel in-memory cache

def _umami_token():
    """Loggar in mot Umami Cloud och returnerar (token, error_str)."""
    now = _time.time()
    if _umami_token_cache['token'] and _umami_token_cache['expires'] > now:
        return _umami_token_cache['token'], None
    email    = _os.environ.get('UMAMI_EMAIL', '')
    password = _os.environ.get('UMAMI_PASSWORD', '')
    if not email:
        return None, 'UMAMI_EMAIL saknas'
    if not password:
        return None, 'UMAMI_PASSWORD saknas'
    try:
        r = requests.post(f'{_UMAMI_BASE}/api/auth/login',
                          json={'email': email, 'password': password}, timeout=10)
        if not r.ok:
            return None, f'Inloggning misslyckades: HTTP {r.status_code} – {r.text[:200]}'
        token = r.json().get('token')
        if not token:
            return None, f'Inget token i svar: {r.text[:200]}'
        _umami_token_cache['token']   = token
        _umami_token_cache['expires'] = now + 82800
        return token, None
    except Exception as e:
        return None, f'Nätverksfel: {e}'

@app.route('/api/umami_stats')
def umami_stats():
    token, err = _umami_token()
    if not token:
        return jsonify({'error': err}), 503
    url    = f'{_UMAMI_BASE}/api/websites/{_UMAMI_WEBSITE_ID}/stats'
    params = {'startAt': 0, 'endAt': int(_time.time() * 1000)}
    try:
        r = requests.get(url, headers={'Authorization': f'Bearer {token}'},
                         params=params, timeout=10)
        if r.status_code == 401:
            _umami_token_cache['token'] = None
            token, err = _umami_token()
            if not token:
                return jsonify({'error': err}), 503
            r = requests.get(url, headers={'Authorization': f'Bearer {token}'},
                             params=params, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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