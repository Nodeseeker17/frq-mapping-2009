"""Free-source product lead research for SCOUT.

This module only contacts two public RSS endpoints. Neither supplies product sales
or vendor inventory. It deliberately does not scrape marketplace pages, use a
billable API, or infer sold units from headlines or search counts.
"""
from __future__ import annotations
import csv, hashlib, io, re, xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse
import requests

NEWS_URL = 'https://news.google.com/rss/search'
TRENDS_URL = 'https://trends.google.com/trending/rss?geo=US'
HT = '{https://trends.google.com/trending/rss}'
USER_AGENT = 'SCOUT-Research-Lab/0.3 (read-only RSS; no scraping)'
MAX_RSS_BYTES = 500_000


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def normalized_name(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value).lower()).strip()[:160]


def clean_text(value, limit=260):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def web_url(value):
    value=str(value or '').strip()
    try:
        u=urlparse(value)
        if u.scheme in ('http','https') and u.hostname and not u.username and not u.password:
            return value[:1600]
    except Exception:
        pass
    return ''


def uid(*parts):
    return hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:16]


def safe_fetch(url, params=None, session=None):
    """Static allowlist prevents arbitrary URL fetches/SSRF. Read RSS only."""
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.netloc not in ('trends.google.com','news.google.com') or parsed.path not in ('/trending/rss','/rss/search'):
        raise ValueError('Research source not in the static allowlist')
    fn=(session or requests).get
    r=fn(url,params=params,headers={'User-Agent':USER_AGENT, 'Accept':'application/rss+xml, application/xml, text/xml'},timeout=12,allow_redirects=False)
    r.raise_for_status()
    if len(r.content)>MAX_RSS_BYTES:raise ValueError('RSS response exceeded size limit')
    return r.content


def child_text(node, tag, maxlen=320):
    el=node.find(tag)
    return clean_text(el.text, maxlen) if el is not None else ''


def news_rss(query, session=None):
    """News headlines are *mentions*, never proof of sales."""
    q='"'+clean_text(query,90).replace('"','')+'" (product OR review OR gadget OR shopping OR accessory)'
    xml=safe_fetch(NEWS_URL,{'q':q,'hl':'en-US','gl':'US','ceid':'US:en'},session)
    root=ET.fromstring(xml)
    rows=[]
    for item in root.findall('./channel/item')[:15]:
        title=child_text(item,'title',260)
        link=web_url(child_text(item,'link',1500))
        if not title or not link:continue
        source=item.find('source')
        publisher=clean_text(source.text,90) if source is not None else 'Google News RSS'
        rows.append({'id':uid('news',link),'type':'news_mention','title':title,'url':link,'source':publisher or 'Google News RSS','published':child_text(item,'pubDate',90),'query':clean_text(query,100),'observed':utc_now(),'sales_verified':False})
    return rows


def trends_rss(session=None):
    xml=safe_fetch(TRENDS_URL,None,session)
    root=ET.fromstring(xml)
    rows=[]
    for item in root.findall('./channel/item')[:20]:
        title=child_text(item,'title',200)
        if not title:continue
        volume=child_text(item,HT+'approx_traffic',50)
        # link is a search page, not proof of product sales
        url='https://trends.google.com/trending?geo=US'
        rows.append({'id':uid('trend',title,child_text(item,'pubDate',90)),'type':'search_trend','title':title,'url':url,'source':'Google Trends RSS','published':child_text(item,'pubDate',90),'query':'US trending searches','traffic_bucket':volume,'observed':utc_now(),'sales_verified':False})
    return rows


def parse_csv(text, max_rows=200):
    if not isinstance(text,str) or len(text)>150_000:raise ValueError('CSV must be under 150 KB')
    stream=io.StringIO(text.lstrip('\ufeff'))
    reader=csv.DictReader(stream)
    if not reader.fieldnames:raise ValueError('CSV needs a header row')
    allowed={'name','category','source_type','source_url','source_name','listed_price','sold_units','sold_period','supplier_cost','shipping_cost','notes'}
    if 'name' not in [f.strip().lower() for f in reader.fieldnames]:raise ValueError('CSV must include a name column')
    result=[]
    for i, original in enumerate(reader):
        if i>=max_rows:raise ValueError(f'CSV exceeds {max_rows} rows')
        row={str(k).strip().lower():str(v or '').strip() for k,v in original.items() if k is not None and str(k).strip().lower() in allowed}
        name=clean_text(row.get('name'),160)
        if len(name)<2:continue
        link=row.get('source_url','')
        if link and not web_url(link):raise ValueError(f'Row {i+2} has an invalid evidence link')
        for k in ('listed_price','sold_units','supplier_cost','shipping_cost'):
            if row.get(k):
                try:
                    v=float(row[k].replace('$','').replace(',',''))
                    if v<0 or v>10_000_000 or not (v < float('inf')):raise ValueError()
                    if k=='sold_units' and v!=int(v):raise ValueError()
                    row[k]=int(v) if k=='sold_units' else v
                except ValueError:raise ValueError(f'Row {i+2}: invalid {k}')
            else:row[k]=None
        if row.get('source_type','').lower() not in ('','competitor_listing','supplier_quote','reported_sales','other'):
            raise ValueError(f'Row {i+2}: invalid source_type')
        row['name']=name
        row['category']=clean_text(row.get('category') or 'Other',60)
        row['source_type']=row.get('source_type') or 'other'
        row['notes']=clean_text(row.get('notes'),500)
        row['source_name']=clean_text(row.get('source_name') or 'User-imported research',100)
        row['sold_period']=clean_text(row.get('sold_period'),70)
        result.append(row)
    return result

CSV_TEMPLATE='name,category,source_type,source_url,source_name,listed_price,sold_units,sold_period,supplier_cost,shipping_cost,notes\n'


# Physical-product discovery. These are *search categories*, not product listings or
# sales estimates. Across runs SCOUT rotates through themes; article titles must
# contain an explicit physical product term before creating a new lead.
DISCOVERY_THEMES = (
    ('Pet enrichment', 'interactive pet toys cat dog enrichment gadgets', 'Pets'),
    ('Pet cleaning', 'pet hair cleaning grooming tools product reviews', 'Pets'),
    ('Pet feeding', 'pet feeding water fountain slow feeder accessories review', 'Pets'),
    ('Car organization', 'car organizer accessories gadgets trunk storage review', 'Auto'),
    ('Car detailing', 'car detailing cleaning tools gadgets product review', 'Auto'),
    ('Car electronics', 'car wireless charger dashcam phone mount accessories', 'Auto'),
    ('Kitchen gadgets', 'new kitchen gadgets cooking accessories reviews', 'Kitchen'),
    ('Food preparation', 'food prep slicer chopper kitchen tools reviews', 'Kitchen'),
    ('Coffee accessories', 'coffee brewing accessories grinder frother reviews', 'Kitchen'),
    ('Home cleaning', 'home cleaning tools gadget product reviews', 'Home'),
    ('Home storage', 'space saving storage organizer home accessory review', 'Home'),
    ('Laundry solutions', 'laundry gadgets lint remover portable steamer reviews', 'Home'),
    ('Desk accessories', 'desk setup accessories cable organizers productivity gadgets', 'Office'),
    ('Phone accessories', 'phone accessories chargers stands magnetic gadgets reviews', 'Electronics'),
    ('Travel essentials', 'travel accessories packing organizers gadgets review', 'Travel'),
    ('Camping products', 'camping gear portable lantern compact accessories review', 'Outdoors'),
    ('Outdoor gear', 'outdoor gear hiking accessories review product', 'Outdoors'),
    ('Garden accessories', 'gardening tools plant watering gadgets reviews', 'Garden'),
    ('Fitness accessories', 'fitness workout accessories resistance recovery gadgets review', 'Fitness'),
    ('Beauty tools', 'beauty hair styling tools accessories review', 'Beauty'),
    ('Baby organization', 'baby stroller organizer bottle accessories product reviews', 'Baby'),
    ('Bathroom storage', 'bathroom organizer shower accessories review', 'Home'),
    ('DIY tools', 'home repair magnetic tool holders measuring gadgets review', 'Tools'),
    ('Tech cables', 'USB C cable organizer charging stand hubs accessories', 'Electronics'),
)

# Grounded item names only. False negatives are preferable to inventing products
# from a headline like 'best new gadgets'. Longer terms win over shorter ones.
PHYSICAL_TERMS = {
 'Pets': ('automatic litter box','cat water fountain','pet water fountain','interactive cat toy','automatic cat feeder','slow feeder bowl','lick mat','snuffle mat','pet grooming glove','pet hair remover','cat scratching post','cat tunnel','pet nail grinder','pet grooming vacuum','pet camera','dog paw cleaner','dog seat cover','cat toy ball','cat feeder'),
 'Auto': ('car vacuum','cordless car vacuum','car phone mount','magnetic phone mount','dash cam','car organizer','trunk organizer','car detailing brush','tire inflator','car jump starter','car trash can','seat gap filler','car seat organizer','car cleaning gel','car air purifier','car charger'),
 'Kitchen': ('portable blender','vegetable chopper','air fryer liner','milk frother','coffee grinder','coffee scale','electric lunch box','silicone baking mat','knife sharpener','garlic press','food vacuum sealer','oil sprayer','water filter pitcher','ice maker','silicone food bags','rice dispenser','manual food chopper'),
 'Home': ('robot vacuum','window cleaning robot','cordless vacuum','electric spin scrubber','lint remover','fabric shaver','steam cleaner','portable steamer','under sink organizer','drawer organizer','shower caddy','motion sensor light','air purifier','humidifier','dehumidifier','shoe rack','laundry basket','clothes drying rack'),
 'Office': ('monitor light bar','desk organizer','cable management tray','wireless keyboard','monitor stand','laptop stand','ergonomic footrest','wireless mouse','under desk drawer'),
 'Electronics': ('magnetic charging stand','wireless charging stand','charging station','usb c hub','portable power bank','power bank','phone tripod','bluetooth tracker','portable projector','mini projector','phone speaker stand','cable organizer','phone holder','magnetic charger','smart plug'),
 'Travel': ('packing cubes','compression packing cubes','travel neck pillow','luggage tracker','travel adapter','toiletry organizer','portable luggage scale','travel bottle set','passport holder'),
 'Outdoors': ('camping lantern','solar camping lantern','headlamp','waterproof dry bag','portable camping stove','inflatable sleeping pad','camping chair','portable water filter','camping cookware','portable hammock'),
 'Garden': ('self watering planter','plant grow light','soil moisture meter','garden hose nozzle','plant watering spike','pruning shears','seed starter tray','plant trellis'),
 'Fitness': ('resistance bands','massage gun','foam roller','jump rope','grip strengthener','ankle weights','yoga mat','balance board','fitness tracker'),
 'Beauty': ('heatless curling rod','hair dryer brush','makeup organizer','scalp massager','facial cleansing brush','makeup brush cleaner','hair straightener','nail lamp'),
 'Baby': ('stroller organizer','bottle warmer','diaper caddy','baby monitor','stroller fan','bottle sterilizer','baby carrier'),
 'Tools': ('laser distance meter','magnetic wristband','digital caliper','stud finder','precision screwdriver','socket organizer','magnetic tool holder','electric screwdriver'),
}

# Protect against naive substring false positives (e.g. 'car vacuum' within
# 'cordless car vacuum'): longer matches are preferred and overlapping spans are removed.
TERM_PATTERNS = sorted(((name,cat,re.compile(r'(?<![a-z0-9])'+re.escape(name).replace('\\ ',r'\s+')+r'(?:s)?(?![a-z0-9])',re.I)) for cat,terms in PHYSICAL_TERMS.items() for name in terms),key=lambda x:-len(x[0]))


def terms_in_headline(headline, max_items=3):
    title=clean_text(headline,260)
    if any(word in title.lower() for word in ('recall alert','product recall','lawsuit','counterfeit')):
        return []
    found=[];spans=[]
    for name,cat,pat in TERM_PATTERNS:
        m=pat.search(title)
        if not m: continue
        if any(m.start()<end and start<m.end() for start,end in spans):continue
        found.append((name.title(),cat))
        spans.append((m.start(),m.end()))
        if len(found)>=max_items:break
    return found


def exploration_themes(cursor, count=4):
    cursor=max(0,int(cursor))
    return [DISCOVERY_THEMES[(cursor+i)%len(DISCOVERY_THEMES)] for i in range(count)]


DISCOVERY_FOCUSES = {
    'Pet enrichment': ('interactive cat toy','cat toy ball','snuffle mat'),
    'Pet cleaning': ('pet hair remover','pet grooming vacuum','dog paw cleaner'),
    'Pet feeding': ('cat water fountain','slow feeder bowl','automatic cat feeder'),
    'Car organization': ('car organizer','seat gap filler','trunk organizer'),
    'Car detailing': ('car detailing brush','car cleaning gel','cordless car vacuum'),
    'Car electronics': ('dash cam','car phone mount','car charger'),
    'Kitchen gadgets': ('portable blender','oil sprayer','air fryer liner'),
    'Food preparation': ('vegetable chopper','garlic press','food vacuum sealer'),
    'Coffee accessories': ('coffee grinder','milk frother','coffee scale'),
    'Home cleaning': ('electric spin scrubber','robot vacuum','steam cleaner'),
    'Home storage': ('under sink organizer','drawer organizer','shoe rack'),
    'Laundry solutions': ('fabric shaver','portable steamer','lint remover'),
    'Desk accessories': ('monitor light bar','desk organizer','laptop stand'),
    'Phone accessories': ('wireless charging stand','phone tripod','power bank'),
    'Travel essentials': ('packing cubes','travel adapter','luggage tracker'),
    'Camping products': ('camping lantern','camping stove','sleeping pad'),
    'Outdoor gear': ('headlamp','dry bag','portable water filter'),
    'Garden accessories': ('soil moisture meter','self watering planter','plant grow light'),
    'Fitness accessories': ('resistance bands','massage gun','foam roller'),
    'Beauty tools': ('hair dryer brush','scalp massager','makeup brush cleaner'),
    'Baby organization': ('stroller organizer','bottle warmer','stroller fan'),
    'Bathroom storage': ('shower caddy','under sink organizer','drawer organizer'),
    'DIY tools': ('laser distance meter','magnetic wristband','stud finder'),
    'Tech cables': ('usb c hub','cable organizer','magnetic charging stand'),
}


def discovery_news_rss(theme, session=None):
    """Search topical consumer-product news, not marketplace sales.

    A predefined query template is used; these are public RSS searches, not
    unrestricted web scraping. Supports optional injected mock session for tests.
    """
    label, query, category=theme
    terms=DISCOVERY_FOCUSES.get(label, ())
    q='('+ ' OR '.join('\"'+term+'\"' for term in terms) +') (review OR best OR product)' if terms else clean_text(query,120)
    xml=safe_fetch(NEWS_URL, {'q':q,'hl':'en-US','gl':'US','ceid':'US:en'},session)
    root=ET.fromstring(xml)
    rows=[]
    for item in root.findall('./channel/item')[:15]:
        title=child_text(item,'title',260)
        link=web_url(child_text(item,'link',1500))
        if not title or not link:continue
        source=item.find('source')
        publisher=clean_text(source.text,90) if source is not None else 'Google News RSS'
        rows.append({'id':uid('discovery',link),'type':'discovery_mention','title':title,'url':link,
                     'source':publisher or 'Google News RSS','published':child_text(item,'pubDate',90),
                     'query':query,'theme':label,'theme_category':category,'observed':utc_now(),
                     'sales_verified':False})
    return rows


# Product families group adjacent opportunities for the operator, but never merge
# their records or erase a user's decision. Car/trunk organizers are related, not identical.
PRODUCT_FAMILIES = {
    'Automotive organization': ('car organizer','trunk organizer','car seat organizer','seat gap filler','car trash can'),
    'Pet hydration and feeding': ('cat water fountain','pet water fountain','automatic cat feeder','cat feeder','slow feeder bowl'),
    'Pet grooming and cleaning': ('pet hair remover','pet grooming glove','pet grooming vacuum','dog paw cleaner'),
    'Pet enrichment': ('interactive cat toy','cat toy ball','snuffle mat','lick mat','cat tunnel'),
    'Phone charging and mounting': ('magnetic charging stand','wireless charging stand','phone holder','magnetic phone mount','charging station','magnetic charger'),
    'Home air and cleaning': ('air purifier','robot vacuum','cordless vacuum','electric spin scrubber','fabric shaver'),
    'Travel organization': ('packing cubes','compression packing cubes','toiletry organizer','passport holder'),
}

def related_family(name):
    key=normalized_name(name)
    for family, members in PRODUCT_FAMILIES.items():
        if key in {normalized_name(m) for m in members}:
            return family
    return ''


def published_at(value):
    """Parse RSS RFC-822 / ISO datetimes. Unknown is not treated as recent."""
    if not value:return None
    try:
        dt=parsedate_to_datetime(str(value))
    except (TypeError,ValueError,IndexError):
        try:dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        except (TypeError,ValueError):return None
    if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def article_key(e):
    # One article may be discovered via many topics or Google News query URLs.
    # Count canonical titles once; duplicates do not build independent demand evidence.
    title=clean_text(e.get('label') or e.get('title'),260).lower()
    title=re.sub(r'\s+[—-]\s+[^—-]+$', '',title)
    title=re.sub(r'[^a-z0-9]+',' ',title).strip()
    return title or str(e.get('url') or e.get('id'))


def market_analysis(p, reference_time=None):
    """Evidence-based follow-up report, NOT product sales or profit probability."""
    today=published_at(reference_time) or datetime.now(timezone.utc)
    ev=p.get('evidence',[]) or []
    headline_types={'news_mention','discovery_mention','investigation_mention'}
    head=[];seen=set()
    for e in ev:
        if e.get('type') not in headline_types:continue
        k=article_key(e)
        if k in seen:continue
        seen.add(k);head.append(e)
    recent=[];moderate=[];stale=[];unknown=[]
    for e in head:
        dt=published_at(e.get('published'))
        if dt is None:unknown.append(e);continue
        age=max(0,(today-dt).days)
        if age<=30:recent.append(e)
        elif age<=90:moderate.append(e)
        else:stale.append(e)
    publisher_count=len({normalized_name(e.get('source')) for e in (recent+moderate) if e.get('source')})
    has_trend=any(e.get('type')=='search_trend' for e in ev)
    competitors=[e for e in ev if e.get('type')=='competitor_listing' and e.get('listed_price') is not None]
    suppliers=[e for e in ev if e.get('type')=='supplier_quote']
    sales=[e for e in ev if e.get('type')=='reported_sales' and e.get('sold_units') is not None]
    risks=[e for e in ev if e.get('type')=='risk_mention']
    # Avoid a perfect priority for mentions. Real listings, user reported figures
    # and supplier notes unlock follow-up priority but remain unverified.
    mention_score=min(42, 9+4*min(len(recent),5)+2*min(len(moderate),4)+3*min(publisher_count,4)+(4 if has_trend else 0)) if (recent or moderate or has_trend) else (5 if stale or unknown else 0)
    market_bonus=(5 if competitors else 0)+(5 if suppliers else 0)+(5 if sales else 0)
    priority=min(60,mention_score+market_bonus)
    missing=[]
    if not recent:missing.append('No independently dated headline in the past 30 days')
    if publisher_count<2:missing.append('Fewer than two independent recent publishers')
    if not competitors:missing.append('Competitor listing prices not recorded')
    if not suppliers:missing.append('Supplier quote and shipping not recorded')
    if not sales:missing.append('Marketplace sales evidence not recorded')
    if p.get('ad_cost') is None:missing.append('Customer acquisition cost unknown')
    if risks:missing.append('Potential safety or recall coverage: review sources before listing')
    prices=[]
    for e in competitors:
        try:
            v=float(e['listed_price'])
            if 0<v<1000000:prices.append(v)
        except (TypeError,ValueError):pass
    return {
        'priority':priority, 'rating':'Follow up / not a sales forecast',
        'unique_headlines':len(head), 'recent_30d':len(recent), 'age_31_90d':len(moderate),
        'stale_over_90d':len(stale), 'undated':len(unknown),
        'independent_publishers_90d':publisher_count,
        'last_published':max((published_at(e.get('published')) for e in head if published_at(e.get('published'))),default=None).isoformat() if any(published_at(e.get('published')) for e in head) else None,
        'competitor_prices':len(prices),'competitor_price_low':round(min(prices),2) if prices else None,
        'competitor_price_high':round(max(prices),2) if prices else None,
        'competitor_price_median':round(sorted(prices)[len(prices)//2] if len(prices)%2 else (sorted(prices)[len(prices)//2-1]+sorted(prices)[len(prices)//2])/2,2) if prices else None,
        'supplier_quotes':len(suppliers),'reported_sales_sources':len(sales),
        'risk_mentions':len(risks), 'missing_checks':missing,
        'evidence_level':('mixed_imported_evidence' if competitors or suppliers or sales else 'mentions_only'),
        'family':related_family(p.get('name','')),
        'disclaimer':'Headlines indicate media mentions, not orders. Imported listings/quotes/sales are user-provided and not independently verified.'
    }


def research_priority(p):
    return market_analysis(p)['priority']


def investigation_links(name):
    q=clean_text(name,100)
    # User-controlled manual research pages. We do not fetch/scrape these sites.
    return [
        {'name':'Google Shopping listings','url':'https://www.google.com/search?tbm=shop&q='+quote(q)},
        {'name':'eBay sold listings','url':'https://www.ebay.com/sch/i.html?_nkw='+quote(q)+'&LH_Sold=1&LH_Complete=1'},
        {'name':'Supplier search','url':'https://www.google.com/search?q='+quote(q+' wholesale supplier shipping US')},
        {'name':'Google Trends comparison','url':'https://trends.google.com/trends/explore?geo=US&q='+quote(q)},
    ]
