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

from flask import Flask, request, jsonify, send_from_directory
import os as _os
from flask_cors import CORS
import requests
import re
import secrets
from urllib.parse import urljoin

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── Konstanter ─────────────────────────────────────────────────────────────
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

@app.route("/")
def index():
    return send_from_directory(_os.path.dirname(_os.path.abspath(__file__)), "faglar-vasterbotten.html")

# ── API-endpoints ───────────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    return jsonify({
        "running":   True,
        "logged_in": bool(_session["access_token"] or _session["subscription_key"]),
        "username":  _session["username"],
        "auth_mode": _session["auth_mode"],
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


# ── Startup ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, io
    # Tvinga UTF-8 på Windows-terminaler (undviker CP1252-krasch med emoji/svenska tecken)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    port = 5050
    print()
    print("*** Faagelobservationer Vaesterbotten – Lokal proxy ***")
    print(f"    Lyssnar paa: http://localhost:{port}")
    print("    Oeppna faglar-vasterbotten.html och logga in.")
    print("    Ctrl+C foer att avsluta")
    print()
    app.run(host="127.0.0.1", port=port, debug=False)
