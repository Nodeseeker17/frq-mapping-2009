"""Scout Commerce OS — local-first zero-spend research dashboard.

Run: pip install -r requirements.txt && python app.py
Secrets: SERPAPI_KEY environment variable. Never place API keys in index.html.
NO_SPEND_MODE rejects external billable research and provides no purchase, ad, or publishing actions.
"""
from __future__ import annotations
import hmac
import csv, io, json, os, re, sqlite3, statistics, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from research import (news_rss, trends_rss, parse_csv, CSV_TEMPLATE, normalized_name,
                      uid, web_url, utc_now, DISCOVERY_THEMES, exploration_themes,
                      discovery_news_rss, terms_in_headline, research_priority, market_analysis, related_family, investigation_links)
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
import uvicorn

ROOT = Path(__file__).resolve().parent
DB_FILE = Path(os.getenv("SCOUT_DB", ROOT / "scout.db"))
LOCK = threading.RLock()
API_KEY = os.getenv("SERPAPI_KEY", "").strip()
NO_SPEND_MODE = True  # Hard lock for v0.3. A future release requires explicit redesign and authorization.
# Do not use a potentially billable provider even if an API key is supplied.
SHOPPING_ENABLED = bool(API_KEY) and not NO_SPEND_MODE
DASHBOARD_TOKEN = os.getenv("SCOUT_ACCESS_TOKEN", "").strip()
# Prevent accidentally deploying a cloud dashboard without an access token
# or a deliberate database path. The no-spend lock is unaffected.
if os.getenv("SCOUT_CLOUD_MODE", "0") == "1":
    if len(DASHBOARD_TOKEN) < 32:
        raise RuntimeError("Cloud mode requires SCOUT_ACCESS_TOKEN of at least 32 characters")
    if not os.getenv("SCOUT_DB", "").strip():
        raise RuntimeError("Cloud mode requires a persistent SCOUT_DB path (e.g. /data/scout.db)")
app = FastAPI(title="Scout Commerce OS", docs_url=None, redoc_url=None)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    c = sqlite3.connect(DB_FILE, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.commit()
    return c


def research_seed():
    return {"signals":[],"runs":[],"source_health":{},"last_run":None,"last_finished":None,
            "last_error":None,"total_imported":0,"explore_cursor":0,"last_themes":[],
            "total_discovered":0,"discovery_index":{}}


def ensure_state(st):
    if "research" not in st: st["research"]=research_seed()
    for k,v in research_seed().items():st["research"].setdefault(k,v if not isinstance(v,(list,dict)) else v.copy())
    st.setdefault("settings",{}).setdefault("watchlist",[])
    old=st.setdefault("bots",bots_default())
    existing_ids={b.get("id") for b in old if isinstance(b,dict)}
    for b in bots_default():
        if b["id"] not in existing_ids:old.append(b)
    st.setdefault("run", {"active":False,"started":None,"finished":None,"message":"Idle","completed":0,"total":0})
    for p in st.get("products",[]):
        p.setdefault("evidence",[])
        p.setdefault("signals",[])
        p.setdefault("sample",False)
        p.setdefault("research_readiness",0)
        p.setdefault("discovery_priority",0)
        p.setdefault("discovered_by",None)
    st["version"]=5
    for product in st.get("products",[]):
        product.setdefault("investigation",{})
    return st


def load():
    with LOCK:
        c = conn()
        try:
            row = c.execute("SELECT value FROM kv WHERE key='workspace'").fetchone()
            if row:
                return ensure_state(json.loads(row[0]))
            state = ensure_state(seed_state())
            c.execute("INSERT INTO kv(key,value) VALUES ('workspace',?)", (json.dumps(state),))
            c.commit()
            return state
        finally:
            c.close()


def save(state):
    with LOCK:
        c = conn()
        try:
            c.execute("INSERT INTO kv(key,value) VALUES ('workspace',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(state),))
            c.commit()
        finally:
            c.close()


def event(state, message, kind="info", product_id=None):
    state["events"].insert(0, {"id":uuid.uuid4().hex[:10], "time":now(), "message":message, "kind":kind, "product_id":product_id})
    state["events"] = state["events"][:250]


def money(x):
    try:
        return float(x) if x is not None and x != "" else None
    except (ValueError, TypeError):
        return None


def compute(product):
    sell, cost, ship, cpa = [money(product.get(k)) for k in ("sell_price", "supplier_cost", "shipping_cost", "ad_cost")]
    fee = money(product.get("payment_fee_percent"))
    fee = 3 if fee is None else fee
    if None in (sell,cost,ship,cpa) or sell <= 0:
        product["profit"] = None
        product["margin"] = None
        product["score"] = None
        product["score_reason"] = "Need selling price, supplier cost, shipping and estimated acquisition cost"
    else:
        profit = round(sell - cost - ship - cpa - sell * fee/100,2)
        margin = round(100*profit/sell,1)
        product["profit"] = profit
        product["margin"] = margin
        product["score"] = min(100,max(0,round(45 + margin*1.3 - (15 if not product.get('verified_supplier') else 0) - (15 if not product.get('verified_demand') else 0))))
        product["score_reason"] = "Provisional estimate; supplier & sales verification are separate checks"
    return product


def seed_state():
    samples = [
        dict(id="sample-cat-clean", name="Pet hair cleaning bundle", category="Pets", status="review", sample=True, source="Sample scenario — not live market data", sell_price=29.99, supplier_cost=7.5, shipping_cost=3.5, ad_cost=8, payment_fee_percent=3, verified_supplier=False, verified_demand=False, created=now(), notes="Consider a bundle instead of a low-price single brush. All prices shown are illustrative.", evidence=[], signals=["Example only", "Supplier unverified"]),
        dict(id="sample-feeder", name="Interactive slow-feeder bowl", category="Pets", status="new", sample=True, source="Sample scenario — not live market data", sell_price=24.99, supplier_cost=None, shipping_cost=None, ad_cost=None, payment_fee_percent=3, verified_supplier=False, verified_demand=False, created=now(), notes="Compare shipping durability and cleaning complaints.", evidence=[], signals=["Example only", "Costs missing"]),
        dict(id="sample-car", name="Magnetic car cable organizer", category="Auto", status="declined", sample=True, source="Sample scenario — not live market data", sell_price=14.99, supplier_cost=5, shipping_cost=4, ad_cost=9, payment_fee_percent=3, verified_supplier=False, verified_demand=False, created=now(), notes="Example decision: insufficient margin after ads.", decision_reason="Example: weak margin after advertising", decided=now(), evidence=[], signals=["Example only"]),
    ]
    for x in samples: compute(x)
    return {"version":5, "research":research_seed(), "products":[], "events":[{"id":"welcome","time":now(),"message":"New workspace ready: no product claims or samples. Free RSS research can be started on the server.","kind":"info","product_id":None}],"settings":{"watchlist":["reusable pet hair remover","interactive cat toy","car detailing brush","cordless car vacuum","magnetic cable organizer"],"budget":0},"run":{"active":False,"started":None,"finished":None,"message":"Awaiting first research run","completed":0,"total":0},"bots":bots_default()}


def bots_default():
    return [dict(id="investigator",name="Market Investigator",role="Audits source freshness and follow-up research; external listings remain manual",state="ready",progress=0,last_run=None,task="Ready to investigate a product without any paid services"),
            dict(id="hunter",name="Product Hunter",role="Collects RSS search mentions for your watched product terms",state="ready",progress=0,last_run=None,task="Free Google News RSS available in server mode"),
            dict(id="trend",name="Trend Watch",role="Archives US Google Trends RSS signals",state="ready",progress=0,last_run=None,task="Search trends do not prove product sales"),
            dict(id="explorer",name="Opportunity Explorer",role="Rotates through 24 product categories and extracts physical products explicitly named by sources",state="ready",progress=0,last_run=None,task="Looking for specific items, not just generic headlines"),
            dict(id="competitor",name="Competitor Analyst",role="Compares imported merchant listings and prices",state="manual",progress=0,last_run=None,task="Waiting for competitor price evidence/CSV"),
            dict(id="supplier",name="Supplier Scout",role="Compiles supplier quotes, shipment assumptions and gaps",state="manual",progress=0,last_run=None,task="Waiting for supplier quotations/CSV"),
            dict(id="economics",name="Profit Analyst",role="Calculates unit economics using explicit assumptions",state="ready",progress=0,last_run=None,task="Never treats unknown costs as zero"),
            dict(id="creative",name="Ad Creative",role="Creates draft-only ad concepts after approval",state="ready",progress=0,last_run=None,task="No publishing or spend"),
            dict(id="store",name="Store Builder",role="Creates draft-only product copy after approval",state="ready",progress=0,last_run=None,task="No publishing or spend"),
            dict(id="campaign",name="Campaign Monitor",role="Requires connected campaigns to monitor",state="needs_connection",progress=0,last_run=None,task="No ad accounts connected")]


def require_access(request: Request):
    if DASHBOARD_TOKEN and not hmac.compare_digest(request.headers.get("X-Scout-Token", ""), DASHBOARD_TOKEN):
        raise HTTPException(status_code=401, detail="Provide the access token in X-Scout-Token. Do not expose an unprotected server to the internet.")


class Candidate(BaseModel):
    name: str = Field(min_length=2,max_length=160)
    category: str = Field(default="Other",max_length=60)
    sell_price: float|None = None
    supplier_cost: float|None = None
    shipping_cost: float|None = None
    ad_cost: float|None = None
    notes: str = Field(default="",max_length=2000)
    source_url: str|None = None

class Decision(BaseModel):
    status: str
    reason: str = Field(default="",max_length=1000)

class UpdateProduct(BaseModel):
    sell_price: float|None = None
    supplier_cost: float|None = None
    shipping_cost: float|None = None
    ad_cost: float|None = None
    notes: str|None = None
    verified_supplier: bool|None = None
    verified_demand: bool|None = None

class SettingsUpdate(BaseModel):
    watchlist: list[str]
    budget: float = Field(ge=0,le=10000000)

@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")

@app.get("/api/state",dependencies=[Depends(require_access)])
def get_state():
    st=load()
    st["connection"]={"online":True,"free_research":True,"shopping":SHOPPING_ENABLED,"shopping_source":"SerpApi Google Shopping" if SHOPPING_ENABLED else "Disabled in no-spend mode","auth_enabled":bool(DASHBOARD_TOKEN)}
    st["safety"]={"mode":"NO_SPEND","spend_limit_usd":0,"shopping_api_locked":True,"purchases_enabled":False,"ad_spend_enabled":False,"publish_enabled":False}
    st["bots"]=refresh_bots(st["bots"])
    for p in st.get("products",[]):
        score_research(p)
        p['market']=market_analysis(p)
        p['discovery_priority']=p['market']['priority']
        p['market']['research_links']=investigation_links(p['name'])
    families={}
    for p in st.get("products",[]):
        f=p.get("market",{}).get("family")
        if f: families.setdefault(f,[]).append({"id":p["id"],"name":p["name"],"status":p["status"]})
    for p in st.get("products",[]):
        family=p.get("market",{}).get("family")
        p["market"]["related_leads"]=[item for item in families.get(family,[]) if item["id"]!=p["id"]] if family else []
    return st


def refresh_bots(bots):
    # State is actual execution status, not proof of external data availability.
    for b in bots:
        if b["id"] in ("hunter","trend") and b["state"]=="needs_connection":
            b["state"]="ready"
            b["task"]="Free RSS available; run only on the Python server"
    return bots

@app.post("/api/products", dependencies=[Depends(require_access)])
def add_candidate(body:Candidate):
    with LOCK:
        st=load()
        url=body.source_url or ""
        if url and urlparse(url).scheme not in ("https","http"):
            raise HTTPException(400,"Only http(s) source URLs are allowed")
        p=dict(id=uuid.uuid4().hex[:12],name=body.name,category=body.category,status="new",sample=False,source="Manual entry",sell_price=body.sell_price,supplier_cost=body.supplier_cost,shipping_cost=body.shipping_cost,ad_cost=body.ad_cost,payment_fee_percent=3,verified_supplier=False,verified_demand=False,created=now(),notes=body.notes,evidence=([dict(label="Provided link",url=url)] if url else []),signals=["Manual candidate"])
        p["research_readiness"]=0
        st["products"].insert(0,compute(p))
        event(st,f"Added candidate: {p['name']}","info",p["id"])
        save(st)
        return p

@app.patch("/api/products/{pid}",dependencies=[Depends(require_access)])
def update_candidate(pid:str,body:UpdateProduct):
    with LOCK:
        st=load(); p=next((x for x in st["products"] if x["id"]==pid),None)
        if not p: raise HTTPException(404,"Product not found")
        for k,v in body.model_dump(exclude_unset=True).items():
            if k in ("verified_supplier","verified_demand") and v is True:
                raise HTTPException(400,"Verification cannot be self-certified by automation in v1. Attach vetted evidence manually in a later version.")
            p[k]=v
        compute(p)
        event(st,f"Updated economics for {p['name']}","info",pid)
        save(st)
        return p

@app.post("/api/products/{pid}/decision",dependencies=[Depends(require_access)])
def decide(pid:str,body:Decision):
    if body.status not in ("approved","declined","review","new"):
        raise HTTPException(400,"Invalid status")
    if body.status=="declined" and not body.reason.strip():
        raise HTTPException(400,"A decline reason is required")
    with LOCK:
        st=load(); p=next((x for x in st["products"] if x["id"]==pid),None)
        if not p:raise HTTPException(404,"Product not found")
        p["status"]=body.status
        p["decided"]=now()
        p["decision_reason"]=body.reason
        event(st,f"{body.status.title()}: {p['name']}"+(f" — {body.reason}" if body.reason else ""),"decision",pid)
        save(st)
        return p

@app.put("/api/settings", dependencies=[Depends(require_access)])
def update_settings(body:SettingsUpdate):
    with LOCK:
        st=load()
        st["settings"]={"watchlist":[x.strip()[:100] for x in body.watchlist[:8] if x.strip()],"budget":body.budget}
        event(st,"Updated research watchlist and budget")
        save(st)
        return st["settings"]

@app.post("/api/import", dependencies=[Depends(require_access)])
def import_workspace(body:dict):
    """Replace server workspace from a validated local export. Reset running jobs and bots."""
    source_products=body.get("products")
    if not isinstance(source_products,list) or len(source_products)>500:
        raise HTTPException(400,"Invalid product export or too many products (max 500)")
    raw_events=body.get("events",[])
    if not isinstance(raw_events,list) or len(raw_events)>1000:
        raise HTTPException(400,"Invalid activity history")
    try:
        if len(json.dumps(body))>2_000_000:
            raise ValueError("File too large")
        st=seed_state()
        st["products"]=[]
        for original in source_products:
            if not isinstance(original,dict): raise ValueError("Invalid product record")
            name=original.get("name")
            if not isinstance(name,str) or not 2<=len(name)<=160:raise ValueError("Invalid product name")
            status=original.get("status","new")
            if status not in ("new","review","approved","declined"):raise ValueError("Invalid product status")
            p={k:original.get(k) for k in ("id","name","category","status","source","sample","notes","created","decided","decision_reason","search_key","researched","listing_count","ad_draft","page_draft")}
            p["id"]=str(p.get("id") or uuid.uuid4().hex[:12])[:50]
            p["sample"]=bool(p.get("sample"))
            p["category"]=str(p.get("category") or "Other")[:60]
            p["source"]=str(p.get("source") or "Imported")[:120]
            p["notes"]=str(p.get("notes") or "")[:3000]
            p["decision_reason"]=str(p.get("decision_reason") or "")[:1000]
            p["verified_supplier"]=False
            p["verified_demand"]=False
            p["payment_fee_percent"]=3
            for k in ("sell_price","supplier_cost","shipping_cost","ad_cost"):
                v=money(original.get(k))
                if v is not None and (not 0<=v<=1_000_000): raise ValueError("Invalid cost")
                p[k]=v
            evid=original.get("evidence",[])
            p["evidence"]=[{"id":str(e.get("id") or uuid.uuid4().hex[:12])[:50],"type":str(e.get("type") or "other")[:50],"label":str(e.get("label") or "Source")[:200],"url":str(e["url"]),"source":str(e.get("source") or "Imported")[:100],"observed":str(e.get("observed") or now())[:80],"listed_price":money(e.get("listed_price")),"sold_units":money(e.get("sold_units")),"sold_period":str(e.get("sold_period") or "")[:70]} for e in evid[:45] if isinstance(e,dict) and isinstance(e.get("url"),str) and urlparse(e["url"]).scheme in ("http","https")]
            p["signals"]=[str(x)[:200] for x in (original.get("signals") or [])[:12]]
            st["products"].append(compute(p))
        st["events"]=[{"id":str(x.get("id") or uuid.uuid4().hex[:10])[:40],"time":str(x.get("time") or now())[:40],"message":str(x.get("message") or "")[:500],"kind":str(x.get("kind") or "info")[:30],"product_id":str(x.get("product_id") or "")[:50]} for x in raw_events[:240] if isinstance(x,dict)]
        settings=body.get("settings",{})
        if not isinstance(settings,dict): settings={}
        st["settings"]={"budget":max(0,min(float(settings.get("budget") or 0),1e7)),"watchlist":[str(x).strip()[:100] for x in (settings.get("watchlist") or [])[:8] if isinstance(x,str) and x.strip()]}
        imported_research=body.get("research")
        if isinstance(imported_research,dict):
            st["research"]["signals"]=[sig for sig in (imported_research.get("signals") or [])[:350] if isinstance(sig,dict) and web_url(sig.get("url"))]
            st["research"]["runs"]=[r for r in (imported_research.get("runs") or [])[:35] if isinstance(r,dict)]
            st["research"]["explore_cursor"]=max(0,int(imported_research.get("explore_cursor",0)))%len(DISCOVERY_THEMES)
        st["bots"]=bots_default()
        st["run"]={"active":False,"started":None,"finished":None,"message":"Ready after import","completed":0,"total":0}
        event(st,f"Imported workspace with {len(st['products'])} candidate(s)","info")
    except (TypeError,ValueError,KeyError,OverflowError) as ex:
        raise HTTPException(400,f"Invalid workspace export: {str(ex)[:150]}")
    with LOCK:
        current=load()
        if current.get("run",{}).get("active"):
            raise HTTPException(409,"Cannot import while a research job is running")
        save(st)
    return {"imported":len(st["products"])}

@app.post("/api/clear-samples", dependencies=[Depends(require_access)])
def clear_samples():
    with LOCK:
        st=load()
        n=len([p for p in st["products"] if p.get("sample")])
        st["products"]=[p for p in st["products"] if not p.get("sample")]
        event(st,f"Cleared {n} sample candidate(s)","info")
        save(st)
        return {"removed":n}

@app.post("/api/analyze", dependencies=[Depends(require_access)])
def analyze():
    with LOCK:
        st=load()
        for p in st["products"]:compute(p)
        b=next(x for x in st["bots"] if x["id"]=="economics")
        b.update(state="ready",progress=100,last_run=now(),task=f"Recalculated {len(st['products'])} candidates")
        event(st,f"Profit Analyst evaluated {len(st['products'])} candidates; unknown costs remain unscored","bot")
        save(st)
        return {"count":len(st["products"])}

@app.post("/api/drafts/{pid}", dependencies=[Depends(require_access)])
def make_drafts(pid:str):
    with LOCK:
        st=load(); p=next((x for x in st["products"] if x["id"]==pid),None)
        if not p: raise HTTPException(404,"Product not found")
        if p["status"]!="approved":raise HTTPException(400,"Approve the candidate first")
        n=p["name"]
        p["ad_draft"]={"hook":f"Still dealing with this problem? Here's an idea worth testing: {n}.","video_plan":"0–3s: show authentic customer problem; 3–10s: real product demo; 10–15s: show result; 15–20s: clear offer and CTA.","caution":"Template only. Test the real product, obtain rights to footage, and verify claims before publishing."}
        p["page_draft"]={"headline":f"{n} — a simpler everyday solution","description":f"Explore whether {n.lower()} fits your needs. Add genuine specs, verified benefits, delivery terms and original product photos before publishing.","status":"Draft only — not published"}
        for bid in ("creative","store"):
            b=next(x for x in st["bots"] if x["id"]==bid)
            b.update(progress=100,last_run=now(),task=f"Draft created for {n}")
        event(st,f"Created ad and product-page drafts for {n}","bot",pid)
        save(st)
        return {"ad_draft":p["ad_draft"],"page_draft":p["page_draft"]}

@app.post("/api/hunt", dependencies=[Depends(require_access)])
def hunt():
    if NO_SPEND_MODE:
        raise HTTPException(403,"NO-SPEND MODE: potentially billable research is disabled. Run free local checks instead.")
    if not API_KEY:raise HTTPException(400,"Live research needs SERPAPI_KEY on the server")
    with LOCK:
        st=load()
        if st["run"].get("active"):raise HTTPException(409,"Research already running")
        queries=st["settings"]["watchlist"][:8]
        if not queries:raise HTTPException(400,"Add at least one search phrase")
        st["run"]={"active":True,"started":now(),"finished":None,"message":"Starting market research","completed":0,"total":len(queries)}
        for bid in ("hunter","competitor"):
            b=next(x for x in st["bots"] if x["id"]==bid)
            b.update(state="running",progress=0,task="Contacting Google Shopping through SerpApi")
        event(st,"Product Hunter started live shopping-price research","bot")
        save(st)
    threading.Thread(target=run_research,args=(queries,),daemon=True).start()
    return {"started":True,"queries":len(queries)}


def run_research(queries):
    if NO_SPEND_MODE:
        return  # Fail closed if called directly, not only through the HTTP route.
    errors=[]
    try:
        for i,query in enumerate(queries):
            try:
                response=requests.get("https://serpapi.com/search.json",params={"engine":"google_shopping","q":query,"gl":"us","hl":"en","api_key":API_KEY},timeout=35)
                response.raise_for_status()
                data=response.json()
                if data.get("error"):
                    raise ValueError(str(data["error"])[:250])
                listings=data.get("shopping_results") or []
                prices=[float(x["extracted_price"]) for x in listings if isinstance(x.get("extracted_price"),(int,float)) and x["extracted_price"]>0]
                pmedian=round(statistics.median(prices),2) if prices else None
                sources=[]
                for item in listings[:8]:
                    link=item.get("product_link") or item.get("link") or ""
                    if link.startswith("http"):
                        sources.append(dict(label=(str(item.get("source") or "Merchant")[:70]+" — "+str(item.get("title") or "Listing")[:100]),url=link,listed_price=item.get("extracted_price")))
                with LOCK:
                    st=load()
                    existing=next((p for p in st["products"] if p.get("search_key","").lower()==query.lower() and not p.get("sample")),None)
                    if existing:
                        p=existing
                    else:
                        p=dict(id=uuid.uuid4().hex[:12],name=query.title(),category="Watchlist",status="new",sample=False,created=now(),supplier_cost=None,shipping_cost=None,ad_cost=None,payment_fee_percent=3,verified_supplier=False,verified_demand=False,notes="Observed shopping listing prices, NOT verified sales volume, growth or supplier pricing.",search_key=query)
                        st["products"].insert(0,p)
                    p.update(source="SerpApi / Google Shopping",sell_price=pmedian,evidence=sources,listing_count=len(listings),signals=[f"{len(listings)} observed shopping listings",f"Median listed price: ${pmedian:.2f}" if pmedian else "No listing prices parsed", "Sales volume unknown", "Trend growth unknown"], researched=now())
                    compute(p)
                    event(st,f"Researched {query}: {len(listings)} listing results; sales volume unverified","bot",p["id"])
                    st["run"].update(completed=i+1,message=f"Researched {i+1}/{len(queries)}: {query}")
                    for bid in ("hunter","competitor"):
                        b=next(x for x in st["bots"] if x["id"]==bid)
                        b.update(progress=round((i+1)/len(queries)*100),task=f"Completed {i+1}/{len(queries)} keywords")
                    save(st)
            except Exception as ex:
                errors.append(f"{query}: {str(ex)[:180]}")
                with LOCK:
                    st=load()
                    st["run"].update(completed=i+1,message=f"Research failed for {query}")
                    event(st,f"Research error for {query}: {str(ex)[:150]}","error")
                    save(st)
    finally:
        with LOCK:
            st=load()
            st["run"].update(active=False,finished=now(),message="Completed with errors" if errors else "Research complete")
            for bid in ("hunter","competitor"):
                b=next(x for x in st["bots"] if x["id"]==bid)
                b.update(state="error" if errors else "ready",last_run=now(),task="; ".join(errors)[:160] if errors else "Last run completed")
            save(st)


# --- FREE RESEARCH PIPELINE: all evidence is explicitly typed and sourced ---

def score_research(p):
    evidence=p.get('evidence',[])
    report=market_analysis(p)
    # A completeness meter, *not* a claim that market evidence is verified.
    # Headline mentions alone can earn at most 18 points, not a perfect score.
    datescore=min(18, report['recent_30d']*4+min(3,report['independent_publishers_90d'])*2)
    pricepoints=20 if report['competitor_prices']>=2 else (10 if report['competitor_prices'] else 0)
    supplierpoints=20 if report['supplier_quotes']>=2 else (12 if report['supplier_quotes'] else 0)
    salespoints=15 if report['reported_sales_sources'] else 0
    costpoints=4*sum(p.get(k) is not None for k in ('sell_price','supplier_cost','shipping_cost','ad_cost'))
    p['research_readiness']=min(89,datescore+pricepoints+supplierpoints+salespoints+costpoints)
    comps=[money(e.get('listed_price')) for e in evidence if e.get('type')=='competitor_listing']
    comps=[v for v in comps if v is not None and v>0]
    p['competitor_median_price']=round(statistics.median(comps),2) if comps else None
    p['competitor_count']=len(comps)
    p['supplier_quote_count']=report['supplier_quotes']
    p['sales_report_count']=report['reported_sales_sources']
    p['research_gaps']=report['missing_checks']
    p['market']=report
    p['discovery_priority']=report['priority']
    return p


def find_or_create_candidate(st,name,category='Watchlist',source='Watchlist / research lead'):
    key=normalized_name(name)
    p=next((p for p in st['products'] if not p.get('sample') and normalized_name(p.get('name'))==key),None)
    if p is not None:return p,False
    p=dict(id=uuid.uuid4().hex[:12],name=str(name)[:160],category=category,status='new',sample=False,source=source,created=now(),
           notes='Unverified research lead. News mentions/search trends are NOT proof of sales, margin or supplier quality.',
           sell_price=None,supplier_cost=None,shipping_cost=None,ad_cost=None,payment_fee_percent=3,
           verified_supplier=False,verified_demand=False,evidence=[],signals=['Unverified lead'],research_readiness=0)
    st['products'].insert(0,p)
    compute(p)
    return p,True


def add_evidence(p,e):
    eid=e.get('id') or uid(e.get('type','other'),e.get('url',''),e.get('source',''),e.get('label',''),e.get('listed_price'),e.get('sold_units'))
    if any(old.get('id')==eid for old in p.setdefault('evidence',[])):
        return False
    e['id']=eid
    p['evidence'].insert(0,e)
    p['evidence']=p['evidence'][:50]
    score_research(p)
    return True


def update_source_health(st,source,success,count=0,error=''):
    st['research']['source_health'][source]={
        'last_checked':now(),'status':'ok' if success else 'error','count':count,
        'detail':error[:240] if error else ('Fetched '+str(count)+' records')}


def commit_research_signals(st,signals):
    current={x.get('id') for x in st['research']['signals']}
    new=[x for x in signals if x.get('id') not in current]
    st['research']['signals']=(new+st['research']['signals'])[:400]
    return len(new)


class ResearchImport(BaseModel):
    csv_text: str=Field(min_length=4,max_length=150000)

class ResearchEvidence(BaseModel):
    name:str=Field(min_length=2,max_length=160)
    category:str=Field(default='Other',max_length=60)
    source_type:str=Field(default='other')
    source_name:str=Field(default='Manual observation',max_length=100)
    source_url:str=Field(default='',max_length=1600)
    listed_price:float|None=None
    sold_units:int|None=None
    sold_period:str=Field(default='',max_length=70)
    supplier_cost:float|None=None
    shipping_cost:float|None=None
    notes:str=Field(default='',max_length=500)


def ingest_row(st,row):
    source_type=row.get('source_type') or 'other'
    if source_type not in ('competitor_listing','supplier_quote','reported_sales','other'):
        raise ValueError('Unsupported evidence type')
    link=row.get('source_url') or ''
    if link and not web_url(link):raise ValueError('Invalid URL')
    p,created=find_or_create_candidate(st,row['name'],row.get('category') or 'Other','User supplied evidence')
    evidence={
        'type':source_type,'label':(row.get('source_name') or 'User supplied evidence')[:100],
        'url':link,'source':(row.get('source_name') or 'User supplied evidence')[:100],
        'observed':now(),'listed_price':row.get('listed_price'),
        'sold_units':row.get('sold_units'),'sold_period':row.get('sold_period',''),
        'notes':row.get('notes','')[:500], 'verification':'user_reported_unverified'}
    # No URL means it's an unsourced user note: show it, but don't claim independent evidence.
    evidence['id']=uid(source_type,p['name'],link,evidence['source'],evidence['listed_price'],evidence['sold_units'],evidence['sold_period'])
    added=add_evidence(p,evidence)
    if source_type=='supplier_quote':
        # Use as a provisional cost assumption only, not as a verified orderable quote.
        if row.get('supplier_cost') is not None:p['supplier_cost']=row['supplier_cost']
        if row.get('shipping_cost') is not None:p['shipping_cost']=row['shipping_cost']
    elif source_type=='other' and row.get('supplier_cost') is not None:
        # Non supplier rows should not masquerade as quote data.
        pass
    compute(p)
    score_research(p)
    return created,added,p['id']

@app.get('/api/research/template.csv',dependencies=[Depends(require_access)])
def research_template():
    return Response(CSV_TEMPLATE,media_type='text/csv',headers={'Content-Disposition':'attachment; filename="scout-research-template.csv"'})

@app.post('/api/research/import-csv',dependencies=[Depends(require_access)])
def research_import(body:ResearchImport):
    try:rows=parse_csv(body.csv_text)
    except ValueError as e:raise HTTPException(400,str(e))
    with LOCK:
        st=load();new=0;observations=0
        for row in rows:
            c,a,_=ingest_row(st,row);new+=int(c);observations+=int(a)
        st['research']['total_imported']+=len(rows)
        for bid in ('competitor','supplier'):
            b=next(b for b in st['bots'] if b['id']==bid)
            b.update(state='ready',progress=100,last_run=now(),task=f'Imported {observations} evidence observations')
        event(st,f'Imported {len(rows)} research rows: {new} new candidates, {observations} new observations','bot')
        save(st)
        return {'rows':len(rows),'new_candidates':new,'new_observations':observations}

@app.post('/api/research/evidence',dependencies=[Depends(require_access)])
def research_evidence(body:ResearchEvidence):
    row=body.model_dump()
    for k in ('listed_price','supplier_cost','shipping_cost','sold_units'):
        v=row.get(k)
        if v is not None and (v<0 or v>10_000_000):raise HTTPException(400,'Invalid nonnegative amount: '+k)
    with LOCK:
        st=load()
        try:created,added,pid=ingest_row(st,row)
        except ValueError as e:raise HTTPException(400,str(e))
        update_source_health(st,'Manual / CSV import',True,1)
        event(st,f'Added {row["source_type"]} observation to {row["name"]}' if added else f'Duplicate observation skipped for {row["name"]}', 'bot', pid)
        save(st)
    return {'created':created,'added':added,'product_id':pid}

# An optional, free, explicit product follow-up research pass. Never scrape
# shopping sites, promise sales numbers, or charge third-party APIs.
@app.post('/api/research/investigate/{pid}',dependencies=[Depends(require_access)])
def launch_market_investigation(pid:str):
    with LOCK:
        st=load()
        p=next((x for x in st['products'] if x['id']==pid),None)
        if not p:raise HTTPException(404,'Product not found')
        previous=p.get('investigation',{})
        if previous.get('status')=='running':raise HTTPException(409,'Already investigating this product')
        last=previous.get('started')
        if last:
            dt=datetime.fromisoformat(last)
            if (datetime.now(timezone.utc)-dt).total_seconds()<6*3600:
                raise HTTPException(429,'Per-product follow-up has a six-hour cooldown to respect free news feeds')
        p['investigation']={'status':'running','started':now(),'finished':None,'new_sources':0,'errors':[]}
        b=next(x for x in st['bots'] if x['id']=='investigator')
        b.update(state='running',progress=0,task='Reading free product-specific headlines for '+p['name'])
        event(st,'Investigating '+p['name']+' — public news only, marketplace data still requires evidence imports','bot',pid)
        save(st)
        name=p['name']
    threading.Thread(target=run_market_investigation,args=(pid,name),daemon=True).start()
    return {'started':True,'product_id':pid,'spend_usd':0,'source':'Google News RSS only'}


def run_market_investigation(pid,name):
    errors=[];added=0;matched=0
    try:
        for i,query in enumerate([name, name+' recall']):
            try:
                rows=news_rss(query)
                with LOCK:
                    st=load();p=next((x for x in st['products'] if x['id']==pid),None)
                    if p is None:return
                    # Require exact product phrase. Results without a product mention
                    # cannot be used to imply relevance, even if RSS matched the query.
                    for row in rows:
                        title=normalized_name(row.get('title'))
                        target=normalized_name(name)
                        if target not in title:continue
                        matched+=1
                        is_risk=any(term in title for term in ('recall','fire hazard','injury','safety warning'))
                        record={'id':uid('market_investigation',row['id'],name),
                                'type':'risk_mention' if is_risk else 'investigation_mention',
                                'label':row['title'],'source':row['source'],'url':row['url'],
                                'published':row.get('published'),'observed':now(),
                                'verification':'headline_only_not_sales'}
                        added+=int(add_evidence(p,record))
                    p['investigation'].update(progress=round(100*(i+1)/2),new_sources=added)
                    b=next(x for x in st['bots'] if x['id']=='investigator')
                    b.update(progress=round(100*(i+1)/2),task=f'Checked {i+1}/2 RSS queries for {name}')
                    score_research(p)
                    update_source_health(st,'Market Investigator RSS',True,len(rows))
                    save(st)
            except Exception as ex:
                errors.append(str(ex)[:140])
                with LOCK:
                    st=load();update_source_health(st,'Market Investigator RSS',False,error=str(ex));save(st)
    finally:
        with LOCK:
            st=load();p=next((x for x in st['products'] if x['id']==pid),None)
            if p:
                p.setdefault('investigation',{}).update(status='error' if errors else 'complete',finished=now(),new_sources=added,matched_headlines=matched,errors=errors)
                score_research(p)
            b=next(x for x in st['bots'] if x['id']=='investigator')
            b.update(state='error' if errors else 'ready',progress=100,last_run=now(),task=f'Investigation ended: {added} new article records for {name}; no sales verified')
            event(st,f'Market Investigator: {name} · {added} new articles, {matched} relevant headlines, {len(errors)} source errors, $0 spent','error' if errors else 'bot',pid)
            save(st)


@app.post('/api/research/run',dependencies=[Depends(require_access)])
def start_free_research():
    with LOCK:
        st=load()
        if st['run'].get('active'):raise HTTPException(409,'A research run is already active')
        previous=st['research'].get('last_run')
        if previous:
            try:
                elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(previous)).total_seconds()
                if elapsed<600:raise HTTPException(429,'Free research has a 10-minute cooldown to respect public RSS sources')
            except ValueError:pass
        queries=[q.strip()[:90] for q in st['settings']['watchlist'][:5] if q.strip()]
        # A watchlist is no longer required for discovery. Exploration rotates
        # through the next four categories every run, even without keyword matches.
        cursor=st['research'].get('explore_cursor',0)
        themes=exploration_themes(cursor,4)
        st['research']['explore_cursor']=(cursor+len(themes))%len(DISCOVERY_THEMES)
        st['research']['last_themes']=[t[0] for t in themes]
        st['research']['last_run']=now()
        st['run']={'active':True,'started':now(),'finished':None,'message':'Exploring new product categories','completed':0,'total':len(queries)+len(themes)+1}
        for bid in ('hunter','trend','explorer'):
            b=next(b for b in st['bots'] if b['id']==bid)
            b.update(state='running',progress=0,task='Collecting free public signals (sales NOT verified)')
        event(st,'Started product discovery: '+', '.join(t[0] for t in themes),'bot')
        save(st)
    threading.Thread(target=run_free_research,args=(queries,themes),daemon=True).start()
    return {'started':True,'watch_queries':len(queries),'discovery_themes':[t[0] for t in themes],'spend_usd':0}


def run_free_research(queries,themes=()):
    started=now();errors=[];new_candidates=0;new_signals=0;source_rows=0;discovered=[]
    try:
        try:
            signals=trends_rss()
            with LOCK:
                st=load()
                new_signals+=commit_research_signals(st,signals)
                update_source_health(st,'Google Trends RSS',True,len(signals))
                st['run'].update(completed=1,message=f'Archived {len(signals)} US trending searches')
                b=next(b for b in st['bots'] if b['id']=='trend')
                b.update(progress=100,last_run=now(),task=f'Archived {len(signals)} trends; no sales inferred')
                # Only explicitly matching terms count as a trend mention; never invent association.
                for signal in signals:
                    # Most US trends are news, sports, or celebrity topics.
                    # Only explicitly matched physical products become leads.
                    names=terms_in_headline(signal['title'])
                    for q in queries:
                        if normalized_name(q)==normalized_name(signal['title']):
                            names.append((q,'Watchlist'))
                    for name,cat in set(names):
                        p,created=find_or_create_candidate(st,name,cat,'Google Trends RSS — search interest only')
                        new_candidates+=int(created)
                        if created:discovered.append(name)
                        add_evidence(p,{'id':uid('trend_product',signal['id'],name),
                                        'type':'search_trend','label':signal['title']+' ('+signal.get('traffic_bucket','')+')',
                                        'url':signal['url'],'source':'Google Trends RSS','observed':now(),
                                        'verification':'unverified_interest_only'})
                        p['discovery_priority']=research_priority(p)
                save(st)
            source_rows+=len(signals)
        except Exception as ex:
            errors.append('Google Trends: '+str(ex)[:140])
            with LOCK:
                st=load();update_source_health(st,'Google Trends RSS',False,error=str(ex));st['run']['completed']=1
                event(st,'Trend Watch error: '+str(ex)[:140],'error');save(st)
        for i,q in enumerate(queries):
            try:
                signals=news_rss(q)
                with LOCK:
                    st=load()
                    new_signals+=commit_research_signals(st,signals)
                    update_source_health(st,'Google News RSS',True,len(signals))
                    if signals and any(normalized_name(q) in normalized_name(sig['title']) for sig in signals):
                        # Explicit title mention required: generic search results are not evidence.
                        matched=[sig for sig in signals if normalized_name(q) in normalized_name(sig['title'])]
                        p,created=find_or_create_candidate(st,q)
                        new_candidates+=int(created)
                        for signal in matched[:8]:
                            add_evidence(p,{'id':uid('article',signal['id'],q),'type':'news_mention','label':signal['title'],
                                'url':signal['url'],'source':signal['source'],'observed':signal['observed'],
                                'verification':'headline_mention_not_sales','published':signal.get('published')})
                        p['last_researched']=now()
                        score_research(p)
                        p['discovery_priority']=research_priority(p)
                        event(st,f'Added {len(matched)} explicit headline mentions for {q}; sales unverified','bot',p['id'])
                    st['run'].update(completed=i+2,message=f'Checked {i+1}/{len(queries)} product terms')
                    b=next(b for b in st['bots'] if b['id']=='hunter')
                    b.update(progress=round((i+1)*100/len(queries)),task=f'Checked {i+1}/{len(queries)} keywords')
                    save(st)
                source_rows+=len(signals)
            except Exception as ex:
                errors.append(q+': '+str(ex)[:130])
                with LOCK:
                    st=load();update_source_health(st,'Google News RSS',False,error=str(ex))
                    st['run'].update(completed=i+2,message=f'Feed failed for {q}')
                    event(st,'Product Hunter error for '+q+': '+str(ex)[:130],'error');save(st)
        # Opportunity Explorer: rotate across different product categories.
        # A headline becomes a candidate ONLY if it explicitly names a physical
        # item from our controlled vocabulary. No invented sales or prices.
        for j, theme in enumerate(themes):
            try:
                signals=discovery_news_rss(theme)
                with LOCK:
                    st=load()
                    new_signals+=commit_research_signals(st,signals)
                    update_source_health(st,'Opportunity Explorer RSS',True,len(signals))
                    theme_hits={}
                    for signal in signals:
                        found=terms_in_headline(signal['title'])
                        for product_name,cat in found:
                            p,created=find_or_create_candidate(st,product_name,cat,'Opportunity Explorer · '+theme[0])
                            new_candidates+=int(created)
                            if created:discovered.append(product_name)
                            if p.get('status')=='declined':
                                # Store fresh evidence while respecting human rejection.
                                pass
                            added=add_evidence(p,{'id':uid('discovery_evidence',signal['id'],product_name),
                                'type':'discovery_mention','label':signal['title'],
                                'url':signal['url'],'source':signal['source'],
                                'observed':signal['observed'],'verification':'headline_mention_not_sales',
                                'published':signal.get('published'),'theme':theme[0]})
                            if added:theme_hits[product_name]=theme_hits.get(product_name,0)+1
                            p['last_researched']=now()
                            p['discovered_by']=p.get('discovered_by') or 'Opportunity Explorer'
                            p['discovery_priority']=research_priority(p)
                            score_research(p)
                    st['run'].update(completed=len(queries)+j+2,message=f'Explored {j+1}/{len(themes)} new categories')
                    b=next(x for x in st['bots'] if x['id']=='explorer')
                    b.update(progress=round((j+1)*100/len(themes)),
                             task=f"{j+1}/{len(themes)} categories checked; {new_candidates} new leads")
                    if theme_hits:
                        event(st,f"{theme[0]}: identified {len(theme_hits)} named product type(s) from headlines; sales unverified",'bot')
                    save(st)
                source_rows+=len(signals)
            except Exception as ex:
                errors.append(theme[0]+': '+str(ex)[:130])
                with LOCK:
                    st=load();update_source_health(st,'Opportunity Explorer RSS',False,error=str(ex))
                    st['run'].update(completed=len(queries)+j+2,message=f'Explorer source failed for {theme[0]}')
                    event(st,'Opportunity Explorer error: '+str(ex)[:130],'error');save(st)
    finally:
        with LOCK:
            st=load()
            st['run'].update(active=False,finished=now(),message='Discovery scan complete' if not errors else 'Completed with source errors')
            st['research']['last_finished']=now()
            st['research']['last_error']='; '.join(errors)[:350] if errors else None
            st['research']['runs'].insert(0,dict(id=uuid.uuid4().hex[:10],started=started,finished=now(),queries=queries,
                new_candidates=new_candidates,new_signals=new_signals,source_records=source_rows,
                themes=[t[0] for t in themes],discovered=discovered[:24],
                errors=errors[:12],spend_usd=0))
            st['research']['runs']=st['research']['runs'][:60]
            st['research']['total_discovered']=st['research'].get('total_discovered',0)+len(discovered)
            for bid in ('hunter','trend','explorer'):
                b=next(b for b in st['bots'] if b['id']==bid)
                b.update(state='error' if errors else 'ready',last_run=now(),task=('; '.join(errors)[:140] if errors else f'Scan completed; {new_signals} new signals'))
            event(st,f'Discovery scan finished: {new_signals} new signals, {new_candidates} new product leads, {len(errors)} errors','bot')
            save(st)


def run_optional_scheduler():
    """Optional server-owned scheduler. Never runs unless explicitly enabled."""
    interval=max(6,float(os.getenv('SCOUT_SCAN_INTERVAL_HOURS','12')))*3600
    while True:
        time.sleep(60)
        try:
            st=load();last=st['research'].get('last_run')
            elapsed=time.time()-datetime.fromisoformat(last).timestamp() if last else interval+1
            if elapsed>=interval and not st['run'].get('active'):
                try:start_free_research()
                except HTTPException:pass
        except Exception as ex:
            print('SCOUT scheduler:',str(ex)[:140])

@app.on_event('startup')
def recover_stale_runs_on_startup():
    with LOCK:
        st=load()
        changed=False
        for p in st.get('products',[]):
            if p.get('investigation',{}).get('status')=='running':
                p['investigation']['status']='interrupted';p['investigation']['finished']=now();changed=True
        for bot in st.get('bots',[]):
            if bot.get('id')=='investigator' and bot.get('state')=='running':
                bot.update(state='ready',task='Interrupted by restart');changed=True
        if st['run'].get('active'):
            st['run'].update(active=False,finished=now(),message='Previous research run interrupted by server restart')
            for bid in ('hunter','trend','explorer'):
                b=next((b for b in st['bots'] if b['id']==bid),None)
                if b and b.get('state')=='running': b.update(state='ready',task='Previous run interrupted')
            event(st,'Recovered an interrupted research run after restart','error')
            save(st)
            changed=False
        if changed:save(st)

@app.get("/health")
def health():
    return {"ok":True}

if __name__=="__main__":
    if os.getenv("SCOUT_AUTO_SCAN","0") == "1":
        threading.Thread(target=run_optional_scheduler,daemon=True).start()
    print("Scout Commerce OS running at http://127.0.0.1:8765")
    uvicorn.run(app,host=os.getenv("SCOUT_HOST","127.0.0.1"),port=int(os.getenv("PORT","8765")))
