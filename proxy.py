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
import zipfile as _zipfile
import xml.etree.ElementTree as _ET

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── Cloudflare R2 (persistent cache) ───────────────────────────────────────
_R2_ACCOUNT_ID = _os.environ.get('R2_ACCOUNT_ID', '')
_R2_ACCESS_KEY = _os.environ.get('R2_ACCESS_KEY_ID', '')
_R2_SECRET_KEY = _os.environ.get('R2_SECRET_ACCESS_KEY', '')
_R2_BUCKET     = _os.environ.get('R2_BUCKET', 'birds-cache')
_r2_client_obj = None
_r2_client_lock = _threading.Lock()

def _r2():
    """Returnerar en boto3 S3-klient mot R2, eller None om ej konfigurerat."""
    global _r2_client_obj
    if not (_R2_ACCOUNT_ID and _R2_ACCESS_KEY and _R2_SECRET_KEY):
        return None
    with _r2_client_lock:
        if _r2_client_obj is None:
            try:
                import boto3
                _r2_client_obj = boto3.client(
                    's3',
                    endpoint_url=f'https://{_R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
                    aws_access_key_id=_R2_ACCESS_KEY,
                    aws_secret_access_key=_R2_SECRET_KEY,
                    region_name='auto',
                )
                print('  R2: klient skapad')
            except Exception as _e:
                print(f'  R2: kunde inte skapa klient – {_e}')
        return _r2_client_obj

def _r2_get(key):
    """Läs ett JSON-objekt från R2. Returnerar dict/list eller None."""
    client = _r2()
    if not client:
        return None
    try:
        resp = client.get_object(Bucket=_R2_BUCKET, Key=key)
        return _json.loads(resp['Body'].read().decode('utf-8'))
    except client.exceptions.NoSuchKey:
        return None
    except Exception as _e:
        print(f'  R2 get {key}: {_e}')
        return None

def _r2_put(key, data):
    """Skriv ett JSON-objekt till R2. Returnerar True vid lyckat."""
    client = _r2()
    if not client:
        return False
    try:
        body = _json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        client.put_object(Bucket=_R2_BUCKET, Key=key, Body=body,
                          ContentType='application/json')
        return True
    except Exception as _e:
        print(f'  R2 put {key}: {_e}')
        _log_error(f'R2 put {key}: {_e}')
        return False

def _r2_list(prefix='stats_cache_'):
    """Lista R2-objekt med givet prefix. Returnerar lista av key-strängar."""
    client = _r2()
    if not client:
        return []
    try:
        resp = client.list_objects_v2(Bucket=_R2_BUCKET, Prefix=prefix)
        return [o['Key'] for o in resp.get('Contents', [])]
    except Exception as _e:
        print(f'  R2 list {prefix}: {_e}')
        return []

# ── Sverige: alla 21 läns feature-IDs (Artportalen/SOS-API) ─────────────────
SE_COUNTY_IDS = [
    "1","3","4","5","6","7","8","9","10","12",
    "13","14","17","18","19","20","21","22","23","24","25",
]  # Stockholm, Uppsala, Södermanland, Östergötland, Jönköping, Kronoberg,
   # Kalmar, Gotland, Blekinge, Skåne, Halland, Västra Götaland, Värmland,
   # Örebro, Västmanland, Dalarna, Gävleborg, Västernorrland, Jämtland,
   # Västerbotten, Norrbotten

_se_obs_lock  = _threading.Lock()
_se_obs_cache = {}   # { year(int): {last_date, reporters: {…}} }

# ── Konstanter ─────────────────────────────────────────────────────────────
APP_VERSION   = "4.1"          # Uppdatera vid varje deploy
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
    # R2-diagnostik
    r2_configured = bool(_R2_ACCOUNT_ID and _R2_ACCESS_KEY and _R2_SECRET_KEY)
    r2_ok = False
    r2_files = []
    if r2_configured:
        try:
            r2_files = _r2_list('stats_cache_')
            r2_ok = True
        except Exception:
            pass
    return jsonify({
        "running":        True,
        "logged_in":      bool(_session["access_token"] or _session["subscription_key"]),
        "username":       _session["username"],
        "auth_mode":      _session["auth_mode"],
        "r2_configured":  r2_configured,
        "r2_ok":          r2_ok,
        "r2_files":       r2_files,
        "cache_keys":     list(_stats_cache.keys()),
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
    try:
        return _obs_map_impl()
    except Exception as e:
        _log_error(f"obs_map oväntat fel: {e}")
        return jsonify({"error": f"Serverfel: {e}"}), 500

def _obs_map_impl():
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
    # översikt: max 5 000 obs | rapportör: max 15 000 (undviker Railway-timeout) | art: max 60 000
    max_pages = 5 if area_overview else (15 if reporter else 60)
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
    cache_key_r2 = f"breeding_cache_{feature_id}_{year}_{min_act}.json"
    today        = str(_date_type.today())
    current_year = str(_date_type.today().year)

    _cached = _r2_get(cache_key_r2)
    if _cached is None:
        # Fallback: lokalt filsystem (för lokal utveckling)
        local_path = _os.path.join(_BASE_DIR, cache_key_r2)
        if _os.path.exists(local_path):
            try:
                with open(local_path, 'r', encoding='utf-8') as _cf:
                    _cached = _json.load(_cf)
            except Exception as _ce:
                _log_error(f"breeding cache read local: {_ce}")
    if _cached is not None:
        # Innevarande år: cacha en dag; äldre år: cachas permanent
        if year != current_year or _cached.get('cached_date') == today:
            print(f"  breeding: cache-träff {cache_key_r2}")
            return jsonify(_cached['payload'])

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
    cache_data = {"cached_date": today, "payload": payload}
    if not _r2_put(cache_key_r2, cache_data):
        # Fallback: lokalt filsystem ENDAST om R2 ej är konfigurerat (lokal dev)
        if not _r2():
            try:
                local_path = _os.path.join(_BASE_DIR, cache_key_r2)
                with open(local_path, 'w', encoding='utf-8') as _cf:
                    _json.dump(cache_data, _cf, ensure_ascii=False)
            except Exception as _ce:
                _log_error(f"breeding cache write local: {_ce}")

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


@app.route("/api/observer_stats")
def observer_stats():
    """Returnerar observatörer sorterade efter unika arter.
    Primärt: läser från hela-Sverige-cachen (observers_se_YYYY.json) i R2.
    Fallback: länscachen (Västerbotten) om Sverige-cachen inte är redo."""
    year = (request.args.get("year") or str(_date_type.today().year)).strip()
    yr   = int(year)

    # ── Primärt: SE-cache (hela Sverige) ────────────────────────────────────
    with _se_obs_lock:
        se_data = _se_obs_cache.get(yr)

    # Inte i minnet? Ladda från R2 och konvertera direkt till minimalt API-format.
    # Lagra ALDRIG råfilen (top-30 + sp_ids per observatör) i _se_obs_cache –
    # den är ~450 MB som Python-objekt. _api_cache_from_compact ger ~7 MB.
    if not se_data:
        raw = _r2_get(_se_obs_r2_key(yr))
        if raw and raw.get('reporters'):
            se_data = _api_cache_from_compact(raw)
            with _se_obs_lock:
                _se_obs_cache[yr] = se_data

    if se_data and se_data.get('reporters'):
        try:
            result = []
            for name, d in se_data['reporters'].items():
                # _se_obs_cache innehåller alltid minimalt API-format (sp/pl top-3 listor)
                sp_list   = d.get('sp', [])
                pl_list   = d.get('pl', [])
                art       = d.get('art', 0)
                top_loc   = pl_list[0]['name'] if pl_list else ''
                top_3_lok = [{'name': x['name'], 'obs': x['obs']} for x in pl_list[:3]]
                top_3_sp  = [{'sv': x['sv'], 'obs': x['obs']} for x in sp_list[:3]]
                result.append({
                    'name':     name,
                    'obs':      d.get('obs', 0),
                    'art':      art,
                    'dagar':    d.get('dagar', 0),
                    'lastObs':  d.get('lastObs', ''),
                    'topLokal': top_loc,
                    'lokaler':  top_3_lok,
                    'species':  top_3_sp,
                    'monthly':  d.get('monthly', [0]*12),
                })
            result.sort(key=lambda x: (-x['art'], -x['obs']))
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Fallback: länscachen (Västerbotten) ─────────────────────────────────
    county_id = (request.args.get("county_id") or DEFAULT_COUNTY_ID).strip()
    cache_key = f"{county_id}_{year}"
    with _stats_lock:
        payload = _stats_cache.get(cache_key)
    if not payload:
        return jsonify({"error": "cache_missing", "cache_key": cache_key,
                        "hint": "SE-cache byggs i bakgrunden – försök igen om en stund"}), 404
    try:
        details    = payload.get('reporter_details', {})
        top_rap    = payload.get('top_reporters', [])
        obs_lookup = {r['name']: r['obs'] for r in top_rap}
        observers  = []
        for name, d in details.items():
            places    = d.get('places', [])
            observers.append({
                'name':     name,
                'obs':      obs_lookup.get(name) or sum(d.get('monthly', [])),
                'art':      len(d.get('species', [])),
                'dagar':    d.get('dagar', 0),
                'lastObs':  d.get('lastObs', ''),
                'topLokal': places[0]['name'] if places else '',
                'lokaler':  [{'name': p['name'], 'obs': p['obs']} for p in places[:3]],
                'species':  [{'sv': s['sv'], 'obs': s['obs']} for s in d.get('species', [])[:3]],
                'monthly':  d.get('monthly', [0]*12),
            })
        observers.sort(key=lambda x: (-x['art'], -x['obs']))
        return jsonify(observers)
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
        'rep_coords':     _defaultdict(lambda: _defaultdict(lambda: {'cnt':0,'sp':{}})),  # name → (lat3,lon3) → {cnt, sp:{key:{sv,sci,obs}}}
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

        locality = (location.get('locality') or location.get('name') or muni_name or '').strip()

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
            lat = location.get('decimalLatitude')
            lon = location.get('decimalLongitude')
            if lat is not None and lon is not None:
                coord_key = (round(float(lat), 3), round(float(lon), 3))
                entry = state['rep_coords'][reporter][coord_key]
                entry['cnt'] += 1
                if key not in entry['sp']:
                    entry['sp'][key] = {'sv': sv_name or '', 'sci': sci_name or '', 'obs': 0}
                entry['sp'][key]['obs'] += 1
                if sv_name:  entry['sp'][key]['sv']  = sv_name
                if sci_name: entry['sp'][key]['sci'] = sci_name

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
        coords_sorted = sorted(
            state['rep_coords'][rep_name].items(),
            key=lambda x: -x[1]['cnt']
        )
        coords = []
        for k, v in coords_sorted:
            sp_list = sorted(v['sp'].values(), key=lambda s: -s['obs'])
            coords.append({
                'lat': k[0], 'lon': k[1], 'cnt': v['cnt'],
                'species': [{'sv': s['sv'], 'sci': s['sci'], 'obs': s['obs']} for s in sp_list],
            })
        reporter_details[rep_name] = {
            'monthly':  state['rep_monthly'][rep_name],
            'species':  species_sorted,
            'places':   places_sorted,
            'coords':   coords,
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
    """Sparar cache till R2 (primärt) och lokalt filsystem (fallback).
    Om cache_key anges sparas bara den posten."""
    with _stats_lock:
        snapshot = {cache_key: _stats_cache[cache_key]} if cache_key and cache_key in _stats_cache \
                   else dict(_stats_cache)
    for ck, data in snapshot.items():
        r2_key = f"stats_cache_{ck}.json"
        # ── R2 (primärt) ──
        if _r2_put(r2_key, {ck: data}):
            print(f'  Stats: sparad till R2: {r2_key}')
            continue
        # ── Lokalt filsystem (fallback ENDAST för lokal utveckling utan R2) ──
        if not _r2():
            try:
                parts = ck.split('_', 1)
                filepath = _cache_file_for(parts[0], parts[1]) if len(parts) == 2 else _CACHE_FILE
                with open(filepath, 'w', encoding='utf-8') as f:
                    _json.dump({ck: data}, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f'  Stats: kunde inte spara {ck} – {e}')
                _log_error(f'Stats: kunde inte spara {ck} – {e}')


def _trigger_on_demand(year, county_id):
    """Startar bakgrundsbygge för en specifik county+year om det inte redan byggs.
    Försöker alltid R2 först (snabbt) innan SOS API anropas (långsamt)."""
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

        # ── Försök läsa från R2 först (undviker SOS API-anrop) ──────────────
        result = None
        r2_data = _r2_get(f'stats_cache_{cache_key}.json')
        if r2_data and r2_data.get(cache_key):
            result = r2_data[cache_key]
            print(f'  Stats: {cache_key} laddad från R2 (ingen SOS-hämtning)')

        # ── Fallback: hämta från SOS API ─────────────────────────────────────
        if not result:
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

    # ── Ladda cache från R2 (primärt) ───────────────────────────────────────
    # Ladda bara innevarande år för defaultlänet vid uppstart.
    # Övriga county×år laddas lat från R2 via _trigger_on_demand när de efterfrågas.
    # Detta förhindrar att hundratals MB historisk statistik hamnar i RAM direkt.
    import glob as _glob
    loaded = {}
    current_year_str = str(_date_type.today().year)
    startup_key = f'stats_cache_{DEFAULT_COUNTY_ID}_{current_year_str}.json'
    r2_keys = _r2_list('stats_cache_')
    if r2_keys:
        print(f'  Stats: {len(r2_keys)} filer i R2 – laddar bara {startup_key}…')
        if startup_key in r2_keys:
            data = _r2_get(startup_key)
            if data:
                loaded.update(data)
        print(f'  Stats: {len(loaded)} poster laddade från R2')
    else:
        # ── Fallback: lokala filer (lokal utveckling eller första körning) ──
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

        # Spara lokala filer upp till R2 om R2 är konfigurerat
        if loaded and _r2():
            print('  Stats: laddar upp lokal cache till R2…')
            for ck, data in loaded.items():
                _r2_put(f'stats_cache_{ck}.json', {ck: data})

    if loaded:
        with _stats_lock:
            _stats_cache = loaded
        print(f'  Stats: cache laddad ({len(loaded)} poster)')

    # ── Ta bort ALLA lokala cachefiler om R2 är konfigurerat (frigör Railway-disk) ─
    if _r2():
        import glob as _glob2
        patterns = [
            'stats_cache_*.json',   # per-år länscache
            'stats_cache.json',     # gammal monolitfil
            'breeding_cache_*.json', # häckningscache
            'observers_se_*.json',  # Sverige-observatörscache
        ]
        for pat in patterns:
            for fp in _glob2.glob(_os.path.join(_BASE_DIR, pat)):
                try:
                    _os.remove(fp)
                    print(f'  Cleanup: tog bort lokal fil {_os.path.basename(fp)}')
                except Exception:
                    pass

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


# ── Sverige-observatörscache (inkrementell dagsfil i R2) ─────────────────────

def _se_obs_r2_key(year):
    return f"observers_se_{year}.json"

def _load_se_obs_r2(year):
    """Läs SE-observatörscachen från R2 och konvertera till in-memory arbetsformat.
    Hanterar både gammalt format (species/places dicts) och nytt kompakt format (art/sp_ids/sp/pl).
    Returnerar data-dict eller tomt skelett."""
    saved = _r2_get(_se_obs_r2_key(year))
    if not saved:
        return {'year': year, 'last_date': None, 'built_at': None, 'reporters': {}}

    reporters = {}
    for name, d in saved.get('reporters', {}).items():
        if 'species' in d:
            # ── Gammalt format (pre-v4.1) – species/places dicts ──────────────
            sp_ids = set(d['species'].keys())
            sp_obs = {k: {'sv': v.get('sv', k), 'obs': v.get('obs', 0)}
                      for k, v in d['species'].items()}
            pl_obs = dict(d.get('places', {}))
            art    = len(sp_ids)
        else:
            # ── Nytt kompakt format – sp_ids lista + top-30 listor ─────────────
            sp_ids = set(str(x) for x in d.get('sp_ids', []))
            sp_obs = {item['id']: {'sv': item['sv'], 'obs': item['obs'],
                                   'ind': item.get('ind', item['obs'])}
                      for item in d.get('sp', []) if 'id' in item}
            pl_obs = {item['name']: item['obs'] for item in d.get('pl', [])}
            art    = d.get('art', len(sp_ids))

        # ── Underarter ────────────────────────────────────────────────────────
        sub_ids = set(str(x) for x in d.get('sub_ids', []))
        sub_obs = {item['id']: {'sv': item['sv'], 'obs': item['obs'],
                                'ind': item.get('ind', item['obs'])}
                   for item in d.get('subsp', []) if 'id' in item}
        # ── Hybrider ──────────────────────────────────────────────────────────
        hyb_ids = set(str(x) for x in d.get('hyb_ids', []))
        hyb_obs = {item['id']: {'sv': item['sv'], 'obs': item['obs'],
                                'ind': item.get('ind', item['obs'])}
                   for item in d.get('hybsp', []) if 'id' in item}

        reporters[name] = {
            'obs':     d.get('obs', 0),
            'monthly': d.get('monthly', [0]*12),
            'art':     art,
            'sp_ids':  sp_ids,
            'sp_obs':  sp_obs,
            'sub_ids': sub_ids,
            'sub_obs': sub_obs,
            'hyb_ids': hyb_ids,
            'hyb_obs': hyb_obs,
            'pl_obs':  pl_obs,
            'dagar':   d.get('dagar', 0),
            'lastObs': d.get('lastObs', ''),
        }

    return {
        'year':      saved.get('year', year),
        'last_date': saved.get('last_date'),
        'built_at':  saved.get('built_at'),
        'reporters': reporters,
    }

def _build_compact_se(year, data):
    """Bygg kompakt reporters-dict från in-memory arbetsformat.
    Returnerar save_data-dict redo för R2 och in-memory cache."""
    reporters = data.get('reporters', {})
    compact = {}
    for name, rep in reporters.items():
        sp_ids  = rep.get('sp_ids',  set())
        sp_obs  = rep.get('sp_obs',  {})
        sub_ids = rep.get('sub_ids', set())
        sub_obs = rep.get('sub_obs', {})
        hyb_ids = rep.get('hyb_ids', set())
        hyb_obs = rep.get('hyb_obs', {})
        pl_obs  = rep.get('pl_obs',  {})

        def _sorted_sp(obs_dict):
            return sorted(
                [{'id': k, 'sv': v['sv'], 'obs': v['obs'], 'ind': v.get('ind', v['obs'])}
                 for k, v in obs_dict.items() if v.get('sv')],
                key=lambda x: -x['ind']
            )

        top_pl = sorted(
            [{'name': k, 'obs': v} for k, v in pl_obs.items()],
            key=lambda x: -x['obs']
        )[:30]
        compact[name] = {
            'obs':     rep.get('obs', 0),
            'monthly': rep.get('monthly', [0]*12),
            'art':     rep.get('art', len(sp_ids)),
            'sub':     len(sub_ids),
            'hyb':     len(hyb_ids),
            'sp_ids':  sorted(sp_ids),
            'sub_ids': sorted(sub_ids),
            'hyb_ids': sorted(hyb_ids),
            'sp':      _sorted_sp(sp_obs),    # alla arter (ingen gräns)
            'subsp':   _sorted_sp(sub_obs),   # alla underarter
            'hybsp':   _sorted_sp(hyb_obs),   # alla hybrider
            'pl':      top_pl,
            'dagar':   rep.get('dagar', 0),
            'lastObs': rep.get('lastObs', ''),
        }
    return {
        'year':      data.get('year', year),
        'last_date': data.get('last_date'),
        'built_at':  _dt.now().isoformat()[:19],
        'reporters': compact,
    }

def _api_cache_from_compact(compact_data):
    """Bygg ett minimalt in-memory API-cacheformat från compact_data (R2-format).
    Tar bara med det som observer_stats-endpointen faktiskt returnerar:
      - Inga sp_ids (bara byggtråden behöver dem)
      - Bara top-3 sp/pl (API visar aldrig mer)
    15 000 observatörer × ~500 B = ~7 MB istället för ~450 MB."""
    api_reps = {}
    for name, d in compact_data.get('reporters', {}).items():
        sp_list  = d.get('sp',    [])
        pl_list  = d.get('pl',    [])
        sub_list = d.get('subsp', [])
        hyb_list = d.get('hybsp', [])
        api_reps[name] = {
            'obs':     d.get('obs',  0),
            'art':     d.get('art',  0),
            'sub':     d.get('sub',  0),
            'hyb':     d.get('hyb',  0),
            'dagar':   d.get('dagar', 0),
            'lastObs': d.get('lastObs', ''),
            'monthly': d.get('monthly', [0]*12),
            'sp':      sp_list[:3],    # top-3 räcker – API visar aldrig mer
            'pl':      pl_list[:3],
            'subsp':   sub_list[:3],   # top-3 för preview i detalj
            'hybsp':   hyb_list[:3],
        }
    return {
        'year':      compact_data.get('year'),
        'last_date': compact_data.get('last_date'),
        'reporters': api_reps,
    }

def _build_species_se(year, data):
    """Bygg artfil för R2 – {sv, obs, ind} per art/underart/hybrid per rapportör.
    Returnerar separata listor 'sp', 'sub', 'hyb' per observatör.
    Ingen sp_ids, inget monthly – enbart det som artliste-endpointen behöver."""
    def _sp_list(obs_dict):
        return sorted(
            [{'sv': v['sv'], 'obs': v['obs'], 'ind': v.get('ind', v['obs'])}
             for v in obs_dict.values() if v.get('sv')],
            key=lambda x: -x['ind'],
        )
    reporters = {}
    for name, rep in data.get('reporters', {}).items():
        entry = {}
        sp  = _sp_list(rep.get('sp_obs',  {}))
        sub = _sp_list(rep.get('sub_obs', {}))
        hyb = _sp_list(rep.get('hyb_obs', {}))
        if sp:   entry['sp']  = sp
        if sub:  entry['sub'] = sub
        if hyb:  entry['hyb'] = hyb
        if entry:
            reporters[name] = entry
    return {
        'year':      year,
        'built_at':  _dt.now().isoformat()[:19],
        'reporters': reporters,
    }

def _save_se_obs_r2(year, data):
    """Komprimera och spara SE-observatörscachen till R2.
    Sparar två filer:
      observers_se_YYYY.json    – kompakt format för listvy (top-30 sp/pl + sp_ids)
      observers_se_sp_YYYY.json – alla artnamn per observatör för detaljvy
    Returnerar (ok: bool, api_cache: dict) – api_cache är ett minimalt format
    lämpligt för _se_obs_cache (inga sp_ids, bara top-3 sp/pl)."""
    save_data = _build_compact_se(year, data)   # full compact → R2
    ok = _r2_put(_se_obs_r2_key(year), save_data)
    if ok:
        print(f'  SE obs: sparad {_se_obs_r2_key(year)} '
              f'({len(save_data["reporters"])} observatörer, last={data["last_date"]})')

    # Spara artfilen – alla artnamn per rapportör (lazy-laddas från webbappen)
    sp_data = _build_species_se(year, data)
    sp_key  = f'observers_se_sp_{year}.json'
    sp_ok   = _r2_put(sp_key, sp_data)
    if sp_ok:
        print(f'  SE obs: sparad {sp_key} ({len(sp_data["reporters"])} rapportörer med artdata)')

    # Spara liten meta-fil (koordinatorn läser bara denna – inte den stora R2-filen)
    _r2_put(f'observers_se_meta_{year}.json', {
        'last_date': data.get('last_date', ''),
        'built_at':  _dt.now().isoformat()[:19],
    })

    api_cache = _api_cache_from_compact(save_data)   # minimal → RAM
    return ok, api_cache

def _fetch_county_obs_day(county_id, date_str):
    """Hämtar alla fågelobservationer för ett givet county och datum.
    Returnerar lista av rårecords från SOS API."""
    results, skip, take = [], 0, 1000
    body = {
        'taxon':       {'ids': [AVES_TAXON_ID], 'includeUnderlyingTaxa': True},
        'date':        {'startDate': date_str, 'endDate': date_str,
                        'dateFilterType': 'OverlappingStartDateAndEndDate'},
        'geographics': {'areas': [{'areaType': 'County', 'featureId': county_id}]},
    }
    while True:
        try:
            resp = requests.post(
                f'{SOS_API_BASE}/Observations/Search',
                headers=_auth_headers(), json=body,
                params={'skip': skip, 'take': take}, timeout=30
            )
            if not resp.ok:
                break
            rd   = resp.json()
            recs = rd.get('records', [])
            tot  = int(rd.get('totalCount') or 0)
            results.extend(recs)
            skip += len(recs)
            if not recs or skip >= tot or skip + take > 50000:
                break
        except Exception as e:
            print(f'  SE obs {county_id}/{date_str}: {e}')
            break
    return results

def _se_rep_empty():
    """Skapar ett tomt in-memory reporter-objekt."""
    return {
        'obs': 0, 'monthly': [0]*12,
        'art': 0,         # antal unika ARTER (underarter/hybrider ej inräknade)
        'sp_ids': set(),  # taxon-IDs för riktiga arter
        'sp_obs': {},     # {taxon_id: {'sv', 'obs', 'ind'}} – riktiga arter
        'sub_ids': set(), # taxon-IDs för underarter
        'sub_obs': {},    # {taxon_id: {'sv', 'obs', 'ind'}} – underarter
        'hyb_ids': set(), # taxon-IDs för hybrider
        'hyb_obs': {},    # {taxon_id: {'sv', 'obs', 'ind'}} – hybrider
        'pl_obs': {},     # {lokal: int} – trimmas vid sparning
        'dagar': 0, 'lastObs': '',
    }

# ── Svenska artsammanslagningar ──────────────────────────────────────────────
# Taxa vars svenska namn ska visas och räknas som ett annat namn.
# Används t.ex. när Dyntaxa delar upp en art i former som ändå ska
# presenteras som en enhet (gråkråka + svartkråka → kråka).
# Nyckel: observerat sv-namn (lowercase), värde: visningsnamn (lowercase)
_SV_NAME_MERGES = {
    'gråkråka':   'kråka',
    'svartkråka': 'kråka',
    'kråka':      'kråka',   # direktobservationer av kråka hamnar i samma bucket
}


def _gbif_rank(sci_name):
    """Slår upp taxon-rank via GBIF species/match.
    Returnerar 'sp', 'sub' eller 'hyb'.
    Anropas bara för taxa som saknas i rank-cachen."""
    if not sci_name:
        return 'sp'
    # Hybridindikator i vetenskapligt namn (× eller ' x ')
    if '×' in sci_name or ' x ' in sci_name.lower():
        return 'hyb'
    try:
        r = requests.get(
            'https://api.gbif.org/v1/species/match',
            params={'name': sci_name, 'class': 'Aves'},
            timeout=8
        )
        if r.ok:
            d = r.json()
            if d.get('matchType', 'NONE') == 'NONE':
                return 'sp'
            rank = d.get('rank', '').upper()
            # Bara SUBSPECIES räknas som underart för fåglar.
            # FORM/VARIETY används i GBIF för domesticerade former (tamduva etc.)
            # och färgmorfer – dessa ska stanna som vanliga "arter".
            if rank == 'SUBSPECIES':
                return 'sub'
            if rank in ('HYBRID', 'NOTHOSPECIES'):
                return 'hyb'
    except Exception as _e:
        print(f'  GBIF rank-lookup fel ({sci_name}): {_e}')
    return 'sp'


def _apply_rank_corrections(reporters, rank_cache):
    """Flytta taxa till rätt bucket (sp/sub/hyb) baserat på rank_cache.
    Idempotent – kan köras flera gånger utan bieffekter."""
    for rep in reporters.values():
        for tid in list(rep.get('sp_ids', set())):
            r = rank_cache.get(tid)
            if r == 'sub':
                rep['sp_ids'].discard(tid)
                rep['sub_ids'].add(tid)
                obs_data = rep['sp_obs'].pop(tid, None)
                if obs_data:
                    rep['sub_obs'].setdefault(tid, obs_data)
            elif r == 'hyb':
                rep['sp_ids'].discard(tid)
                rep['hyb_ids'].add(tid)
                obs_data = rep['sp_obs'].pop(tid, None)
                if obs_data:
                    rep['hyb_obs'].setdefault(tid, obs_data)
        # Räkna om art-antalet från den faktiska sp_ids-mängden
        rep['art'] = len(rep.get('sp_ids', set()))


def _merge_se_records(reporters, records, date_str, rank_cache=None, new_sci_names=None):
    """Mergar SOS API-records in i reporters-dicten (in-place).
    rank_cache: {taxon_id_str: 'sp'|'sub'|'hyb'} – används för klassificering.
    new_sci_names: dict som fylls med {taxon_id: sci_name} för okända taxa."""
    seen_rd = set()  # (reporter, date) – för korrekt dagar-räkning
    for rec in records:
        taxon    = rec.get('taxon')      or {}
        occ      = rec.get('occurrence') or {}
        event    = rec.get('event')      or {}
        location = rec.get('location')   or {}

        reporter = (occ.get('reportedBy') or occ.get('observer') or '').strip()
        if not reporter:
            continue

        taxon_id = str(taxon.get('id') or taxon.get('taxonId') or taxon.get('dyntaxaId') or '')
        sv_name  = (taxon.get('vernacularName') or taxon.get('commonName') or '').strip()
        sci_name = (taxon.get('scientificName') or '').strip()

        locality = (location.get('locality') or location.get('name') or '').strip()
        if not locality:
            muni = location.get('municipality') or {}
            locality = (muni.get('name') or '').strip()

        start_dt = (event.get('startDate') or '').strip()
        obs_date = start_dt[:10] if len(start_dt) >= 10 else date_str
        try:
            month_0 = int(obs_date[5:7]) - 1
        except (ValueError, IndexError):
            month_0 = int(date_str[5:7]) - 1

        # ── Namnsammanslagning (t.ex. gråkråka/svartkråka → kråka) ──────────
        # ── Hybriddetektering – kolla båda namnen och båda x-varianterna ──────
        def _looks_hybrid(s):
            s = (s or '').lower()
            return '×' in s or ' x ' in s or s.startswith('x ')

        merged_target = _SV_NAME_MERGES.get(sv_name.lower()) if sv_name else None
        if merged_target:
            # Alla taxa i merge-gruppen delar en syntetisk nyckel och visningsnamn
            eff_id    = f'svname:{merged_target}'
            eff_sv    = merged_target
            is_hybrid = False
            is_sub    = False
        elif _looks_hybrid(sv_name) or _looks_hybrid(sci_name):
            # Hybridnamn identifierat från svenska/vetenskapliga namnet
            eff_id    = taxon_id or f'svname:hyb:{sv_name.lower()}'
            eff_sv    = sv_name
            is_hybrid = True
            is_sub    = False
            # Uppdatera rank_cache direkt så GBIF-lookup hoppas över
            if rank_cache is not None and eff_id and eff_id not in rank_cache:
                rank_cache[eff_id] = 'hyb'
        else:
            eff_id   = taxon_id
            eff_sv   = sv_name
            # ── Klassificera via rank_cache; okända taxa samlas för GBIF-lookup ─
            if eff_id:
                cached_rank = (rank_cache or {}).get(eff_id, '')
                is_hybrid = cached_rank == 'hyb'
                is_sub    = cached_rank == 'sub' and not is_hybrid
                if new_sci_names is not None and not cached_rank and sci_name:
                    new_sci_names[eff_id] = sci_name
            else:
                is_hybrid = False
                is_sub    = False

        if reporter not in reporters:
            reporters[reporter] = _se_rep_empty()
        rep = reporters[reporter]
        rep['obs'] += 1
        if 0 <= month_0 < 12:
            rep['monthly'][month_0] += 1

        # Artspårning – routa till rätt bucket med effektivt ID och namn
        if eff_id:
            ind = int(occ.get('individualCount') or occ.get('quantity') or 1)
            if is_hybrid:
                bucket_ids = rep['hyb_ids']
                bucket_obs = rep['hyb_obs']
                count_art  = False
            elif is_sub:
                bucket_ids = rep['sub_ids']
                bucket_obs = rep['sub_obs']
                count_art  = False
            else:
                bucket_ids = rep['sp_ids']
                bucket_obs = rep['sp_obs']
                count_art  = True

            if eff_id not in bucket_ids:
                bucket_ids.add(eff_id)
                if count_art:
                    rep['art'] += 1
            if eff_id not in bucket_obs:
                bucket_obs[eff_id] = {'sv': eff_sv or eff_id, 'obs': 0, 'ind': 0}
            elif eff_sv and not bucket_obs[eff_id].get('sv'):
                bucket_obs[eff_id]['sv'] = eff_sv
            bucket_obs[eff_id]['obs'] += 1
            bucket_obs[eff_id]['ind'] = bucket_obs[eff_id].get('ind', 0) + ind

        # Lokalspårning
        if locality:
            rep['pl_obs'][locality] = rep['pl_obs'].get(locality, 0) + 1

        rd_key = (reporter, obs_date)
        if rd_key not in seen_rd:
            seen_rd.add(rd_key)
            rep['dagar'] += 1
        if obs_date > rep['lastObs']:
            rep['lastObs'] = obs_date


def _se_build_one_pass(year):
    """Bygger alla utestående dagar för year och avslutar.

    Körs ALLTID i en separat OS-process (via multiprocessing).
    När funktionen returnerar avslutar processen och OS återtar
    omedelbart all RAM – ingen Python-heap kvarhålls.

    Rank-cachen (taxon_ranks_se.json i R2) sparar GBIF-uppslagningar
    permanent så att varje taxon bara slås upp en gång.

    VIKTIGT – vid kodfixar som kräver omklassificering:
      Ta BARA bort taxon_ranks_se.json från R2.
      Lämna observers_se_*.json kvar – de skrivs över när bygget är klart.
      Om observer-filen raderas faller appen tillbaka på Västerbotten
      under hela byggtiden (~1,5 h), vilket ger felaktig visning.
    """
    today     = _date_type.today()
    yesterday = (today - _timedelta(days=1)).isoformat()

    # ── Ladda rank-cache från R2 (tom dict om filen saknas) ─────────────────
    rank_cache    = _r2_get('taxon_ranks_se.json') or {}
    new_sci_names = {}   # {taxon_id: sci_name} – okända taxa som hittades under bygget

    data      = _load_se_obs_r2(year)
    last      = data.get('last_date')
    reporters = data.get('reporters', {})
    data['reporters'] = reporters

    if last:
        nxt = (_dt.strptime(last, '%Y-%m-%d') + _timedelta(days=1)).strftime('%Y-%m-%d')
    else:
        nxt = f'{year}-01-01'

    if nxt > yesterday:
        # Redan à jour – uppdatera rank-klassificering och avsluta
        _apply_rank_corrections(reporters, rank_cache)
        _save_se_obs_r2(year, data)
        print(f'  SE obs {year}: subprocess – à jour (last={last})')
        return   # → process exit → OS frigör all RAM

    # ── Bygg dag för dag ────────────────────────────────────────────────────
    current = nxt
    while current <= yesterday:
        print(f'  SE obs: hämtar {current} ({len(SE_COUNTY_IDS)} län)…')
        total_recs = 0
        for cid in SE_COUNTY_IDS:
            recs = _fetch_county_obs_day(cid, current)
            if recs:
                _merge_se_records(reporters, recs, current, rank_cache, new_sci_names)
                total_recs += len(recs)
            _time.sleep(1.5)

        data['last_date'] = current
        data['year']      = year
        _save_se_obs_r2(year, data)

        print(f'  SE obs {current}: {total_recs} records, {len(reporters)} observatörer totalt')
        current = (_dt.strptime(current, '%Y-%m-%d') + _timedelta(days=1)).strftime('%Y-%m-%d')
        _time.sleep(30)

    # ── GBIF rank-lookup för nya okända taxa ─────────────────────────────────
    if new_sci_names:
        print(f'  SE obs: GBIF rank-lookup för {len(new_sci_names)} nya taxa…')
        for i, (tid, sci) in enumerate(new_sci_names.items()):
            rank_cache[tid] = _gbif_rank(sci)
            if (i + 1) % 50 == 0:
                print(f'  SE obs: GBIF {i + 1}/{len(new_sci_names)}…')
            _time.sleep(0.15)   # skonsamt mot GBIF (max ~6 req/s)
        print(f'  SE obs: GBIF rank-lookup klar '
              f'({sum(1 for v in rank_cache.values() if v=="sub")} underarter, '
              f'{sum(1 for v in rank_cache.values() if v=="hyb")} hybrider i cachen)')

        # Flytta felklassificerade taxa till rätt bucket och spara slutlig data
        _apply_rank_corrections(reporters, rank_cache)
        _save_se_obs_r2(year, data)

        # Spara uppdaterad rank-cache till R2 (permanent för framtida byggen)
        _r2_put('taxon_ranks_se.json', rank_cache)
        print(f'  SE obs: rank-cache sparad ({len(rank_cache)} taxa)')

    print(f'  SE obs {year}: subprocess ikapp!')
    # → process exit → OS återtar ALL RAM omedelbart


def _se_observers_builder():
    """Koordinatortråd – extremt lätt, håller bara ett datumvärde i minnet.

    All tung beräkning (laddning av R2, summering, sparning) sker i
    _se_build_one_pass() som körs i en separat process. När den processen
    avslutar returnerar OS all dess RAM – Flask-processen förblir liten.

    Minnesprofil:
      Under bygge (~1,5 h):  ~500 MB i subprocess, Flask oförändrad (~150 MB)
      Steady-state:          ~150 MB totalt – subprocess finns inte
    """
    import multiprocessing as _mp

    print('  SE obs: väntar 5 min innan start av bakgrundsbygge…')
    _time.sleep(300)

    # Vänta på autentisering (max 10 min)
    for _ in range(120):
        if _session['access_token'] or _session['subscription_key']:
            break
        _time.sleep(5)

    if not _session['access_token'] and not _session['subscription_key']:
        print('  SE obs: ingen autentisering – avbryter tråd')
        return

    if not _r2():
        print('  SE obs: R2 ej konfigurerat – avbryter tråd')
        return

    last_built_date = None  # hålls i minnet mellan sov-cykler – enda data koordinatorn behöver

    while True:
        today     = _date_type.today()
        year      = today.year
        yesterday = (today - _timedelta(days=1)).isoformat()

        # ── Snabbkontroll utan R2-laddning ────────────────────────────────────
        if last_built_date and last_built_date >= yesterday:
            print(f'  SE obs {year}: à jour (last={last_built_date}), sover 6h')
            _time.sleep(6 * 3600)
            continue

        # ── Spawna subprocess för bygget ──────────────────────────────────────
        print(f'  SE obs: startar subprocess för {year}…')
        try:
            p = _mp.get_context('fork').Process(
                target=_se_build_one_pass, args=(year,), daemon=False)
            p.start()
            p.join()   # koordinatortråden väntar – Flask hanterar requests som vanligt
        except Exception as exc:
            print(f'  SE obs: kunde inte starta subprocess: {exc}, väntar 30 min')
            _time.sleep(1800)
            continue

        if p.exitcode != 0:
            print(f'  SE obs: subprocess misslyckades (exit={p.exitcode}), väntar 30 min')
            _time.sleep(1800)
            continue

        # ── Subprocess klart – läs last_date från liten meta-fil ─────────────
        meta = _r2_get(f'observers_se_meta_{year}.json') or {}
        last_built_date = meta.get('last_date') or yesterday

        # Invalidera gammal API-cache – nästa request laddar ny data från R2
        with _se_obs_lock:
            _se_obs_cache.pop(year, None)

        print(f'  SE obs {year}: subprocess klar (last={last_built_date}), sover 6h')
        _time.sleep(6 * 3600)


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


# ── Fågellokaler från natursidan.se (Google My Maps KMZ) ────────────────────
_bird_lokaler_cache = None
_bird_lokaler_lock  = _threading.Lock()

# Ikonmappning: Google My Maps icon-id → platskategori
_ICON_TYPE = {
    'icon-1621': 'lokal',      # Kikare-person – fågellokal
    'icon-1760': 'fågel',      # Fågel – samlingsplats
    'icon-1644': 'område',     # Kluster – flerplatsområde
    'icon-1611': 'utsikt',     # Kikare – utsiktsplats
    'icon-1874': 'parkering',  # Parkering
    'icon-1603': 'gömsle',     # Gömsle
    'icon-1789': 'matning',    # Matningsstation
    'icon-1733': 'toalett',    # Toalett
    'icon-1783': 'karta',      # Länk till underkarta
}

# Koordinater för Västerbotten (generöst tilltagna)
_VB_LAT_MIN, _VB_LAT_MAX = 63.0, 66.5
_VB_LON_MIN, _VB_LON_MAX = 14.5, 22.0

@app.route('/api/bird_lokaler')
def bird_lokaler():
    global _bird_lokaler_cache
    with _bird_lokaler_lock:
        if _bird_lokaler_cache is not None:
            return jsonify(_bird_lokaler_cache)

    KMZ_URL = ('https://www.google.com/maps/d/kml'
               '?mid=1Gn96TCZx92PN6B5HlmZvDLJcQAwe6dQ2&forcekml=1')
    try:
        r = requests.get(KMZ_URL, timeout=30,
                         headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
    except Exception as e:
        return jsonify({'error': f'Kunde inte hämta KMZ: {e}'}), 502

    # Packa upp KMZ (ZIP) och läs KML-filen
    try:
        with _zipfile.ZipFile(_io.BytesIO(r.content)) as z:
            kml_name = next(n for n in z.namelist() if n.endswith('.kml'))
            kml_bytes = z.read(kml_name)
    except Exception:
        # Filen är kanske redan KML (ej zippat)
        kml_bytes = r.content

    root = _ET.fromstring(kml_bytes)
    KNS  = 'http://www.opengis.net/kml/2.2'

    features = []
    for pm in root.iter(f'{{{KNS}}}Placemark'):
        # Koordinater
        pt = pm.find(f'.//{{{KNS}}}Point/{{{KNS}}}coordinates')
        if pt is None or not pt.text:
            continue
        parts = pt.text.strip().split(',')
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue

        # Filtrera till Västerbotten
        if not (_VB_LAT_MIN <= lat <= _VB_LAT_MAX and
                _VB_LON_MIN <= lon <= _VB_LON_MAX):
            continue

        # Namn
        name_el = pm.find(f'{{{KNS}}}name')
        name    = (name_el.text or '').strip() if name_el is not None else ''

        # Beskrivning (strip HTML-taggar)
        desc_el = pm.find(f'{{{KNS}}}description')
        desc    = ''
        if desc_el is not None and desc_el.text:
            desc = re.sub(r'<[^>]+>', '', desc_el.text).strip()[:300]

        # Platskategori via styleUrl
        su_el  = pm.find(f'{{{KNS}}}styleUrl')
        su     = su_el.text if su_el is not None else ''
        ptype  = next((v for k, v in _ICON_TYPE.items() if k in su), 'lokal')

        # Hoppa över rena navigationsplatser
        if ptype in ('karta', 'toalett'):
            continue

        features.append({
            'lat':  round(lat, 6),
            'lon':  round(lon, 6),
            'name': name,
            'type': ptype,
            'desc': desc,
        })

    result = {'features': features, 'count': len(features)}
    with _bird_lokaler_lock:
        _bird_lokaler_cache = result
    return jsonify(result)


# ── Starta statistik-bakgrundstrådar ────────────────────────────────────────
_stats_thread = _threading.Thread(target=_stats_builder, daemon=True, name='stats-builder')
_stats_thread.start()

_se_obs_thread = _threading.Thread(target=_se_observers_builder, daemon=True, name='se-obs-builder')
_se_obs_thread.start()

# ── Startup (endast vid direktkörning lokalt) ────────────────────────────────
if __name__ == "__main__":
    import sys, io
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    port = int(_os.environ.get("PORT", 5050))
    print(f"Startar på 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)