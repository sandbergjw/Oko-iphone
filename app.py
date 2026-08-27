import io, json, re, uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from PIL import Image

try:
    from supabase import create_client
except Exception:
    create_client = None

st.set_page_config(page_title='Øko-robot', page_icon='🥬', layout='centered')
st.title('🥬 Øko-robot')
st.caption('v1.0 · rigtig mobil prototype')

HEADERS={'User-Agent':'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile Safari/604.1'}
STORES={
 'Netto':{'url':'https://netto.dk/netto-avisen/','trust':'live'},
 'Lidl':{'url':'https://www.lidl.dk/c/c2428','trust':'live'},
 'REMA 1000':{'url':'https://rema1000.dk/','trust':'candidate'},
 '365discount':{'url':'https://365discount.coop.dk/365avis/','trust':'candidate'},
 'føtex':{'url':'https://www.foetex.dk/tilbudsavis/','trust':'candidate'},
 'Nemlig.com':{'url':'https://www.nemlig.com/tilbud','trust':'candidate'},
}
LOCAL=Path('habits.json')

def normalize(text):
    s=str(text).lower().strip()
    for x in ['økologiske','økologisk','økologi','øko','øgo','änglamark']:
        s=s.replace(x,' ')
    s=re.sub(r'[^a-z0-9æøå%\- ]+',' ',s)
    return ' '.join(s.split())

def organic(text):
    t=str(text).lower()
    return any(k in t for k in ['økolog','øko','øgo','änglamark'])

def parse_price(text):
    pats=[r'(\d{1,4})\s*[.,]\s*(\d{2})\s*(?:kr\.?)',r'(\d{1,4})\s*,\-',r'(\d{1,4})\s*\.\-',r'(\d{1,4})\s+kr\b']
    for pat in pats:
        m=re.search(pat,text,re.I)
        if m:
            return float(f'{m.group(1)}.{m.group(2)}') if len(m.groups())==2 and m.group(2) else float(m.group(1))
    return None

def qty(text):
    t=text.lower().replace(',','.')
    for pat,unit,factor in [(r'(\d+(?:\.\d+)?)\s*kg\b','kg',1.0),(r'(\d+(?:\.\d+)?)\s*g\b','kg',.001),(r'(\d+(?:\.\d+)?)\s*l(?:iter)?\b','l',1.0),(r'(\d+(?:\.\d+)?)\s*ml\b','l',.001),(r'(\d+)\s*stk\b','stk',1.0)]:
        m=re.search(pat,t)
        if m:return float(m.group(1))*factor,unit
    return None,None

def unit_price(price,text):
    q,u=qty(text)
    return (round(price/q,2),u) if q and price else (None,u)

def clean_name(text):
    text=re.sub(r'\s+',' ',text).strip()
    return text[:150].strip(' -|')

def sb_client():
    if create_client is None:return None
    try:return create_client(st.secrets['SUPABASE_URL'],st.secrets['SUPABASE_KEY'])
    except Exception:return None

def ocr_key():
    try:return st.secrets['OCRSPACE_API_KEY']
    except Exception:return None

def load_habits():
    sb=sb_client()
    if sb:
        try:
            rows=sb.table('receipt_items').select('item').execute().data or []
            out={}
            for r in rows:
                k=normalize(r.get('item',''))
                if k:out[k]=out.get(k,0)+1
            return out
        except Exception:pass
    if LOCAL.exists():
        try:return json.loads(LOCAL.read_text(encoding='utf-8'))
        except Exception:pass
    return {}

def save_items(items,store):
    items=[str(x).strip() for x in items if str(x).strip()]
    sb=sb_client()
    if sb:
        now=datetime.now(timezone.utc).isoformat()
        payload=[{'id':str(uuid.uuid4()),'item':x,'normalized_item':normalize(x),'store':store or None,'created_at':now} for x in items]
        if payload:sb.table('receipt_items').insert(payload).execute()
        return len(payload),'Supabase'
    h=load_habits()
    for x in items:
        k=normalize(x)
        if k:h[k]=h.get(k,0)+1
    LOCAL.write_text(json.dumps(h,ensure_ascii=False,indent=2),encoding='utf-8')
    return len(items),'lokal fallback'

@st.cache_data(ttl=1800,show_spinner=False)
def scrape_netto():
    r=requests.get(STORES['Netto']['url'],headers=HEADERS,timeout=20);r.raise_for_status()
    soup=BeautifulSoup(r.text,'html.parser');rows=[]
    for h in soup.find_all(['h4','h5','h6']):
        name=' '.join(h.stripped_strings)
        if not name or not organic(name):continue
        parent=h
        for _ in range(4):
            if parent.parent:parent=parent.parent
        context=' '.join(parent.stripped_strings)
        p=parse_price(context)
        if p is None:
            m=re.search(r'(?<!\d)(\d{1,3})\s*[.]\s*-',context)
            if m:p=float(m.group(1))
        if p is None:continue
        up,u=unit_price(p,context)
        rows.append({'Butik':'Netto','Vare':clean_name(name),'Pris':p,'Enhedspris':up,'Enhed':u,'Kilde':STORES['Netto']['url'],'Status':'Live'})
    return pd.DataFrame(rows).drop_duplicates(subset=['Vare','Pris']) if rows else pd.DataFrame()

@st.cache_data(ttl=1800,show_spinner=False)
def scrape_lidl():
    r=requests.get(STORES['Lidl']['url'],headers=HEADERS,timeout=20);r.raise_for_status()
    soup=BeautifulSoup(r.text,'html.parser');links=[]
    for a in soup.find_all('a',href=True):
        href=a['href'];txt=' '.join(a.stripped_strings)
        if '/p/' in href or organic(txt):links.append(urljoin(STORES['Lidl']['url'],href))
    rows=[]
    for url in list(dict.fromkeys(links))[:50]:
        try:
            rr=requests.get(url,headers=HEADERS,timeout=10)
            if not rr.ok:continue
            p=BeautifulSoup(rr.text,'html.parser');text=' '.join(p.stripped_strings)
            if not organic(text):continue
            h1=p.find('h1');name=' '.join(h1.stripped_strings) if h1 else ''
            pr=parse_price(text)
            if pr is None:
                m=re.search(r'(?<!\d)(\d{1,3})\s*,\-',text)
                if m:pr=float(m.group(1))
            if not name or pr is None:continue
            up,u=unit_price(pr,text)
            rows.append({'Butik':'Lidl','Vare':clean_name(name),'Pris':pr,'Enhedspris':up,'Enhed':u,'Kilde':url,'Status':'Live'})
        except Exception:continue
    return pd.DataFrame(rows).drop_duplicates(subset=['Vare','Pris']) if rows else pd.DataFrame()

@st.cache_data(ttl=1800,show_spinner=False)
def scrape_candidate(store):
    r=requests.get(STORES[store]['url'],headers=HEADERS,timeout=20);r.raise_for_status()
    soup=BeautifulSoup(r.text,'html.parser');rows=[]
    for el in soup.find_all(['article','li','div']):
        text=' '.join(el.stripped_strings)
        if not (5<len(text)<450 and organic(text)):continue
        pr=parse_price(text)
        if pr is None:continue
        chunks=[x.strip() for x in el.stripped_strings if len(x.strip())>2]
        name=clean_name(chunks[0] if chunks else text)
        if len(name)<3:continue
        up,u=unit_price(pr,text)
        rows.append({'Butik':store,'Vare':name,'Pris':pr,'Enhedspris':up,'Enhed':u,'Kilde':STORES[store]['url'],'Status':'Kandidat'})
    return pd.DataFrame(rows).drop_duplicates(subset=['Vare','Pris']).head(150) if rows else pd.DataFrame()

def fetch_store(store):
    return scrape_netto() if store=='Netto' else scrape_lidl() if store=='Lidl' else scrape_candidate(store)

def fetch_all():
    frames=[];status=[]
    for store in STORES:
        try:
            df=fetch_store(store);n=len(df);status.append((store,n,'OK' if n else 'Ingen sikre fund'))
            if n:frames.append(df)
        except Exception:status.append((store,0,'Fejl'))
    cols=['Butik','Vare','Pris','Enhedspris','Enhed','Kilde','Status']
    return (pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=cols)),status

ALIASES={'mælk':['mælk','letmælk','minimælk','sødmælk'],'æg':['æg'],'bananer':['banan','bananer'],'banan':['banan','bananer'],'smør':['smør'],'pasta':['pasta','spaghetti','penne','fusilli'],'rugbrød':['rugbrød'],'hakket kød':['hakket oksekød','hakket kød'],'hakket oksekød':['hakket oksekød'],'kylling':['kylling','kyllingebryst','kyllingefilet'],'gulerødder':['gulerod','gulerødder'],'kartofler':['kartoffel','kartofler']}

def match_score(query,product):
    q=normalize(query);p=normalize(product)
    if not q or not p:return 0.0
    aliases=ALIASES.get(q,[q])
    if any(a in p for a in aliases):return 1.0
    qa=set(q.split());pa=set(p.split())
    return len(qa&pa)/len(qa|pa) if qa and pa else 0.0

def wishlist(data,items):
    out=[]
    for item in items:
        cand=[]
        for _,r in data.iterrows():
            s=match_score(item,r['Vare'])
            if s>=.34:cand.append((s,r))
        if not cand:out.append({'Du mangler':item,'Butik':'Ikke fundet','Tilbud':'','Pris':None,'Status':''})
        else:
            cand.sort(key=lambda x:(-x[0],x[1]['Pris']));_,r=cand[0]
            out.append({'Du mangler':item,'Butik':r['Butik'],'Tilbud':r['Vare'],'Pris':r['Pris'],'Status':r['Status']})
    return pd.DataFrame(out)

def personal(data,h):
    rows=[]
    for item,count in sorted(h.items(),key=lambda x:x[1],reverse=True):
        for _,r in data.iterrows():
            s=match_score(item,r['Vare'])
            if s>=.45:rows.append({'Din vare':item,'Køb':count,'Butik':r['Butik'],'Tilbud':r['Vare'],'Pris':r['Pris'],'Match':round(s*100),'Status':r['Status']})
    if not rows:return pd.DataFrame()
    df=pd.DataFrame(rows)
    return df.sort_values(['Din vare','Match','Pris'],ascending=[True,False,True]).groupby('Din vare',as_index=False).first().sort_values(['Køb','Pris'],ascending=[False,True])

def preprocess(upload):
    img=Image.open(upload).convert('RGB')
    if img.width>1600:
        ratio=1600/img.width;img=img.resize((1600,int(img.height*ratio)))
    out=io.BytesIO();img.save(out,'JPEG',quality=88,optimize=True)
    return img,out.getvalue()

def run_ocr(data):
    key=ocr_key()
    if not key:raise RuntimeError('OCRSPACE_API_KEY mangler i Streamlit Secrets.')
    r=requests.post('https://api.ocr.space/parse/image',files={'file':('receipt.jpg',data,'image/jpeg')},data={'apikey':key,'language':'auto','detectOrientation':'true','scale':'true','isTable':'true','OCREngine':'2'},timeout=60)
    r.raise_for_status();j=r.json()
    if j.get('IsErroredOnProcessing'):raise RuntimeError(str(j.get('ErrorMessage') or 'OCR-fejl'))
    text='\n'.join(x.get('ParsedText','') for x in j.get('ParsedResults',[]))
    if not text.strip():raise RuntimeError('Ingen tekst fundet.')
    return text

def parse_receipt(text):
    skip=['total','visa','moms','dankort','kontant','betaling','subtotal','at betale'];rows=[]
    for raw in text.splitlines():
        line=re.sub(r'\s+',' ',raw).strip()
        if len(line)<2 or any(k in line.lower() for k in skip):continue
        m=re.search(r'(-?\d{1,4}[,.]\d{2})\s*-?\s*$',line);pr=float(m.group(1).replace(',','.')) if m else None
        item=line[:m.start()].strip(' .:-') if m else line
        if re.search(r'[A-Za-zÆØÅæøå]',item) and len(item)>=2:rows.append({'Vare':item[:120],'Pris':pr})
    return pd.DataFrame(rows)

if 'live_data' not in st.session_state:st.session_state.live_data=pd.DataFrame()
if 'source_status' not in st.session_state:st.session_state.source_status=[]

tabs=st.tabs(['🏠','📝 Jeg mangler','🎯 Til mig','🔥 Tilbud','📸 Bon','🧠 Vaner','⚙️'])
with tabs[0]:
    h=load_habits();st.success('Din økologiske indkøbshjælper')
    a,b=st.columns(2);a.metric('Butikker',6);b.metric('Lærte varer',len(h))
    st.write('Netto og Lidl behandles som live-kilder. De øvrige vises kun, når siden kan aflæses sikkert.')
    if st.button('🔄 Hent aktuelle tilbud',type='primary'):
        with st.spinner('Henter fra seks butikker…'):data,status=fetch_all()
        st.session_state.live_data=data;st.session_state.source_status=status;st.success(f'{len(data)} øko-kandidater fundet i alt.')
with tabs[1]:
    st.subheader('Hvad mangler du?');txt=st.text_area('Én varetype pr. linje','mælk\næg\npasta\nhakket kød\nbananer',height=190)
    if st.button('Match med ugens tilbud',type='primary'):
        data=st.session_state.live_data
        if data.empty:
            with st.spinner('Henter aktuelle tilbud først…'):data,status=fetch_all()
            st.session_state.live_data=data;st.session_state.source_status=status
        if data.empty:st.warning('Ingen aktuelle tilbud kunne aflæses sikkert lige nu.')
        else:
            result=wishlist(data,[x.strip() for x in txt.splitlines() if x.strip()]);st.dataframe(result,hide_index=True,use_container_width=True)
            found=result['Pris'].dropna()
            if not found.empty:st.metric('Pris for fundne varer',f'{found.sum():.2f} kr.')
with tabs[2]:
    st.subheader('Personlige tilbud');h=load_habits()
    if not h:st.info('Scan nogle boner først, så robotten lærer dine faste varer.')
    elif st.button('🎯 Find tilbud på mine varer',type='primary'):
        data=st.session_state.live_data
        if data.empty:
            with st.spinner('Henter aktuelle tilbud…'):data,status=fetch_all()
            st.session_state.live_data=data;st.session_state.source_status=status
        m=personal(data,h);st.info('Ingen gode match fundet denne gang.') if m.empty else st.dataframe(m,hide_index=True,use_container_width=True)
with tabs[3]:
    st.subheader('Aktuelle øko-tilbud')
    if st.button('Opdater alle butikker'):
        with st.spinner('Henter…'):data,status=fetch_all()
        st.session_state.live_data=data;st.session_state.source_status=status
    data=st.session_state.live_data
    if data.empty:st.info("Tryk 'Opdater alle butikker' for at hente aktuelle data.")
    else:
        live_only=st.toggle('Vis kun Live',value=False);shown=data[data['Status']=='Live'] if live_only else data
        stores=st.multiselect('Butikker',list(STORES),default=list(STORES));shown=shown[shown['Butik'].isin(stores)]
        st.dataframe(shown.sort_values(['Butik','Pris']),hide_index=True,use_container_width=True,column_config={'Kilde':st.column_config.LinkColumn('Kilde',display_text='Åbn')})
with tabs[4]:
    st.subheader('Scan bon');camera=st.camera_input('📷 Tag billede af bon');upload=st.file_uploader('🖼️ Vælg bon fra Fotos',type=['jpg','jpeg','png','webp']);source=camera or upload
    if source:
        img,data=preprocess(source);st.image(img,caption='Valgt bon',use_container_width=True)
        if ocr_key():
            if st.button('✨ Læs bon automatisk',type='primary'):
                try:
                    with st.spinner('Læser bonen…'):st.session_state.receipt_text=run_ocr(data)
                    st.success('Bonen er aflæst.')
                except Exception as e:st.error(str(e))
        else:st.warning('Automatisk OCR er ikke aktiveret. Du kan stadig indsætte bontekst manuelt.')
    text=st.text_area('Bontekst – ret fejl før lagring',value=st.session_state.get('receipt_text',''),height=230);store=st.selectbox('Butik',['']+list(STORES))
    if text:
        parsed=parse_receipt(text)
        if parsed.empty:st.info('Ingen sikre varelinjer fundet endnu.')
        else:
            edited=st.data_editor(parsed,num_rows='dynamic',hide_index=True,use_container_width=True)
            if st.button('Gem bon og lær',type='primary'):
                n,where=save_items(edited['Vare'].tolist(),store);st.success(f'{n} varer gemt via {where}.')
with tabs[5]:
    st.subheader('Det robotten har lært');h=load_habits()
    if not h:st.info('Ingen gemte varer endnu.')
    else:
        rows=[[item,count,'Fast vane' if count>=4 else 'Mulig vane' if count>=2 else 'Engangskøb'] for item,count in sorted(h.items(),key=lambda x:x[1],reverse=True)]
        st.dataframe(pd.DataFrame(rows,columns=['Vare','Køb','Vane']),hide_index=True,use_container_width=True)
with tabs[6]:
    st.subheader('Status');st.write('Permanent lagring:','✅ Supabase' if sb_client() else '⚠️ lokal fallback');st.write('Bon-OCR:','✅ OCR.Space' if ocr_key() else '⚠️ ikke aktiveret')
    st.divider();st.write('**Datakilder**')
    if st.session_state.source_status:
        for store,count,msg in st.session_state.source_status:st.write(f"{'🟢' if count else '🟡'} {store}: {count} fund · {msg}")
    else:
        for store,cfg in STORES.items():st.write(f"• {store}: {'Live-adapter' if cfg['trust']=='live' else 'Kandidat-adapter'}")
    st.caption('Robotten viser ikke opdigtede priser. Hvis en butik ikke kan aflæses sikkert, får den ingen pris i resultatet.')
st.caption('Øko-robot v1.0')
