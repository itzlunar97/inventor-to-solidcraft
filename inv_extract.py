#!/usr/bin/env python3
"""
inv_extract.py - best-effort reverse-engineering extractor for Autodesk Inventor
.ipt / .iam files (OLE compound documents, "RSe" storage, schema 2024).

Pulls out, WITHOUT Inventor:
  * file properties (author, Inventor version, material)
  * feature tree names (browser segment) + sketch names
  * named model parameters (name -> value, stored in cm internally -> mm)
  * unnamed model dimensions (sketch / feature dims, "d#" parameters)
  * assembly occurrence tree (component instances + counts, resolved to files)
  * a complexity class (simple / medium / complex) to guide rebuild effort

Does NOT recover: solid geometry (needs Autodesk's kernel), sketch constraints,
assembly mate transforms. Values are heuristics validated on sample files -
see README_SFF_TOOLS.md for the confidence notes.

Usage:
    python inv_extract.py <project_root> [--out <dir>] [--include-old]
Requires: pip install olefile   (openpyxl optional, for the .xlsx output)
"""
import argparse, csv, json, os, re, struct, sys, zlib
from collections import Counter, OrderedDict

try:
    import olefile
except ImportError:
    sys.exit("Missing dependency: pip install olefile")

CM_TO_MM = 10.0
UTF16_RE = re.compile(rb'(?:[\x20-\x7e]\x00){3,}')
IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
# strings that look like identifiers but are browser nodes / binary noise, not parameters
NOISE_RE = re.compile(
    r'^(Solid\d*|Sketch\d*|Origin|Default|Generic|Extrusion\d*|Fillet\d*|Chamfer\d*|Hole\d*|Sweep\d*|'
    r'Revolution\d*|Loft\d*|Shell\d*|Mirror\d*|Work\w*|Center|Point|UCS\d*|'
    r'[A-Z0-9]{2,5}|[A-Z]{2,5}x|[A-Za-z]{1,2}\d?)$')
MASTER_FILES = ('Reference Structure.ipt', 'General Dimensions.ipt')


# --------------------------------------------------------------------------- OLE
def inflate_segment(raw):
    """RSe 'B*' streams hold a zlib stream a short way into the data."""
    for off in range(0, min(128, len(raw))):
        if raw[off:off + 1] == b'\x78' and raw[off + 1:off + 2] in (b'\x5e', b'\x9c', b'\xda', b'\x01'):
            try:
                out = zlib.decompressobj().decompress(raw[off:])
                if out:
                    return out
            except zlib.error:
                pass
    return None


def load_segments(path):
    ole = olefile.OleFileIO(path)
    segs, props = {}, {}
    for e in ole.listdir(streams=True):
        name = e[-1]
        data = ole.openstream(e).read()
        if e[0] == 'RSeStorage' and name.startswith('B'):
            z = inflate_segment(data)
            if z:
                segs[name] = z
        else:
            props[name] = data
    ole.close()
    return segs, props


def u16(data):
    return [m.group().decode('utf-16le') for m in UTF16_RE.finditer(data)]


def u16_pos(data):
    return [(m.start(), m.group().decode('utf-16le')) for m in UTF16_RE.finditer(data)]


def pick(segs, *needles, any_of=False):
    """Return the segment that contains all (or any) of the UTF-16 needles."""
    enc = [n.encode('utf-16le') for n in needles]
    best, best_len = None, -1
    for k, d in segs.items():
        hit = [e in d for e in enc]
        if (any(hit) if any_of else all(hit)) and len(d) > best_len:
            best, best_len = d, len(d)
    return best


# ------------------------------------------------------------------ properties
def read_props(props):
    out = {}
    for name, d in props.items():
        for m in UTF16_RE.finditer(d):
            pass
    blob = b''.join(props.values())
    s = u16(blob)
    for i, t in enumerate(s):
        if t == 'Design Tracking Properties':
            for x in s[i + 1:i + 14]:
                if 'Build' in x:
                    out['inventor_version'] = x
                if x.startswith('C:\\') and 'Materials' in x:
                    out['material_lib'] = x
        if 'Build' in t and 'inventor_version' not in out:
            out['inventor_version'] = t
    # also ascii versions (user properties like author)
    for m in re.finditer(rb'(?:[\x20-\x7e]\x00){3,}', blob):
        pass
    return out


def user_property_values(props):
    """Inventor User Defined Properties: id->string value (e.g. '4.500 mm')."""
    out = {}
    for name, d in props.items():
        if b'I\x00n\x00v\x00e\x00n\x00t\x00o\x00r\x00 \x00U\x00s\x00e\x00r' not in d:
            continue
        # dictionary: id(4) len(4) utf16 name ; value table at start
        try:
            n_props = struct.unpack_from('<I', d, 0x30 + 4)[0]
            table = {}
            for i in range(n_props):
                pid, off = struct.unpack_from('<II', d, 0x38 + i * 8)
                table[pid] = off
            names = {}
            # dictionary entries follow the 'Inventor User Defined Properties' title
            for m in re.finditer(rb'(.{4})(.{4})((?:[\x20-\x7e]\x00)+)', d):
                pid = struct.unpack('<I', m.group(1))[0]
                ln = struct.unpack('<I', m.group(2))[0]
                nm = m.group(3).decode('utf-16le')
                if 0 < pid < 0x100 and ln - 1 == len(nm):
                    names[pid] = nm
            for pid, off in table.items():
                if pid in names and off + 8 < len(d):
                    typ, ln = struct.unpack_from('<II', d, 0x30 + off)
                    if typ == 0x1F:
                        val = d[0x30 + off + 8:0x30 + off + 8 + ln * 2].decode('utf-16le', 'ignore').strip('\x00')
                        out[names[pid]] = val
        except Exception:
            pass
    return out


# -------------------------------------------------------------- parameters/DC
def dup_pair_doubles(data):
    """Inventor writes each parameter value twice, 8 bytes apart (cm units)."""
    vals = {}
    for k in range(0, len(data) - 16):
        v = struct.unpack_from('<d', data, k)[0]
        if v == v and 1e-4 <= abs(v) <= 5000 and abs(v * 1e6 - round(v * 1e6)) < 1e-6:
            vals[k] = v
    return [(k, v) for k, v in vals.items() if vals.get(k + 8) == v]


def chains(pairs, lo=70, hi=125):
    """Group dup-pairs whose spacing is the regular parameter-record stride."""
    out, cur = [], []
    for k, v in pairs:
        if cur and lo <= k - cur[-1][0] <= hi:
            cur.append((k, v))
        else:
            if cur:
                out.append(cur)
            cur = [(k, v)]
    if cur:
        out.append(cur)
    return out


def clean_names(segment):
    """Ordered parameter names, grouped into DCx-delimited blocks (creation order)."""
    pos = u16_pos(segment)
    # 'DCx' shows up as a short UTF-16-looking string between name groups
    dcx = [p for p, t in pos if t.startswith('DCx')]
    stop = segment.find('Solid Bodies'.encode('utf-16le'))
    if stop < 0:
        stop = len(segment)
    first = pos[0][0] if pos else 0

    def idents(lo, hi):
        res = []
        for p, t in pos:
            if lo <= p < hi:
                t = t.rstrip(' ')
                if IDENT_RE.match(t) and not t.startswith(('Pm', 'DCx', 'RDx')) and not NOISE_RE.match(t):
                    res.append(t)
        return res

    blocks = []
    if dcx:
        # groups AFTER each DCx marker are the clean, creation-ordered names;
        # the group before the first marker is the sorted list with id-byte suffixes
        bounds = dcx + [stop]
        for a, b in zip(bounds, bounds[1:]):
            n = idents(a, b)
            if n:
                blocks.append(n)
    else:
        allnames = idents(first, stop)
        half = len(allnames) // 2
        if half and sorted(allnames[:half]) == allnames[:half]:
            blocks.append(allnames[half:half * 2])
        elif allnames:
            blocks.append(allnames)
    return blocks


def extract_parameters(seg):
    """Returns (named_params, unnamed_dims, notes)."""
    named, notes = [], []
    blocks = clean_names(seg)
    pairs = dup_pair_doubles(seg)
    ch = [c for c in chains(pairs) if len(c) >= 2]
    used = set()
    ci = 0
    for names in blocks:
        n = len(names)
        found = None
        # a value chain can run on into unrelated records, so take the first
        # chain that is AT LEAST as long as the block and use its first n values
        for j in range(ci, len(ch)):
            if len(ch[j]) >= n:
                found = j
                ch[j] = ch[j][:n]
                break
        if found is None:
            # tolerate chains split in two by a stray record
            for j in range(ci, len(ch) - 1):
                if len(ch[j]) + len(ch[j + 1]) == n:
                    merged = ch[j] + ch[j + 1]
                    ch[j:j + 2] = [merged]
                    found = j
                    break
        if found is None:
            notes.append(f'block of {n} names ({names[:3]}...) had no matching value chain')
            continue
        ci = found + 1
        for nm, (k, v) in zip(names, ch[found]):
            used.add(k)
            named.append({'name': nm, 'value_cm': round(v, 6), 'value_mm': round(v * CM_TO_MM, 4)})
    # leftovers: sketch/feature dimensions not in a named chain
    chain_keys = {k for c in ch for k, _ in c}
    dims = []
    for k, v in pairs:
        if k in chain_keys or abs(v) == 1.0:
            continue
        dims.append(round(v * CM_TO_MM, 4))
    return named, dims, notes


# ----------------------------------------------------------------- feature tree
FEATURE_PREFIX = ('Extrusion', 'Revolution', 'Sweep', 'Loft', 'Fillet', 'Chamfer', 'Hole', 'Thread',
                  'Rectangular Pattern', 'Circular Pattern', 'Mirror', 'Shell', 'Split', 'Combine',
                  'Coil', 'Emboss', 'Rib', 'Face Draft', 'Derive', 'Thicken', 'Boundary', 'Cut',
                  'Flange', 'Contour', 'Bend', 'Corner', 'Face', 'Move', 'Delete', 'Scale', 'Patch',
                  'Stitch', 'Trim', 'Extend', 'Offset', 'Copy', 'iFeature', 'Direct', 'Pattern')


def extract_features(segs):
    """Feature tree = names listed after 'Center Point' in the browser segment."""
    tree, sketches = [], []
    b = pick(segs, 'Origin', 'Center Point')
    if b:
        s = u16(b)
        if 'Center Point' in s:
            i = s.index('Center Point') + 1
            for t in s[i:]:
                if t in ('NbDefault', "Engineer's Notebook") or t.startswith('UCx') or t.startswith('NBx'):
                    break
                if t.startswith(('Work Plane', 'Work Axis', 'Work Point', 'UCS')):
                    tree.append(t)
                    continue
                tree.append(t)
    br = pick(segs, 'PmBRepSegment', 'Solid Bodies') or pick(segs, 'Solid Bodies', any_of=True)
    if br:
        for t in u16(br):
            if re.match(r'^Sketch\s?\d+', t) and t not in sketches:
                sketches.append(t)
    for t in tree[:]:
        if re.match(r'^Sketch\s?\d+', t) and t not in sketches:
            sketches.append(t)
    features = [t for t in tree if not t.startswith(('Work ', 'UCS')) and not re.match(r'^Sketch\s?\d+', t)]
    workgeo = [t for t in tree if t.startswith(('Work ', 'UCS'))]
    return features, sketches, workgeo


def brep_size(segs):
    br = pick(segs, 'PmBRepSegment', any_of=True)
    asm = [d for d in segs.values() if b'ASM BinaryFile' in d]
    return len(br) if br else 0, sum(len(a) for a in asm)


# ------------------------------------------------------------------- assemblies
def extract_assembly(segs):
    b = pick(segs, 'AmPartInstEntry', 'AmAssmInstEntry', any_of=True)
    if not b:
        return [], 0
    s = u16(b)
    occ = []
    for i, t in enumerate(s):
        if t in ('AmPartInstEntry', 'AmAssmInstEntry') and i > 0:
            nm = s[i - 1]
            m = re.match(r'^(.*):(\d+)$', nm)
            if m:
                occ.append((m.group(1), 'part' if t == 'AmPartInstEntry' else 'assembly'))
    cons = sum(1 for t in s if t == 'AmConstraintsEntry')
    cnt = OrderedDict()
    for nm, kind in occ:
        cnt.setdefault((nm, kind), 0)
        cnt[(nm, kind)] += 1
    return [{'component': k[0], 'kind': k[1], 'count': v} for k, v in cnt.items()], len(occ)


# ------------------------------------------------------------------------ main
def classify(n_features, brep_kb, is_asm):
    if is_asm:
        return 'assembly'
    if n_features <= 4 and brep_kb < 60:
        return 'simple'
    if n_features <= 12 and brep_kb < 400:
        return 'medium'
    return 'complex'


def process(path, root, index):
    rel = os.path.relpath(path, root)
    is_asm = path.lower().endswith('.iam')
    rec = {'file': rel, 'type': 'assembly' if is_asm else 'part'}
    try:
        segs, props = load_segments(path)
    except Exception as e:
        rec['error'] = f'{type(e).__name__}: {e}'
        return rec, [], [], []
    rec.update(read_props(props))
    seg = pick(segs, 'PmBRepSegment') or pick(segs, 'AmBREPSegment') or pick(segs, 'Solid Bodies', any_of=True)
    if seg is None:
        seg = max(segs.values(), key=len) if segs else b''
    # DC data lives in the segment that carries names + 'DCx'
    named, dims, notes = extract_parameters(seg) if seg else ([], [], ['no brep/DC segment'])
    mat = [t for t in u16(pick(segs, 'Miscellaneous', any_of=True) or b'') if t.endswith('Generic') or 'Carbon' in t or 'Aluminum' in t]
    rec['material_hint'] = ', '.join(OrderedDict.fromkeys(mat))[:120]
    features, sketches, workgeo = extract_features(segs)
    brep_b, asm_b = brep_size(segs)
    rec['n_features'] = len(features)
    rec['n_sketches'] = len(sketches)
    rec['features'] = ' > '.join(sketches + features)
    rec['n_named_params'] = len(named)
    rec['n_unnamed_dims'] = len(dims)
    rec['brep_kb'] = round(brep_b / 1024, 1)
    rec['asm_geom_kb'] = round(asm_b / 1024, 1)
    comps, n_occ = ([], 0)
    if is_asm:
        comps, n_occ = extract_assembly(segs)
        rec['n_occurrences'] = n_occ
        rec['n_unique_components'] = len(comps)
    rec['class'] = classify(len(features), rec['brep_kb'], is_asm)
    rec['notes'] = '; '.join(notes)
    prow = [{'file': rel, **p} for p in named]
    drow = [{'file': rel, 'dim_mm': d} for d in dims]
    crow = []
    for c in comps:
        target = index.get(c['component'].lower())
        crow.append({'assembly': rel, **c, 'resolved_file': target or ''})
    return rec, prow, drow, crow


def write_csv(path, rows, fields=None):
    if not rows:
        return
    fields = fields or list(OrderedDict.fromkeys(k for r in rows for k in r))
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('--out', default=None)
    ap.add_argument('--include-old', action='store_true', help='also scan OldVersions folders')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
    os.makedirs(out, exist_ok=True)

    files = []
    for dp, dn, fn in os.walk(root):
        if not a.include_old and 'OldVersions' in dp.split(os.sep):
            continue
        for f in fn:
            if f.lower().endswith(('.ipt', '.iam')):
                files.append(os.path.join(dp, f))
    index = {os.path.splitext(os.path.basename(f))[0].lower(): os.path.relpath(f, root) for f in files}
    print(f'{len(files)} Inventor files under {root}')

    parts, params, dims, refs = [], [], [], []
    for i, f in enumerate(sorted(files), 1):
        r, p, d, c = process(f, root, index)
        parts.append(r); params += p; dims += d; refs += c
        if i % 25 == 0 or i == len(files):
            print(f'  {i}/{len(files)}')

    # confidence: compare every named value to the master parameter sheets
    master = {}
    for p in params:
        if p['file'].endswith(MASTER_FILES):
            master.setdefault(p['name'], p['value_mm'])
    for p in params:
        nm = p['name'].split('_')[0] if p['name'].rsplit('_', 1)[-1].isdigit() else p['name']
        if nm in master:
            p['confidence'] = 'verified' if abs(master[nm] - p['value_mm']) < 1e-6 else 'MISMATCH'
        else:
            p['confidence'] = 'part-specific'
    for r in parts:
        mine = [p for p in params if p['file'] == r['file']]
        r['n_mismatch'] = sum(1 for p in mine if p['confidence'] == 'MISMATCH')

    write_csv(os.path.join(out, 'parts.csv'), parts)
    write_csv(os.path.join(out, 'parameters.csv'), params)
    write_csv(os.path.join(out, 'dimensions_unnamed.csv'), dims)
    write_csv(os.path.join(out, 'assembly_refs.csv'), refs)
    with open(os.path.join(out, 'summary.json'), 'w') as f:
        json.dump({'files': len(files), 'classes': Counter(p.get('class', 'error') for p in parts)}, f, indent=2)
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        for name, rows in (('parts', parts), ('parameters', params), ('dimensions', dims), ('assembly_refs', refs)):
            ws = wb.create_sheet(name)
            if rows:
                fields = list(OrderedDict.fromkeys(k for r in rows for k in r))
                ws.append(fields)
                for r in rows:
                    ws.append([r.get(k, '') for k in fields])
        del wb['Sheet']
        wb.save(os.path.join(out, 'inventor_dump.xlsx'))
    except ImportError:
        print('openpyxl not installed - skipped .xlsx (CSV files written)')
    print(Counter(p.get('class', 'error') for p in parts))
    print('Output:', out)


if __name__ == '__main__':
    main()
