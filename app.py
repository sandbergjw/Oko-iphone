import io
import re
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from PIL import Image

try:
    from supabase import create_client
except Exception:
    create_client = None

st.set_page_config(page_title="Øko-robot", page_icon="🥬", layout="centered")
st.title("🥬 Øko-robot")
st.caption("v1.3 · tilbudsaviser + prisrobot")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
}

# VIGTIGT:
# Disse kilder er tilbudsavis-oversigter, ikke butikkernes almindelige webshop/programmer.
FLYER_SOURCES = {
    "Netto": "https://dinetilbudsaviser.dk/netto-tilbudsavis",
    "REMA 1000": "https://dinetilbudsaviser.dk/rema1000-tilbudsavis",
    "Lidl": "https://dinetilbudsaviser.dk/lidl-tilbudsavis",
    "føtex": "https://dinetilbudsaviser.dk/fotex-tilbudsavis",
}

# 365discount har en officiel digital avis, men den er JS-tung.
# Denne tekstkilde bruges som sekundær avis-aflæsning, ikke som normal webshop.
SECONDARY_FLYER_SOURCES = {
    "365discount": "https://tilbudsaviser.com/fakta-coop365discount",
}

# Nemlig har ikke en klassisk ugeavis på samme måde som de fysiske kæder.
ONLINE_ONLY = {
    "Nemlig.com": "https://www.nemlig.com/tilbud",
}

LOCAL_HABITS = Path("habits.json")


def normalize(text):
    s = str(text).lower().strip()
    for token in [
        "økologiske", "økologisk", "økologi", "øko", "øgo",
        "änglamark", "salling øko", "365 økologiske"
    ]:
        s = s.replace(token, "")
    s = re.sub(r"[^a-z0-9æøå%\- ]+", " ", s)
    return " ".join(s.split())


def looks_organic(text):
    t = str(text).lower().strip()
    return any(x in t for x in [
        "økolog", " øko", "øgo", "änglamark", "salling øko", "365 øko"
    ]) or bool(re.match(r"^o\s+[a-zæøå]", t))


def money(text):
    text = str(text)
    patterns = [
        r"(\d{1,4})[.,](\d{2})\s*kr",
        r"(\d{1,4})\s*,\-\s*",
        r"(\d{1,4})\s*kr\b",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            if len(m.groups()) == 2 and m.group(2):
                return float(f"{m.group(1)}.{m.group(2)}")
            return float(m.group(1))
    return None


def supabase_client():
    if create_client is None:
        return None
    try:
        return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
    except Exception:
        return None


def ocr_key():
    try:
        return st.secrets["OCRSPACE_API_KEY"]
    except Exception:
        return None


def load_habits():
    """Købsvaner kommer først fra den nye purchases-tabel."""
    sb = supabase_client()
    if sb:
        try:
            rows = sb.table("purchases").select("item").execute().data or []
            result = {}
            for r in rows:
                k = normalize(r.get("item", ""))
                if k:
                    result[k] = result.get(k, 0) + 1
            if result:
                return result
        except Exception:
            pass
        # Bagudkompatibilitet med de første versioner
        try:
            rows = sb.table("receipt_items").select("item").execute().data or []
            result = {}
            for r in rows:
                k = normalize(r.get("item", ""))
                if k:
                    result[k] = result.get(k, 0) + 1
            if result:
                return result
        except Exception:
            pass

    if LOCAL_HABITS.exists():
        try:
            return json.loads(LOCAL_HABITS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_habits(items, store):
    items = [str(x).strip() for x in items if str(x).strip()]
    sb = supabase_client()
    if sb:
        now = datetime.now(timezone.utc).isoformat()
        payload = [{
            "id": str(uuid.uuid4()),
            "item": item,
            "normalized_item": normalize(item),
            "store": store or None,
            "created_at": now,
        } for item in items]
        if payload:
            sb.table("receipt_items").insert(payload).execute()
        return len(payload), "Supabase"

    h = load_habits()
    for item in items:
        k = normalize(item)
        if k:
            h[k] = h.get(k, 0) + 1
    LOCAL_HABITS.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items), "lokal fallback"


@st.cache_data(ttl=3600, show_spinner=False)
def scrape_flyer_table(store, url):
    """Læs den aktuelle tilbudsavis' produkt-tabel."""
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()

    # Pandas finder tabellen "Produkter i ... tilbudsaviser".
    tables = pd.read_html(io.StringIO(r.text))
    rows = []

    for table in tables:
        cols = [str(c).strip().lower() for c in table.columns]
        table.columns = cols

        # Vi kræver produkt/beskrivelse + pris for ikke at tage irrelevante tabeller.
        product_col = next((c for c in cols if "produkt" in c), None)
        desc_col = next((c for c in cols if "beskrivelse" in c), None)
        price_col = next((c for c in cols if "pris" in c), None)
        flyer_col = next((c for c in cols if "tilbudsavis" in c), None)
        page_col = next((c for c in cols if "side" in c), None)

        if not price_col or not (product_col or desc_col):
            continue

        for _, row in table.iterrows():
            product = str(row.get(product_col, "")) if product_col else ""
            desc = str(row.get(desc_col, "")) if desc_col else ""
            combined = f"{product} {desc}".strip()
            pr = money(row.get(price_col, ""))

            if not combined or pr is None:
                continue

            rows.append({
                "Butik": store,
                "Vare": product if product and product != "nan" else desc,
                "Beskrivelse": desc if desc != "nan" else "",
                "Pris": pr,
                "Øko": looks_organic(combined),
                "Avis": str(row.get(flyer_col, "")) if flyer_col else "",
                "Side": str(row.get(page_col, "")) if page_col else "",
                "Kilde": url,
                "Type": "Tilbudsavis",
            })

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .drop_duplicates(subset=["Butik", "Vare", "Pris", "Avis"])
        .reset_index(drop=True)
    )


@st.cache_data(ttl=3600, show_spinner=False)
def scrape_365_flyer():
    """Aflæs konkrete 'kun xx kr.' varer fra 365-avisens tekstspejl."""
    url = SECONDARY_FLYER_SOURCES["365discount"]
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = "\n".join(soup.stripped_strings)

    rows = []
    # Eksempel: "365 økologiske æbler kun 18,00 kr."
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        m = re.search(r"(.{3,140}?)\s+kun\s+(\d{1,4}[.,]\d{2})\s*kr", line, re.I)
        if not m:
            continue
        name = m.group(1).strip(" -*:")
        pr = float(m.group(2).replace(",", "."))
        rows.append({
            "Butik": "365discount",
            "Vare": name,
            "Beskrivelse": "",
            "Pris": pr,
            "Øko": looks_organic(name),
            "Avis": "Aktuel 365-tilbudsavis",
            "Side": "",
            "Kilde": url,
            "Type": "Tilbudsavis",
        })

    return pd.DataFrame(rows).drop_duplicates(subset=["Vare", "Pris"]) if rows else pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def scrape_nemlig_online():
    """Nemlig har ikke klassisk ugeavis; beholdes særskilt som online tilbud."""
    url = ONLINE_ONLY["Nemlig.com"]
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []

    for el in soup.find_all(["article", "li", "div"]):
        txt = " ".join(el.stripped_strings)
        if not (5 < len(txt) < 500):
            continue
        if not looks_organic(txt):
            continue
        pr = money(txt)
        if pr is None:
            continue
        chunks = [x.strip() for x in el.stripped_strings if len(x.strip()) > 2]
        name = chunks[0][:140] if chunks else txt[:140]
        rows.append({
            "Butik": "Nemlig.com",
            "Vare": name,
            "Beskrivelse": "",
            "Pris": pr,
            "Øko": True,
            "Avis": "",
            "Side": "",
            "Kilde": url,
            "Type": "Online tilbud",
        })

    return pd.DataFrame(rows).drop_duplicates(subset=["Vare", "Pris"]).head(100) if rows else pd.DataFrame()


def fetch_all(include_nemlig=True):
    frames = []
    status = []

    for store, url in FLYER_SOURCES.items():
        try:
            df = scrape_flyer_table(store, url)
            status.append((store, len(df), "Tilbudsavis"))
            if not df.empty:
                frames.append(df)
        except Exception:
            status.append((store, 0, "Kunne ikke læse avis"))

    try:
        d365 = scrape_365_flyer()
        status.append(("365discount", len(d365), "Tilbudsavis"))
        if not d365.empty:
            frames.append(d365)
    except Exception:
        status.append(("365discount", 0, "Kunne ikke læse avis"))

    if include_nemlig:
        try:
            nemlig = scrape_nemlig_online()
            status.append(("Nemlig.com", len(nemlig), "Online tilbud"))
            if not nemlig.empty:
                frames.append(nemlig)
        except Exception:
            status.append(("Nemlig.com", 0, "Kunne ikke læse online tilbud"))

    data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["Butik", "Vare", "Beskrivelse", "Pris", "Øko", "Avis", "Side", "Kilde", "Type"]
    )
    # Gem det robotten så i dag. Fejl i prishukommelsen må aldrig blokere avis-læsningen.
    try:
        save_offer_snapshots(data)
    except Exception:
        pass
    return data, status


ALIASES = {
    "mælk": ["mælk", "letmælk", "minimælk", "sødmælk", "skummetmælk"],
    "æg": ["æg"],
    "banan": ["banan", "bananer"],
    "bananer": ["banan", "bananer"],
    "smør": ["smør"],
    "pasta": ["pasta", "spaghetti", "penne", "fusilli"],
    "rugbrød": ["rugbrød"],
    "hakket kød": ["hakket oksekød", "hakket kød", "hakket gris", "hakket kylling"],
    "hakket oksekød": ["hakket oksekød"],
    "kylling": ["kylling", "kyllingebryst", "kyllingefilet", "kyllingeinderfilet"],
    "gulerødder": ["gulerod", "gulerødder"],
    "kartofler": ["kartoffel", "kartofler"],
    "æbler": ["æble", "æbler"],
    "yoghurt": ["yoghurt", "skyr"],
}


def match_score(query, product, description=""):
    q = normalize(query)
    p = normalize(f"{product} {description}")
    if not q or not p:
        return 0

    aliases = ALIASES.get(q, [q])
    if any(a in p for a in aliases):
        return 1.0

    qa = set(q.split())
    pa = set(p.split())
    if not qa or not pa:
        return 0
    return len(qa & pa) / len(qa | pa)


def wishlist_match(data, items, organic_only=True):
    base = data.copy()
    if organic_only:
        base = base[base["Øko"] == True]

    rows = []
    for item in items:
        candidates = []
        for _, r in base.iterrows():
            s = match_score(item, r["Vare"], r["Beskrivelse"])
            if s >= 0.34:
                candidates.append((s, r))

        if candidates:
            candidates.sort(key=lambda x: (-x[0], x[1]["Pris"]))
            _, r = candidates[0]
            rows.append({
                "Du mangler": item,
                "Butik": r["Butik"],
                "Vare": r["Vare"],
                "Pris": r["Pris"],
                "Prisgrundlag": r["Type"],
                "Senest set": "Denne uge",
                "Svar": f"Aktuelt tilbud hos {r['Butik']} til {float(r['Pris']):.2f} kr.",
            })
            continue

        # Ingen aktuel avispris: brug kun en pris vi faktisk tidligere har observeret.
        hist = historical_best_price(item, organic_only=organic_only)
        if hist:
            if hist.get("stale"):
                answer = (
                    f"Desværre ingen tilbud lige nu. Jeg har tidligere set varen hos {hist['store']} "
                    f"til {hist['price']:.2f} kr., men prisen er over 60 dage gammel, så jeg bruger den ikke "
                    f"til at udpege den billigste butik."
                )
                shown_store = "Historik: " + str(hist["store"])
            else:
                age_note = "Prisen er frisk." if hist.get("age", 999) <= 30 else f"Senest set for {hist.get('age')} dage siden."
                answer = (
                    f"Desværre ingen tilbud lige nu. Ud fra priser set de seneste 60 dage plejer den "
                    f"at være billigst hos {hist['store']} til ca. {hist['price']:.2f} kr. {age_note}"
                )
                shown_store = hist["store"]
            rows.append({
                "Du mangler": item,
                "Butik": shown_store,
                "Vare": hist["item"],
                "Pris": hist["price"],
                "Prisgrundlag": hist["label"],
                "Senest set": hist["date"],
                "Svar": answer,
            })
        else:
            rows.append({
                "Du mangler": item,
                "Butik": "Ikke fundet",
                "Vare": "",
                "Pris": None,
                "Prisgrundlag": "Ingen sikker pris endnu",
                "Senest set": "",
                "Svar": "Desværre ingen tilbud lige nu, og jeg har endnu ikke nok bonhistorik til at pege på den billigste butik.",
            })

    return pd.DataFrame(rows)


def preprocess_receipt(upload):
    img = Image.open(upload).convert("RGB")
    if img.width > 1600:
        ratio = 1600 / img.width
        img = img.resize((1600, int(img.height * ratio)))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    return img, out.getvalue()


def run_ocr(image_bytes):
    key = ocr_key()
    if not key:
        raise RuntimeError("OCRSPACE_API_KEY mangler i Streamlit Secrets.")
    r = requests.post(
        "https://api.ocr.space/parse/image",
        files={"file": ("receipt.jpg", image_bytes, "image/jpeg")},
        data={
            "apikey": key,
            "language": "auto",
            "detectOrientation": "true",
            "scale": "true",
            "isTable": "true",
            "OCREngine": "2",
        },
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("IsErroredOnProcessing"):
        raise RuntimeError(str(payload.get("ErrorMessage") or "OCR-fejl"))
    text = "\n".join(x.get("ParsedText", "") for x in payload.get("ParsedResults", []))
    if not text.strip():
        raise RuntimeError("Ingen tekst fundet.")
    return text


def parse_receipt(text):
    rows = []
    skip = ["total", "visa", "moms", "dankort", "kontant", "betaling", "subtotal"]
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 2 or any(x in line.lower() for x in skip):
            continue
        m = re.search(r"(-?\d{1,4}[,.]\d{2})\s*-?\s*$", line)
        pr = float(m.group(1).replace(",", ".")) if m else None
        item = line[:m.start()].strip(" .:-") if m else line
        if re.search(r"[A-Za-zÆØÅæøå]", item):
            rows.append({"Vare": item[:120], "Pris": pr})
    return pd.DataFrame(rows)



# ---------- v1.3: fælles prishukommelse ----------
def quantity_info(text):
    """Find pakkestørrelse og omregningsgrundlag til kg/l/stk når muligt."""
    t = str(text).lower().replace(",", ".")
    multi = re.search(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(kg|g|l|ml|cl|stk)\b", t)
    if multi:
        n = float(multi.group(1)); val = float(multi.group(2)); unit = multi.group(3)
        amount = n * val
        if unit == "g": amount /= 1000; base = "kg"
        elif unit == "ml": amount /= 1000; base = "l"
        elif unit == "cl": amount /= 100; base = "l"
        else: base = unit
        return multi.group(0), amount, base
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g|l|ml|cl|stk)\b", t)
    if not m:
        return "", None, None
    val = float(m.group(1)); unit = m.group(2)
    if unit == "g": return m.group(0), val/1000, "kg"
    if unit == "ml": return m.group(0), val/1000, "l"
    if unit == "cl": return m.group(0), val/100, "l"
    return m.group(0), val, unit


def unit_price(price, text):
    _, amount, base = quantity_info(text)
    if amount and amount > 0:
        return round(float(price) / amount, 2), base
    return None, None


def save_price_observations(payload):
    client = supabase_client()
    if not client or not payload:
        return 0
    # SQL-filen opretter en unik indeks, så samme pris ikke gemmes igen og igen samme dag.
    client.table("price_observations").upsert(
        payload, on_conflict="store,normalized_item,observed_date,price_type,price"
    ).execute()
    return len(payload)


def save_offer_snapshots(df):
    if df is None or df.empty:
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for _, r in df.iterrows():
        name = str(r.get("Vare", "")).strip()
        try:
            price = float(r.get("Pris"))
        except Exception:
            continue
        if not name or price <= 0:
            continue
        desc = str(r.get("Beskrivelse", ""))
        qtxt, _, _ = quantity_info(f"{name} {desc}")
        upr, uunit = unit_price(price, f"{name} {desc}")
        payload.append({
            "id": str(uuid.uuid4()),
            "item": name,
            "normalized_item": normalize(name),
            "store": str(r.get("Butik", "")) or None,
            "price": price,
            "price_type": "offer",
            "organic": bool(r.get("Øko", False)),
            "quantity_text": qtxt or None,
            "unit_price": upr,
            "unit_name": uunit,
            "observed_date": today,
            "source_url": str(r.get("Kilde", "")) or None,
            "created_at": now,
        })
    return save_price_observations(payload)


def save_receipt_price_observations(df, store):
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for _, row in df.iterrows():
        name = str(row.get("Vare", "")).strip()
        if not name:
            continue
        def num(v):
            try:
                if pd.isna(v) or v == "": return None
                return float(v)
            except Exception:
                return None
        normal = num(row.get("Normalpris"))
        paid = num(row.get("Betalt pris"))
        discount = abs(num(row.get("Rabat")) or 0.0)
        qtxt = str(row.get("Mængde", "")).strip()
        if not qtxt:
            qtxt, _, _ = quantity_info(name)
        for ptype, price in (("receipt_normal", normal), ("receipt_paid", paid)):
            if price is None or price <= 0:
                continue
            # Hvis der ikke var rabat, er bonens pris vores bedste observation af en almindelig hyldepris.
            if ptype == "receipt_normal" and discount == 0:
                ptype = "regular_observed"
            upr, uunit = unit_price(price, qtxt or name)
            payload.append({
                "id": str(uuid.uuid4()),
                "item": name,
                "normalized_item": normalize(name),
                "store": store or None,
                "price": price,
                "price_type": ptype,
                "organic": looks_organic(name),
                "quantity_text": qtxt or None,
                "unit_price": upr,
                "unit_name": uunit,
                "observed_date": today,
                "source_url": None,
                "created_at": now,
            })
    return save_price_observations(payload)


def historical_best_price(query, organic_only=True):
    client = supabase_client()
    if not client:
        return None

    MAX_CURRENT_AGE_DAYS = 60
    FRESH_AGE_DAYS = 30
    today = date.today()
    candidates = []

    def age_days(value):
        if not value:
            return 99999
        try:
            d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
            return (today - d).days
        except Exception:
            return 99999

    try:
        rows = client.table("price_observations").select(
            "item,normalized_item,store,price,price_type,organic,observed_date,unit_price,unit_name"
        ).order("observed_date", desc=True).limit(1500).execute().data or []
    except Exception:
        rows = []

    for r in rows:
        if r.get("price_type") == "offer":
            continue
        if organic_only and r.get("organic") is not True and not looks_organic(r.get("item", "")):
            continue
        if match_score(query, r.get("item", "")) < 0.55:
            continue
        try:
            price = float(r.get("price"))
        except Exception:
            continue
        if price <= 0 or not r.get("store"):
            continue
        candidates.append({
            "item": r.get("item", ""),
            "store": r.get("store"),
            "price": price,
            "date": r.get("observed_date", ""),
            "age": age_days(r.get("observed_date")),
        })

    try:
        purchases = client.table("purchases").select(
            "item,store,normal_price,discount,paid_price,purchased_at"
        ).order("purchased_at", desc=True).limit(1500).execute().data or []
    except Exception:
        purchases = []

    for r in purchases:
        item_name = r.get("item", "")
        if match_score(query, item_name) < 0.55:
            continue
        if organic_only and not looks_organic(item_name):
            continue
        raw_price = r.get("normal_price")
        if raw_price is None:
            raw_price = r.get("paid_price")
        try:
            price = float(raw_price)
        except Exception:
            continue
        if price <= 0 or not r.get("store"):
            continue
        candidates.append({
            "item": item_name,
            "store": r.get("store"),
            "price": price,
            "date": r.get("purchased_at", ""),
            "age": age_days(r.get("purchased_at")),
        })

    if not candidates:
        return None

    usable = [r for r in candidates if r["age"] <= MAX_CURRENT_AGE_DAYS]
    if usable:
        by_store = {}
        for r in usable:
            by_store.setdefault(r["store"], []).append(r)

        ranked = []
        for store, rs in by_store.items():
            vals = sorted(float(x["price"]) for x in rs)
            n = len(vals)
            median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
            latest = min(rs, key=lambda x: x["age"])
            ranked.append((median, len(rs), latest))

        ranked.sort(key=lambda x: (x[0], -x[1]))
        median, observations, latest = ranked[0]
        freshness = "Frisk bonpris" if latest["age"] <= FRESH_AGE_DAYS else "Ældre bonpris"
        return {
            "store": latest["store"],
            "item": latest["item"],
            "price": round(median, 2),
            "label": f"{freshness} ({observations} observationer)",
            "date": latest.get("date", ""),
            "observations": observations,
            "stale": False,
            "age": latest["age"],
        }

    # Alt er ældre end 60 dage: vis kun som historisk information,
    # og brug det ikke til at kalde en butik aktuelt billigst.
    latest_old = min(candidates, key=lambda x: x["age"])
    return {
        "store": latest_old["store"],
        "item": latest_old["item"],
        "price": round(float(latest_old["price"]), 2),
        "label": "Historisk pris – over 60 dage gammel",
        "date": latest_old.get("date", ""),
        "observations": 1,
        "stale": True,
        "age": latest_old["age"],
    }


# ---------- v1.2: intelligent bon + prishukommelse ----------
def load_purchase_history():
    client = supabase_client()
    if client:
        try:
            return client.table("purchases").select("*").order("purchased_at", desc=True).execute().data or []
        except Exception:
            return []
    return []

def save_purchase_history(df, store):
    client = supabase_client()
    if not client:
        raise RuntimeError("Supabase er ikke forbundet.")
    payload = []
    for _, row in df.iterrows():
        name = str(row.get("Vare", "")).strip()
        if not name:
            continue
        def num(v):
            try:
                if pd.isna(v) or v == "":
                    return None
                return float(v)
            except Exception:
                return None
        normal = num(row.get("Normalpris"))
        discount = abs(num(row.get("Rabat")) or 0.0)
        paid = num(row.get("Betalt pris"))
        if paid is None and normal is not None:
            paid = round(max(0, normal - discount), 2)
        payload.append({
            "id": str(uuid.uuid4()),
            "item": name,
            "normalized_item": normalize(name),
            "store": store or None,
            "normal_price": normal,
            "discount": discount,
            "paid_price": paid,
            "quantity_text": str(row.get("Mængde", "")).strip() or None,
            "purchased_at": datetime.now(timezone.utc).date().isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    if payload:
        client.table("purchases").insert(payload).execute()
        # Samme bon fodrer også den fælles prisdatabase.
        save_receipt_price_observations(df, store)
    return len(payload)

def receipt_amount(line):
    m = re.search(r"(-?\s*\d{1,4}[,.]\d{2})\s*(?:kr)?\s*[A-Z]?\s*$", line)
    if not m:
        return None, None
    try:
        return float(m.group(1).replace(" ", "").replace(",", ".")), m
    except Exception:
        return None, None

def parse_receipt_smart(text):
    rows = []
    discount_words = ("rabat", "discount", "kupon", "bonus")
    ignore_words = ("total", "visa", "moms", "dankort", "kontant", "betaling",
                    "subtotal", "at betale", "returbeløb")
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 2:
            continue
        low = line.lower()
        if any(w in low for w in ignore_words):
            continue
        amount, match = receipt_amount(line)
        is_discount = any(w in low for w in discount_words) or (amount is not None and amount < 0)
        if is_discount:
            if amount is not None and rows:
                disc = abs(amount)
                rows[-1]["Rabat"] = round(rows[-1]["Rabat"] + disc, 2)
                rows[-1]["Betalt pris"] = round(max(0, rows[-1]["Normalpris"] - rows[-1]["Rabat"]), 2)
            continue
        if amount is None or not re.search(r"[A-Za-zÆØÅæøå]", line):
            continue
        name = line[:match.start()].strip(" .:-*")
        name = re.sub(r"\b(x\d+|\d+\s*x)\b", " ", name, flags=re.I)
        name = re.sub(r"\s+", " ", name).strip()
        if len(name) < 2:
            continue
        qtxt, _, _ = quantity_info(name)
        rows.append({
            "Vare": name[:120],
            "Normalpris": round(amount, 2),
            "Rabat": 0.0,
            "Betalt pris": round(amount, 2),
            "Mængde": qtxt,
        })
    return pd.DataFrame(rows)

def price_stats(query):
    history = load_purchase_history()
    prices = []
    for row in history:
        if match_score(query, row.get("item", "")) >= 0.55:
            try:
                p = float(row.get("paid_price"))
                if p > 0:
                    prices.append(p)
            except Exception:
                pass
    if not prices:
        return None
    return {"n": len(prices), "avg": sum(prices)/len(prices), "min": min(prices), "max": max(prices)}

def price_verdict(query, offer):
    stats = price_stats(query)
    if not stats:
        return "Ny vare – ingen prishistorik"
    pct = (stats["avg"] - float(offer)) / stats["avg"] * 100
    if pct >= 15:
        return f"🔥 {pct:.0f}% billigere end du plejer"
    if pct >= 5:
        return f"👍 {pct:.0f}% billigere end du plejer"
    if pct > -5:
        return "≈ Omkring din normale pris"
    return "⚠️ Dyrere end du plejer"


if "flyer_data" not in st.session_state:
    st.session_state["flyer_data"] = pd.DataFrame()
if "source_status" not in st.session_state:
    st.session_state["source_status"] = []

tabs = st.tabs(["🏠", "📝 Jeg mangler", "📰 Aviser", "🎯 Til mig", "📸 Bon", "🧠 Vaner", "⚙️"])

with tabs[0]:
    st.success("Nu læser robotten tilbudsaviser og bygger sin egen prishukommelse")
    st.write(
        "Netto, REMA 1000, Lidl, føtex og 365discount behandles som **tilbudsaviser**. "
        "Nemlig.com står separat som **online tilbud**, fordi Nemlig ikke har en klassisk ugeavis. "
        "Hver gang aviserne læses, gemmes de priser robotten faktisk har set."
    )
    a, b = st.columns(2)
    a.metric("Tilbudsaviser", 5)
    b.metric("Online butik", 1)

    include_nemlig = st.toggle("Tag Nemlig.com online-tilbud med", value=True)
    if st.button("🔄 Læs ugens tilbudsaviser", type="primary"):
        with st.spinner("Gennemgår tilbudsaviserne…"):
            data, status = fetch_all(include_nemlig=include_nemlig)
        st.session_state["flyer_data"] = data
        st.session_state["source_status"] = status
        st.success(f"{len(data)} avis-/tilbudsvarer fundet.")

with tabs[1]:
    st.subheader("Hvad vil du gerne købe?")
    txt = st.text_area(
        "Én varetype pr. linje",
        "mælk\næg\npasta\nhakket kød\nbananer",
        height=190,
    )
    organic_only = st.toggle("Kun økologiske tilbud", value=True)
    include_nemlig_w = st.toggle("Tag Nemlig.com med", value=True, key="nemlig_w")

    if st.button("Find bedste pris", type="primary"):
        data = st.session_state["flyer_data"]
        if data.empty:
            with st.spinner("Læser tilbudsaviserne først…"):
                data, status = fetch_all(include_nemlig=include_nemlig_w)
            st.session_state["flyer_data"] = data
            st.session_state["source_status"] = status

        result = wishlist_match(
            data,
            [x.strip() for x in txt.splitlines() if x.strip()],
            organic_only=organic_only,
        )
        st.dataframe(result, hide_index=True, use_container_width=True)
        for answer in result.get("Svar", pd.Series(dtype=str)).dropna().tolist():
            st.write(f"• {answer}")
        st.caption("Hvis der ikke er et aktuelt tilbud, bruger robotten dine gemte bonpriser og andre priser, den faktisk har observeret – aldrig en gættet normalpris.")
        found = result["Pris"].dropna()
        if not found.empty:
            st.metric("Samlet pris for fundne varer", f"{found.sum():.2f} kr.")

with tabs[2]:
    st.subheader("Ugens tilbudsaviser")
    if st.button("Opdater aviser"):
        with st.spinner("Læser aviser…"):
            data, status = fetch_all(include_nemlig=True)
        st.session_state["flyer_data"] = data
        st.session_state["source_status"] = status

    data = st.session_state["flyer_data"]
    if data.empty:
        st.info("Tryk 'Opdater aviser' for at hente indhold.")
    else:
        stores = st.multiselect(
            "Butikker",
            ["Netto", "REMA 1000", "365discount", "Lidl", "føtex", "Nemlig.com"],
            default=["Netto", "REMA 1000", "365discount", "Lidl", "føtex"],
        )
        only_organic = st.toggle("Vis kun økologisk", value=True, key="only_org_flyer")
        shown = data[data["Butik"].isin(stores)]
        if only_organic:
            shown = shown[shown["Øko"] == True]

        columns = ["Butik", "Vare", "Beskrivelse", "Pris", "Avis", "Side", "Type", "Kilde"]
        st.dataframe(
            shown[columns].sort_values(["Butik", "Pris"]),
            hide_index=True,
            use_container_width=True,
            column_config={"Kilde": st.column_config.LinkColumn("Kilde", display_text="Åbn")},
        )

with tabs[3]:
    st.subheader("Tilbud på dine faste varer")
    h = load_habits()
    if not h:
        st.info("Scan nogle boner først.")
    else:
        data = st.session_state["flyer_data"]
        if data.empty:
            st.info("Læs tilbudsaviserne under 📰 først.")
        else:
            rows = []
            organic = data[data["Øko"] == True]
            for item, count in sorted(h.items(), key=lambda x: x[1], reverse=True):
                candidates = []
                for _, r in organic.iterrows():
                    s = match_score(item, r["Vare"], r["Beskrivelse"])
                    if s >= 0.4:
                        candidates.append((s, r))
                if candidates:
                    candidates.sort(key=lambda x: (-x[0], x[1]["Pris"]))
                    _, r = candidates[0]
                    rows.append([item, count, r["Butik"], r["Vare"], r["Pris"], r["Type"]])
            if rows:
                st.dataframe(
                    pd.DataFrame(rows, columns=["Din vare", "Køb", "Butik", "Tilbud", "Pris", "Kilde-type"]),
                    hide_index=True,
                    use_container_width=True,
                )
            else:
                st.info("Ingen af dine faste varer matchede ugens øko-tilbud.")

with tabs[4]:
    st.subheader("Scan bon")

    # Kameraet er slukket som standard. Brugeren tænder det aktivt,
    # så iPhone ikke åbner kameravisningen bare ved at gå ind på Bon.
    if "receipt_camera_on" not in st.session_state:
        st.session_state["receipt_camera_on"] = False

    c1, c2 = st.columns(2)
    with c1:
        if not st.session_state["receipt_camera_on"]:
            if st.button("📷 Tænd kamera", use_container_width=True):
                st.session_state["receipt_camera_on"] = True
                st.rerun()
        else:
            if st.button("⏹️ Sluk kamera", use_container_width=True):
                st.session_state["receipt_camera_on"] = False
                st.rerun()

    camera = None
    if st.session_state["receipt_camera_on"]:
        camera = st.camera_input("📷 Tag billede af bon")

    upload = st.file_uploader("🖼️ Vælg bon fra Fotos", type=["jpg", "jpeg", "png", "webp"])
    source = camera or upload

    if source:
        img, image_bytes = preprocess_receipt(source)
        st.image(img, caption="Valgt bon", use_container_width=True)
        if ocr_key() and st.button("✨ Læs bon automatisk", type="primary"):
            try:
                with st.spinner("Læser bonen…"):
                    st.session_state["receipt_text"] = run_ocr(image_bytes)
                st.success("Bonen er aflæst.")
            except Exception as e:
                st.error(str(e))

    text = st.text_area(
        "Bontekst – ret fejl før lagring",
        value=st.session_state.get("receipt_text", ""),
        height=220,
    )
    store = st.selectbox(
        "Bonens butik",
        ["", "Netto", "REMA 1000", "365discount", "Lidl", "føtex", "Nemlig.com"],
    )
    if text:
        parsed = parse_receipt_smart(text)
        st.markdown("### Varer fundet på bonen")
        if not parsed.empty:
            edited = st.data_editor(parsed, num_rows="dynamic", hide_index=True, use_container_width=True)
            if st.button("💾 Gem køb, priser og rabatter", type="primary", use_container_width=True):
                try:
                    n = save_purchase_history(edited, store)
                    st.success(f"{n} køb gemt i Supabase. Robotten husker nu de faktiske priser.")
                except Exception as e:
                    st.error(f"Kunne ikke gemme: {e}")
        else:
            st.warning("Jeg kunne ikke finde sikre varelinjer endnu. Ret eventuelt bonteksten ovenfor og prøv igen.")

with tabs[5]:
    st.subheader("Det robotten har lært")
    history = load_purchase_history()
    if history:
        st.markdown("### 💰 Dine bonpriser")
        pdf = pd.DataFrame(history)
        if not pdf.empty:
            show = pd.DataFrame({
                "Dato": pdf.get("purchased_at"),
                "Butik": pdf.get("store"),
                "Vare": pdf.get("item"),
                "Normalpris": pdf.get("normal_price"),
                "Rabat": pdf.get("discount"),
                "Betalt": pdf.get("paid_price"),
            })
            st.dataframe(show, hide_index=True, use_container_width=True)
        st.markdown("### 🧠 Købsvaner")
    h = load_habits()
    if not h:
        st.info("Ingen gemte varer endnu.")
    else:
        rows = []
        for item, count in sorted(h.items(), key=lambda x: x[1], reverse=True):
            level = "Fast vane" if count >= 4 else ("Mulig vane" if count >= 2 else "Engangskøb")
            rows.append([item, count, level])
        st.dataframe(
            pd.DataFrame(rows, columns=["Vare", "Køb", "Vane"]),
            hide_index=True,
            use_container_width=True,
        )

with tabs[6]:
    st.subheader("Datakilder")
    status = st.session_state["source_status"]
    if status:
        for store, count, typ in status:
            icon = "🟢" if count else "🟡"
            st.write(f"{icon} **{store}** · {count} varer · {typ}")
    else:
        st.write("Netto · tilbudsavis")
        st.write("REMA 1000 · tilbudsavis")
        st.write("365discount · tilbudsavis")
        st.write("Lidl · tilbudsavis")
        st.write("føtex · tilbudsavis")
        st.write("Nemlig.com · online tilbud (ingen klassisk ugeavis)")

    st.divider()
    st.write("**Permanent lagring:**", "✅ Supabase" if supabase_client() else "⚠️ lokal fallback")
    st.write("**Bon-OCR:**", "✅ aktiv" if ocr_key() else "⚠️ ikke aktiveret")
    st.caption("Netto+ og andre medlemsprogrammer er ikke datakilden. Gamle tilbud gemmes som tilbudshistorik og bruges aldrig som normalpris.")

st.caption("Øko-robot v1.3.4 · flyer-first + prisrobot")
