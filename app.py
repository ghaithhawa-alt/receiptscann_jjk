from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
import anthropic
import base64
import os
import json
from datetime import datetime
from io import BytesIO

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "receiptscann-secret-2025")

def get_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return key.replace("\n","").replace("\r","").replace(" ","").strip()

# ── In-Memory Datenbank ──────────────────────────────────────────
USERS    = {}
RECEIPTS = {}
FOLDERS  = {}  # email -> {folder_id: {name, receipt_ids}}

# Monatlich: normale Limits
# Jährlich: höhere Limits + Jahrespreis (monatlicher Äquivalent x12)
PLAN_LIMITS = {
    "starter":  {"monthly": 500,  "yearly": 6599},
    "pro":      {"monthly": 1500, "yearly": 15000},
    "business": {"monthly": 5000, "yearly": 55999},
}
# Preise in Cent – yearly = monatlicher Äquivalent x12 als Einmalzahlung
PLAN_PRICES = {
    "starter":  {"monthly": 699,  "yearly": 7380},   # 6,15 x 12 = 73,80
    "pro":      {"monthly": 1299, "yearly": 13716},  # 11,43 x 12 = 137,16
    "business": {"monthly": 4899, "yearly": 51732},  # 43,11 x 12 = 431,12 (abgerundet)
}
# Monatliche Äquivalente für Anzeige
PLAN_MONTHLY_EQUIV = {
    "starter":  {"yearly": "6,15"},
    "pro":      {"yearly": "11,43"},
    "business": {"yearly": "43,11"},
}
PLAN_NAMES = {"starter": "Starter", "pro": "Pro", "business": "Business"}

# Lizenzen: superadmin kann für Partner/Kunden manuelle Lizenzen erstellen
LICENSES = {}  # license_code -> {email, receipts_limit, expires, created_by, plan_name}

# Superadmin
_sa_email = os.environ.get("SUPERADMIN_EMAIL","").strip()
_sa_pw    = os.environ.get("SUPERADMIN_PASSWORD","").strip()
if _sa_email and _sa_pw:
    USERS[_sa_email] = {"name":"Superadmin","password":_sa_pw,"phone":"",
        "plan":"business","receipts_used":0,"receipts_limit":999999,
        "role":"superadmin","created":datetime.now().isoformat()}
    RECEIPTS[_sa_email] = []
    FOLDERS[_sa_email]  = {}

# ── Seiten ───────────────────────────────────────────────────────

@app.route("/")
def landing():
    # Never auto-login from landing page
    return render_template("landing.html")

@app.route("/api/check-auth")
def check_auth():
    if "user" in session and session["user"] in USERS:
        return jsonify({"logged_in": True, "role": USERS[session["user"]].get("role","user")})
    return jsonify({"logged_in": False})

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        data  = request.get_json()
        email = data.get("email","").lower().strip()
        pw    = data.get("password","")
        user  = USERS.get(email)
        if not user:
            return jsonify({"success":False,"message":"E-Mail nicht gefunden."})
        if user.get("banned"):
            return jsonify({"success":False,"message":"Konto gesperrt."})
        if user["password"] != pw:
            return jsonify({"success":False,"message":"Passwort falsch."})
        session["user"] = email
        return jsonify({"success":True,"role":user.get("role","user")})
    return render_template("login.html")

@app.route("/register", methods=["POST"])
def register():
    data  = request.get_json()
    email = data.get("email","").lower().strip()
    pw    = data.get("password","")
    plan  = data.get("plan","")
    name  = data.get("name","").strip()
    billing = data.get("billing","monthly")

    if not email or not pw or not name:
        return jsonify({"success":False,"message":"Bitte alle Felder ausfüllen."})
    if plan not in PLAN_LIMITS:
        return jsonify({"success":False,"message":"Bitte einen Abo-Plan wählen."})
    if len(pw) < 8:
        return jsonify({"success":False,"message":"Passwort muss mindestens 8 Zeichen haben."})
    if email in USERS:
        return jsonify({"success":False,"message":"E-Mail bereits registriert. Bitte anmelden."})

    limit = PLAN_LIMITS[plan][billing] if isinstance(PLAN_LIMITS[plan], dict) else PLAN_LIMITS[plan]
    USERS[email] = {
        "name":name,"password":pw,"phone":data.get("phone",""),
        "plan":plan,"billing":billing,
        "receipts_used":0,"receipts_limit":limit,
        "role":"user","created":datetime.now().isoformat()
    }
    RECEIPTS[email] = []
    FOLDERS[email]  = {}
    session["user"] = email
    # Stripe Checkout direkt nach Registrierung
    return jsonify({"success":True,"redirect_checkout":True,"plan":plan,"billing":billing})

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    email = session["user"]
    if email not in USERS:
        session.clear()
        return redirect(url_for("login"))
    user = USERS[email]
    if user.get("role") in ("admin","superadmin"):
        return redirect(url_for("admin"))
    # Ensure storage exists
    if email not in RECEIPTS: RECEIPTS[email] = []
    if email not in FOLDERS:  FOLDERS[email]  = {}
    # Fix missing receipts_limit (license users)
    if not user.get("receipts_limit"):
        plan = user.get("plan","starter")
        lim  = PLAN_LIMITS.get(plan,{})
        user["receipts_limit"] = lim.get("monthly",500) if isinstance(lim,dict) else (lim or 500)
    return render_template("dashboard.html", user=user,
        receipts=RECEIPTS.get(email,[]),
        folders=FOLDERS.get(email,{}))

@app.route("/admin")
def admin():
    if "user" not in session:
        return redirect(url_for("login"))
    user = USERS.get(session["user"],{})
    if user.get("role") not in ("admin","superadmin"):
        return redirect(url_for("dashboard"))
    return render_template("admin.html", user=user, all_users=USERS)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))

# ── Stripe Checkout ──────────────────────────────────────────────

@app.route("/api/create-checkout", methods=["POST"])
def create_checkout():
    data    = request.get_json()
    plan    = data.get("plan","starter")
    billing = data.get("billing","monthly")
    email   = data.get("email","") or (session.get("user",""))

    stripe_key = os.environ.get("STRIPE_SECRET_KEY","").strip()
    if not stripe_key:
        return jsonify({"error":"Stripe nicht konfiguriert."}), 500

    try:
        import stripe
        stripe.api_key = stripe_key
        amount   = PLAN_PRICES.get(plan,{}).get(billing, 699)
        interval = "year" if billing == "yearly" else "month"
        base_url = request.host_url.rstrip("/")
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data":{
                    "currency":"eur",
                    "product_data":{"name":f"RECEIPTscann {PLAN_NAMES.get(plan,'Starter')} ({'Jährlich' if billing=='yearly' else 'Monatlich'})"},
                    "unit_amount":amount,
                    "recurring":{"interval":interval},
                },
                "quantity":1,
            }],
            mode="subscription",
            success_url=base_url+"/dashboard?payment=success",
            cancel_url=base_url+"/dashboard?payment=cancelled",
            customer_email=email or None,
        )
        return jsonify({"url":checkout.url})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

# ── API: Scan ────────────────────────────────────────────────────

@app.route("/api/scan", methods=["POST"])
def scan_receipt():
    if "user" not in session:
        return jsonify({"error":"Nicht eingeloggt"}), 401
    email = session["user"]
    user  = USERS.get(email)
    if not user:
        return jsonify({"error":"Benutzer nicht gefunden"}), 401
    if user["receipts_used"] >= user["receipts_limit"]:
        return jsonify({"error":"Belegtokens aufgebraucht."}), 403

    api_key = get_api_key()
    if not api_key.startswith("sk-"):
        return jsonify({"error":"API Key fehlt. Bitte in Railway Variables prüfen."}), 500

    file = request.files.get("file")
    if not file:
        return jsonify({"error":"Keine Datei hochgeladen"}), 400

    try:
        cl  = anthropic.Anthropic(api_key=api_key)
        b64 = base64.standard_b64encode(file.read()).decode("utf-8")
        mt  = file.content_type or "image/jpeg"
        msg = cl.messages.create(
            model="claude-opus-4-5", max_tokens=1024,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":mt,"data":b64}},
                {"type":"text","text":(
                    'Lies diese Rechnung aus. Antworte NUR mit reinem JSON ohne Markdown: '
                    '{"vendor":"Name","date":"TT.MM.JJJJ","net":0.0,"vat":0.0,"total":0.0,'
                    '"category":"Kraftstoff oder KFZ oder Einkauf oder Material oder Gastronomie oder Sonstiges",'
                    '"currency":"EUR"}'
                )}
            ]}]
        )
        raw = msg.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        d = json.loads(raw.strip())
        receipt = {
            "id": len(RECEIPTS.get(email,[])) + 1,
            "vendor":   d.get("vendor","Unbekannt"),
            "date":     d.get("date",""),
            "net":      d.get("net",0),
            "vat":      d.get("vat",0),
            "total":    d.get("total",0),
            "category": d.get("category","Sonstiges"),
            "currency": d.get("currency","EUR"),
            "scanned_at": datetime.now().isoformat(),
            "folder_id": None
        }
        RECEIPTS.setdefault(email,[]).append(receipt)
        user["receipts_used"] += 1
        return jsonify({"success":True,"receipt":receipt})
    except json.JSONDecodeError:
        return jsonify({"error":"KI konnte Beleg nicht lesen. Besseres Foto versuchen."}), 500
    except Exception as e:
        msg = str(e)
        print(f"[SCAN ERROR] {msg}")
        if "Illegal header" in msg or "LocalProtocol" in msg:
            return jsonify({"error":"API Key ungültig – als eine Zeile in Railway eintragen."}), 500
        return jsonify({"error":f"Fehler: {msg}"}), 500

# ── API: Belege ──────────────────────────────────────────────────

@app.route("/api/receipts", methods=["GET"])
def get_receipts():
    if "user" not in session: return jsonify({"error":"Nicht eingeloggt"}), 401
    return jsonify(RECEIPTS.get(session["user"],[]))

@app.route("/api/receipts/<int:rid>", methods=["DELETE"])
def delete_receipt(rid):
    if "user" not in session: return jsonify({"error":"Nicht eingeloggt"}), 401
    email = session["user"]
    RECEIPTS[email] = [r for r in RECEIPTS.get(email,[]) if r["id"] != rid]
    return jsonify({"success":True})

@app.route("/api/receipts/<int:rid>", methods=["PUT"])
def update_receipt(rid):
    if "user" not in session: return jsonify({"error":"Nicht eingeloggt"}), 401
    data = request.get_json()
    for r in RECEIPTS.get(session["user"],[]):
        if r["id"] == rid:
            for k,v in data.items():
                if k in r: r[k] = v
            return jsonify({"success":True,"receipt":r})
    return jsonify({"error":"Nicht gefunden"}), 404

# ── API: Ordner ──────────────────────────────────────────────────

@app.route("/api/folders", methods=["GET"])
def get_folders():
    if "user" not in session: return jsonify({"error":"Nicht eingeloggt"}), 401
    return jsonify(FOLDERS.get(session["user"],{}))

@app.route("/api/folders", methods=["POST"])
def create_folder():
    if "user" not in session: return jsonify({"error":"Nicht eingeloggt"}), 401
    email = session["user"]
    data  = request.get_json()
    name  = data.get("name","").strip()
    if not name: return jsonify({"error":"Name erforderlich"}), 400
    fid   = str(len(FOLDERS.get(email,{})) + 1)
    FOLDERS.setdefault(email,{})[fid] = {"name":name,"receipt_ids":[]}
    return jsonify({"success":True,"folder_id":fid,"name":name})

@app.route("/api/folders/<fid>", methods=["PUT"])
def rename_folder(fid):
    if "user" not in session: return jsonify({"error":"Nicht eingeloggt"}), 401
    email = session["user"]
    data  = request.get_json()
    name  = data.get("name","").strip()
    if fid in FOLDERS.get(email,{}):
        FOLDERS[email][fid]["name"] = name
        return jsonify({"success":True})
    return jsonify({"error":"Nicht gefunden"}), 404

@app.route("/api/folders/<fid>", methods=["DELETE"])
def delete_folder(fid):
    if "user" not in session: return jsonify({"error":"Nicht eingeloggt"}), 401
    email = session["user"]
    if fid in FOLDERS.get(email,{}):
        # Belege aus Ordner entfernen
        for r in RECEIPTS.get(email,[]):
            if r.get("folder_id") == fid: r["folder_id"] = None
        del FOLDERS[email][fid]
        return jsonify({"success":True})
    return jsonify({"error":"Nicht gefunden"}), 404

@app.route("/api/folders/<fid>/add-receipt", methods=["POST"])
def add_receipt_to_folder(fid):
    if "user" not in session: return jsonify({"error":"Nicht eingeloggt"}), 401
    email = session["user"]
    data  = request.get_json()
    rid   = data.get("receipt_id")
    if fid not in FOLDERS.get(email,{}):
        return jsonify({"error":"Ordner nicht gefunden"}), 404
    for r in RECEIPTS.get(email,[]):
        if r["id"] == rid:
            r["folder_id"] = fid
            if rid not in FOLDERS[email][fid]["receipt_ids"]:
                FOLDERS[email][fid]["receipt_ids"].append(rid)
            return jsonify({"success":True})
    return jsonify({"error":"Beleg nicht gefunden"}), 404

# ── API: Excel Export ────────────────────────────────────────────

@app.route("/api/export/excel")
def export_excel():
    if "user" not in session: return jsonify({"error":"Nicht eingeloggt"}), 401
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Belege"
        ws.append(["ID","Haendler","Datum","Netto (EUR)","MwSt (EUR)","Gesamt (EUR)","Kategorie","Ordner"])
        folders = FOLDERS.get(session["user"],{})
        for r in RECEIPTS.get(session["user"],[]):
            fname = ""
            if r.get("folder_id") and r["folder_id"] in folders:
                fname = folders[r["folder_id"]]["name"]
            ws.append([r.get("id"),r.get("vendor"),r.get("date"),
                r.get("net"),r.get("vat"),r.get("total"),r.get("category"),fname])
        buf = BytesIO(); wb.save(buf); buf.seek(0)
        return send_file(buf, download_name="belege.xlsx", as_attachment=True,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        return jsonify({"error":"openpyxl fehlt"}), 500

# ── API: Admin ───────────────────────────────────────────────────

def is_admin():
    return session.get("user") and USERS.get(session["user"],{}).get("role") in ("admin","superadmin")

@app.route("/api/admin/ban/<email>", methods=["POST"])
def ban_user(email):
    if not is_admin(): return jsonify({"error":"Kein Zugriff"}), 403
    if email in USERS: USERS[email]["banned"] = True
    return jsonify({"success":True})

@app.route("/api/admin/unban/<email>", methods=["POST"])
def unban_user(email):
    if not is_admin(): return jsonify({"error":"Kein Zugriff"}), 403
    if email in USERS: USERS[email].pop("banned",None)
    return jsonify({"success":True})

@app.route("/api/admin/set-role", methods=["POST"])
def set_role():
    if not is_admin(): return jsonify({"error":"Kein Zugriff"}), 403
    data  = request.get_json()
    email = data.get("email","").lower().strip()
    role  = data.get("role","user")
    if email in USERS and role in ("user","support","admin","superadmin"):
        USERS[email]["role"] = role
        return jsonify({"success":True})
    return jsonify({"error":"Nicht gefunden"}), 404

@app.route("/api/admin/licenses", methods=["GET"])
def get_licenses():
    if not is_admin(): return jsonify({"error":"Kein Zugriff"}), 403
    return jsonify(LICENSES)

@app.route("/api/admin/licenses", methods=["POST"])
def create_license():
    if USERS.get(session.get("user",""),{}).get("role") != "superadmin":
        return jsonify({"error":"Nur Superadmin"}), 403
    import secrets as sec, string
    from datetime import timedelta
    data      = request.get_json()
    email     = data.get("email","").lower().strip()
    receipts  = int(data.get("receipts_limit", 500))
    duration  = data.get("duration","monthly")
    cmonths   = int(data.get("custom_months",1))
    plan_name = data.get("plan_name","Partner-Lizenz")
    code = "LIC-"+"".join(sec.choice(string.ascii_uppercase+string.digits) for _ in range(10))
    if duration=="monthly":     exp=(datetime.now()+timedelta(days=30)).isoformat()
    elif duration=="2months":   exp=(datetime.now()+timedelta(days=60)).isoformat()
    elif duration=="3months":   exp=(datetime.now()+timedelta(days=90)).isoformat()
    elif duration=="yearly":    exp=(datetime.now()+timedelta(days=365)).isoformat()
    elif duration=="unlimited": exp="unlimited"
    else:                       exp=(datetime.now()+timedelta(days=30*cmonths)).isoformat()
    LICENSES[code]={"email":email,"receipts_limit":receipts,"expires":exp,
        "plan_name":plan_name,"created_by":session["user"],
        "created":datetime.now().isoformat(),"activated":False}
    if email in USERS:
        USERS[email]["receipts_limit"]=receipts
        USERS[email]["plan"]=plan_name
        LICENSES[code]["activated"]=True
    return jsonify({"success":True,"code":code,"license":LICENSES[code]})

@app.route("/api/admin/licenses/<code>", methods=["DELETE"])
def delete_license(code):
    if USERS.get(session.get("user",""),{}).get("role") != "superadmin":
        return jsonify({"error":"Nur Superadmin"}), 403
    if code in LICENSES:
        del LICENSES[code]
        return jsonify({"success":True})
    return jsonify({"error":"Nicht gefunden"}), 404

@app.route("/api/admin/update-user", methods=["POST"])
def admin_update_user():
    if not is_admin(): return jsonify({"error":"Kein Zugriff"}), 403
    data  = request.get_json()
    email = data.get("email","").lower().strip()
    if email not in USERS:
        return jsonify({"error":"Nutzer nicht gefunden"}), 404
    if "phone" in data: USERS[email]["phone"] = data["phone"]
    if "new_email" in data and data["new_email"]:
        new_e = data["new_email"].lower().strip()
        if new_e != email and new_e in USERS:
            return jsonify({"error":"E-Mail bereits vergeben"}), 400
        if new_e != email:
            USERS[new_e] = USERS.pop(email)
            if email in RECEIPTS: RECEIPTS[new_e] = RECEIPTS.pop(email)
            if email in FOLDERS:  FOLDERS[new_e]  = FOLDERS.pop(email)
    return jsonify({"success":True})

@app.route("/api/redeem-license", methods=["POST"])
def redeem_license():
    data  = request.get_json()
    code  = data.get("code","").strip().upper()
    email = data.get("email","").lower().strip()
    pw    = data.get("password","")
    name  = data.get("name","").strip()

    if code not in LICENSES:
        return jsonify({"success":False,"message":"Lizenz-Code ungültig oder nicht gefunden."})

    lic = LICENSES[code]

    # Check expiry
    if lic["expires"] != "unlimited":
        from datetime import datetime as dt
        try:
            if dt.fromisoformat(lic["expires"]) < dt.now():
                return jsonify({"success":False,"message":"Dieser Lizenz-Code ist abgelaufen."})
        except: pass

    # If user already exists just activate license
    if email in USERS:
        USERS[email]["receipts_limit"] = lic["receipts_limit"]
        USERS[email]["plan"]           = lic["plan_name"]
        lic["activated"] = True
        session["user"]  = email
        return jsonify({"success":True,"message":"Lizenz aktiviert!"})

    # Create new account with license
    if not email or not pw or not name:
        return jsonify({"success":False,"message":"Bitte Name, E-Mail und Passwort eingeben."})
    if len(pw) < 8:
        return jsonify({"success":False,"message":"Passwort muss mindestens 8 Zeichen haben."})

    USERS[email] = {
        "name":name,"password":pw,"phone":"",
        "plan":lic["plan_name"],"billing":"license",
        "receipts_used":0,"receipts_limit":lic["receipts_limit"],
        "role":"user","created":datetime.now().isoformat()
    }
    RECEIPTS[email] = []
    FOLDERS[email]  = {}
    lic["activated"] = True
    session["user"]  = email
    return jsonify({"success":True,"message":"Konto erstellt und Lizenz aktiviert!"})

@app.route("/api/debug/licenses")
def debug_licenses():
    # Show how many licenses exist (for debugging)
    return jsonify({"count": len(LICENSES), "codes": list(LICENSES.keys())})

@app.route("/api/debug/key")
def debug_key():
    key = get_api_key()
    raw = os.environ.get("ANTHROPIC_API_KEY","")
    return jsonify({"key_set":bool(key),"starts_with_sk":key.startswith("sk-"),
        "had_newlines":"\n" in raw or "\r" in raw,"length":len(key),"preview":key[:12]+"..." if key else "leer"})

if __name__ == "__main__":
    app.run(debug=True)
