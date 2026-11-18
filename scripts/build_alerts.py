#!/usr/bin/env python3
"""Crowd Flood Map / แจ้งภัย - all-hazards public alerts builder.

Writes alerts.json from public sources only. Never invents items: every item
is a real headline/record with its real https source link. No personal data,
no crowd-reporter coordinates (only CRISIS WATCH *news* items and official
TMD earthquake epicentres carry a location).

Sources:
  - Firestore `incidents` (public read) where source in news/rss  -> app's own news collector
  - TMD earthquakes RSS (earthquake.tmd.go.th)                      -> earthquake, geo
  - TMD weather warnings RSS (tmd.go.th/api/xml/warning-news)       -> storm/official
  - Google News RSS (Thai) per hazard: ปภ./กรมอุตุฯ/กรมชลฯ/GISTDA etc. -> headlines, no pin

Usage:
  python3 build_alerts.py               # build -> ./out/alerts.json
  python3 build_alerts.py --push        # build, push to GitHub only if content changed
Env (for --push):
  GITHUB_ALERTS_TOKEN  fine-grained PAT, Contents RW on ALERTS_REPO only
                       (falls back to `gh auth token` if gh is logged in)
  ALERTS_REPO          default ponnhub/crowdflood-hazard-alerts
"""
import base64, datetime as dt, email.utils, hashlib, json, os, re, subprocess, sys
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET

os.environ.setdefault('AREAS_JSON', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'areas.json'))
import area_match

UTC = dt.timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get('ALERTS_REPO', 'ponnhub/crowdflood-hazard-alerts')
UA = {'User-Agent': 'Mozilla/5.0 (crowdflood-hazard-alerts; +https://github.com/%s)' % REPO}
NOW = dt.datetime.now(UTC)
MAX_AGE_H = 48
MAX_ITEMS = 200
HAZARDS = ['flood', 'flash_flood', 'storm', 'landslide', 'earthquake', 'wildfire',
           'drought', 'pm25', 'heat', 'tsunami', 'other']
URGENT_TYPES = {'earthquake', 'flash_flood', 'tsunami'}

def get(url, timeout=20, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers={**UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()

def iso(t): return t.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')

# ---- classification (deterministic keyword rules, deepest/most specific first)
RULES = [
    ('tsunami', r'สึนามิ|tsunami'),
    ('earthquake', r'แผ่นดินไหว|อาฟเตอร์ช็อก|earthquake'),
    ('flash_flood', r'น้ำป่า|น้ำป่าไหลหลาก|flash ?flood'),
    ('landslide', r'ดินโคลนถล่ม|ดินถล่ม|ดินสไลด์|landslide'),
    ('wildfire', r'ไฟป่า|จุดความร้อน|hotspot|wildfire'),
    ('pm25', r'pm ?2\.5|ฝุ่นละออง|ฝุ่นพิษ|หมอกควัน|คุณภาพอากาศ'),
    ('heat', r'อากาศร้อนจัด|คลื่นความร้อน|ฮีทสโตรก|heat ?stroke|heatwave'),
    ('drought', r'ภัยแล้ง|แล้งจัด|ขาดแคลนน้ำ|drought'),
    ('storm', r'พายุ|ลมแรง|ลูกเห็บ|มรสุม|ดีเปรสชัน|ไต้ฝุ่น|คลื่นสูง|ฝนตกหนัก|storm|typhoon'),
    ('flood', r'น้ำท่วม|ท่วมขัง|อุทกภัย|น้ำล้นตลิ่ง|ระดับน้ำ|flood'),
]
def classify(text, default='other'):
    for h, rx in RULES:
        if re.search(rx, text, re.I): return h
    return default

OFFICIAL_RE = re.compile(r'ปภ\.|กรมป้องกันและบรรเทาสาธารณภัย|กรมอุตุ|กรมชลประทาน|GISTDA|จิสด้า|ประกาศเตือน|เตือนภัย|แจ้งเตือน|ฉบับที่', re.I)
DISASTER_RE = re.compile('|'.join(rx for _, rx in RULES) + r'|ภัยพิบัติ|สาธารณภัย|อพยพ|เตือนภัย', re.I)
JUNK_RE = re.compile(r'ดวง|หวย|เลขเด็ด|ละคร|ซีรีส์|ราคาทอง|ฟุตบอล|โปรโมชั่น|ส่วนลด', re.I)

def gnews(q): return 'https://news.google.com/rss/search?q=%s&hl=th&gl=TH&ceid=TH:th' % urllib.parse.quote(q + ' when:2d')
GN_QUERIES = [
    ('flood', 'น้ำท่วม OR อุทกภัย ปภ. OR กรมชลประทาน'),
    ('flash_flood', 'น้ำป่าไหลหลาก'),
    ('storm', 'พายุ OR ฝนตกหนัก กรมอุตุนิยมวิทยา ประกาศ'),
    ('landslide', 'ดินโคลนถล่ม OR ดินถล่ม'),
    ('earthquake', 'แผ่นดินไหว ไทย'),
    ('wildfire', 'ไฟป่า OR จุดความร้อน GISTDA'),
    ('drought', 'ภัยแล้ง'),
    ('pm25', 'ฝุ่น PM2.5 เกินมาตรฐาน'),
    ('heat', 'อากาศร้อนจัด เตือน'),
    ('tsunami', 'สึนามิ เตือนภัย'),
    ('other', 'ปภ. สาธารณภัย แจ้งเตือน'),
]

def item(**kw):
    kw['id'] = hashlib.sha1(kw['source_url'].encode()).hexdigest()[:16]
    return kw

def rss_items(xml):
    root = ET.fromstring(xml)
    return root.iter('item')

def txt(e, tag):
    x = e.find(tag)
    return (x.text or '').strip() if x is not None and x.text else ''

def from_gnews():
    out = []
    for default, q in GN_QUERIES:
        try: xml = get(gnews(q))
        except Exception as e: print('gnews fail', q, e, file=sys.stderr); continue
        for it in list(rss_items(xml))[:12]:
            title, link = txt(it, 'title'), txt(it, 'link')
            src = txt(it, 'source')
            if not link.startswith('https://') or not title: continue
            if src and title.endswith(' - ' + src): title = title[:-len(src) - 3]
            if JUNK_RE.search(title) or not DISASTER_RE.search(title): continue
            try: t = email.utils.parsedate_to_datetime(txt(it, 'pubDate'))
            except Exception: continue
            h = classify(title, default)
            official = bool(OFFICIAL_RE.search(title))
            out.append(item(title=title, summary='', category='news', hazard_type=h,
                            severity='high' if official else 'medium',
                            urgent=official and bool(re.search(r'ประกาศ|เตือน|ด่วน', title)) and h != 'other',
                            official=official, source=src or 'Google News', source_url=link,
                            time=iso(t), location=None))
    return out

TMD_NS = {'geo': 'http://www.w3.org/2003/01/geo/', 'tmd': 'http://www.earthquake.tmd.go.th'}
def from_tmd_quakes():
    out = []
    try: xml = get('https://earthquake.tmd.go.th/feed/rss_tmd.xml')
    except Exception as e: print('tmd quake fail', e, file=sys.stderr); return out
    for it in rss_items(xml):
        try:
            lat = float(it.find('geo:lat', TMD_NS).text); lng = float(it.find('geo:long', TMD_NS).text)
            mag = float(it.find('tmd:magnitude', TMD_NS).text)
            t = dt.datetime.strptime(it.find('tmd:time', TMD_NS).text.strip(), '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=UTC)
        except Exception: continue
        link = txt(it, 'link')
        if not link.startswith('https://'): continue
        place = txt(it, 'title'); near = txt(it, 'comments')
        felt_th = 5 <= lat <= 21 and 97 <= lng <= 106
        out.append(item(title='แผ่นดินไหว ขนาด %.1f %s' % (mag, place), summary=near,
                        category='official', hazard_type='earthquake',
                        severity='high' if mag >= 5 or (felt_th and mag >= 3.5) else 'medium',
                        urgent=mag >= 5 or (felt_th and mag >= 4), official=True,
                        source='กรมอุตุนิยมวิทยา', source_url=link, time=iso(t),
                        magnitude=mag,
                        location={'name': place, 'lat': lat, 'lng': lng, 'precision': 'epicentre'}))
    return out

def from_tmd_warnings():
    out = []
    try: xml = get('https://www.tmd.go.th/api/xml/warning-news')
    except Exception as e: print('tmd warn fail', e, file=sys.stderr); return out
    for it in rss_items(xml):
        title = txt(it, 'title'); link = txt(it, 'link').replace('http://', 'https://')
        try:
            d, m, y_hms = txt(it, 'pubDate').split('/', 2); y, hms = y_hms.split(' ')
            t = dt.datetime.strptime('%s-%s-%s %s' % (int(y) - 543, m, d, hms), '%Y-%m-%d %H:%M:%S').replace(tzinfo=dt.timezone(dt.timedelta(hours=7)))
        except Exception: continue
        if not link.startswith('https://') or not title: continue
        h = classify(title, 'storm')
        out.append(item(title=title, summary='', category='official', hazard_type=h, severity='high',
                        urgent=True, official=True, source='กรมอุตุนิยมวิทยา', source_url=link,
                        time=iso(t), location=None))
    return out

FS = 'https://firestore.googleapis.com/v1/projects/research-thai-rtaf/databases/(default)/documents:runQuery'
def fsval(v):
    if not isinstance(v, dict): return None
    for k in ('stringValue', 'booleanValue', 'timestampValue'):
        if k in v: return v[k]
    if 'integerValue' in v: return int(v['integerValue'])
    if 'doubleValue' in v: return v['doubleValue']
    if 'mapValue' in v: return {k: fsval(x) for k, x in v['mapValue'].get('fields', {}).items()}
    if 'arrayValue' in v: return [fsval(x) for x in v['arrayValue'].get('values', [])]
    return None

def from_firestore_news():
    """The app's own public news collector output (incidents, source news/rss)."""
    since = iso(NOW - dt.timedelta(hours=MAX_AGE_H))
    q = {'structuredQuery': {'from': [{'collectionId': 'incidents'}],
         'where': {'fieldFilter': {'field': {'fieldPath': 'created_at'}, 'op': 'GREATER_THAN_OR_EQUAL',
                                   'value': {'timestampValue': since}}},
         'orderBy': [{'field': {'fieldPath': 'created_at'}, 'direction': 'DESCENDING'}], 'limit': 500}}
    try: rows = json.loads(get(FS, data=json.dumps(q).encode(), headers={'content-type': 'application/json'}, method='POST'))
    except Exception as e: print('firestore fail', e, file=sys.stderr); return []
    out = []
    for r in rows:
        doc = r.get('document')
        if not doc: continue
        f = {k: fsval(v) for k, v in doc['fields'].items()}
        if str(f.get('source', '')).lower() not in ('news', 'rss'): continue
        if f.get('hq_hidden') or f.get('hq_deleted') or f.get('merged_into'): continue
        url = f.get('source_url') or ''
        if not url.startswith('https://'): continue
        title = (f.get('title') or f.get('text') or '').strip().split('\n')[0][:200]
        loc = None
        locs = f.get('locations') or []
        area = f.get('map_hidden') is True or f.get('geo_precision') == 'area'
        if locs and isinstance(locs[0], dict) and not area:
            l = locs[0]; hq = f.get('hq_location')
            lat = (hq or {}).get('lat', l.get('lat')); lng = (hq or {}).get('lng', l.get('lng', l.get('lon')))
            prec = l.get('precision')
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and prec not in ('province', 'area', 'city'):
                loc = {'name': l.get('name') or '', 'lat': lat, 'lng': lng, 'precision': prec or 'geocoded'}
        area_name = None
        if loc is None:
            facts = f.get('facts') if isinstance(f.get('facts'), dict) else {}
            hint = locs[0].get('name') if locs and isinstance(locs[0], dict) else ''
            area_name = area_match.match('%s %s %s' % (hint or '', title, f.get('text') or ''),
                                         province=facts.get('province'), district=facts.get('district'),
                                         subdistrict=facts.get('subdistrict'))
        text = '%s %s' % (title, f.get('text') or '')
        out.append(item(title=title, summary=(f.get('summary') or '')[:400], category=f.get('category') or 'news',
                        hazard_type=classify(text, 'flood'), severity=f.get('severity') or 'medium',
                        urgent=False, official=bool(OFFICIAL_RE.search(text)),
                        source=f.get('author_name') or 'ข่าว', source_url=url,
                        time=f.get('created_at') if isinstance(f.get('created_at'), str) else iso(NOW),
                        location=loc, area=area_name, incident_id=doc['name'].rsplit('/', 1)[1]))
    return out

def norm_title(t): return re.sub(r'\W+', '', t.lower())[:60]

def build():
    raw = from_firestore_news() + from_tmd_quakes() + from_tmd_warnings() + from_gnews()
    cutoff = NOW - dt.timedelta(hours=MAX_AGE_H)
    seen_u, seen_t, items = set(), set(), []
    for it in sorted(raw, key=lambda x: (x.get('official', False), x['time']), reverse=True):
        try: t = dt.datetime.fromisoformat(it['time'].replace('Z', '+00:00'))
        except Exception: continue
        if t < cutoff or t > NOW + dt.timedelta(hours=1): continue
        if it['hazard_type'] not in HAZARDS: it['hazard_type'] = 'other'
        nt = norm_title(it['title'])
        if it['source_url'] in seen_u or nt in seen_t: continue
        seen_u.add(it['source_url']); seen_t.add(nt)
        if not it.get('location') and not it.get('area'):
            it['area'] = area_match.match('%s %s' % (it['title'], it.get('summary') or ''))
        if not it.get('area'): it.pop('area', None)
        items.append(it)
    items.sort(key=lambda x: (x['urgent'], x['time']), reverse=True)
    items = items[:MAX_ITEMS]
    counts = {h: sum(1 for i in items if i['hazard_type'] == h) for h in HAZARDS}
    return {'schema': 2, 'areas_source': 'Royal Thai Survey Department via OCHA HDX COD-AB (CC BY-IGO 3.0)', 'updated_at': iso(NOW), 'hazard_types': HAZARDS, 'counts': counts, 'items': items}

def content_hash(doc):
    return hashlib.sha256(json.dumps(doc['items'], ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def token():
    t = os.environ.get('GITHUB_ALERTS_TOKEN')
    if t: return t
    try: return subprocess.check_output(['gh', 'auth', 'token'], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception: return None

def push(doc):
    tok = token()
    if not tok: sys.exit('No GITHUB_ALERTS_TOKEN (fine-grained, Contents RW on %s) and gh not logged in' % REPO)
    api = 'https://api.github.com/repos/%s/contents/alerts.json' % REPO
    h = {'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json'}
    sha, old = None, None
    try:
        cur = json.loads(get(api, headers=h)); sha = cur['sha']
        old = json.loads(base64.b64decode(cur['content']))
    except urllib.error.HTTPError as e:
        if e.code != 404: raise
    if old and content_hash(old) == content_hash(doc):
        print('unchanged (%d items) - no push' % len(doc['items'])); return
    body = {'message': 'alerts: %d items @ %s' % (len(doc['items']), doc['updated_at']),
            'content': base64.b64encode(json.dumps(doc, ensure_ascii=False, indent=1).encode()).decode()}
    if sha: body['sha'] = sha
    get(api, data=json.dumps(body).encode(), headers={**h, 'content-type': 'application/json'}, method='PUT')
    print('pushed %d items' % len(doc['items']))

if __name__ == '__main__':
    doc = build()
    os.makedirs(os.path.join(HERE, 'out'), exist_ok=True)
    with open(os.path.join(HERE, 'out', 'alerts.json'), 'w') as f: json.dump(doc, f, ensure_ascii=False, indent=1)
    print('built', len(doc['items']), 'items', doc['counts'])
    if '--push' in sys.argv: push(doc)
