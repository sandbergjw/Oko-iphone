import streamlit as st
import pandas as pd
from PIL import Image

st.set_page_config(page_title="Øko-robot",page_icon="🥬",layout="centered")
st.title("🥬 Øko-robot")
st.caption("Mobil prototype · Netto · REMA 1000 · 365discount · føtex · Nemlig.com")

offers = pd.DataFrame([
["Netto","Letmælk",14.95],["REMA 1000","Letmælk",13.95],["365discount","Letmælk",12.95],["føtex","Letmælk",15.95],["Nemlig.com","Letmælk",13.50],
["Netto","Æg 10 stk",29.95],["REMA 1000","Æg 10 stk",27.95],["365discount","Æg 10 stk",24.95],["føtex","Æg 10 stk",29.95],["Nemlig.com","Æg 10 stk",28.00],
["Netto","Bananer",18.95],["REMA 1000","Bananer",14.95],["365discount","Bananer",16.95],["føtex","Bananer",19.95],["Nemlig.com","Bananer",17.95],
],columns=["Butik","Vare","Pris"])

home,liste,tilbud,bon=st.tabs(["🏠 Hjem","🛒 Liste","🔥 Tilbud","📸 Bon"])

with home:
    st.success("Din økologiske indkøbshjælper")
    st.write("Sammenligner øko-priser og bliver senere koblet til dine boner og indkøbsvaner.")
    st.info("Priserne i denne første online-version er testdata.")

with liste:
    txt=st.text_area("Én vare pr. linje","Letmælk\nÆg 10 stk\nBananer")
    if st.button("Find billigste øko-køb",type="primary"):
        total=0
        for item in [x.strip() for x in txt.splitlines() if x.strip()]:
            m=offers[offers["Vare"].str.lower()==item.lower()]
            if m.empty:
                st.warning(f"{item}: ikke fundet")
            else:
                r=m.sort_values("Pris").iloc[0]
                total+=r["Pris"]
                st.write(f"**{item}** → {r['Butik']} · {r['Pris']:.2f} kr.")
        st.metric("Samlet pris",f"{total:.2f} kr.")

with tilbud:
    st.subheader("Bedste øko-priser")
    best=offers.sort_values("Pris").groupby("Vare",as_index=False).first()
    st.dataframe(best,hide_index=True,use_container_width=True)

with bon:
    st.subheader("Scan bon")
    up=st.file_uploader("Tag et billede eller vælg fra Fotos",type=["jpg","jpeg","png","webp"])
    if up:
        st.image(Image.open(up),use_container_width=True)
        st.success("Bonen er modtaget. Automatisk AI-aflæsning kobles på i næste version.")

st.caption("Øko-robot prototype")
