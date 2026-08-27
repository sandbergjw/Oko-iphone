import re, json, uuid
from datetime import datetime
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
st.caption("v0.6 · øko-tilbud + boner + permanent lagring")

HEADERS={"User-Agent":"Mozilla/5.0"}
STORE_LINKS={
    "Netto":"https://netto.dk/netto-avisen/",
    "REMA 1000":"https://rema1000.dk/",
    "365discount":"https://365discount.coop.dk/365avis/",
    "føtex":"https://www.foetex.dk/tilbudsavis/",
    "Nemlig.com":"https://www.nemlig.com/tilbud",
}

DEMO=pd.DataFrame([
["Netto","Letmælk",14.95],["REMA 1000","Letmælk",13.95],["365discount","Letmælk",12.95],["føtex","Letmælk",15.95],["Nemlig.com","Letmælk",13.50],
["Netto","Æg 10 stk",29.95],["REMA 1000","Æg 10 stk",27.95],["365discount","Æg 10 stk",24.95],["føtex","Æg 10 stk",29.95],["Nemlig.com","Æg 10 stk",28.00],
["Netto","Bananer",18.95],["REMA 1000","Bananer",14.95],["365discount","Bananer",16.95],["føtex","Bananer",19.95],["Nemlig.com","Bananer",17.95],
["Netto","Hakket oksekød 8-12%",55.00],["REMA 1000","Hakket oksekød 8-12%",59.95],["365discount","Hakket oksekød 8-12%",57.00],
],columns=["Butik","Vare","Pris"])

LOCAL_FILE=Path("habits.json")

def normalize(s):
    s=str(s).lower().strip()
    for x in ["økologisk ","øko ","øgo ","änglamark "]:
        s=s.replace(x,"")
    return " ".join(s.split())

def supabase_client():
    if create_client is None:
        return None
    try:
        url=st.secrets["SUPABASE_URL"]
        key=st.secrets["SUPABASE_KEY"]
        return create_client(url,key)
    except Exception:
        return None

def storage_mode():
    return "Supabase" if supabase_client() else "Lokal fallback"

def load_habits():
    sb=supabase_client()
    if sb:
        try:
            res=sb.table("receipt_items").select("item").execute()
            counts={}
            for row in (res.data or []):
                k=normalize(row.get("item",""))
                if k:
                    counts[k]=counts.get(k,0)+1
            return counts
        except Exception:
            pass
    if LOCAL_FILE.exists():
        try: return json.loads(LOCAL_FILE.read_text(encoding="utf-8"))
        except: pass
    return {}

def save_receipt_items(items,store=""):
    sb=supabase_client()
    if sb:
        payload=[]
        now=datetime.utcnow().isoformat()
        for item in items:
            if not item: continue
            payload.append({
                "id":str(uuid.uuid4()),
                "item":item,
                "normalized_item":normalize(item),
                "store":store,
                "created_at":now
            })
        if payload:
            sb.table("receipt_items").insert(payload).execute()
        return len(payload), "Supabase"
    habits=load_habits()
    n=0
    for item in items:
        k=normalize(item)
        if not k: continue
        habits[k]=habits.get(k,0)+1
        n+=1
    LOCAL_FILE.write_text(json.dumps(habits,ensure_ascii=False,indent=2),encoding="utf-8")
    return n, "lokal fallback"

def is_organic(s):
    t=str(s).lower()
    return any(x in t for x in ["økolog","øko","øgo","änglamark"])

def parse_price(s):
    m=re.search(r'(?<!\d)(\d{1,3}(?:[,.]\d{1,2})?)\s*(?:kr\.?|,-)',s,re.I)
    if not m: return None
    try: return float(m.group(1).replace(",","."))
    except: return None

@st.cache_data(ttl=1800)
def fetch_organic_candidates(store,url):
    r=requests.get(url,headers=HEADERS,timeout=15)
    r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    rows=[]
    for el in soup.find_all(["article","li","div"]):
        txt=" ".join(el.stripped_strings)
        if len(txt)<5 or len(txt)>500 or not is_organic(txt):
            continue
        p=parse_price(txt)
        if p is None: continue
        chunks=[x.strip() for x in el.stripped_strings if len(x.strip())>2]
        name=chunks[0][:120] if chunks else txt[:120]
        rows.append([store,name,p])
    if not rows:
        return pd.DataFrame(columns=["Butik","Vare","Pris"])
    return pd.DataFrame(rows,columns=["Butik","Vare","Pris"]).drop_duplicates().head(150)

def cheapest(data,items):
    out=[]
    for item in items:
        k=normalize(item)
        m=data[data["Vare"].map(normalize).str.contains(k,regex=False,na=False)]
        if m.empty: out.append([item,"Ikke fundet","",None])
        else:
            r=m.sort_values("Pris").iloc[0]
            out.append([item,r["Butik"],r["Vare"],float(r["Pris"])])
    return pd.DataFrame(out,columns=["Ønsket","Butik","Vare","Pris"])

def receipt_lines(text):
    rows=[]
    for raw in text.splitlines():
        line=raw.strip()
        if len(line)<2: continue
        if any(x in line.lower() for x in ["total","visa","moms","dankort","kontant","retur"]): continue
        m=re.search(r'(-?\d+[.,]\d{2})\s*$',line)
        item=line[:m.start()].strip(" .:-") if m else line
        if item: rows.append({"Vare":item})
    return pd.DataFrame(rows)

tabs=st.tabs(["🏠","🛒","🔥","📸","🧠","💾","🔄"])

with tabs[0]:
    habits=load_habits()
    st.success("Din personlige øko-indkøbshjælper")
    a,b=st.columns(2)
    a.metric("Butikker",5)
    b.metric("Lærte varer",len(habits))
    st.caption(f"Lagring: {storage_mode()}")
    if habits:
        st.subheader("Det køber du oftest")
        for item,n in sorted(habits.items(),key=lambda x:x[1],reverse=True)[:6]:
            st.write(f"• **{item}** · {n} køb")

with tabs[1]:
    st.subheader("Min indkøbsliste")
    habits=load_habits()
    suggestion="\n".join([x[0] for x in sorted(habits.items(),key=lambda x:x[1],reverse=True)[:5]])
    default=suggestion or "Letmælk\nÆg 10 stk\nBananer"
    txt=st.text_area("Én vare pr. linje",default,height=160)
    if st.button("Find billigste øko-køb",type="primary"):
        plan=cheapest(DEMO,[x.strip() for x in txt.splitlines() if x.strip()])
        st.dataframe(plan,hide_index=True,use_container_width=True)
        st.metric("Samlet pris",f"{plan['Pris'].fillna(0).sum():.2f} kr.")

with tabs[2]:
    st.subheader("Live øko-kandidater")
    store=st.selectbox("Butik",list(STORE_LINKS))
    if st.button("Hent aktuelle kandidater",type="primary"):
        try:
            live=fetch_organic_candidates(store,STORE_LINKS[store])
            if live.empty:
                st.warning("Ingen sikre pris/øko-kandidater blev fundet.")
            else:
                st.success(f"{len(live)} kandidater fundet")
                st.dataframe(live.sort_values("Pris"),hide_index=True,use_container_width=True)
        except Exception:
            st.warning("Denne butik kunne ikke parses stabilt lige nu.")

with tabs[3]:
    st.subheader("Bon")
    img=st.file_uploader("Tag billede eller vælg fra Fotos",type=["jpg","jpeg","png","webp"])
    if img:
        st.image(Image.open(img),use_container_width=True)
        st.success("Billedet er modtaget.")
        st.caption("Automatisk billed-OCR kommer i næste lag.")
    txt=st.text_area("Indsæt bontekst",placeholder="ØKO LETMÆLK 1L  13,95\nBANANER ØKO  16,50",height=150)
    store=st.selectbox("Butik for bonen",["","Netto","REMA 1000","365discount","føtex","Nemlig.com"])
    if txt:
        parsed=receipt_lines(txt)
        edited=st.data_editor(parsed,num_rows="dynamic",use_container_width=True)
        if st.button("Gem bon og lær",type="primary"):
            items=[str(x).strip() for x in edited["Vare"].tolist() if str(x).strip()]
            n,where=save_receipt_items(items,store)
            st.success(f"{n} varer gemt via {where}.")

with tabs[4]:
    st.subheader("Mine vaner")
    habits=load_habits()
    if not habits:
        st.info("Ingen gemte vaner endnu.")
    else:
        rows=[]
        for item,n in sorted(habits.items(),key=lambda x:x[1],reverse=True):
            level="Fast vane" if n>=4 else ("Mulig vane" if n>=2 else "Engangskøb")
            rows.append([item,n,level])
        st.dataframe(pd.DataFrame(rows,columns=["Vare","Køb","Vane"]),hide_index=True,use_container_width=True)

with tabs[5]:
    st.subheader("Permanent lagring")
    if supabase_client():
        st.success("Supabase er forbundet. Dine bonvarer kan gemmes permanent.")
    else:
        st.warning("Supabase er ikke sat op endnu. Appen bruger lokal fallback.")
        st.markdown("""
**Når du har oprettet Supabase:**
1. Opret tabellen `receipt_items`.
2. Gå til Streamlit → App settings → Secrets.
3. Tilføj:
```toml
SUPABASE_URL="din-url"
SUPABASE_KEY="din-anon-key"
```
4. Gem og genstart appen.
        """)

with tabs[6]:
    st.subheader("Datastatus")
    st.write("🟢 Netto – live-parser aktiv")
    st.write("🟡 REMA 1000 – generisk parser")
    st.write("🟡 365discount – generisk parser")
    st.write("🟡 føtex – generisk parser")
    st.write("🟡 Nemlig.com – generisk parser")

st.caption("Øko-robot v0.6")
