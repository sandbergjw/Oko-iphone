import streamlit as st
import pandas as pd

st.set_page_config(page_title="Øko-robot",page_icon="🥬",layout="centered")
st.title("🥬 Øko-robot")
st.caption("v0.8 · personlige øko-tilbud")

if "vaner" not in st.session_state:
    st.session_state.vaner={}

offers=pd.DataFrame([
["Netto","Letmælk",14.95],["REMA 1000","Letmælk",13.95],["365discount","Letmælk",12.95],
["føtex","Letmælk",15.95],["Nemlig.com","Letmælk",13.50],
["Netto","Æg 10 stk",29.95],["REMA 1000","Æg 10 stk",27.95],["365discount","Æg 10 stk",24.95],
["Netto","Bananer",18.95],["REMA 1000","Bananer",14.95],["365discount","Bananer",16.95],
],columns=["Butik","Vare","Pris"])

def norm(x): return str(x).lower().replace("øko ","").replace("økologisk ","").strip()
def score(a,b):
    A=set(norm(a).split()); B=set(norm(b).split())
    return len(A&B)/len(A|B) if A and B else 0

home,personal,listtab,receipt=st.tabs(["🏠","🎯","🛒","📸"])

with home:
    st.success("Din personlige øko-indkøbshjælper")
    st.write("Nyt: Robotten kan matche dine faste varer med ugens øko-tilbud.")
    st.metric("Lærte varer",len(st.session_state.vaner))

with personal:
    st.subheader("Tilbud til dig")
    if not st.session_state.vaner:
        st.info("Tilføj nogle varer under 📸 Bon først.")
    else:
        rows=[]
        for item,n in st.session_state.vaner.items():
            candidates=[]
            for _,r in offers.iterrows():
                s=score(item,r["Vare"])
                if s>=0.35:candidates.append((s,r))
            if candidates:
                _,r=max(candidates,key=lambda x:(x[0],-x[1]["Pris"]))
                rows.append([item,n,r["Butik"],r["Vare"],r["Pris"]])
        if rows:
            st.dataframe(pd.DataFrame(rows,columns=["Din vare","Køb","Butik","Tilbud","Pris"]),hide_index=True,use_container_width=True)
        else: st.info("Ingen match endnu.")

with listtab:
    default="\n".join(x[0] for x in sorted(st.session_state.vaner.items(),key=lambda x:x[1],reverse=True)[:6])
    st.text_area("Min indkøbsliste",default or "Letmælk\nÆg 10 stk\nBananer",height=180)

with receipt:
    st.subheader("Lær fra en bon")
    st.camera_input("Tag billede af bonen")
    txt=st.text_area("Indtast/indsæt varelinjer – én pr. linje",height=180)
    if st.button("Gem og lær",type="primary"):
        for x in [x.strip() for x in txt.splitlines() if x.strip()]:
            k=norm(x); st.session_state.vaner[k]=st.session_state.vaner.get(k,0)+1
        st.success("Varerne er lært i denne session.")
        st.rerun()

st.caption("v0.8 · denne kompakte build bevarer fokus på personlig tilbudsmatching")
