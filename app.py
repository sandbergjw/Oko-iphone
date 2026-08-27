import streamlit as st
import pandas as pd
from PIL import Image

st.set_page_config(page_title="Øko-robot",page_icon="🥬",layout="centered")
st.title("🥬 Øko-robot")
st.caption("v0.9 · tilbudsmatch + boner fra Fotos")

OFFERS = pd.DataFrame([
["Netto","Økologisk letmælk 1 L",14.95],
["REMA 1000","Økologisk letmælk 1 L",13.95],
["365discount","Økologisk letmælk 1 L",12.95],
["føtex","Økologisk letmælk 1 L",15.95],
["Nemlig.com","Økologisk letmælk 1 L",13.50],
["Netto","Økologiske æg 10 stk",29.95],
["REMA 1000","Økologiske æg 10 stk",27.95],
["365discount","Økologiske æg 10 stk",24.95],
["føtex","Økologiske æg 10 stk",29.95],
["Nemlig.com","Økologiske æg 10 stk",28.00],
["Netto","Økologiske bananer",18.95],
["REMA 1000","Økologiske bananer",14.95],
["365discount","Økologiske bananer",16.95],
["føtex","Økologiske bananer",19.95],
["Nemlig.com","Økologiske bananer",17.95],
["Netto","Økologisk hakket oksekød 8-12%",55.00],
["REMA 1000","Økologisk hakket oksekød 8-12%",59.95],
["365discount","Økologisk hakket oksekød 8-12%",57.00],
["føtex","Økologisk pasta 500 g",18.00],
["Nemlig.com","Økologisk pasta 500 g",16.95],
], columns=["Butik","Vare","Pris"])

ALIASES = {
    "mælk":["mælk","letmælk","sødmælk","minimælk"],
    "æg":["æg"],
    "banan":["banan","bananer"],
    "hakket kød":["hakket oksekød","hakket kød","oksekød"],
    "hakket oksekød":["hakket oksekød","oksekød"],
    "pasta":["pasta","spaghetti","penne","fusilli"],
}

def norm(s):
    return str(s).lower().strip()

def matches(query, product):
    q=norm(query)
    p=norm(product)
    if q in p:
        return True
    for key, aliases in ALIASES.items():
        if q == key or q in aliases:
            return any(a in p for a in aliases)
    words=[w for w in q.split() if len(w)>2]
    return bool(words) and all(w in p for w in words)

def find_matches(items):
    rows=[]
    for item in items:
        m=OFFERS[OFFERS["Vare"].apply(lambda x: matches(item,x))]
        if m.empty:
            rows.append([item,"Ikke fundet","",None])
        else:
            r=m.sort_values("Pris").iloc[0]
            rows.append([item,r["Butik"],r["Vare"],float(r["Pris"])])
    return pd.DataFrame(rows,columns=["Du mangler","Bedste butik","Matchet tilbud","Pris"])

home,need,deals,receipt = st.tabs(["🏠","📝 Jeg mangler","🔥 Tilbud","📸 Bon"])

with home:
    st.success("Din økologiske indkøbshjælper")
    st.write("Skriv hvad du mangler, så matcher robotten det med relevante tilbud.")
    st.info("Priserne i denne prototype er stadig testdata.")

with need:
    st.subheader("Hvad mangler du?")
    txt=st.text_area(
        "Skriv én varetype pr. linje",
        "mælk\næg\npasta\nhakket kød",
        height=180
    )
    items=[x.strip() for x in txt.splitlines() if x.strip()]
    if st.button("Match med tilbud",type="primary"):
        result=find_matches(items)
        st.dataframe(result,hide_index=True,use_container_width=True)
        total=result["Pris"].fillna(0).sum()
        if total:
            st.metric("Pris for fundne tilbud",f"{total:.2f} kr.")
        missing=result[result["Bedste butik"]=="Ikke fundet"]
        if not missing.empty:
            st.warning("Nogle varetyper havde ikke et match i de aktuelle data.")

with deals:
    st.subheader("Øko-tilbud")
    st.dataframe(OFFERS.sort_values(["Vare","Pris"]),hide_index=True,use_container_width=True)

with receipt:
    st.subheader("Bon")
    camera=st.camera_input("📷 Tag billede af bon")
    upload=st.file_uploader(
        "🖼️ Vælg bon fra Fotos",
        type=["jpg","jpeg","png","webp"]
    )
    source=camera or upload
    if source:
        st.image(Image.open(source),caption="Valgt bon",use_container_width=True)
        st.success("Bonen er klar til aflæsning i OCR-versionen.")

st.caption("Øko-robot v0.9")
