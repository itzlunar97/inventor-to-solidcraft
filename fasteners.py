"""
fasteners.py - stand-ins for Inventor Content Center hardware.

Inventor assemblies often use library fasteners (ANSI / DIN / ISO / AS screws, nuts, washers)
that live in Inventor's Content Center, not in the project. Their names describe them fully
("ANSI B18.3.1M - M3x0.5 x 16", "DIN 125 - A 3.2", "ANSI B18.2.4.2M - M3x0.5", ...), so this
module parses the name and builds a parametric SolidCraft part from standard proportions:
socket head cap screw, flat countersunk screw, hex nut, plain washer.
Convention (Inventor's): the screw axis is Z, the underside of the head at Z=0, the shank along -Z.
"""
import json, os, re, tempfile

INCH = 25.4
# nominal diameters of numbered (UNC/UNF) screws
NUMBERED = {0: 0.060, 1: 0.073, 2: 0.086, 3: 0.099, 4: 0.112, 5: 0.125, 6: 0.138, 8: 0.164, 10: 0.190, 12: 0.216}


def _frac(s):
    s = s.strip()
    total = 0.0
    for part in s.split():
        if '/' in part:
            a, b = part.split('/')
            total += float(a) / float(b)
        else:
            total += float(part)
    return total


def parse(name):
    """Content Center name -> {'kind', 'd', 'L'} in mm, or None."""
    n = name.strip()
    low = n.lower()
    # washers
    m = re.search(r'\b(?:din 125|iso 7089|iso 7090)\b.*?(?:a\s*)?(\d+(?:\.\d+)?)', low)
    if m:
        return {'kind': 'washer', 'id': float(m.group(1)), 'name': n}
    m = re.search(r'\bas 1237\b\D*(\d+(?:\.\d+)?)', low)
    if m:
        d = float(m.group(1))
        return {'kind': 'washer', 'id': d + 0.3, 'name': n}
    m = re.search(r'\bid\s*(\d+(?:\.\d+)?)\s*_?\s*od\s*(\d+(?:\.\d+)?)\s*_?\s*thick\s*(\d+(?:\.\d+)?)', low)
    if m:
        return {'kind': 'washer', 'id': float(m.group(1)), 'od': float(m.group(2)), 't': float(m.group(3)), 'name': n}
    # nuts
    if re.search(r'\bb18\.2\.4|\bnut\b|\biso 4032\b|\bdin 934\b', low):
        m = re.search(r'm(\d+(?:\.\d+)?)', low)
        if m:
            return {'kind': 'nut', 'd': float(m.group(1)), 'name': n}
    # metric screws: "M3x0.5 x 16", "M3 x 16", "M3x8"
    m = re.search(r'\bm(\d+(?:\.\d+)?)\s*(?:x\s*\d+(?:\.\d+)?\s*)?x\s*(\d+(?:\.\d+)?)', low) or \
        re.search(r'\bm(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)', low)
    if m and not re.search(r'insert', low):
        d, L = float(m.group(1)), float(m.group(2))
        if 'pitch' in low and L < d:      # "M3x0.5" with no length
            return None
        kind = 'flat' if re.search(r'b18\.3\.5|countersunk|\bfhcs|iso 10642|din 7991', low) else 'socket'
        return {'kind': kind, 'd': d, 'L': L, 'name': n}
    # numbered inch screws: "No. 6 - 32 UNC - 1/4 ..." or "6-32 UNC - 0.75 Inch"
    m = re.search(r'no\.\s*(\d+)\s*-\s*\d+\s*un[cf]\s*-\s*([\d /]+)', low) or \
        re.search(r'\b(\d+)-\d+\s*un[cf]\s*-\s*([\d.]+)\s*inch', low)
    if m and int(m.group(1)) in NUMBERED:
        d = NUMBERED[int(m.group(1))] * INCH
        L = _frac(m.group(2)) * INCH
        return {'kind': 'flat' if 'flat' in low else 'socket', 'd': round(d, 4), 'L': round(L, 4), 'name': n}
    return None


def _profile(spec):
    """half cross-section in the sketch (x = radius, y = Z), counter-clockwise."""
    k = spec['kind']
    if k == 'socket':
        d, L = spec['d'], spec['L']
        r, R, H = d / 2, 0.75 * d, d                 # ISO 4762: head ~1.5d across, d high
        return [[0, -L], [r, -L], [r, 0], [R, 0], [R, H], [0, H]]
    if k == 'flat':
        d, L = spec['d'], spec['L']
        r, R = d / 2, d                              # ISO 10642: head ~2d across, 90 deg
        hh = R - r                                   # 45-degree flank
        return [[0, -(L - hh)], [r, -(L - hh)], [r, 0], [R, hh], [0, hh]]
    if k == 'washer':
        ri = spec['id'] / 2
        ro = spec.get('od', spec['id'] * 2.0) / 2
        t = spec.get('t', max(0.3, round(spec['id'] * 0.17, 2)))
        return [[ri, 0], [ro, 0], [ro, t], [ri, t]]
    return None


def script(spec):
    """SolidCraft command list that builds the stand-in part."""
    name = spec['name']
    plane = {'origin': [0, 0, 0], 'x_dir': [1, 0, 0], 'y_dir': [0, 0, 1]}
    cmds = [{'command': 'sketch.create', 'params': {'plane': plane, 'name': 'Profile'}}]
    if spec['kind'] == 'nut':
        d = spec['d']
        s = 1.6 * d + 0.7 if d < 5 else 1.6 * d       # across flats (M3: 5.5, M5: 8)
        m = 0.8 * d                                  # thickness
        rc = s / 2 / 0.8660254                       # corner radius of the hexagon
        import math
        pts = [[round(rc * math.cos(math.radians(30 + 60 * i)), 5), round(rc * math.sin(math.radians(30 + 60 * i)), 5)]
               for i in range(6)]
        cmds[0]['params']['plane'] = 'XY'
        cmds += [{'command': 'sketch.line', 'params': {'points': pts, 'closed': True}},
                 {'command': 'sketch.circle.center', 'params': {'center': [0, 0], 'radius': d / 2}},
                 {'command': 'sketch.finish', 'params': {}},
                 {'command': 'solid.extrude', 'params': {'distance': f'{m} mm', 'operation': 'new',
                                                        'name': 'Nut', 'body_name': name[:60]}}]
        return cmds
    prof = _profile(spec)
    if not prof:
        return None
    cmds += [{'command': 'sketch.line', 'params': {'points': prof, 'closed': True}},
             {'command': 'sketch.finish', 'params': {}},
             {'command': 'solid.revolve', 'params': {'axis': 'Z', 'angle': '360 deg', 'operation': 'new',
                                                    'name': spec['kind'].title(), 'body_name': name[:60]}}]
    return cmds


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|]', '-', name).strip().rstrip('.')


def make(name, folder, run_script):
    """build (or reuse) the stand-in for a Content Center name; returns its .sc path or None."""
    spec = parse(name)
    if not spec:
        return None
    path = os.path.join(folder, safe_filename(name) + '.sc')
    if os.path.exists(path):
        return path
    cmds = script(spec)
    if not cmds:
        return None
    os.makedirs(folder, exist_ok=True)
    ok, errs = run_script(cmds, path)
    if ok:
        with open(path + '.report.json', 'w', encoding='utf-8') as f:
            json.dump({'part': name, 'result': 'standard-part stand-in', 'spec': spec}, f, indent=1)
        return path
    return None


if __name__ == '__main__':
    for n in ['ANSI B18.3.1M - M3x0.5 x 16, FSHCSM', 'ASME/ANSI B18.3.5M - M3x8(2)', 'AS 1420 - 1973 - M5 x 35',
              'ANSI B18.2.4.2M - M3x0.5', 'DIN 125 - A 3.2', 'AS 1237 - 4', 'BS EN ISO 4762 - M3 x 30',
              'ANSI B18.3 - No. 6 - 32 UNC - 1/4 HS HCS', 'ANSI B18.3 - No. 6 - 32 UNC - 1 1/4 HS HCS',
              'AS 1420 - 1973 - M3 x 6', 'M3 Threaded Insert', 'Splt Loom Bushing 2.0']:
        print(f'{n!r:50} -> {parse(n)}')
