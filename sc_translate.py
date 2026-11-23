#!/usr/bin/env python3
"""
sc_translate.py - turn one decoded Inventor part (il_decode.py output) into a SolidCraft design.

  python sc_translate.py <decoded out dir> <dest.sc> [--name "Part Name"] [--keep-scripts DIR]

Builds, in order:
  1. every named Inventor parameter (user + model d#), with its real expression when that
     expression re-evaluates to the decoded value, otherwise the exact value;
  2. every 2D sketch on its exact plane: points, lines, circles, arcs, ellipses, the geometric
     constraints SolidCraft has, and the dimensions driven by the parameters;
  3. the feature timeline (Extrude, Revolve) when every solid feature of the part is supported.
Then it checks the native result against the exact solid (InventorLoader's ACIS->STEP) by
volume and bounding box. If the native build is missing or does not match, the design is
rebuilt as: exact STEP body + all parameters + all sketches, so nothing is lost.
Writes <dest>.report.json next to the design.
"""
import argparse, json, math, os, re, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tool_paths import freecadcmd, solidcraft_cli
SC_CLI = solidcraft_cli()
CM = 10.0  # Inventor internal length unit is cm
UNIT_MM = {'mm': 1.0, 'cm': 10.0, 'm': 1000.0, 'in': 25.4, 'inch': 25.4, 'ft': 304.8, 'um': 0.001}
UNIT_DEG = {'°': 1.0, 'deg': 1.0, 'rad': 180.0 / math.pi, 'grad': 0.9}
NATIVE_FEATURES = {'Extrude', 'Revolve', 'Fillet', 'Chamfer'}
IGNORED_FEATURES = {'Reference'}   # base bodies; only matter when nothing else exists


# ----------------------------------------------------------------------------- helpers
def ref(v):
    return v.get('ref') if isinstance(v, dict) else None


def vec(v):
    return [float(x) for x in v] if isinstance(v, (list, tuple)) else None


def mm(v):
    return round(float(v) * CM, 6)


def parse_matrix(s):
    m = re.search(r'm=\[\[(.*)\]\]', s or '')
    if not m:
        return None
    rows = [[float(x) for x in r.split(',')] for r in m.group(1).split('],[')]
    return rows if len(rows) >= 3 else None


def parse_quantity(text):
    """'25mm' / '0.125in' / '90°' / '1' -> (number in mm|deg|plain, kind, display_unit)."""
    if text is None:
        return None, None, None
    t = str(text).strip()
    m = re.match(r'^([-+]?[\d.]+(?:[eE][-+]?\d+)?)\s*([A-Za-z°]*)$', t)
    if not m:
        return None, None, None
    num, unit = float(m.group(1)), m.group(2)
    if unit in UNIT_MM:
        return num * UNIT_MM[unit], 'length', unit
    if unit in UNIT_DEG:
        return num * UNIT_DEG[unit], 'angle', 'deg' if unit == '°' else unit
    if unit in ('', 'ul'):
        return num, 'plain', ''
    return None, None, unit


def sc_literal(text):
    """Inventor value text -> SolidCraft expression literal ('0.125in' -> '0.125 in')."""
    v, kind, unit = parse_quantity(text)
    if v is None:
        return None
    num = re.match(r'^([-+]?[\d.]+(?:[eE][-+]?\d+)?)', str(text).strip()).group(1)
    if kind == 'plain':
        return num
    return f'{num} {unit}'


EXPR_TOKEN = re.compile(r'\s*(?:(?P<num>\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+)\s*(?P<unit>mm|cm|m|in|ft|°|deg|rad|ul)?(?![A-Za-z_])|(?P<id>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>[-+*/^(),;]))')
FUNCS = {'sin': lambda x: math.sin(math.radians(x)), 'cos': lambda x: math.cos(math.radians(x)),
         'tan': lambda x: math.tan(math.radians(x)), 'sqrt': math.sqrt, 'abs': abs,
         'ceil': math.ceil, 'floor': math.floor, 'round': round, 'min': min, 'max': max}


def translate_expr(expr, values):
    """Inventor expression -> (SolidCraft expression, numeric value in mm/deg) or (None, None)."""
    if not expr:
        return None, None
    pos, py, sc, idents = 0, [], [], set()
    s = expr.strip()
    while pos < len(s):
        m = EXPR_TOKEN.match(s, pos)
        if not m or m.end() == pos:
            return None, None
        pos = m.end()
        if m.group('num'):
            n, u = m.group('num'), m.group('unit') or ''
            f = float(n)
            if u in UNIT_MM:
                py.append(repr(f * UNIT_MM[u])); sc.append(f'{n} {u}')
            elif u in UNIT_DEG:
                py.append(repr(f * UNIT_DEG[u])); sc.append(f'{n} deg' if u == '°' else f'{n} {u}')
            else:
                py.append(repr(f)); sc.append(n)
        elif m.group('id'):
            name = m.group('id')
            if name in FUNCS:
                py.append(f'F["{name}"]'); sc.append(name)
            elif name.upper() == 'PI':
                py.append(repr(math.pi)); sc.append('pi')
            elif name in values:
                py.append(repr(values[name])); sc.append(name); idents.add(name)
            else:
                return None, None
        else:
            op = m.group('op')
            py.append('**' if op == '^' else (',' if op == ';' else op))
            sc.append('^' if op == '^' else (',' if op == ';' else op))
    try:
        val = eval(' '.join(py), {'__builtins__': {}}, {'F': FUNCS})
    except Exception:
        return None, None
    return ' '.join(sc).replace('( ', '(').replace(' )', ')'), float(val), idents


# ------------------------------------------------------------------------ the translator
class Part:
    def __init__(self, out_dir):
        path = os.path.join(out_dir, 'part.dc.json')
        with open(path, encoding='utf-8') as f:
            self.N = {n['index']: n for n in json.load(f)}
        self.step = os.path.join(out_dir, 'part.step')
        self.notes = []
        self.cmds = []

    def node(self, r):
        return self.N.get(r) if r is not None else None

    def by_type(self, t):
        return [n for n in self.N.values() if n.get('type') == t]

    # .................................................................. parameters
    def parameters(self):
        params = [n for n in self.by_type('Parameter') if n.get('name') and not n['name'].startswith('RDxVar')]
        info = {}
        # SolidCraft names: letters, digits, _ and a leading letter ("iFeature1:1:insertOD" -> "iFeature1_1_insertOD")
        self.safe_name = {}
        for p in params:
            safe = re.sub(r'\W', '_', p['name'])
            if not safe[:1].isalpha():
                safe = 'p_' + safe
            while safe in self.safe_name.values():
                safe += '_'
            self.safe_name[p['name']] = safe
        for p in params:
            v, kind, unit = parse_quantity(p.get('value'))
            info[self.safe_name[p['name']]] = {'node': p, 'value': v, 'kind': kind, 'unit': unit}
        values = {k: i['value'] for k, i in info.items() if i['value'] is not None}
        resolved, out, pending = {}, [], dict(info)
        # emit in dependency order; expressions that do not re-evaluate to the decoded value
        # (e.g. constants Inventor typed in another unit) fall back to the exact value
        for _ in range(len(pending) + 2):
            progressed = False
            for name in list(pending):
                i = pending[name]
                lit = sc_literal(i['node'].get('value'))
                expr_txt = i['node'].get('expr')
                choice = None
                tr = translate_expr(expr_txt, values) if expr_txt else (None, None)
                if tr and tr[0] is not None and i['value'] is not None:
                    sc_expr, val, idents = tr
                    if not (idents - set(resolved)) and abs(val - i['value']) <= 1e-6 * max(1.0, abs(i['value'])):
                        choice = sc_expr if idents else (lit or sc_expr)
                    elif idents - set(resolved):
                        continue   # wait for what it references
                if choice is None:
                    if lit is None:
                        self.notes.append(f'parameter {name}: unreadable value {i["node"].get("value")!r}')
                        del pending[name]
                        continue
                    if expr_txt and tr and tr[0] is not None and tr[1] is not None:
                        self.notes.append(f'parameter {name}: expression "{expr_txt}" gives {tr[1]:g}, '
                                          f'decoded value {i["node"].get("value")} - used the value')
                    choice = lit
                out.append((name, choice))
                resolved[name] = choice
                del pending[name]
                progressed = True
            if not pending or not progressed:
                break
        for name, i in pending.items():          # circular or unresolved: exact values
            lit = sc_literal(i['node'].get('value'))
            if lit:
                out.append((name, lit))
                self.notes.append(f'parameter {name}: expression "{i["node"].get("expr")}" unresolved - used the value')
        self.literals = {}
        for name, i in info.items():
            lit = sc_literal(i['node'].get('value'))
            if lit:
                self.literals[name] = lit
        for name, expr in out:
            prm = {'name': name, 'expression': expr}
            if info.get(name, {}).get('kind') == 'plain':
                prm['unit'] = ''          # a count / ratio (Inventor "ul"), not a length
            self.cmds.append({'command': 'parameters.add', 'params': prm})
        self.param_names = {n for n, _ in out}
        self.values = values
        return len(out)

    def param_expr(self, r):
        p = self.node(r)
        if not p:
            return None
        safe = getattr(self, 'safe_name', {}).get(p.get('name'))
        if safe and safe in getattr(self, 'param_names', ()):
            return safe
        return sc_literal(p.get('value'))

    # ...................................................................... sketches
    def ai_of(self, n):
        nx = self.node(ref(n['props'].get('next')))
        hops = 0
        while nx is not None and hops < 6:
            if nx.get('type') == 'FxAttrStyles':
                return nx['props'].get('associativeID')
            nx = self.node(ref(nx['props'].get('next')))
            hops += 1
        return None

    def sketch(self, sk):
        """emit one Sketch2D; returns {'name', 'ai': {associativeID: sc curve id}, 'normal'}."""
        p = sk['props']
        tr = self.node(ref(p.get('transformation')))
        m = parse_matrix(tr['props'].get('transformation') if tr else '')
        if m is None:
            self.notes.append(f'sketch {sk.get("name")}: no placement - skipped')
            return None
        origin = [round(m[0][3], 6), round(m[1][3], 6), round(m[2][3], 6)]
        xd = [m[0][0], m[1][0], m[2][0]]
        yd = [m[0][1], m[1][1], m[2][1]]
        normal = [xd[1] * yd[2] - xd[2] * yd[1], xd[2] * yd[0] - xd[0] * yd[2], xd[0] * yd[1] - xd[1] * yd[0]]
        name = sk.get('name') or f'Sketch_{sk["index"]}'
        self.cmds.append({'command': 'sketch.create',
                          'params': {'plane': {'origin': origin, 'x_dir': xd, 'y_dir': yd}, 'name': name}})
        ents = [self.node(ref(e)) for e in p.get('entities') or []]
        ents = [e for e in ents if e]
        sc_id, ai_map, counters = {}, {}, {'c': 0, 'a': 0, 'e': 0, 'l': 0}
        unsupported = 0

        def pid(r):
            return sc_id.get(r) if r in sc_id and sc_id[r].startswith('P') else None

        for e in ents:                                   # points first
            if e['type'] == 'Point2D':
                pos = vec(e['props'].get('pos'))
                if pos:
                    sid = f'P{e["index"]}'
                    sc_id[e['index']] = sid
                    self.cmds.append({'command': 'sketch.point',
                                      'params': {'point': [mm(pos[0]), mm(pos[1])], 'id': sid, 'connect': False}})
        for e in ents:                                   # then curves
            t, ep = e['type'], e['props']
            cons = bool(e.get('construction'))
            if t == 'Line2D':
                # points = [start, end, points lying on the line (midpoints...)]
                pts = [ref(x) for x in (ep.get('points') or [])[:2]]
                if len(pts) == 2 and all(pid(x) for x in pts) and pts[0] != pts[1]:
                    sid = f'L{e["index"]}'
                    self.cmds.append({'command': 'sketch.line', 'params': {
                        'points': [pid(pts[0]), pid(pts[1])], 'ids': [sid], 'connect': False,
                        'construction': cons}})
                    sc_id[e['index']] = sid
                else:
                    unsupported += 1
            elif t == 'Circle2D':
                c = ref(ep.get('center'))
                pts = [ref(x) for x in ep.get('points') or []]
                r = ep.get('r')
                if not pid(c) or r is None:
                    unsupported += 1
                    continue
                if len(pts) == 2 and all(pid(x) for x in pts) and pts[0] != pts[1]:
                    counters['a'] += 1
                    sid = f'a{counters["a"]}'
                    self.cmds.append({'command': 'sketch.arc.center_point', 'params': {
                        'center': pid(c), 'start': pid(pts[0]), 'end': pid(pts[1]), 'connect': False,
                        'construction': cons}})
                else:
                    counters['c'] += 1
                    sid = f'c{counters["c"]}'
                    self.cmds.append({'command': 'sketch.circle.center', 'params': {
                        'center': pid(c), 'radius': mm(r), 'connect': False, 'construction': cons}})
                sc_id[e['index']] = sid
            elif t == 'Ellipse2D':
                c = self.node(ref(ep.get('center')))
                pts = [ref(x) for x in ep.get('points') or []]
                da, a, b = vec(ep.get('dA')), ep.get('a'), ep.get('b')
                if c is None or da is None or a is None or b is None or (pts and all(pts) and pts[0] != pts[1]):
                    unsupported += 1          # elliptical arcs are not translated
                    continue
                cp = vec(c['props'].get('pos'))
                counters['e'] += 1
                sid = f'e{counters["e"]}'
                self.cmds.append({'command': 'sketch.ellipse', 'params': {
                    'center': [mm(cp[0]), mm(cp[1])],
                    'major': [mm(cp[0] + da[0] * a), mm(cp[1] + da[1] * a)],
                    'minor_radius': mm(b), 'connect': False}})
                counters['l'] += 2
                sc_id[e['index']] = sid
            elif t in ('Spline2D', 'BSpline2D', 'OffsetSpline2D', 'SplineHandle2D'):
                unsupported += 1
        for e in ents:                                   # associative ids for profiles
            ai = self.ai_of(e)
            if ai is not None and e['index'] in sc_id:
                ai_map[ai] = sc_id[e['index']]
        if unsupported:
            self.notes.append(f'sketch {name}: {unsupported} curve(s) not translated (splines / elliptical arcs)')
        # geometry as Inventor stored it (mm), used to keep only constraints/dimensions it satisfies
        geo = {}
        for e in ents:
            sid = sc_id.get(e['index'])
            if not sid:
                continue
            ep = e['props']
            if e['type'] == 'Point2D':
                pos = vec(ep.get('pos'))
                geo[sid] = ('P', (mm(pos[0]), mm(pos[1])))
            elif e['type'] == 'Line2D':
                a, b = [sc_id.get(ref(x)) for x in (ep.get('points') or [])[:2]]
                geo[sid] = ('L', a, b)
            elif e['type'] in ('Circle2D',):
                geo[sid] = ('C', sc_id.get(ref(ep.get('center'))), mm(ep.get('r')))
            elif e['type'] == 'Ellipse2D':
                geo[sid] = ('E',)
        self.geo = geo
        skipped = 0
        for e in ents:
            cmd = self.constraint(e, sc_id)
            if cmd is None:
                continue
            if self.consistent(cmd):
                self.cmds.append(cmd)
            else:
                skipped += 1
        if skipped:
            self.notes.append(f'sketch {name}: {skipped} constraint(s)/dimension(s) left out (do not match the stored geometry)')
        self.cmds.append({'command': 'sketch.finish', 'params': {}})
        return {'name': name, 'ai': ai_map, 'normal': normal, 'sc_id': sc_id}

    # ---- geometric consistency: only keep what Inventor's stored geometry already satisfies
    def _pt(self, i):
        g = self.geo.get(i)
        return g[1] if g and g[0] == 'P' else None

    def _line(self, i):
        g = self.geo.get(i)
        if g and g[0] == 'L':
            a, b = self._pt(g[1]), self._pt(g[2])
            if a and b:
                return a, b
        return None

    def _circle(self, i):
        g = self.geo.get(i)
        if g and g[0] == 'C' and self._pt(g[1]):
            return self._pt(g[1]), g[2], g[1]
        return None

    def _value(self, v):
        if v is None:
            return None
        if v in getattr(self, 'values', {}):
            return self.values[v]
        q, _, _ = parse_quantity(v.replace(' ', '') if isinstance(v, str) else v)
        return q

    def consistent(self, cmd, tol=1e-3):
        c, p = cmd['command'], cmd['params']
        d2 = lambda a, b: math.hypot(a[0] - b[0], a[1] - b[1])

        def pl(pt, ln):          # point to infinite line
            (x1, y1), (x2, y2) = ln
            L = math.hypot(x2 - x1, y2 - y1)
            return abs((x2 - x1) * (y1 - pt[1]) - (x1 - pt[0]) * (y2 - y1)) / L if L > 1e-12 else d2(pt, ln[0])

        def direc(ln):
            (x1, y1), (x2, y2) = ln
            L = math.hypot(x2 - x1, y2 - y1) or 1.0
            return ((x2 - x1) / L, (y2 - y1) / L)
        if c == 'sketch.constraint.coincident':
            a, b = p['a'], p['b']
            pa, pb, la, lb, ca, cb = self._pt(a), self._pt(b), self._line(a), self._line(b), self._circle(a), self._circle(b)
            if pa and pb:
                return d2(pa, pb) < tol
            if pa and lb:
                return pl(pa, lb) < tol
            if pb and la:
                return pl(pb, la) < tol
            if pa and cb:
                return abs(d2(pa, cb[0]) - cb[1]) < tol
            if pb and ca:
                return abs(d2(pb, ca[0]) - ca[1]) < tol
            return False
        if c == 'sketch.constraint.horizontal_vertical':
            if 'line' in p:
                ln = self._line(p['line'])
                if not ln:
                    return False
                (x1, y1), (x2, y2) = ln
            else:
                a, b = (self._pt(x) for x in p['points'])
                if not a or not b:
                    return False
                (x1, y1), (x2, y2) = a, b
            return abs(y2 - y1) < tol if p.get('mode') == 'horizontal' else abs(x2 - x1) < tol
        if c in ('sketch.constraint.parallel', 'sketch.constraint.perpendicular', 'sketch.constraint.collinear'):
            la, lb = self._line(p['a']), self._line(p['b'])
            if not la or not lb:
                return False
            u, v = direc(la), direc(lb)
            cross, dot = abs(u[0] * v[1] - u[1] * v[0]), abs(u[0] * v[0] + u[1] * v[1])
            if c.endswith('perpendicular'):
                return dot < 1e-4
            if c.endswith('parallel'):
                return cross < 1e-4
            return cross < 1e-4 and pl(lb[0], la) < tol
        if c == 'sketch.constraint.tangent':
            la, lb, ca, cb = self._line(p['a']), self._line(p['b']), self._circle(p['a']), self._circle(p['b'])
            if la and cb:
                return abs(pl(cb[0], la) - cb[1]) < tol
            if lb and ca:
                return abs(pl(ca[0], lb) - ca[1]) < tol
            if ca and cb:
                dd = d2(ca[0], cb[0])
                return abs(dd - (ca[1] + cb[1])) < tol or abs(dd - abs(ca[1] - cb[1])) < tol
            return False
        if c == 'sketch.constraint.equal':
            la, lb, ca, cb = self._line(p['a']), self._line(p['b']), self._circle(p['a']), self._circle(p['b'])
            if la and lb:
                return abs(d2(*la) - d2(*lb)) < tol
            if ca and cb:
                return abs(ca[1] - cb[1]) < tol
            return False
        if c == 'sketch.constraint.symmetry':
            ln = self._line(p['line'])
            a, b = self._pt(p['a']), self._pt(p['b'])
            if not ln or not a or not b:
                return False
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            u = direc(ln)
            ab = (b[0] - a[0], b[1] - a[1])
            return pl(mid, ln) < tol and abs(ab[0] * u[0] + ab[1] * u[1]) < tol
        if c == 'sketch.constraint.fix':
            return True
        if c == 'sketch.dimension':
            v = self._value(p.get('value'))
            ents, typ = p['entities'], p.get('type')
            if v is None:
                return False
            close = lambda x: abs(x - v) <= max(tol, 1e-6 * abs(v))
            if typ in ('radius', 'diameter'):
                cc = self._circle(ents[0])
                return bool(cc) and close(cc[1] if typ == 'radius' else 2 * cc[1])
            if typ == 'length':
                ln = self._line(ents[0])
                return bool(ln) and close(d2(*ln))
            if typ == 'angle':
                la, lb = self._line(ents[0]), self._line(ents[1])
                if not la or not lb:
                    return False
                u, w = direc(la), direc(lb)
                ang = math.degrees(math.acos(max(-1.0, min(1.0, u[0] * w[0] + u[1] * w[1]))))
                return any(abs(x - v) < 1e-3 for x in (ang, 180 - ang))
            if typ in ('distance', 'horizontal', 'vertical'):
                # a circle in an Inventor distance means its centre
                ents = [self._circle(x)[2] if self._circle(x) else x for x in ents]
                p['entities'] = ents
                a, b = ents
                pa, pb, la, lb = self._pt(a), self._pt(b), self._line(a), self._line(b)
                if pa and pb:
                    options = {'distance': d2(pa, pb), 'horizontal': abs(pa[0] - pb[0]), 'vertical': abs(pa[1] - pb[1])}
                    order = [typ] + [k for k in options if k != typ]
                    for k in order:
                        if close(options[k]):
                            p['type'] = k
                            return True
                    return False
                if pa and lb:
                    return close(pl(pa, lb))
                if pb and la:
                    return close(pl(pb, la))
                if la and lb:
                    u, w = direc(la), direc(lb)
                    return abs(u[0] * w[1] - u[1] * w[0]) < 1e-4 and close(pl(lb[0], la))
                return False
        return True

    def constraint(self, e, sc_id):
        t, p = e['type'], e['props']
        g = lambda k: sc_id.get(ref(p.get(k)))
        if t == 'Geometric_Coincident2D':
            a, b = g('entity1'), g('entity2')
            if a and b and a != b:
                return {'command': 'sketch.constraint.coincident', 'params': {'a': a, 'b': b}}
        elif t in ('Geometric_Horizontal2D', 'Geometric_Vertical2D'):
            ln = g('line')
            if ln:
                return {'command': 'sketch.constraint.horizontal_vertical',
                        'params': {'line': ln, 'mode': 'horizontal' if 'Horizontal' in t else 'vertical'}}
        elif t in ('Geometric_AlignHorizontal2D', 'Geometric_AlignVertical2D'):
            a, b = g('entity1') or g('point1'), g('entity2') or g('point2')
            if a and b:
                return {'command': 'sketch.constraint.horizontal_vertical',
                        'params': {'points': [a, b], 'mode': 'horizontal' if 'Horizontal' in t else 'vertical'}}
        elif t in ('Geometric_Parallel2D', 'Geometric_Perpendicular2D', 'Geometric_Collinear2D'):
            a, b = g('line1') or g('entity1'), g('line2') or g('entity2')
            kind = {'Geometric_Parallel2D': 'parallel', 'Geometric_Perpendicular2D': 'perpendicular',
                    'Geometric_Collinear2D': 'collinear'}[t]
            if a and b:
                return {'command': f'sketch.constraint.{kind}', 'params': {'a': a, 'b': b}}
        elif t == 'Geometric_Tangential2D':
            a, b = g('entity1'), g('entity2')
            if a and b:
                return {'command': 'sketch.constraint.tangent', 'params': {'a': a, 'b': b}}
        elif t in ('Geometric_EqualLength2D', 'Geometric_EqualRadius2D'):
            a = g('line1') or g('circle1') or g('entity1')
            b = g('line2') or g('circle2') or g('entity2')
            if a and b:
                return {'command': 'sketch.constraint.equal', 'params': {'a': a, 'b': b}}
        elif t == 'Geometric_SymmetryLine2D':
            a, b, ln = g('entity1'), g('entity2'), g('symmetry')
            if a and b and ln:
                return {'command': 'sketch.constraint.symmetry', 'params': {'a': a, 'b': b, 'line': ln}}
        elif t == 'Geometric_Fix2D':
            x = g('entity') or g('entity1')
            if x:
                return {'command': 'sketch.constraint.fix', 'params': {'entity': x}}
        elif t.startswith('Dimension_'):
            val = self.param_expr(ref(p.get('parameter')))
            if not val:
                return None
            base = {'value': val, 'if_redundant': 'driven', 'scale_sketch': False}
            if t in ('Dimension_Diameter2D', 'Dimension_Radius2D'):
                c = g('circle')
                if c:
                    return {'command': 'sketch.dimension', 'params': dict(
                        base, entities=[c], type='diameter' if 'Diameter' in t else 'radius')}
            elif t in ('Dimension_Distance2D', 'Dimension_Distance_Horizontal2D', 'Dimension_Distance_Vertical2D'):
                a, b = g('entity1'), g('entity2')
                typ = 'horizontal' if 'Horizontal' in t else 'vertical' if 'Vertical' in t else 'distance'
                ents = [x for x in (a, b) if x]
                if len(ents) == 2:
                    return {'command': 'sketch.dimension', 'params': dict(base, entities=ents, type=typ)}
                if len(ents) == 1 and ents[0].startswith('L'):
                    return {'command': 'sketch.dimension', 'params': dict(base, entities=ents, type='length')}
            elif t == 'Dimension_Angle2Line2D':
                a, b = g('line1'), g('line2')
                if a and b:
                    prm = dict(base, entities=[a, b], type='angle')
                    pos = vec(p.get('pos'))
                    if pos:
                        prm['text_at'] = [mm(pos[0]), mm(pos[1])]
                    return {'command': 'sketch.dimension', 'params': prm}
        return None

    # ...................................................................... features
    def features(self):
        return sorted([n for n in self.by_type('Feature')], key=lambda n: n['index'])

    def prop(self, f, i):
        lst = f['props'].get('properties') or []
        return self.node(ref(lst[i])) if i < len(lst) else None

    def profiles(self, patch, sk):
        if patch is None or sk is None:
            return None
        outer = self.node(ref(patch['props'].get('profile')))
        if outer is None:
            return None
        profs = []
        for lp in outer['props'].get('parts') or []:
            loop = self.node(ref(lp))
            # loop flags: bit 8 = outer boundary, 16 = hole (24 = outer boundary that has holes);
            # holes are implied by SolidCraft ("islands inside are its holes")
            op = loop['props'].get('operation') if loop else None
            if not isinstance(op, int) or not (op & 8):
                continue
            curves = []
            for er in loop['props'].get('edges') or []:
                se = self.node(ref(er))
                cid = sk['ai'].get(se['props'].get('entityAI')) if se else None
                if cid is None:
                    return None
                if cid not in curves:
                    curves.append(cid)
            if curves:
                profs.append({'curves': curves})
        return profs or None

    def extrude(self, f, sketches):
        sk = sketches.get((f.get('participants') or [None])[0])
        op = (self.prop(f, 0) or {}).get('props', {}).get('value')
        opname = {1: 'new', 2: 'cut', 3: 'join', 4: 'intersect'}.get(op)
        if sk is None or opname is None:
            return None, 'no sketch / surface extrude'
        profs = self.profiles(self.prop(f, 1), sk)
        if not profs:
            return None, 'profile not resolved'
        dirn = vec((self.prop(f, 2) or {}).get('props', {}).get('dir'))
        extent = (self.prop(f, 6) or {}).get('props', {}).get('value')
        midplane = (self.prop(f, 7) or {}).get('props', {}).get('value')
        prm = {'sketch': sk['name'], 'profiles': profs, 'operation': opname, 'name': f.get('name')}
        if extent == 5:
            prm['through_all'] = True
        elif extent in (0, 1, 2):
            d1 = self.param_expr(ref((f['props'].get('properties') or [None] * 5)[4]))
            if not d1:
                return None, 'no length'
            prm['distance'] = d1
        else:
            return None, f'extent type {extent} not supported'
        taper = self.prop(f, 5)
        if taper is not None:
            tv, _, _ = parse_quantity(taper.get('value'))
            if tv:
                prm['taper'] = self.param_expr(taper['index'])
        if midplane:
            prm['direction'] = 'symmetric'
        else:
            if dirn and sum(a * b for a, b in zip(dirn, sk['normal'])) < 0:
                prm['direction'] = 'negative'
            lst = f['props'].get('properties') or []
            if len(lst) > 0x1B and lst[0x1B]:
                d2 = self.param_expr(ref(lst[0x1B]))
                if d2:
                    v2, _, _ = parse_quantity(self.node(ref(lst[0x1B])).get('value'))
                    if v2:
                        prm['distance2'] = d2
        return {'command': 'solid.extrude', 'params': prm}, None

    def fillet_spec(self, f):
        """constant-radius fillet -> (radius expression, edge count) or (None, why)."""
        if self.prop(f, 1) is not None:
            return None, 'variable-radius fillet'
        sets_node = self.prop(f, 0)
        if sets_node is None or sets_node.get('type') != 'FxFilletEdgeSetsConstantR':
            return None, 'fillet type not supported'
        radii, count = set(), 0
        for s in sets_node['props'].get('sets') or []:
            sn = self.node(ref(s))
            if not sn:
                continue
            radii.add(self.param_expr(ref(sn['props'].get('radius'))))
            ec = self.node(ref(sn['props'].get('edges')))
            count += len((ec or {}).get('props', {}).get('items') or [])
        radii.discard(None)
        if len(radii) != 1:
            return None, 'fillet sets with different radii'
        return {'radius': radii.pop(), 'count': count}, None

    def chamfer_spec(self, f):
        d1, d2 = self.prop(f, 2), self.prop(f, 3)
        typ = self.prop(f, 4)
        kind = None
        if typ is not None:
            vals, v = typ['props'].get('Values'), typ['props'].get('value')
            if isinstance(vals, list) and isinstance(v, int) and v < len(vals):
                kind = str(vals[v])
        if d1 is None:
            return None, 'chamfer without distance'
        spec = {'distance': self.param_expr(d1['index'])}
        if kind and 'Angle' in kind:
            ang = self.prop(f, 0x0A)
            if ang is None:
                return None, 'distance-angle chamfer without angle'
            spec['angle'] = self.param_expr(ang['index'])
        elif kind and 'Two' in kind and d2 is not None:
            spec['distance2'] = self.param_expr(d2['index'])
        flip = self.prop(f, 5)
        if flip is not None and flip['props'].get('value') is True:
            spec['flip'] = True
        return spec, None

    def revolve(self, f, sketches):
        sk = sketches.get((f.get('participants') or [None])[0])
        op = (self.prop(f, 0) or {}).get('props', {}).get('value')
        opname = {1: 'new', 2: 'cut', 3: 'join', 4: 'intersect'}.get(op)
        if sk is None or opname is None:
            return None, 'no sketch / surface revolve'
        profs = self.profiles(self.prop(f, 1), sk)
        axis = self.prop(f, 2)
        if not profs or axis is None:
            return None, 'profile/axis not resolved'
        pos, d = vec(axis['props'].get('pos')), vec(axis['props'].get('dir'))
        if not pos or not d:
            return None, 'axis has no position'
        prm = {'sketch': sk['name'], 'profiles': profs, 'operation': opname, 'name': f.get('name'),
               'axis': {'origin': [mm(x) for x in pos], 'dir': d}}
        extent = (self.prop(f, 3) or {}).get('props', {}).get('value')
        if extent == 1:
            a1 = self.prop(f, 4)
            direction = (self.prop(f, 5) or {}).get('props', {}).get('value')
            prm['angle'] = self.param_expr(a1['index']) if a1 else '360 deg'
            if direction == 1:
                prm['angle'] = f'-({prm["angle"]})'
            elif direction == 2:
                prm['direction'] = 'symmetric'
            lst = f['props'].get('properties') or []
            if len(lst) > 0x12 and lst[0x12]:
                prm['angle2'] = self.param_expr(ref(lst[0x12]))
        elif extent == 3:
            prm['angle'] = '360 deg'
        else:
            return None, f'revolve extent {extent} not supported'
        return {'command': 'solid.revolve', 'params': prm}, None


# ------------------------------------------------------------------------- SolidCraft I/O
def sc(*args, timeout=600):
    r = subprocess.run([SC_CLI, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def measure(path):
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'm.json')
        code, so, se = sc('eval', path, '--json-out', out, '--allow-errors')
        if not os.path.exists(out):
            return None
        with open(out, encoding='utf-8') as f:
            d = json.load(f)
    tot = d.get('total', {})
    names = [b.get('name') for b in d.get('bodies', [])]
    boxes = [b.get('bbox') for b in d.get('bodies', []) if b.get('bbox')]
    if not boxes:
        return {'volume': tot.get('volume_mm3', 0), 'bbox': None, 'bodies': 0, 'names': names,
                'timeline': d.get('timeline', [])}
    lo = [min(b['min'][i] for b in boxes) for i in range(3)]
    hi = [max(b['max'][i] for b in boxes) for i in range(3)]
    return {'volume': tot.get('volume_mm3', 0), 'bbox': [lo, hi], 'bodies': len(boxes), 'names': names,
            'timeline': d.get('timeline', [])}


def query(design, cmds):
    """run read-only commands against a saved design; returns their results."""
    with tempfile.TemporaryDirectory() as td:
        sp, res = os.path.join(td, 'q.json'), os.path.join(td, 'r.json')
        with open(sp, 'w', encoding='utf-8') as f:
            json.dump({'commands': cmds}, f)
        sc('run', sp, '--in', design, '--allow-errors', '--results', '--json-out', res, '--quiet')
        if not os.path.exists(res):
            return []
        with open(res, encoding='utf-8') as f:
            try:
                out = json.load(f)
            except Exception:
                return []
    return out if isinstance(out, list) else []


class EdgeFinder:
    """Finds the edges a fillet/chamfer acted on: edges of the native body built so far whose
    midpoint is no longer on the surface of the exact final solid."""

    def __init__(self, step, workdir):
        self.exact = os.path.join(workdir, 'exact.sc')
        ok, _ = run_script([{'command': 'file.insert_step', 'params': {'path': step, 'flatten': True}}], self.exact)
        m = measure(self.exact) if ok else None
        self.bodies = [n for n in (m or {}).get('names', []) if n]

    def changed_edges(self, design, body_names):
        edges = []
        for r in query(design, [{'command': 'model.edges', 'params': {'body': b}} for b in body_names]):
            if isinstance(r, dict):
                for e in r.get('edges', []) or []:
                    if e.get('mid'):
                        edges.append((r.get('body'), e['mid']))
        if not edges or not self.bodies:
            return []
        on = [False] * len(edges)
        for b in self.bodies:
            for i0 in range(0, len(edges), 256):
                chunk = edges[i0:i0 + 256]
                res = query(self.exact, [{'command': 'inspect.classify_points',
                                          'params': {'body': b, 'points': [p for _, p in chunk]}}])
                cls = (res[0] or {}).get('results', []) if res and isinstance(res[0], dict) else []
                for j, c in enumerate(cls):
                    if isinstance(c, dict) and c.get('class') == 'on':
                        on[i0 + j] = True
        return [edges[i] for i in range(len(edges)) if not on[i]]


def same(a, b, vol_tol=2e-3, box_tol=0.05):
    if not a or not b or not a['bbox'] or not b['bbox']:
        return False
    if abs(a['volume'] - b['volume']) > vol_tol * max(1.0, abs(b['volume'])):
        return False
    return all(abs(a['bbox'][k][i] - b['bbox'][k][i]) <= box_tol for k in range(2) for i in range(3))


def run_script(cmds, dest, keep=None):
    script = {'about': {'generator': 'sc_translate.py (Inventor via InventorLoader)'}, 'commands': cmds}
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, 'script.json')
        with open(sp, 'w', encoding='utf-8') as f:
            json.dump(script, f, indent=1)
        if keep:
            with open(keep, 'w', encoding='utf-8') as f:
                json.dump(script, f, indent=1)
        res = os.path.join(td, 'res.json')
        code, so, se = sc('run', sp, '--save', dest, '--allow-errors', '--results', '--json-out', res, '--quiet')
        results = []
        if os.path.exists(res):
            with open(res, encoding='utf-8') as f:
                try:
                    results = json.load(f)
                except Exception:
                    results = []
    errors = []
    if isinstance(results, list):
        for c, r in zip(cmds, results):
            if isinstance(r, dict) and (r.get('error') or r.get('refused')):
                errors.append(f"{c['command']}: {str(r.get('error') or r.get('refused'))[:160]}")
    if code != 0 and not errors:
        errors.append((se or so).strip()[-400:])
    return os.path.exists(dest), errors


DROPPABLE = ('sketch.dimension', 'sketch.constraint.', 'sketch.point', 'sketch.line', 'sketch.circle',
             'sketch.arc', 'sketch.ellipse', 'parameters.add')
STEP_ERR = re.compile(r'step (\d+) \(([\w.]+)\)')


def run_script_robust(cmds, dest, keep=None, max_retries=80, literals=None):
    """run_script, but a sketch constraint / dimension / entity SolidCraft refuses (for example
    one that would over-constrain the sketch) is dropped and the build re-run; anything else
    (a feature) still fails. Returns ok, errors, dropped[(command, reason)]."""
    cmds = list(cmds)
    dropped = []
    for _ in range(max_retries):
        if os.path.exists(dest):
            os.remove(dest)
        ok, errors = run_script(cmds, dest, keep)
        if ok and not errors:
            return ok, errors, dropped, cmds
        m = STEP_ERR.search(' '.join(errors))
        if not m:
            return ok, errors, dropped, cmds
        n, name = int(m.group(1)), m.group(2)
        # SolidCraft numbers steps from 1
        idx = n - 1 if 0 <= n - 1 < len(cmds) and cmds[n - 1]['command'] == name else None
        if idx is None or not cmds[idx]['command'].startswith(DROPPABLE):
            return ok, errors, dropped, cmds
        reason = ' '.join(errors)[:200]
        # a parameter expression SolidCraft's unit checking refuses (Inventor is lenient about
        # mixing units): keep the parameter with its exact value instead of dropping it
        if cmds[idx]['command'] == 'parameters.add' and literals:
            prm = cmds[idx]['params']
            lit = literals.get(prm.get('name'))
            if lit and prm.get('expression') != lit:
                dropped.append(('parameters.add', f"{prm['name']}: expression \"{prm['expression']}\" -> value {lit}", reason))
                cmds[idx] = {'command': 'parameters.add', 'params': dict(prm, expression=lit)}
                continue
        dropped.append((cmds[idx]['command'], json.dumps(cmds[idx]['params'])[:120], reason))
        del cmds[idx]
    return os.path.exists(dest), ['too many refused sketch commands'], dropped, cmds


FREECADCMD = freecadcmd()


def solidify(step, dest):
    """sew a surfaces-only STEP into solids with FreeCAD (fc_solidify.py), else mesh it."""
    out_step, out_stl = dest + '.sewn.step', dest + '.surfaces.stl'
    env = dict(os.environ, FC_IN=step, FC_OUT_STEP=out_step, FC_OUT_STL=out_stl)
    try:
        subprocess.run([FREECADCMD, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fc_solidify.py')],
                       env=env, timeout=900, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {'route': None, 'error': 'timeout'}
    try:
        with open(out_step + '.json') as f:
            r = json.load(f)
    except Exception:
        return {'route': None, 'error': 'no result'}
    r['step'], r['stl'] = out_step, out_stl
    return r


# --------------------------------------------------------------------------------- main
def translate(out_dir, dest, name=None, keep_dir=None, stl_hint=None):
    part = Part(out_dir)
    rep = {'part': name or os.path.basename(dest), 'notes': part.notes}
    n_params = part.parameters()
    param_cmds = list(part.cmds)

    sketches, sk_cmds = {}, []
    for sk in sorted(part.by_type('Sketch2D'), key=lambda n: n['index']):
        before = len(part.cmds)
        r = part.sketch(sk)
        if r:
            sketches[sk['index']] = r
    sk_cmds = part.cmds[len(param_cmds):]

    # clean the parameter + sketch part once: drop what SolidCraft refuses (over-constraining
    # dimensions, unnamed parameters...), then reuse the cleaned commands for every build
    with tempfile.TemporaryDirectory() as td:
        _, base_errs, dropped, cleaned = run_script_robust(param_cmds + sk_cmds, os.path.join(td, 'base.sc'),
                                                           literals=getattr(part, 'literals', None))
    n_param = sum(1 for c in cleaned if c['command'] == 'parameters.add')
    param_cmds, sk_cmds = cleaned[:n_param], cleaned[n_param:]
    rep['dropped_sketch_commands'] = len(dropped)
    if dropped:
        rep['dropped_examples'] = [f'{c} {p}: {r[-120:]}' for c, p, r in dropped[:8]]
    if base_errs:
        rep['sketch_errors'] = base_errs[:5]

    feats = part.features()
    kinds = [f.get('subtype') or '?' for f in feats]
    solid_feats = [f for f in feats if f.get('subtype') not in IGNORED_FEATURES]
    unsupported = sorted({k for k in kinds if k not in NATIVE_FEATURES | IGNORED_FEATURES})
    if any(k in IGNORED_FEATURES for k in kinds) and solid_feats:
        unsupported = sorted(set(unsupported) | {'Reference body'})
    rep.update({'parameters': n_params, 'sketches': len(sketches),
                'features': [f'{f.get("name")} ({f.get("subtype")})' for f in feats]})

    exact = measure(part.step) if os.path.exists(part.step) else None
    rep['exact'] = {'volume_mm3': exact and round(exact['volume'], 3), 'bbox': exact and exact['bbox']}
    keep = (lambda tag: os.path.join(keep_dir, f'{os.path.basename(dest)}.{tag}.script.json')) if keep_dir else (lambda tag: None)

    fx_cmds, fx_problems = [], []
    if solid_feats and not unsupported:
        with tempfile.TemporaryDirectory() as td:
            finder, bodies = None, []
            for f in solid_feats:
                st = f.get('subtype')
                if st in ('Extrude', 'Revolve'):
                    cmd, why = (part.extrude if st == 'Extrude' else part.revolve)(f, sketches)
                    if cmd and cmd['params'].get('operation') == 'new':
                        bodies.append(f'Body{len(bodies) + 1}')
                        cmd['params']['body_name'] = bodies[-1]
                elif st in ('Fillet', 'Chamfer'):
                    spec, why = (part.fillet_spec if st == 'Fillet' else part.chamfer_spec)(f)
                    cmd = None
                    if spec:
                        if finder is None:
                            finder = EdgeFinder(part.step, td)
                        state = os.path.join(td, 'state.sc')
                        ok, _ = run_script(param_cmds + sk_cmds + fx_cmds, state)
                        changed = finder.changed_edges(state, bodies) if ok else []
                        if not changed:
                            why = 'could not find the edges it acted on'
                        else:
                            by_body = {}
                            for b, p in changed:
                                by_body.setdefault(b, []).append(p)
                            for k, (b, pts) in enumerate(sorted(by_body.items())):
                                prm = {'edges': pts, 'tangent_chain': False, 'body': b,
                                       'name': f.get('name') if k == 0 else f'{f.get("name")}_{k + 1}'}
                                if st == 'Fillet':
                                    prm['radius'] = spec['radius']
                                    fx_cmds.append({'command': 'solid.fillet', 'params': prm})
                                else:
                                    prm.update({k2: v for k2, v in spec.items()})
                                    fx_cmds.append({'command': 'solid.chamfer', 'params': prm})
                            continue
                else:
                    cmd, why = None, f'{st} not supported'
                if cmd:
                    fx_cmds.append(cmd)
                else:
                    fx_problems.append(f'{f.get("name")}: {why}')
                    break
    rep.update({'unsupported_features': unsupported, 'feature_problems': fx_problems})

    native_ok = False
    if fx_cmds and not fx_problems:
        ok, errs = run_script(param_cmds + sk_cmds + fx_cmds, dest, keep('native'))
        got = measure(dest) if ok else None
        rep['native'] = {'errors': errs[:25], 'volume_mm3': got and round(got['volume'], 3), 'bbox': got and got['bbox']}
        native_ok = ok and same(got, exact)
    if native_ok:
        rep['result'] = 'native'
    else:
        # exact geometry + everything editable that did translate; geometry fallbacks:
        # exact STEP -> faces sewn into solids (FreeCAD) -> surface mesh -> project STL -> none
        base_cmds = list(param_cmds) + list(sk_cmds)
        has_faces = os.path.exists(part.step) and 'ADVANCED_FACE' in open(part.step, encoding='latin-1').read()
        attempts = []
        if has_faces:
            attempts.append(('step+sketches', {'command': 'file.insert_step', 'params': {'path': part.step, 'flatten': True}}))
        ok, errs, got, result = False, [], None, None
        for label, geo_cmd in attempts:
            ok, errs = run_script(base_cmds + [geo_cmd], dest, keep('step'))
            if ok:
                result = label
                break
        if not ok and has_faces and any('no bodies' in e or 'no solids' in e for e in errs):
            fixed = solidify(part.step, dest)
            rep['solidify'] = fixed
            if fixed.get('route') == 'sewn-solids':
                ok, errs = run_script(base_cmds + [{'command': 'file.insert_step', 'params': {
                    'path': fixed['step'], 'flatten': True}}], dest, keep('step'))
                result = 'sewn-solid+sketches' if ok else None
            elif fixed.get('route') == 'mesh':
                ok, errs = run_script(base_cmds + [{'command': 'file.insert_mesh', 'params': {
                    'path': fixed['stl']}}], dest, keep('step'))
                result = 'mesh+sketches (surfaces only in Inventor file)' if ok else None
        if not ok and stl_hint:
            ok, errs = run_script(base_cmds + [{'command': 'file.insert_mesh', 'params': {'path': stl_hint}}],
                                  dest, keep('step'))
            result = 'project-STL+sketches (no solid stored in Inventor file)' if ok else None
            rep['stl_used'] = stl_hint
        if not ok:
            ok, errs = run_script(base_cmds, dest, keep('step'))
            result = ('parameters+sketches only (no geometry in Inventor file)' if not has_faces
                      else 'parameters+sketches only (geometry could not be imported)') if ok else 'failed'
        got = measure(dest) if ok else None
        rep['step_build'] = {'errors': errs[:25], 'volume_mm3': got and round(got['volume'], 3)}
        if result == 'step+sketches' and not same(got, exact):
            result = 'step+sketches (unverified)'
        rep['result'] = result
        if fx_cmds and not fx_problems:
            rep['why_not_native'] = 'native rebuild did not match the exact solid'
        elif unsupported:
            rep['why_not_native'] = 'uses ' + ', '.join(unsupported)
        elif fx_problems:
            rep['why_not_native'] = '; '.join(fx_problems)[:300]
        else:
            rep['why_not_native'] = 'no solid features'
    with open(dest + '.report.json', 'w', encoding='utf-8') as f:
        json.dump(rep, f, indent=1)
    return rep


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('out_dir')
    ap.add_argument('dest')
    ap.add_argument('--name')
    ap.add_argument('--keep-scripts')
    a = ap.parse_args()
    if a.keep_scripts:
        os.makedirs(a.keep_scripts, exist_ok=True)
    r = translate(a.out_dir, a.dest, a.name, a.keep_scripts)
    print(json.dumps({k: r[k] for k in ('part', 'result', 'parameters', 'sketches') if k in r}),
          r.get('why_not_native', ''))
