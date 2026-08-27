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
st.caption("v1.1 · tilbudsaviser først")

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
    t = str(text).lower()
    return any(x in t for x in [
        "økolog", " øko", "øgo", "änglamark", "salling øko", "365 øko"
    ])


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
    sb = supabase_client()
    if sb:
        try:
            rows = sb.table("receipt_items").select("item").execute().data or []
            result = {}
            for r in rows:
                k = normalize(r.get("item", ""))
                if k:
                    result[k] = result.get(k, 0) + 1
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

        if not candidates:
            rows.append({
                "Du mangler": item,
                "Butik": "Ikke fundet",
                "Tilbud": "",
                "Pris": None,
                "Kilde-type": "",
            })
            continue

        candidates.sort(key=lambda x: (-x[0], x[1]["Pris"]))
        s, r = candidates[0]
        rows.append({
            "Du mangler": item,
            "Butik": r["Butik"],
            "Tilbud": r["Vare"],
            "Pris": r["Pris"],
            "Kilde-type": r["Type"],
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


if "flyer_data" not in st.session_state:
    st.session_state["flyer_data"] = pd.DataFrame()
if "source_status" not in st.session_state:
    st.session_state["source_status"] = []

tabs = st.tabs(["🏠", "📝 Jeg mangler", "📰 Aviser", "🎯 Til mig", "📸 Bon", "🧠 Vaner", "⚙️"])

with tabs[0]:
    st.success("Nu læser robotten tilbudsaviser – ikke Netto+")
    st.write(
        "Netto, REMA 1000, Lidl, føtex og 365discount behandles som **tilbudsaviser**. "
        "Nemlig.com står separat som **online tilbud**, fordi Nemlig ikke har en klassisk ugeavis."
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

    if st.button("Match mod tilbudsaviser", type="primary"):
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
        parsed = parse_receipt(text)
        if not parsed.empty:
            edited = st.data_editor(parsed, num_rows="dynamic", hide_index=True, use_container_width=True)
            if st.button("Gem bon og lær", type="primary"):
                n, where = save_habits(edited["Vare"].tolist(), store)
                st.success(f"{n} varer gemt via {where}.")

with tabs[5]:
    st.subheader("Det robotten har lært")
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
    st.caption("Netto+ og andre medlemsprogrammer er ikke datakilden i denne version.")

st.caption("Øko-robot v1.1 · flyer-first")
