import io
import re
import json
import uuid
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from PIL import Image

try:
    from supabase import create_client
except Exception:
    create_client = None

APP_VERSION = "2.1.7"
APP_VERSION_TEXT = "flere avisfund + sikker prisrobot"

st.set_page_config(page_title="Øko-robot", page_icon="🥬", layout="centered")
st.title("🥬 Øko-robot")
st.caption(f"v{APP_VERSION} · {APP_VERSION_TEXT}")

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
NEMLIG_CATALOG_PAGES = [
    "https://www.nemlig.com/dagligvarer/nye-varer-inspiration/oekologi",
    "https://www.nemlig.com/dagligvarer/mejeri",
    "https://www.nemlig.com/dagligvarer/frugt-groent",
    "https://www.nemlig.com/dagligvarer/koed-fisk",
    "https://www.nemlig.com/dagligvarer/broed-bager",
]

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


def product_family(text, rules=None):
    """Saml bonnavne til en menneskelig grundvare. Manuelle regler vinder over automatik."""
    raw_text = str(text)
    raw = raw_text.lower()
    n = normalize(text)

    # Manuelle rettelser fra Vaner har første prioritet.
    if rules is None:
        try:
            rules = load_habit_rules()
        except Exception:
            rules = {}
    rule = rules.get(n) if isinstance(rules, dict) else None
    if rule:
        if rule.get("hidden"):
            return None
        target = str(rule.get("target_name") or "").strip()
        if target:
            return target

    families = [
        ("kærnemælk", ("kærnemælk", "kaernemaelk", "kærnem", "kaernem")),
        ("piskefløde", ("piskefløde", "piskeflø", "piskefloede", "piskeflo")),
        ("græsk yoghurt", (
            "græsk yoghurt", "graesk yoghurt", "græsk yogurt", "graesk yogurt",
            "græsk yog", "graesk yog", "græsk yo", "graesk yo",
            "græsk", "graesk", "grsk yoghurt", "grsk yog", "grsk"
        )),
        ("minimælk", ("minimælk", "minimaelk")),
        ("letmælk", ("letmælk", "letmaelk")),
        ("sødmælk", ("sødmælk", "soedmaelk")),
        ("æbler", (
            " æble ", " æbler ", "æble 4", "æbler 4",
            "pink lady", "royal gala", "gala æble", "gala æbler",
            "golden delicious", "granny smith", "jazz æble", "jazz æbler"
        )),
        ("smørbar", ("smørbar", "smørbart", "blandingsprodukt", "smørblanding")),
        ("smør", (" smør ", "smør 200", "smør 250", "smør 500", "butter")),
        ("leverpostej", ("leverpostej", "leverpost")),
        ("æg", (" æg ", "æg ", " æg", "aeg")),
    ]
    padded = f" {n} "
    for family, variants in families:
        if any(v in raw or v in n or v in padded for v in variants):
            return family

    # Fjern typiske støjord fra boner: mærke, fedtprocent, størrelse og tal.
    x = n
    x = re.sub(r"\b(arla|engvang|løgismose|salling|365|lidl|rema|netto)\b", " ", x)
    x = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:%|kg|g|l|ml|cl|stk)?\b", " ", x)
    x = re.sub(r"\b(?:m|l|xl)\b", " ", x)
    x = " ".join(x.split())
    return x or n


def habit_summary(history):
    """Én række pr. grundvare/butik med nyttig pris- og købshistorik."""
    if not history:
        return pd.DataFrame()
    df = pd.DataFrame(history)
    if df.empty or "item" not in df.columns:
        return pd.DataFrame()

    # Ukendt butik er ikke gyldig data og vises derfor aldrig som en vane.
    if "store" in df.columns:
        store_clean = df["store"].fillna("").astype(str).str.strip().str.lower()
        df = df[~store_clean.isin(["", "ukendt", "unknown", "none"])]
    if df.empty:
        return pd.DataFrame()

    rules = load_habit_rules()
    df["Grundvare"] = df["item"].map(lambda x: product_family(x, rules=rules))
    df = df[df["Grundvare"].notna() & (df["Grundvare"].astype(str).str.strip() != "")]
    if df.empty:
        return pd.DataFrame()

    df["Butik"] = df.get("store", pd.Series([""] * len(df))).fillna("")
    df["Pris"] = pd.to_numeric(df.get("paid_price"), errors="coerce")
    normal = pd.to_numeric(df.get("normal_price"), errors="coerce")
    df["Pris"] = df["Pris"].fillna(normal)
    df["Dato"] = pd.to_datetime(df.get("purchased_at"), errors="coerce")

    rows = []
    for (family, store), g in df.groupby(["Grundvare", "Butik"], dropna=False):
        prices = g["Pris"].dropna()
        dates = g["Dato"].dropna()
        count = len(g)
        level = "Fast vane" if count >= 4 else ("Mulig vane" if count >= 2 else "Engangskøb")
        rows.append({
            "Vare": family,
            "Butik": store or "Ukendt",
            "Køb": count,
            "Typisk pris": round(float(prices.median()), 2) if not prices.empty else None,
            "Laveste": round(float(prices.min()), 2) if not prices.empty else None,
            "Seneste pris": round(float(g.sort_values("Dato")["Pris"].dropna().iloc[-1]), 2) if not prices.empty else None,
            "Senest købt": dates.max().strftime("%d/%m/%Y") if not dates.empty else "",
            "Vane": level,
        })
    return pd.DataFrame(rows).sort_values(["Køb", "Vare"], ascending=[False, True])


def looks_organic(text):
    t = str(text).lower().strip()
    # Accepter også bon/OCR-varianter som "Øko. Kærnemælk",
    # "ØKO ARLA..." og "O ARLA..." hvor Ø er læst som O.
    return (
        "økolog" in t
        or bool(re.search(r"(^|\s)(?:øko|øgo|ogo|0go)(?:\s|[.,;:()/-]|$)", t))
        or "änglamark" in t
        or "salling øko" in t
        or "365 øko" in t
        or bool(re.match(r"^o(?:\s|[.,;:()/-])", t))
    )


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


@st.cache_data(ttl=30, show_spinner=False)
def load_habit_rules():
    """Manuelle sammenkædninger, omdøbninger og skjulte varer."""
    sb = supabase_client()
    if not sb:
        return {}
    try:
        rows = sb.table("habit_rules").select("*").execute().data or []
        return {
            str(r.get("source_normalized") or ""): {
                "source_item": r.get("source_item"),
                "target_name": r.get("target_name"),
                "hidden": bool(r.get("hidden")),
            }
            for r in rows if r.get("source_normalized")
        }
    except Exception:
        return {}


def save_habit_rule(source_item, target_name=None, hidden=False):
    sb = supabase_client()
    if not sb:
        raise RuntimeError("Supabase er ikke forbundet.")
    source_item = str(source_item).strip()
    if not source_item:
        raise ValueError("Vælg en vare.")
    payload = {
        "source_normalized": normalize(source_item),
        "source_item": source_item,
        "target_name": (str(target_name).strip() or None) if target_name is not None else None,
        "hidden": bool(hidden),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    sb.table("habit_rules").upsert(payload, on_conflict="source_normalized").execute()
    load_habit_rules.clear()


def delete_habit_rule(source_item):
    sb = supabase_client()
    if not sb:
        raise RuntimeError("Supabase er ikke forbundet.")
    sb.table("habit_rules").delete().eq("source_normalized", normalize(source_item)).execute()
    load_habit_rules.clear()


def permanently_delete_raw_item(source_item):
    """Slet alle køb med præcis dette rå bonnavn samt dets bon-prisobservationer."""
    sb = supabase_client()
    if not sb:
        raise RuntimeError("Supabase er ikke forbundet.")
    norm = normalize(source_item)
    sb.table("purchases").delete().eq("normalized_item", norm).execute()
    # Slet kun bonbaserede priser – aldrig tilbudsavis-observationer.
    for typ in ("receipt_normal", "receipt_paid", "regular_observed"):
        sb.table("price_observations").delete().eq("normalized_item", norm).eq("price_type", typ).execute()
    try:
        sb.table("habit_rules").delete().eq("source_normalized", norm).execute()
    except Exception:
        pass
    load_habit_rules.clear()


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



def _flyer_image_url(img, base_url):
    """Find det bedst mulige billede fra lazy-load/srcset."""
    candidates = []
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        val = img.get(attr)
        if val:
            candidates.append(str(val).strip())
    for attr in ("data-srcset", "srcset"):
        val = img.get(attr)
        if val:
            parts = [x.strip().split()[0] for x in str(val).split(",") if x.strip()]
            candidates.extend(reversed(parts))
    for val in candidates:
        if val and not val.startswith("data:"):
            return urljoin(base_url, val)
    return ""


def _ocr_image_url(image_url):
    """OCR.Space på én avis-side. Bruger samme nøgle som bon-scanneren."""
    key = ocr_key()
    if not key or not image_url:
        return ""
    r = requests.post(
        "https://api.ocr.space/parse/image",
        data={
            "apikey": key,
            "url": image_url,
            "language": "auto",
            "detectOrientation": "true",
            "scale": "true",
            "isTable": "false",
            "OCREngine": "2",
        },
        timeout=70,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("IsErroredOnProcessing"):
        return ""
    return "\n".join(
        x.get("ParsedText", "") for x in payload.get("ParsedResults", [])
    ).strip()



def _ocr_image_bytes(image_bytes):
    """OCR.Space på et udsnit af en avisside."""
    key = ocr_key()
    if not key or not image_bytes:
        return ""
    r = requests.post(
        "https://api.ocr.space/parse/image",
        files={"file": ("flyer_tile.jpg", image_bytes, "image/jpeg")},
        data={
            "apikey": key,
            "language": "auto",
            "detectOrientation": "true",
            "scale": "true",
            "isTable": "false",
            "OCREngine": "2",
        },
        timeout=70,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("IsErroredOnProcessing"):
        return ""
    return "\n".join(
        x.get("ParsedText", "") for x in payload.get("ParsedResults", [])
    ).strip()


def _organic_marker_in_text(text):
    low = str(text or "").lower()
    return (
        looks_organic(text)
        or "økolog" in low
        or "okolog" in low
        or "ekolog" in low
        or bool(re.search(r"(^|\s)(?:øgo|ogo|0go)(?:\s|[.,;:()/-]|$)", low))
    )


def _download_flyer_image(image_url):
    r = requests.get(image_url, headers=HEADERS, timeout=35)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _flyer_tiles(img, cols=3, rows=3, overlap=0.05):
    """Små overlappende felter. Mindsker risikoen for at blande nabovarer."""
    w, h = img.size
    tile_w = w / cols
    tile_h = h / rows
    ox = int(w * overlap)
    oy = int(h * overlap)

    out = []
    for row in range(rows):
        for col in range(cols):
            x0 = max(0, int(col * tile_w) - ox)
            y0 = max(0, int(row * tile_h) - oy)
            x1 = min(w, int((col + 1) * tile_w) + ox)
            y1 = min(h, int((row + 1) * tile_h) + oy)
            tile = img.crop((x0, y0, x1, y1))
            buf = io.BytesIO()
            tile.save(buf, "JPEG", quality=90, optimize=True)
            out.append(buf.getvalue())
    return out


OCR_WORD_FIXES = {
    "ogo": "ØGO",
    "0go": "ØGO",
    "øgo": "ØGO",
    "hamburgerryo": "hamburgerryg",
    "hamburgerry0": "hamburgerryg",
    "hamburgerrvg": "hamburgerryg",
    "okologisk": "økologisk",
    "økologlsk": "økologisk",
    "okologiske": "økologiske",
    "solsikkerugbrod": "solsikkerugbrød",
    "rugbrod": "rugbrød",
    "gulerodder": "gulerødder",
    "maelk": "mælk",
}

def clean_flyer_ocr_text(text):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""

    tokens = value.split()
    fixed = []
    for token in tokens:
        lead = re.match(r"^\W*", token).group(0)
        tail = re.search(r"\W*$", token).group(0)
        end = len(token) - len(tail) if tail else len(token)
        core = token[len(lead):end]
        replacement = OCR_WORD_FIXES.get(core.lower())
        if replacement:
            if core[:1].isupper() and replacement != "ØGO":
                replacement = replacement.capitalize()
            core = replacement
        fixed.append(f"{lead}{core}{tail}")

    value = " ".join(fixed)
    value = re.sub(r"(?i)\b(?:O|0|Ø)\s*GO\b", "ØGO", value)
    return value.strip()


def clean_flyer_product_name(name):
    """Lav et kort varenavn. Fjern logo-, mængde- og kampagnetekst."""
    name = clean_flyer_ocr_text(name)

    name = re.sub(r"(?i)\bØGO\s+(?:ØGO|SØGO|OGO|0GO)\b", "ØGO", name)
    name = re.sub(r"(?i)\bØKO\s+ØKO\b", "ØKO", name)

    # OCR mister nogle gange starten af ordet økologisk.
    name = re.sub(r"(?i)^\s*(?:øko(?:logisk(?:e)?)?|oko(?:logisk(?:e)?)?|ekologisk(?:e)?|logisk)\s*", "", name)
    name = re.sub(r"(?i)^\s*(?:ØGO|OGO|0GO)\s*", "", name)

    # Kampagne-/brugsfraser er ikke varenavne.
    name = re.sub(r"(?i)\b(?:aktuel|tilbudsavis|spotvarer?|pr\.?\s+kunde\s+pr\.?\s+dag)\b.*$", "", name)
    name = re.sub(r"(?i)\b(?:findes på køl|til denne pris|først til mølle)\b.*$", "", name)
    name = re.sub(r"(?i)\bkun\s+\d+\s+pr\.?\s+kunde\b.*$", "", name)

    # OCR-fragment: "eller tomater" -> "tomater".
    name = re.sub(r"(?i)^eller\s+", "", name)

    # Gentagelser.
    name = re.sub(r"(?i)\bdansk\s+danske\b", "Danske", name)
    words = name.split()
    deduped = []
    for word in words:
        if deduped and normalize(deduped[-1]) == normalize(word):
            continue
        deduped.append(word)
    name = " ".join(deduped)

    name = re.sub(r"\s+", " ", name).strip(" -–·,.;:")

    # Mængder alene er ikke produkter.
    if re.fullmatch(r"(?i)\d+(?:[.,]\d+)?\s*(?:g|kg|ml|cl|l|stk)", name):
        return ""
    if re.fullmatch(r"(?i)\d+\s*[-–]\s*\d+\s*(?:g|kg|ml|cl|l|stk)", name):
        return ""

    if normalize(name) in {
        "", "øgo", "øko", "ogo", "søgo", "sogo",
        "sport", "spot", "delikatess", "delikatess sport",
    }:
        return ""

    return name[:100]


def flyer_name_quality(name):
    """Kun produktlignende tekst får lov i den almindelige tilbudsliste."""
    n = normalize(name)
    if not n:
        return 0

    reject = [
        "pr kunde pr dag", "spotvarer fås", "først til mølle",
        "gode vaner", "findes på køl", "til denne pris",
        "red bull",  # ikke øko; typisk nabotekst fra forsiden
    ]
    if any(x in n for x in reject):
        return 0

    if re.fullmatch(r"\d+(?:[.,]\d+)?\s*(g|kg|ml|cl|l|stk)", n):
        return 0
    if re.fullmatch(r"\d+\s*[-–]\s*\d+\s*(g|kg|ml|cl|l|stk)", n):
        return 0

    words = [w for w in re.findall(r"[a-zæøå0-9%]+", n) if len(w) >= 2]
    if not words:
        return 0

    # Tekst med kun generiske øko-/kampagneord er ikke nok.
    generic = {
        "øko", "økologisk", "økologiske", "spot", "sport",
        "dansk", "danske", "findes", "køl", "denne", "pris",
    }
    product_words = [w for w in words if w not in generic and not re.fullmatch(r"\d+%?", w)]
    if not product_words:
        return 0

    score = 2
    if any(len(w) >= 5 for w in product_words):
        score += 1
    return score



def _flyer_price_from_lines(lines, center, radius=5):
    """Find en sandsynlig tilbudspris tæt på en øko-produktlinje."""
    best = None
    best_dist = 999
    lo = max(0, center - radius)
    hi = min(len(lines), center + radius + 1)

    for i in range(lo, hi):
        raw = re.sub(r"\s+", " ", lines[i]).strip()
        low = raw.lower()

        # Typiske avispriser: 15-, 15,-, 15.00, 15,00, "15 kr."
        patterns = [
            r"^\s*(\d{1,3})\s*[-–]\s*$",
            r"^\s*(\d{1,3})\s*[,.:]\s*[-–]?\s*$",
            r"^\s*(\d{1,3})[,.](\d{2})\s*(?:kr\.?)?\s*$",
            r"(?:^|\s)(\d{1,3})[,.](\d{2})\s*kr\.?(?:\s|$)",
            r"(?:^|\s)(\d{1,3})\s*kr\.?(?:\s|$)",
        ]
        value = None
        for pat in patterns:
            m = re.search(pat, low)
            if not m:
                continue
            try:
                whole = int(m.group(1))
                decimals = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else 0
                value = whole + decimals / 100
            except Exception:
                value = None
            break

        if value is None or value <= 0 or value > 500:
            continue

        dist = abs(i - center)
        if dist < best_dist:
            best = float(value)
            best_dist = dist

    return best


def _flyer_name_from_lines(lines, center, radius=4):
    """Find den mest produktlignende linje tæt på øko-markøren."""
    lo = max(0, center - radius)
    hi = min(len(lines), center + radius + 1)
    candidates = []

    for i in range(lo, hi):
        line = re.sub(r"\s+", " ", lines[i]).strip(" •|")
        low = line.lower()
        if not line or len(line) > 110:
            continue
        if not re.search(r"[a-zæøå]", low):
            continue
        if any(x in low for x in (
            "spotvarer", "pr. kg", "pr kg", "pr. stk", "pr stk",
            "pr. pose", "pr pose", "pr. bakke", "pr bakke",
            "side ", "annonce", "netto.dk", "pr. liter", "pr liter",
        )):
            continue
        if re.fullmatch(r"[\d\s.,:/%-]+(?:kg|g|l|ml|cl|stk)?", low):
            continue

        cleaned = clean_flyer_product_name(line)
        quality = flyer_name_quality(cleaned)
        if quality <= 0:
            continue

        # Nærhed til markøren vægter højt.
        distance = abs(i - center)
        score = quality * 10 - distance

        # En linje med både øko-markør og et rigtigt produktord er stærk.
        if _organic_marker_in_text(line):
            score += 3
        candidates.append((score, line))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1][:150]



def _strict_organic_marker(line):
    low = str(line or "").lower()
    return (
        "økolog" in low
        or "okolog" in low
        or "ekolog" in low
        or bool(re.search(r"(^|\s)(?:øgo|ogo|0go)(?:\s|[.,;:()/-]|$)", low))
    )


def _nearest_price(lines, anchor, radius=4):
    best = None
    best_dist = 999
    for i in range(max(0, anchor-radius), min(len(lines), anchor+radius+1)):
        price = _flyer_price_from_lines(lines, i, radius=0)
        if price is None:
            continue
        dist = abs(i-anchor)
        if dist < best_dist:
            best = price
            best_dist = dist
    return best, best_dist


def _nearest_product_line(lines, anchor, radius=3):
    candidates = []
    for i in range(max(0, anchor-radius), min(len(lines), anchor+radius+1)):
        raw = re.sub(r"\s+", " ", str(lines[i] or "")).strip()
        if not raw:
            continue

        cleaned = clean_flyer_product_name(raw)
        quality = flyer_name_quality(cleaned)
        if quality < 2:
            continue

        n = normalize(cleaned)
        if n in {"økologisk", "økologiske", "øko", "øgo", "ogo"}:
            continue

        dist = abs(i-anchor)
        score = quality * 10 - dist
        if i == anchor and len(cleaned.split()) >= 2:
            score += 8
        candidates.append((score, dist, raw, cleaned))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0]



def _looks_like_truncated_word(text):
    raw = str(text or "").strip()
    n = normalize(raw)
    words = re.findall(r"[a-zæøå]+", n)
    if not words:
        return True
    if words[0] in {"jisk", "akologisk", "ekologisk", "logisk"}:
        return True
    if len(words) == 1 and len(words[0]) <= 3:
        return True
    if raw.isupper() and len("".join(words)) <= 5:
        return True
    return False


def _product_block_candidate(lines, marker_i, radius=3):
    """Find ét produktnavn helt tæt på en tydelig øko-markør."""
    candidates = []
    for i in range(max(0, marker_i-radius), min(len(lines), marker_i+radius+1)):
        raw = re.sub(r"\s+", " ", str(lines[i] or "")).strip()
        cleaned = clean_flyer_product_name(raw)
        if not cleaned or flyer_name_quality(cleaned) < 2:
            continue
        if _looks_like_truncated_word(cleaned):
            continue

        n = normalize(cleaned)
        if n in {
            "økologisk", "økologiske", "øko", "øgo", "ogo",
            "ekstra", "ekstra jomfru", "kærgården", "kaergården",
            "urtekram", "valsemøllen", "hindrer",
        }:
            continue

        dist = abs(i - marker_i)
        score = 72 - dist * 10
        if i == marker_i:
            score += 15
        if _strict_organic_marker(raw):
            score += 8
        meaningful = [w for w in re.findall(r"[a-zæøå]+", n) if len(w) >= 4]
        score += min(15, len(meaningful) * 4)
        candidates.append((score, dist, raw, cleaned))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0]


def _confidence_label(score):
    # Lidt mindre stramt end v2.1.6, så reelle varer ikke forsvinder.
    if score >= 74:
        return "Høj"
    if score >= 60:
        return "Mellem"
    return "Lav"


def _organic_offers_from_ocr(store, text, source_url, page_no):
    """Produktblok-baseret OCR med sikkerhedsscore."""
    if not text:
        return []

    lines = [clean_flyer_ocr_text(x) for x in text.splitlines() if str(x).strip()]
    rows = []

    for marker_i, marker_line in enumerate(lines):
        if not _strict_organic_marker(marker_line):
            continue

        product = _product_block_candidate(lines, marker_i, radius=3)
        if not product:
            continue

        name_score, name_dist, raw_name, clean_name = product
        price, price_dist = _nearest_price(lines, marker_i, radius=3)
        if price is None:
            continue

        if name_dist > 2 or price_dist > 3:
            continue

        n = normalize(clean_name)
        reject_phrases = [
            "100 bomuld", "til denne pris", "findes på køl",
            "pr kunde", "spotvarer", "hindrer",
        ]
        if any(x in n for x in reject_phrases):
            continue

        score = name_score - price_dist * 8
        if _strict_organic_marker(raw_name):
            score += 10

        rows.append({
            "Butik": store,
            "Vare": clean_name,
            "Beskrivelse": raw_name,
            "Pris": float(price),
            "Øko": True,
            "Avis": "Aktuel tilbudsavis · produktblok-OCR",
            "Side": str(page_no),
            "Kilde": source_url,
            "Type": "Tilbudsavis produktblok-OCR",
            "Sikkerhed": _confidence_label(score),
            "_score": score,
        })

    if not rows:
        return []

    best = {}
    for row in rows:
        key = (normalize(row["Vare"]), row["Side"])
        if key not in best or row["_score"] > best[key]["_score"]:
            best[key] = row

    result = list(best.values())
    for row in result:
        row.pop("_score", None)
    return result


@st.cache_data(ttl=21600, show_spinner=False)
def scrape_flyer_pages_ocr(store, overview_url, max_pages=40, pipeline_version="2.1.7"):
    """
    Grundig avis-scanning.
    Finder de faktiske avis-sidebilleder og OCR-læser øko-tilbud, som
    produkt-tabellen på oversigtssiden ikke har registreret.
    """
    if not ocr_key():
        return pd.DataFrame()

    r = requests.get(overview_url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Find aktuelle detail-links til avisviseren.
    detail_urls = []
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        txt = " ".join(a.stripped_strings).lower()
        if "/avis-" in href or ("vis tilbudsavis" in txt and "tilbudsavis" in href):
            u = urljoin(overview_url, href)
            if u not in detail_urls:
                detail_urls.append(u)

    # Nogle sider bruger direkte tilbudsavis-link uden /avis- i synlig tekst.
    if not detail_urls:
        for a in soup.find_all("a", href=True):
            href = str(a.get("href") or "")
            if "tilbudsavis" in href and href.rstrip("/") != overview_url.rstrip("/"):
                u = urljoin(overview_url, href)
                if u not in detail_urls:
                    detail_urls.append(u)

    all_rows = []
    page_counter = 0

    # Højst to aktuelle avis-dele; Netto har typisk mad + nonfood.
    for detail_url in detail_urls[:2]:
        try:
            dr = requests.get(detail_url, headers=HEADERS, timeout=25, allow_redirects=True)
            dr.raise_for_status()
            dsoup = BeautifulSoup(dr.text, "html.parser")
        except Exception:
            continue

        images = []
        for img in dsoup.find_all("img"):
            alt = str(img.get("alt") or "")
            low = alt.lower()
            if "tilbud" not in low or "side" not in low:
                continue
            image_url = _flyer_image_url(img, dr.url)
            if image_url and image_url not in images:
                images.append(image_url)

        for image_url in images:
            if page_counter >= max_pages:
                break
            page_counter += 1
            try:
                # Først en billig helside-læsning som filter.
                page_text = _ocr_image_url(image_url)
                if not _organic_marker_in_text(page_text):
                    continue

                # På øko-sider deles billedet i fire felter. Det holder varenavn
                # og pris sammen og undgår tekst fra produkter på den anden side.
                page_img = _download_flyer_image(image_url)
                for tile_bytes in _flyer_tiles(page_img, cols=3, rows=3, overlap=0.04):
                    tile_text = _ocr_image_bytes(tile_bytes)
                    if not any(_strict_organic_marker(x) for x in tile_text.splitlines()):
                        continue
                    all_rows.extend(
                        _organic_offers_from_ocr(
                            store, tile_text, detail_url, page_counter
                        )
                    )
            except Exception:
                continue

        if page_counter >= max_pages:
            break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df
    df["_name_norm"] = df["Vare"].map(normalize)
    df = df.drop_duplicates(subset=["Butik", "_name_norm", "Side"], keep="first")
    return df.drop(columns=["_name_norm"]).reset_index(drop=True)


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
    """
    Nemlig er en onlinebutik, ikke en klassisk tilbudsavis.
    Hent både tilbud og offentligt synlige katalogpriser, men hold typerne adskilt.
    Vi gemmer aldrig en pris, medmindre pris + produkttekst kan findes på samme produktkort.
    """
    rows = []

    def parse_page(url, price_type):
        r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Nemlig kan ændre frontend. Vi prøver derfor flere almindelige produktkort,
        # men kræver altid et lokalt produktnavn og en pris i samme element.
        selectors = [
            "article",
            "[class*='product-card']",
            "[class*='productCard']",
            "[data-product-id]",
            "li[class*='product']",
        ]
        seen_nodes = set()
        candidates = []
        for selector in selectors:
            for el in soup.select(selector):
                ident = id(el)
                if ident not in seen_nodes:
                    seen_nodes.add(ident)
                    candidates.append(el)

        for el in candidates:
            txt = " ".join(el.stripped_strings)
            if not (5 < len(txt) < 900):
                continue

            # Pris skal være eksplicit i kortet. money() bruges kun på kortets egen tekst.
            pr = money(txt)
            if pr is None or pr <= 0 or pr > 5000:
                continue

            chunks = [re.sub(r"\s+", " ", x).strip()
                      for x in el.stripped_strings if 2 < len(x.strip()) < 180]
            if not chunks:
                continue

            # Vælg første tekststump der ligner et varenavn, ikke pris/badge/navigation.
            name = ""
            for chunk in chunks:
                low = chunk.lower()
                if re.fullmatch(r"[\d\s,.]+(?:kr\.?)?", low):
                    continue
                if low in {"tilbud", "prismatch", "premium", "læg i kurv", "se mere"}:
                    continue
                if any(word in low for word in ("cookie", "log ind", "kundeservice")):
                    continue
                name = chunk[:140]
                break
            if not name:
                continue

            rows.append({
                "Butik": "Nemlig.com",
                "Vare": name,
                "Beskrivelse": txt[:300],
                "Pris": float(pr),
                "Øko": looks_organic(txt),
                "Avis": "",
                "Side": "",
                "Kilde": url,
                "Type": price_type,
            })

    # Tilbud har sin egen type og må gerne indgå som aktuelt tilbud.
    try:
        parse_page(ONLINE_ONLY["Nemlig.com"], "Online tilbud")
    except Exception:
        pass

    # Katalogsider bruges som aktuelle online-normalpriser/prisreference.
    # De må ikke fejlagtigt kaldes tilbud.
    for url in NEMLIG_CATALOG_PAGES:
        try:
            parse_page(url, "Online pris")
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["Vare", "Pris", "Type"])
    # Begræns kun dubletter/støj – ikke antallet til 100 som før.
    return df.reset_index(drop=True)


def fetch_all(include_nemlig=True):
    frames = []
    status = []

    for store, url in FLYER_SOURCES.items():
        try:
            df = scrape_flyer_table(store, url)

            # Pilot: Netto får en ekstra dyb OCR-scanning af selve avis-siderne,
            # fordi oversigtstabellen kun indeholder en lille del af varerne.
            if store == "Netto" and ocr_key():
                try:
                    deep = scrape_flyer_pages_ocr(store, url, max_pages=40, pipeline_version=APP_VERSION)
                    if not deep.empty:
                        # Behold ALLE OCR-fund til Aviser-fanen.
                        # Usikre fund filtreres først fra, når Prisrobotten matcher.
                        df = pd.concat([df, deep], ignore_index=True)
                        df = df.drop_duplicates(
                            subset=["Butik", "Vare", "Pris", "Side"],
                            keep="first",
                        ).reset_index(drop=True)
                    status.append((store, len(df), "Tilbudsavis + dyb OCR"))
                except Exception:
                    status.append((store, len(df), "Tilbudsavis · OCR kunne ikke supplere"))
            else:
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
        status.append(("Nemlig.com", 0, "Direkte produktsøgning ved behov"))

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
    "smørbar": ["smørbar", "smørbart", "blandingsprodukt", "smørblanding"],
    "pasta": ["pasta", "spaghetti", "penne", "fusilli"],
    "rugbrød": ["rugbrød"],
    "hakket kød": ["hakket oksekød", "hakket kød", "hakket gris", "hakket kylling"],
    "hakket oksekød": ["hakket oksekød"],
    "kylling": ["kylling", "kyllingebryst", "kyllingefilet", "kyllingeinderfilet"],
    "gulerødder": ["gulerod", "gulerødder"],
    "kartofler": ["kartoffel", "kartofler"],
    "æble": ["æble", "æbler", "pink lady", "royal gala", "golden delicious", "granny smith", "jazz æble"],
    "æbler": ["æble", "æbler", "pink lady", "royal gala", "golden delicious", "granny smith", "jazz æble"],
    "yoghurt": ["yoghurt", "skyr"],

    "kærnemælk": ["kærnemælk", "kærnem", "kaernemaelk", "kaernem"],
    "græsk yoghurt": [
        "græsk yoghurt", "græsk yogurt", "graesk yoghurt", "graesk yogurt",
        "græsk yog", "graesk yog", "græsk yo", "graesk yo",
        "græsk", "graesk", "grsk yoghurt", "grsk yog", "grsk"
    ],
    "græsk yogurt": [
        "græsk yoghurt", "græsk yogurt", "graesk yoghurt", "graesk yogurt",
        "græsk yog", "graesk yog", "græsk yo", "graesk yo",
        "græsk", "graesk", "grsk yoghurt", "grsk yog", "grsk"
    ],
    "piskefløde": ["piskefløde", "piskeflø", "piskefloede", "piskeflo"],
}



MATCH_BRAND_WORDS = {
    "naturli", "naturlig", "løgismose", "loegismose",
    "arla", "engvang", "salling", "rema", "netto", "lidl",
    "365", "føtex", "foetex", "coop", "änglamark", "anglamark",
}

def meaningful_match_tokens(text):
    n = normalize(text)
    return {
        t for t in n.split()
        if t not in MATCH_BRAND_WORDS
        and len(t) >= 3
        and not re.fullmatch(r"\d+(?:[.,]\d+)?", t)
        and t not in {"stk", "kg", "g", "l", "ml", "cl", "pak", "pk"}
    }


MATCH_GENERIC_WORDS = {
    "øko", "økologisk", "økologiske", "organic",
    "brun", "brune", "grøn", "grønne", "rød", "røde", "hvid", "hvide",
    "gul", "gule", "mild", "milde", "frisk", "friske",
    "stor", "store", "lille", "små", "mini",
}

def core_product_tokens(text):
    """Produktord som faktisk beskriver varen – ikke fx 'øko' eller farven 'brune'."""
    return {
        t for t in meaningful_match_tokens(text)
        if t not in MATCH_GENERIC_WORDS
    }


def product_words_related(a, b):
    """Forsigtig lighed mellem rigtige produktord."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5:
        return False
    if a.startswith(b) or b.startswith(a) or a in b or b in a:
        return True

    # Små bøjnings-/OCR-forskelle: fx falafel/falafler.
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio() >= 0.80


def has_core_product_overlap(query, product):
    """Kræv mindst ét rigtigt produktord i fællesskab."""
    qa = core_product_tokens(query)
    pa = core_product_tokens(product)
    if not qa or not pa:
        return False
    return any(product_words_related(qw, pw) for qw in qa for pw in pa)


def safe_wishlist_product_match(query, product, query_family=None, product_family_name=None):
    """Sikker match til Jeg mangler.
    Manuel kategori må matche direkte. Ellers kræves et reelt produktord.
    """
    same_family = (
        query_family and product_family_name
        and normalize(query_family) == normalize(product_family_name)
    )
    if same_family:
        return True, 1.0

    if not has_core_product_overlap(query, product):
        return False, 0.0

    score = match_score(query, product, "")
    # Core-ordet er den vigtigste sikkerhed; score bruges til rangering.
    return True, max(score, 0.60)


def match_score(query, product, description=""):
    q = normalize(query)
    p = normalize(f"{product} {description}")
    if not q or not p:
        return 0

    # "Æbler" betyder frisk frugt – ikke produkter med æble i navnet.
    # Denne kontrol ligger før den generelle alias/familie-matchning.
    apple_queries = {"æble", "æbler"}
    apple_not_fruit = (
        "nektar", "juice", "most", "mos", "puré", "pure",
        "cider", "saft", "drik", "smoothie", "eddike",
        "marmelade", "grød", "kompot"
    )
    if q in apple_queries and any(word in p for word in apple_not_fruit):
        return 0

    # Hvis både søgning og bon/avis kan reduceres til samme grundvare,
    # skal de matche selv om bonen kun skriver fx "GRÆSK", "GRÆSK YOG" osv.
    rules = load_habit_rules()
    q_family = product_family(query, rules=rules)
    p_family = product_family(f"{product} {description}", rules=rules)
    if q_family and p_family and normalize(q_family) == normalize(p_family):
        return 1.0

    # "Smør" og "smørbar" er forskellige produkter.
    # En søgning efter rigtig smør må ikke ramme smørbar/blandingsprodukt.
    if q == "smør" and (
        "smørbar" in p
        or "smørbart" in p
        or "blandingsprodukt" in p
        or "smørblanding" in p
    ):
        return 0
    if q == "smørbar" and " smør " in f" {p} " and not any(
        x in p for x in ("smørbar", "smørbart", "blandingsprodukt", "smørblanding")
    ):
        return 0

    aliases = ALIASES.get(q, [q])
    for a in aliases:
        if a in p:
            return 1.0
        # Bon-OCR kan forkorte lange varenavne, fx "kærnem" eller "græsk".
        # Brug kun præfiks-match på ord på mindst 5 tegn.
        if len(a) >= 5:
            for pw in p.split():
                if len(pw) >= 5 and (a.startswith(pw) or pw.startswith(a)):
                    if min(len(a), len(pw)) >= 5:
                        return 0.95

    qa = meaningful_match_tokens(q)
    pa = meaningful_match_tokens(p)
    if not qa or not pa:
        return 0

    exact = len(qa & pa) / len(qa | pa)
    if exact > 0:
        return exact

    # OCR kan klippe slutningen af et langt ord:
    # fx "piskefløde" -> "piskeflø".
    for qw in qa:
        if len(qw) < 5:
            continue
        for pw in pa:
            if len(pw) < 5:
                continue
            shorter = min(len(qw), len(pw))
            if shorter >= 6 and (qw.startswith(pw) or pw.startswith(qw)):
                return 0.9

    return 0



NEMLIG_API_BASE = "https://www.nemlig.com/webapi"
NEMLIG_SEARCH_GATEWAY = "https://webapi.prod.knl.nemlig.it/searchgateway/api"


@st.cache_data(ttl=3000, show_spinner=False)
def nemlig_session_info():
    """Hent anonym token + katalog-timestamp. Ingen Nemlig-login nødvendig."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://www.nemlig.com/",
        "version": "11.201.0",
        "platform": "web",
        "device-size": "mobile",
    }
    sess = requests.Session()
    sess.headers.update(headers)

    token_r = sess.get(f"{NEMLIG_API_BASE}/Token", timeout=20)
    token_r.raise_for_status()
    token = (token_r.json() or {}).get("access_token")
    if not token:
        raise RuntimeError("Nemlig gav ikke en anonym adgangstoken")

    auth_headers = dict(headers)
    auth_headers["Authorization"] = f"Bearer {token}"

    settings_r = sess.get(
        f"{NEMLIG_API_BASE}/v2/AppSettings/Website",
        headers=auth_headers,
        timeout=20,
    )
    settings_r.raise_for_status()
    settings = settings_r.json() or {}

    combined = settings.get("CombinedProductsAndSitecoreTimestamp")
    if not combined:
        raise RuntimeError("Nemlig gav ikke produkt-timestamp")

    # Nemlig-shopper-projektet bruger en anonym standard-timeslot til søgning.
    timeslot = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d") + "15-60-240"

    return {
        "token": token,
        "timestamp": combined,
        "timeslot": timeslot,
        "timeslot_id": 0,
    }


@st.cache_data(ttl=1200, show_spinner=False)
def search_nemlig_products(query, limit=20):
    """Direkte søgning i Nemligs produktkatalog via deres søge-gateway."""
    info = nemlig_session_info()
    headers = {
        "X-Correlation-Id": str(uuid.uuid4()),
        "Origin": "https://www.nemlig.com",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://www.nemlig.com/",
        "Authorization": f"Bearer {info['token']}",
    }
    params = {
        "query": query,
        "take": int(limit),
        "skip": 0,
        "recipeCount": 0,
        "timestamp": info["timestamp"],
        "timeslotUtc": info["timeslot"],
        "deliveryZoneId": 1,
        "includeFavorites": "0",
        "TimeSlotId": info["timeslot_id"],
    }

    r = requests.get(
        f"{NEMLIG_SEARCH_GATEWAY}/search",
        params=params,
        headers=headers,
        timeout=25,
    )
    r.raise_for_status()
    data = r.json() or {}
    products_data = data.get("Products", {})
    if isinstance(products_data, dict):
        products = products_data.get("Products", [])
    elif isinstance(products_data, list):
        products = products_data
    else:
        products = []

    rows = []
    for item in products[:limit]:
        try:
            price = float(item.get("Price"))
        except Exception:
            continue
        if price <= 0:
            continue

        name = str(item.get("Name") or "").strip()
        if not name:
            continue
        description = str(item.get("Description") or "").strip()
        labels = item.get("Labels") or []
        labels_text = " ".join(str(x) for x in labels)
        organic = (
            looks_organic(f"{name} {description} {labels_text}")
            or any("øko" in str(x).lower() for x in labels)
        )
        availability = item.get("Availability") or {}
        available = (
            availability.get("IsDeliveryAvailable", True)
            and availability.get("IsAvailableInStock", True)
        )
        if not available:
            continue

        # Nemlig kan returnere DiscountItem som et objekt/metadata.
        # Det er kun et tilbud, når API'et eksplicit siger True.
        discount_item = item.get("DiscountItem")
        is_discount_item = item.get("IsDiscountItem")
        on_discount = (discount_item is True) or (is_discount_item is True)
        rows.append({
            "Butik": "Nemlig.com",
            "Vare": name,
            "Beskrivelse": description,
            "Pris": price,
            "Øko": organic,
            "Avis": "",
            "Side": "",
            "Kilde": "https://www.nemlig.com/",
            "Type": "Online tilbud" if on_discount else "Online pris",
        })

    return pd.DataFrame(rows)


def save_nemlig_search_prices(df):
    """Gem direkte Nemlig-søgeresultater med korrekt pris-type."""
    if df is None or df.empty:
        return
    try:
        save_offer_snapshots(df)
    except Exception:
        pass


def candidate_unit_info(row):
    try:
        price = float(row.get("Pris"))
    except Exception:
        return None, None
    return unit_price(price, f"{row.get('Vare', '')} {row.get('Beskrivelse', '')}")


def rank_current_candidates(candidates):
    """Rangér sammenlignelige aktuelle varer på enhedspris før pakkepris."""
    if not candidates:
        return []
    enriched = []
    for score, row in candidates:
        upr, unit = candidate_unit_info(row)
        enriched.append((score, row, upr, unit))

    # Hvis flere kandidater kan sammenlignes på samme enhed, brug den enhed med flest kandidater.
    counts = {}
    for _, _, upr, unit in enriched:
        if upr is not None and unit in ("kg", "l", "stk"):
            counts[unit] = counts.get(unit, 0) + 1
    preferred = max(counts, key=counts.get) if counts else None

    def key(x):
        score, row, upr, unit = x
        if preferred and unit == preferred and upr is not None:
            return (0, float(upr), -float(score))
        return (1, -float(score), float(row["Pris"]))

    return sorted(enriched, key=key)


def current_vs_history(current_row, hist):
    """Returnér True hvis en frisk historisk pris reelt er billigere end aktuel online-normalpris."""
    if not hist or hist.get("stale"):
        return False
    cu, cun = candidate_unit_info(current_row)
    hu = hist.get("unit_price")
    hun = hist.get("unit_name")
    try:
        if cu is not None and hu is not None and cun and cun == hun:
            return float(hu) < float(cu) * 0.995
    except Exception:
        pass

    # Kun sammenlign rå pakkepris hvis pakkestørrelsen kan læses og er den samme.
    cq, ca, cb = quantity_info(f"{current_row.get('Vare','')} {current_row.get('Beskrivelse','')}")
    hq, ha, hb = quantity_info(hist.get("item", ""))
    if ca and ha and cb and cb == hb and abs(float(ca)-float(ha)) < 0.0001:
        try:
            return float(hist["price"]) < float(current_row["Pris"]) * 0.995
        except Exception:
            return False
    return False

def wishlist_match(data, items, organic_only=True, include_nemlig=True):
    base = data.copy()

    # Aviser må gerne vise tvivlsomme OCR-fund, men de må ikke styre Prisrobotten.
    if not base.empty and "Sikkerhed" in base.columns:
        ocr_mask = base.get("Type", pd.Series(index=base.index, dtype=str)).fillna("").astype(str).str.contains("OCR", case=False, na=False)
        base = base[(~ocr_mask) | base["Sikkerhed"].fillna("").eq("Høj")].copy()

    if not include_nemlig and not base.empty and "Butik" in base.columns:
        base = base[
            base["Butik"].fillna("").astype(str).str.strip().str.lower() != "nemlig.com"
        ].copy()

    if organic_only:
        base = base[base["Øko"] == True]

    rows = []
    for item in items:
        candidates = []
        query_family = product_family(item, rules=load_habit_rules())
        for _, r in base.iterrows():
            product_name = str(r["Vare"])
            product_family_name = product_family(product_name, rules=load_habit_rules())
            ok, score = safe_wishlist_product_match(
                item, product_name, query_family, product_family_name
            )
            if ok:
                candidates.append((score, r))

        # Nemlig søges direkte på præcis den vare brugeren mangler.
        # Det er mere stabilt end at forsøge at scrape hele webshoppen.
        if include_nemlig:
            try:
                nemlig_df = search_nemlig_products(item, limit=24)
                if organic_only and not nemlig_df.empty:
                    nemlig_df = nemlig_df[nemlig_df["Øko"] == True]
                if not nemlig_df.empty:
                    save_nemlig_search_prices(nemlig_df)
                    for _, nr in nemlig_df.iterrows():
                        product_name = str(nr["Vare"])
                        product_family_name = product_family(product_name, rules=load_habit_rules())
                        ok, score = safe_wishlist_product_match(
                            item, product_name, query_family, product_family_name
                        )
                        if ok:
                            candidates.append((score, nr))
            except Exception:
                pass

        if candidates:
            # Rigtige tilbud har første prioritet. Inden for samme type vælger vi
            # laveste kr/kg, kr/l eller kr/stk frem for den mindste pakkepris.
            offer_candidates = [
                x for x in candidates
                if str(x[1].get("Type", "")).strip().lower() != "online pris"
            ]
            pool = offer_candidates if offer_candidates else candidates
            ranked_now = rank_current_candidates(pool)
            _, r, upr, uunit = ranked_now[0]

            # En almindelig Nemlig-onlinepris må ikke overtage en billigere,
            # frisk bonpris fra fx Netto alene fordi Nemlig-pakken er mindre.
            if not offer_candidates and str(r.get("Type", "")).strip().lower() == "online pris":
                hist_compare = historical_best_price(item, organic_only=organic_only, include_nemlig=include_nemlig)
                if current_vs_history(r, hist_compare):
                    candidates = []
                else:
                    hist_compare = None

            if candidates:
                enhed = f"{upr:.2f} kr/{uunit}" if upr is not None and uunit else ""
                verdict, verdict_note = price_verdict(item, float(r["Pris"]))
                rows.append({
                    "Du mangler": item,
                    "Butik": r["Butik"],
                    "Vare": r["Vare"],
                    "Pris": r["Pris"],
                    "Vurdering": verdict,
                    "Enhedspris": enhed,
                    "Prisgrundlag": r["Type"],
                    "Senest set": "Denne uge",
                    "Svar": (
                        (
                            f"Aktuel onlinepris hos {r['Butik']} til {float(r['Pris']):.2f} kr. "
                            if str(r.get("Type", "")).strip().lower() == "online pris"
                            else f"Aktuelt tilbud hos {r['Butik']} til {float(r['Pris']):.2f} kr. "
                        )
                        + f"{verdict} – {verdict_note}."
                        + (f" ({enhed})" if enhed else "")
                    ),
                })
                continue

        # Ingen aktuel avispris: brug kun en pris vi faktisk tidligere har observeret.
        hist = historical_best_price(item, organic_only=organic_only, include_nemlig=include_nemlig)
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
                unit_note = ""
                if hist.get("unit_price") is not None and hist.get("unit_name"):
                    unit_note = f" ({float(hist['unit_price']):.2f} kr/{hist['unit_name']})"
                answer = (
                    f"Desværre ingen billigere aktuelt tilbud/pris. Ud fra priser set de seneste 60 dage "
                    f"plejer den at være billigst hos {hist['store']} til ca. {hist['price']:.2f} kr.{unit_note} {age_note}"
                )
                shown_store = hist["store"]
            hist_unit = ""
            if hist.get("unit_price") is not None and hist.get("unit_name"):
                try:
                    hist_unit = f"{float(hist['unit_price']):.2f} kr/{hist['unit_name']}"
                except Exception:
                    hist_unit = ""
            hist_verdict, hist_verdict_note = price_verdict(item, hist["price"])
            rows.append({
                "Du mangler": item,
                "Butik": shown_store,
                "Vare": hist["item"],
                "Pris": hist["price"],
                "Vurdering": hist_verdict,
                "Enhedspris": hist_unit,
                "Prisgrundlag": hist["label"],
                "Senest set": hist["date"],
                "Svar": answer + (f" Enhedspris: {hist_unit}." if hist_unit else ""),
            })
        else:
            rows.append({
                "Du mangler": item,
                "Butik": "Ikke fundet",
                "Vare": "",
                "Pris": None,
                "Vurdering": "",
                "Enhedspris": "",
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

    # En bon kan indeholde samme vare flere gange til samme pris.
    # Supabase kan ikke upserte to identiske konflikt-nøgler i samme kommando,
    # så vi fjerner dubletter i payloaden først.
    unique = {}
    for row in payload:
        key = (
            str(row.get("store") or ""),
            str(row.get("normalized_item") or ""),
            str(row.get("observed_date") or ""),
            str(row.get("price_type") or ""),
            str(row.get("price") or ""),
        )
        unique[key] = row

    clean_payload = list(unique.values())
    if not clean_payload:
        return 0

    client.table("price_observations").upsert(
        clean_payload,
        on_conflict="store,normalized_item,observed_date,price_type,price"
    ).execute()
    return len(clean_payload)


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
            "price_type": (
                "regular_observed"
                if str(r.get("Type", "")).strip().lower() == "online pris"
                else "offer"
            ),
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
            # Pakkepris gemmes pr. pakke; kr/kg, kr/l eller kr/stk beregnes ud fra varenavnet.
            upr, uunit = unit_price(price, name)
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



def same_product_family(a, b):
    """Sikker grundvare-match på tværs af bonnavne, OCR og manuelle regler."""
    rules = load_habit_rules()
    fa = product_family(a, rules=rules)
    fb = product_family(b, rules=rules)
    if not fa or not fb:
        return False
    return normalize(fa) == normalize(fb)


def historical_best_price(query, organic_only=True, include_nemlig=True):
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
        canonical_item = product_family(r.get("item", ""), rules=load_habit_rules())
        if organic_only and (
            r.get("organic") is not True
            and not looks_organic(r.get("item", ""))
            and not looks_organic(canonical_item or "")
        ):
            continue
        if not same_product_family(query, r.get("item", "")) and match_score(query, r.get("item", "")) < 0.55:
            continue
        try:
            price = float(r.get("price"))
        except Exception:
            continue
        store_name = str(r.get("store") or "").strip()
        if price <= 0 or store_name.lower() in ("", "ukendt", "unknown", "none"):
            continue
        if not include_nemlig and store_name.lower() == "nemlig.com":
            continue
        candidates.append({
            "item": r.get("item", ""),
            "store": r.get("store"),
            "price": price,
            "unit_price": r.get("unit_price"),
            "unit_name": r.get("unit_name"),
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
        if not same_product_family(query, item_name) and match_score(query, item_name) < 0.55:
            continue
        canonical_item = product_family(item_name, rules=load_habit_rules())
        if organic_only and not (
            looks_organic(item_name)
            or looks_organic(canonical_item or "")
        ):
            continue
        raw_price = r.get("normal_price")
        if raw_price is None:
            raw_price = r.get("paid_price")
        try:
            price = float(raw_price)
        except Exception:
            continue
        purchase_store = str(r.get("store") or "").strip()
        if not include_nemlig and purchase_store.lower() == "nemlig.com":
            continue
        store_name = str(r.get("store") or "").strip()
        if price <= 0 or store_name.lower() in ("", "ukendt", "unknown", "none"):
            continue
        upr, uunit = unit_price(price, item_name)
        candidates.append({
            "item": item_name,
            "store": r.get("store"),
            "price": price,
            "unit_price": upr,
            "unit_name": uunit,
            "date": r.get("purchased_at", ""),
            "age": age_days(r.get("purchased_at")),
        })

    if not candidates:
        return None

    usable = [r for r in candidates if r["age"] <= MAX_CURRENT_AGE_DAYS]
    if usable:
        # Når vi har kr/kg, kr/l eller kr/stk, er det dét der afgør billigste butik.
        unit_groups = {}
        for r in usable:
            try:
                up = float(r.get("unit_price"))
            except Exception:
                up = None
            un = str(r.get("unit_name") or "").strip()
            if up and up > 0 and un in ("kg", "l", "stk"):
                unit_groups.setdefault(un, []).append(r)

        ranking_unit = None
        ranking_rows = usable
        if unit_groups:
            # Vælg den enhed vi har mest sammenlignelig historik for.
            ranking_unit = max(unit_groups, key=lambda u: len(unit_groups[u]))
            ranking_rows = unit_groups[ranking_unit]

        by_store = {}
        for r in ranking_rows:
            by_store.setdefault(r["store"], []).append(r)

        ranked = []
        for store, rs in by_store.items():
            if ranking_unit:
                vals = sorted(float(x["unit_price"]) for x in rs if x.get("unit_price") is not None)
            else:
                vals = sorted(float(x["price"]) for x in rs)
            if not vals:
                continue
            n = len(vals)
            median_basis = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
            latest = min(rs, key=lambda x: x["age"])
            # Vis den seneste observerede pakkepris, men rangér på enhedspris når muligt.
            ranked.append((median_basis, len(rs), latest))

        if ranked:
            ranked.sort(key=lambda x: (x[0], -x[1]))
            median_basis, observations, latest = ranked[0]
            freshness = "Frisk bonpris" if latest["age"] <= FRESH_AGE_DAYS else "Ældre bonpris"
            return {
                "store": latest["store"],
                "item": latest["item"],
                "price": round(float(latest["price"]), 2),
                "unit_price": round(float(median_basis), 2) if ranking_unit else latest.get("unit_price"),
                "unit_name": ranking_unit or latest.get("unit_name"),
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
    valid_stores = {"Netto", "REMA 1000", "365discount", "Lidl", "føtex", "Nemlig.com"}
    if store not in valid_stores:
        raise ValueError("Vælg bonens butik før du gemmer. 'Ukendt' gemmes ikke længere.")
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
                disc_total = abs(amount)
                qty = int(rows[-1].get("_antal", 1) or 1)
                # Rabatten på bonlinjen er typisk for hele linjen.
                # Prisrobotten gemmer per pakke/enhed, så fordel rabatten.
                disc_per_item = round(disc_total / max(qty, 1), 2)
                rows[-1]["Rabat"] = round(rows[-1]["Rabat"] + disc_per_item, 2)
                rows[-1]["Betalt pris"] = round(
                    max(0, rows[-1]["Normalpris"] - rows[-1]["Rabat"]), 2
                )
            continue

        if amount is None or not re.search(r"[A-Za-zÆØÅæøå]", line):
            continue

        before_total = line[:match.start()].strip(" .:-*")

        # Eksempler fra boner:
        # "Øko. æg 10 stk. M/L 36,95 x 2 73,90 B"
        # "Øko. minimælk 12,95 x 3 38,85 B"
        # Her er sidste beløb linjetotalen, mens 36,95 / 12,95 er pakkeprisen.
        multi = re.search(
            r"(\d{1,4}[,.]\d{2})\s*[x×]\s*(\d{1,3})\s*$",
            before_total,
            re.I,
        )

        qty = 1
        unit_normal = float(amount)
        name = before_total

        if multi:
            try:
                unit_candidate = float(multi.group(1).replace(",", "."))
                qty_candidate = int(multi.group(2))
                expected_total = round(unit_candidate * qty_candidate, 2)

                # OCR kan afvige få øre. Accepter en lille tolerance.
                if qty_candidate >= 2 and abs(expected_total - float(amount)) <= 0.15:
                    unit_normal = unit_candidate
                    qty = qty_candidate
                    name = before_total[:multi.start()].strip(" .:-*")
            except Exception:
                pass

        name = re.sub(r"\b(x\d+|\d+\s*x)\b", " ", name, flags=re.I)
        name = re.sub(r"\s+", " ", name).strip()
        if len(name) < 2:
            continue

        pack_text, _, _ = quantity_info(name)
        if qty > 1:
            qtxt = f"{pack_text} · købt {qty}".strip(" ·") if pack_text else f"købt {qty}"
        else:
            qtxt = pack_text

        rows.append({
            "Vare": name[:120],
            "Normalpris": round(unit_normal, 2),
            "Rabat": 0.0,
            "Betalt pris": round(unit_normal, 2),
            "Mængde": qtxt,
            "_antal": qty,
            "_linjetotal": round(float(amount), 2),
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Interne hjælpefelter bruges kun under parsing.
    return df.drop(columns=["_antal", "_linjetotal"], errors="ignore")

def price_stats(query):
    """Prisniveau fra brugerens egne køb af samme grundvare."""
    history = load_purchase_history()
    prices = []
    rules = load_habit_rules()
    for row in history:
        item_name = row.get("item", "")
        if not same_product_family(query, item_name) and match_score(query, item_name) < 0.55:
            continue
        try:
            p = float(row.get("paid_price"))
            if p > 0:
                prices.append(p)
        except Exception:
            pass
    if not prices:
        return None
    prices = sorted(prices)
    median = float(pd.Series(prices).median())
    return {
        "n": len(prices),
        "median": median,
        "avg": sum(prices) / len(prices),
        "min": min(prices),
        "max": max(prices),
    }


def price_verdict(query, price):
    """Forsigtig vurdering: bruger medianen af faktiske tidligere køb."""
    stats = price_stats(query)
    if not stats:
        return "🆕 Ny pris", "Ikke nok prishistorik endnu"
    normal = stats["median"]
    if normal <= 0:
        return "🆕 Ny pris", "Ikke nok prishistorik endnu"
    pct = (normal - float(price)) / normal * 100

    if pct >= 15:
        return "🔥 Superpris", f"{pct:.0f}% under din typiske pris"
    if pct >= 5:
        return "👍 God pris", f"{pct:.0f}% under din typiske pris"
    if pct > -5:
        return "😐 Normal pris", f"omkring din typiske pris på {normal:.2f} kr."
    return "⚠️ Dyrt", f"{abs(pct):.0f}% over din typiske pris"


if "flyer_data" not in st.session_state:
    st.session_state["flyer_data"] = pd.DataFrame()
if "source_status" not in st.session_state:
    st.session_state["source_status"] = []

def danish_sort_key(text):
    """Dansk alfabetisk rækkefølge: ... z, æ, ø, å."""
    t = str(text or "").strip().lower()
    trans = str.maketrans({"æ": "{", "ø": "|", "å": "}"})
    return t.translate(trans)


def canonical_shopping_items():
    """Jeg mangler viser brugerens egne kategorinavne – ikke gamle standardnavne."""
    rules = load_habit_rules()
    items = []

    # Når brugeren har oprettet kategorier, er det KUN disse navne,
    # der skal være faste valg i "Jeg mangler".
    for rule in rules.values():
        if rule.get("hidden"):
            continue
        target = str(rule.get("target_name") or "").strip()
        if target:
            items.append(target)

    # Hvis der endnu ikke findes egne kategorier, falder vi tilbage på
    # de aktive vane-familier. Der er ingen hardcoded standardliste længere.
    if not items:
        try:
            summary = habit_summary(load_purchase_history())
            if not summary.empty:
                items.extend(summary["Vare"].dropna().astype(str).str.strip().tolist())
        except Exception:
            pass

    seen, out = set(), []
    for x in items:
        key = normalize(x)
        if key and key not in seen:
            seen.add(key)
            out.append(str(x).strip().capitalize())

    return sorted(out, key=danish_sort_key)


tabs = st.tabs(["🏠", "📝 Jeg mangler", "📰 Aviser", "🎯 Til mig", "📸 Bon", "🧠 Vaner", "⚙️"])

with tabs[0]:
    st.success("Nu læser robotten tilbudsaviser og bygger sin egen prishukommelse")
    st.write(
        "Netto, REMA 1000, Lidl, føtex og 365discount behandles som **tilbudsaviser**. "
        "Nemlig.com søges **direkte i produktkataloget**, når du trykker “Find bedste pris”. "
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
    st.subheader("Hvad mangler du?")
    shopping_options = canonical_shopping_items()
    selected_items = st.multiselect(
        "Vælg varer",
        shopping_options,
        placeholder="Tryk og vælg fra dine faste varer…",
        help="Listen viser dine egne kategorinavne. Gamle standardnavne vises ikke længere.",
    )
    new_item = st.text_input(
        "Mangler varen på listen?",
        placeholder="Skriv fx Avocado",
        help="Nye varer kan bruges med det samme. Når de bliver en vane, dukker de automatisk op på listen.",
    )
    if st.button("➕ Tilføj til denne søgning", use_container_width=True):
        if new_item.strip():
            st.session_state["extra_wishlist_item"] = new_item.strip().capitalize()
            st.rerun()

    extra_item = st.session_state.get("extra_wishlist_item", "")
    wanted_items = sorted(list(selected_items), key=danish_sort_key)
    if extra_item and normalize(extra_item) not in {normalize(x) for x in wanted_items}:
        wanted_items.append(extra_item)
        wanted_items = sorted(wanted_items, key=danish_sort_key)
        st.caption(f"Ekstra vare: **{extra_item}**")

    organic_only = st.toggle("Kun økologiske tilbud", value=True)
    include_nemlig_w = st.toggle("Tag Nemlig.com med", value=True, key="nemlig_w")

    if st.button("Find bedste pris", type="primary", disabled=not wanted_items):
        data = st.session_state["flyer_data"]
        if data.empty:
            with st.spinner("Læser tilbudsaviserne først…"):
                data, status = fetch_all(include_nemlig=include_nemlig_w)
            st.session_state["flyer_data"] = data
            st.session_state["source_status"] = status

        result = wishlist_match(
            data,
            wanted_items,
            organic_only=organic_only,
            include_nemlig=include_nemlig_w,
        )
        if not result.empty and "Du mangler" in result.columns:
            result = result.sort_values(
                "Du mangler",
                key=lambda col: col.map(danish_sort_key),
                kind="stable",
            ).reset_index(drop=True)
        st.dataframe(result, hide_index=True, use_container_width=True)
        st.markdown("### Prisrobotten siger")
        for _, rr in result.iterrows():
            vare = str(rr.get("Du mangler", ""))
            butik = str(rr.get("Butik", ""))
            pris = rr.get("Pris")
            vurdering = str(rr.get("Vurdering", "") or "")
            try:
                pris_txt = f"{float(pris):.2f} kr."
            except Exception:
                pris_txt = "Ingen sikker pris"
            st.markdown(f"**{vare}** · {vurdering}")
            st.write(f"{butik} · {pris_txt}")
            if rr.get("Enhedspris"):
                st.caption(str(rr.get("Enhedspris")))
            st.caption(str(rr.get("Svar", "")))
            st.divider()
        st.caption("Hvis der ikke er et aktuelt tilbud, bruger robotten dine gemte bonpriser og andre priser, den faktisk har observeret – aldrig en gættet normalpris.")
        found = result["Pris"].dropna()
        if not found.empty:
            st.metric("Samlet pris for fundne varer", f"{found.sum():.2f} kr.")

with tabs[2]:
    st.subheader("Ugens tilbudsaviser")
    st.caption("Netto læses nu i to lag: produktlisten + en grundig OCR-scanning af selve avis-siderne. Første opdatering kan derfor tage lidt længere tid.")
    if st.button("Opdater aviser"):
        with st.spinner("Læser aviser… Netto-scanningen kan tage et par minutter første gang."):
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
        shown = data[data["Butik"].isin(stores)].copy()
        if only_organic:
            shown = shown[shown["Øko"] == True]

        if shown.empty:
            st.info("Ingen varer fundet med de valgte filtre.")
        else:
            shown["Vare_vis"] = shown["Vare"].fillna("").map(clean_flyer_product_name)
            shown["_Butik_sort"] = shown["Butik"].fillna("").map(danish_sort_key)
            shown["_Vare_sort"] = shown["Vare_vis"].fillna("").map(danish_sort_key)
            shown = shown.sort_values(["_Butik_sort", "_Vare_sort", "Pris"], kind="stable")

            if "Sikkerhed" not in shown.columns:
                shown["Sikkerhed"] = ""
            trusted = shown[~shown["Sikkerhed"].isin(["Mellem", "Lav"])].copy()
            possible = shown[shown["Sikkerhed"].isin(["Mellem", "Lav"])].copy()

            st.caption(f"{len(trusted)} sikre tilbud · {len(possible)} mulige OCR-fund · parser v{APP_VERSION}")

            for _, row in trusted.iterrows():
                store = str(row.get("Butik") or "")
                product = str(row.get("Vare_vis") or row.get("Vare") or "").strip()
                try:
                    price_txt = f"{float(row.get('Pris')):.2f} kr.".replace(".00", "")
                except Exception:
                    price_txt = "Pris ukendt"

                source_type = str(row.get("Type") or row.get("Avis") or "").strip()
                st.markdown(f"**{product or 'Ukendt vare'}**")
                st.write(f"{store} · **{price_txt}**")
                page = str(row.get("Side") or "").strip()
                meta = source_type + (f" · side {page}" if page else "")
                if meta:
                    st.caption(meta)
                st.divider()

            if not possible.empty:
                with st.expander(f"🟡 Mulige OCR-fund ({len(possible)})"):
                    st.caption("Disse er for usikre til at påvirke Prisrobotten.")
                    for _, row in possible.iterrows():
                        product = str(row.get("Vare_vis") or row.get("Vare") or "").strip()
                        store = str(row.get("Butik") or "")
                        try:
                            price_txt = f"{float(row.get('Pris')):.2f} kr.".replace(".00", "")
                        except Exception:
                            price_txt = "Pris ukendt"
                        st.write(f"**{product}** · {store} · {price_txt} · {row.get('Sikkerhed', '')}")

            with st.expander("🔎 Se tekniske avisdetaljer"):
                columns = ["Butik", "Vare_vis", "Pris", "Avis", "Side", "Type", "Kilde"]
                detail = shown[[c for c in columns if c in shown.columns]].rename(columns={"Vare_vis": "Vare"})
                st.dataframe(
                    detail,
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
                    # Til mig skal matche selve varen – ikke en bred avisbeskrivelse,
                    # som kan omtale flere helt forskellige produkter.
                    s = match_score(item, r["Vare"], "")
                    q_family = product_family(item, rules=load_habit_rules())
                    p_family = product_family(r["Vare"], rules=load_habit_rules())
                    same_family = (
                        q_family and p_family
                        and normalize(q_family) == normalize(p_family)
                    )
                    if s >= 0.55 or same_family:
                        candidates.append((max(s, 1.0 if same_family else s), r))
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
        ["Netto", "REMA 1000", "365discount", "Lidl", "føtex", "Nemlig.com"],
        index=None,
        placeholder="Vælg butik",
    )
    if text:
        parsed = parse_receipt_smart(text)
        st.markdown("### Varer fundet på bonen")
        if not parsed.empty:
            edited = st.data_editor(parsed, num_rows="dynamic", hide_index=True, use_container_width=True)
            if st.button("💾 Gem køb, priser og rabatter", type="primary", use_container_width=True):
                if not store:
                    st.warning("Vælg først hvilken butik bonen er fra.")
                else:
                    try:
                        n = save_purchase_history(edited, store)
                        st.success(f"{n} køb gemt i Supabase. Robotten husker nu de faktiske priser.")
                    except Exception as e:
                        st.error(f"Kunne ikke gemme: {e}")
        else:
            st.warning("Jeg kunne ikke finde sikre varelinjer endnu. Ret eventuelt bonteksten ovenfor og prøv igen.")

with tabs[5]:
    st.subheader("Dine vaner")
    history = load_purchase_history()

    if not history:
        st.info("Ingen gemte bonkøb endnu.")
    else:
        summary = habit_summary(history)
        if summary.empty:
            st.info("Ingen synlige vaner endnu.")
        else:
            st.caption("Tryk på en vare for at se bonhistorik og rette kategorien.")

            # Saml butikker under samme kategori, så én vare kun får ét foldbart kort.
            summary = summary.sort_values("Vare", key=lambda c: c.map(danish_sort_key), kind="stable")
            rules = load_habit_rules()
            hist_df = pd.DataFrame(history)
            hist_df["Grundvare"] = hist_df["item"].map(lambda x: product_family(x, rules=rules))

            for family, fam_rows in summary.groupby("Vare", sort=False):
                total_buys = int(fam_rows["Køb"].sum())
                habit_level = "Fast vane" if total_buys >= 4 else ("Mulig vane" if total_buys >= 2 else "Engangskøb")

                prices = pd.to_numeric(fam_rows["Typisk pris"], errors="coerce").dropna()
                typical = f"{float(prices.median()):.2f} kr." if not prices.empty else "–"
                shops = ", ".join(sorted(
                    {str(x).strip() for x in fam_rows["Butik"] if str(x).strip()},
                    key=danish_sort_key
                ))

                with st.expander(f"{str(family).capitalize()} · {habit_level} · {total_buys} køb"):
                    st.write(f"**Butik:** {shops or '–'}")
                    st.write(f"**Typisk pris:** {typical}")

                    family_history = hist_df[
                        hist_df["Grundvare"].astype(str).map(normalize) == normalize(family)
                    ].copy()

                    if not family_history.empty:
                        family_history["Dato_sort"] = pd.to_datetime(
                            family_history.get("purchased_at"), errors="coerce"
                        )
                        family_history = family_history.sort_values("Dato_sort", ascending=False)

                        st.markdown("#### 🧾 Bonhistorik")
                        hist_show = pd.DataFrame({
                            "Dato": family_history["Dato_sort"].dt.strftime("%d/%m/%Y"),
                            "Butik": family_history.get("store"),
                            "Bonlinje": family_history.get("item"),
                            "Betalt": pd.to_numeric(family_history.get("paid_price"), errors="coerce"),
                            "Normalpris": pd.to_numeric(family_history.get("normal_price"), errors="coerce"),
                            "Rabat": pd.to_numeric(family_history.get("discount"), errors="coerce"),
                        })
                        st.dataframe(hist_show, hide_index=True, use_container_width=True)

                        raw_variants = sorted(
                            {str(x).strip() for x in family_history["item"] if str(x).strip()},
                            key=danish_sort_key,
                        )

                        st.markdown("#### 🔗 Varenavne i kategorien")
                        for raw in raw_variants:
                            st.caption(f"↳ {raw}")

                        st.markdown("#### ✏️ Ret kategorien")
                        rename_key = "rename_" + re.sub(r"[^a-z0-9]+", "_", normalize(family))
                        new_name = st.text_input(
                            "Nyt kategorinavn",
                            value=str(family).capitalize(),
                            key=rename_key,
                        )
                        if st.button("💾 Omdøb kategori", key="save_" + rename_key):
                            if not new_name.strip():
                                st.warning("Skriv et kategorinavn.")
                            else:
                                try:
                                    # Gem reglen på hver rå bonvariant, så historikken bevares,
                                    # men alle varianter vises under det nye navn.
                                    for raw in raw_variants:
                                        save_habit_rule(raw, target_name=new_name.strip(), hidden=False)
                                    st.success(f"Kategorien hedder nu “{new_name.strip()}”.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Kunne ikke omdøbe kategorien: {e}")

                        move_variant = st.selectbox(
                            "Flyt en bonvariant",
                            [""] + raw_variants,
                            key="move_variant_" + rename_key,
                            help="Brug dette hvis en bonlinje er havnet i den forkerte kategori.",
                        )
                        target_options = [
                            x for x in canonical_shopping_items()
                            if normalize(x) != normalize(family)
                        ]
                        move_target = st.selectbox(
                            "Flyt til kategori",
                            [""] + target_options + ["➕ Ny kategori"],
                            key="move_target_" + rename_key,
                        )
                        custom_move = ""
                        if move_target == "➕ Ny kategori":
                            custom_move = st.text_input(
                                "Navn på ny kategori",
                                key="move_custom_" + rename_key,
                            )
                        final_target = custom_move.strip() if move_target == "➕ Ny kategori" else move_target
                        if st.button("↗️ Flyt bonvariant", key="move_btn_" + rename_key):
                            if not move_variant or not final_target:
                                st.warning("Vælg både bonvariant og kategori.")
                            else:
                                try:
                                    save_habit_rule(move_variant, target_name=final_target, hidden=False)
                                    st.success(f"“{move_variant}” er flyttet til “{final_target}”.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Kunne ikke flytte varen: {e}")

        raw_names = sorted(
            {str(x.get("item", "")).strip() for x in history if str(x.get("item", "")).strip()},
            key=danish_sort_key,
        )

        st.markdown("### 🧹 Flere rettelser")
        mode = st.radio(
            "Hvad vil du gøre?",
            ["Kategorisér nye bonnavne", "Fjern fra vaner", "Fortryd manuel rettelse", "Slet varehistorik permanent"],
            horizontal=False,
        )

        if mode == "Kategorisér nye bonnavne":
            rules = load_habit_rules()
            pending_names = []
            for name in raw_names:
                rule = rules.get(normalize(name), {})
                already_handled = bool(rule.get("target_name")) or bool(rule.get("hidden"))
                if not already_handled:
                    pending_names.append(name)

            if pending_names:
                st.caption(f"{len(pending_names)} varevariant(er) mangler stadig at blive kategoriseret.")
                chosen = st.multiselect("Vælg bonvarianter", pending_names)
                existing_targets = canonical_shopping_items()
                target_choice = st.selectbox(
                    "Skal høre under…",
                    ["➕ Nyt grundvarenavn"] + existing_targets,
                )
                custom_target = ""
                if target_choice == "➕ Nyt grundvarenavn":
                    custom_target = st.text_input("Nyt grundvarenavn", placeholder="fx Græsk yoghurt")
                target = custom_target.strip() if target_choice == "➕ Nyt grundvarenavn" else target_choice
                if st.button("💾 Gem kategori", type="primary"):
                    if not chosen or not target:
                        st.warning("Vælg mindst én bonvariant og en kategori.")
                    else:
                        try:
                            for item in chosen:
                                save_habit_rule(item, target_name=target, hidden=False)
                            st.success("Kategorien er gemt.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Kunne ikke gemme: {e}")
            else:
                st.success("Alle bonvarianter er kategoriseret ✅")

        elif mode == "Fjern fra vaner":
            chosen = st.multiselect("Vælg varer der ikke skal tælle som vaner", raw_names)
            if st.button("🙈 Fjern fra Vaner", type="primary"):
                if not chosen:
                    st.warning("Vælg mindst én vare.")
                else:
                    try:
                        for item in chosen:
                            save_habit_rule(item, target_name=None, hidden=True)
                        st.success("Varen er skjult i Vaner, men historikken er bevaret.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Kunne ikke gemme ændringen: {e}")

        elif mode == "Fortryd manuel rettelse":
            rules = load_habit_rules()
            rule_names = sorted(
                [r.get("source_item") or key for key, r in rules.items()],
                key=danish_sort_key,
            )
            chosen = st.multiselect("Vælg manuelle rettelser der skal nulstilles", rule_names)
            if st.button("↩️ Brug automatik igen"):
                if not chosen:
                    st.warning("Vælg mindst én rettelse.")
                else:
                    try:
                        for item in chosen:
                            delete_habit_rule(item)
                        st.success("Den manuelle rettelse er fjernet.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Kunne ikke nulstille: {e}")

        else:
            chosen = st.selectbox("Vælg rå bonvare", [""] + raw_names)
            confirm = st.checkbox("Jeg forstår, at købshistorikken for denne vare slettes permanent.")
            st.caption("Tilbudsavis-priser slettes ikke. Kun dine bonkøb og bonbaserede prisobservationer for varen.")
            if st.button("🗑️ Slet permanent", type="primary"):
                if not chosen or not confirm:
                    st.warning("Vælg en vare og markér bekræftelsen.")
                else:
                    try:
                        permanently_delete_raw_item(chosen)
                        st.success("Varehistorikken er slettet permanent.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Kunne ikke slette: {e}")

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

st.caption(f"Øko-robot v{APP_VERSION} · {APP_VERSION_TEXT}")
