"""Match Thai place text to the most specific admin area (COD-AB pcodes).

match(text, province=None, district=None, subdistrict=None) ->
  {'code','level','name','bbox'} or None. Never returns a point.
"""
import json, os, re
_D = json.load(open(os.environ.get('AREAS_JSON', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out', 'areas.json'))))
P = {c: (n, b) for c, n, b in _D['p']}
A = {c: (n, p, b) for c, n, p, b in _D['a']}
T = {c: (n, a, b) for c, n, a, b in _D['t']}
ALIAS = {'กทม': 'TH10', 'กรุงเทพ': 'TH10', 'กรุงเทพฯ': 'TH10', 'โคราช': 'TH30', 'อยุธยา': 'TH14', 'บางกอก': 'TH10'}
PROV_BY_NAME = {n: c for c, (n, _) in P.items()}
PROV_BY_NAME.update(ALIAS)
_prov_names = sorted(PROV_BY_NAME, key=len, reverse=True)

def _clean(s): return re.sub(r'\s+', '', s or '')
def _strip(s):
    s = _clean(s)
    return re.sub(r'^(จังหวัด|จ\.|อำเภอ|อ\.|กิ่งอำเภอ|เขต|ตำบล|ต\.|แขวง)', '', s)

def find_province(text, hint=None):
    for src in (hint, text):
        s = _clean(src)
        if not s: continue
        h = _strip(s)
        if h in PROV_BY_NAME: return PROV_BY_NAME[h]
        for n in _prov_names:
            # Short names (เลย, ตาก, น่าน, แพร่ ...) are common words: need a prefix.
            if len(n) <= 4 and n not in ALIAS:
                if re.search(r'(จ\.|จังหวัด)' + re.escape(n), s): return PROV_BY_NAME[n]
            elif n in s: return PROV_BY_NAME[n]
    return None

def _children(table, parent): return [(c, v) for c, v in table.items() if v[1] == parent]

def _find_child(table, parent, text, hint, prefixes, prov_name=None):
    s = _clean(text); kids = _children(table, parent)
    if hint:
        h = _strip(hint)
        for c, v in kids:
            if v[0] == h or (prov_name and h == 'เมือง' and v[0] == 'เมือง' + prov_name): return c
    best = None
    for c, v in kids:
        n = v[0]
        names = [n] + (['เมือง'] if prov_name and n == 'เมือง' + prov_name else [])
        for nm in names:
            for pre in prefixes:
                if pre + nm in s and (best is None or len(nm) > best[1]): best = (c, len(nm))
    return best[0] if best else None

def display(code):
    if code in T:
        n, a, _ = T[code]; return ('แขวง' if code.startswith('TH10') else 'ต.') + n + ' ' + display(a)
    if code in A:
        n, p, _ = A[code]; return ('เขต' if p == 'TH10' else 'อ.') + n + ' ' + display(p)
    if code in P: return P[code][0] if code == 'TH10' else 'จ.' + P[code][0]
    return ''

NATIONAL_RE = re.compile(r'\d+\s*(จังหวัด|จว\.?)|ทั่วไทย|ทั่วประเทศ|หลายจังหวัด|\d\s*ภาค|ภาคกลาง|ภาคใต้|ภาคเหนือ|ภาคอีสาน|ภาคตะวันออก|ปริมณฑล')

def provinces_in(text):
    s = _clean(text); found = set()
    for n in _prov_names:
        if len(n) <= 4 and n not in ALIAS:
            if re.search(r'(จ\.|จังหวัด)' + re.escape(n), s): found.add(PROV_BY_NAME[n])
        elif n in s: found.add(PROV_BY_NAME[n])
    return found

def match(text, province=None, district=None, subdistrict=None):
    text = text or ''
    # Region-wide or multi-province news has no single area: no highlight.
    if NATIONAL_RE.search(_clean(text)): return None
    if not province and len(provinces_in(text)) > 1: return None
    p = find_province(text, province)
    if not p: return None
    pname = P[p][0]
    a = _find_child(A, p, text, district, ['อ.', 'อำเภอ', 'เขต'], pname)
    t = _find_child(T, a, text, subdistrict, ['ต.', 'ตำบล', 'แขวง']) if a else None
    code = t or a or p
    level = 'tambon' if t else 'amphoe' if a else 'province'
    bbox = (T.get(code) or A.get(code) or (None, None, P[p][1]))[-1]
    return {'code': code, 'level': level, 'name': display(code), 'bbox': bbox}

if __name__ == '__main__':
    for s in ['น้ำท่วม จ.ชลบุรี หนัก', 'น้ำป่าทะลัก ต.สะบ้าย้อย อ.สะบ้าย้อย จ.สงขลา', 'กทม. แขวงคลองจั่น เขตบางกะปิ น้ำขัง',
              'ชาวนา จ.อุทัยธานี อ.เมือง เดือดร้อน', 'ฝนตกทั่วไทย', 'เลยต้องอพยพ', 'น้ำท่วม จ.เลย', 'ปภ. เผยน้ำท่วม 20 จังหวัด-กทม.', 'ลุยน้ำท่วมปราจีนบุรี-ฉะเชิงเทรา']:
        print(s, '->', match(s))
