#!/usr/bin/env python3
"""
sc_assembly.py - rebuild Inventor assemblies (.iam) as SolidCraft assemblies.

  python sc_assembly.py "<asm work dir>[;<more work dirs>]" <export root> [--only substr]

For each decoded .iam (batch_decode.py --assemblies / --extra): component definitions, their
occurrence ids and placement matrices (AmRxSegment), occurrence names (AmBrowserSegment) and the
files the assembly really references (UFRxDoc). Each component is matched to its translated
design and every occurrence is placed at Inventor's exact matrix:

  * a design with closed solids           -> linked component (component.insert_design)
  * a part whose geometry is surfaces/mesh -> its exact STEP (or STL) inserted and moved there
  * a parameters-only design               -> skipped (nothing to show)

Assemblies are saved as <relative path>.iam.sc (parts keep <name>.sc), sub-assemblies first.
Writes <export root>/assembly_report.csv.
"""
import argparse, csv, difflib, json, os, re, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sc_translate import parse_matrix, run_script, measure
import fasteners

ASM_SUFFIX = '.iam.sc'


def load(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def file_refs(iam_path):
    """(base name, 'ipt'|'iam') of every file an .iam references - its UFRxDoc stream keeps the
    original paths."""
    import olefile
    refs = []
    try:
        ole = olefile.OleFileIO(iam_path)
        for e in ole.listdir(streams=True):
            if e[-1] != 'UFRxDoc':
                continue
            d = ole.openstream(e).read()
            texts = [m.group().decode('utf-16le') for m in re.finditer(rb'(?:[\x20-\x7e]\x00){4,}', d)]
            texts += [m.group().decode('latin-1') for m in re.finditer(rb'[\x20-\x7e]{6,}', d)]
            for t in texts:
                for m in re.finditer(r'([^\\/:*?"<>|]+)\.(ipt|iam)\b', t, re.I):
                    key = (m.group(1).strip(), m.group(2).lower())
                    if key[0] and not key[0].lower().startswith(('standard (', 'weldment (')) and key not in refs:
                        refs.append(key)
        ole.close()
    except Exception:
        pass
    return refs


def tokens(s):
    return set(re.findall(r'[a-z0-9]+', s.lower()))


def similarity(a, b):
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / max(1, len(ta | tb)) + 0.5 * difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def read_assembly(out_dir):
    rx = load(os.path.join(out_dir, 'part.AmRxSegment.json'))
    br = load(os.path.join(out_dir, 'part.AmBrowserSegment.json'))
    N = {n['index']: n for n in rx}
    comps = []
    for n in rx:
        if n.get('type') != '08B9DB46' or not n.get('name'):
            continue
        occ = []
        for r in n['props'].get('lst0') or []:
            a = N.get(r.get('ref')) if isinstance(r, dict) else None
            occ += [v for v in (a or {}).get('props', {}).get('lst0') or [] if isinstance(v, int)]
        if not occ and isinstance(n['props'].get('u32_0'), int):
            # sub-assembly occurrences of a top-level assembly use a record that does not decode;
            # the definition's own number is its (first) occurrence id
            occ = [n['props']['u32_0']]
        if any(c['name'] == n['name'] and c['occurrences'] == occ for c in comps):
            continue          # the same component listed again (model-state variants)
        comps.append({'name': n['name'], 'occurrences': occ})
    placements = {}
    for n in rx:
        if n.get('type') != '232792BC':
            continue
        amrx = N.get((n['props'].get('ref_0') or {}).get('ref'))
        occ_id = (amrx or {}).get('props', {}).get('u32_0')
        m = parse_matrix(n['props'].get('transformation'))
        if isinstance(occ_id, int) and m:
            placements[occ_id] = [m[0][0], m[0][1], m[0][2], m[0][3], m[1][0], m[1][1], m[1][2], m[1][3],
                                  m[2][0], m[2][1], m[2][2], m[2][3]]
    counts = defaultdict(int)
    for n in br:
        nm = n.get('name')
        m = re.match(r'^(.*):(\d+)$', nm) if isinstance(nm, str) else None
        if m:
            counts[m.group(1)] += 1
    return comps, placements, dict(counts)


STEP_ERR = re.compile(r'step (\d+) \(([\w.]+)\)')


def build(cmds, dest, max_retries=60):
    """run the placement script; an insert SolidCraft refuses (e.g. a part with no closed solid)
    is dropped together with the commands that place it, and the build re-run.
    Returns ok, errors, dropped descriptions."""
    cmds, dropped = list(cmds), []
    for _ in range(max_retries):
        if os.path.exists(dest):
            os.remove(dest)
        ok, errs = run_script(cmds, dest)
        if ok and not errs:
            return ok, errs, dropped
        m = STEP_ERR.search(' '.join(errs))
        if not m or not (0 < int(m.group(1)) <= len(cmds)):
            return ok, errs, dropped
        i = int(m.group(1)) - 1            # SolidCraft numbers steps from 1
        c = cmds[i]
        tag = c['params'].get('name') if c['command'].startswith(('file.insert', 'component.insert')) else \
            (c['params'].get('occurrence') or '').rsplit(':', 1)[0]
        # drop the insert and the ground/move commands that belong to the same placement
        j = i
        while j > 0 and cmds[j]['command'].startswith('occurrence.') and \
                (cmds[j - 1]['params'].get('name') == tag or cmds[j - 1]['params'].get('occurrence', '').startswith(f'{tag}:')):
            j -= 1
        k = j + 1
        while k < len(cmds) and cmds[k]['command'].startswith('occurrence.') and \
                cmds[k]['params'].get('occurrence', '').startswith(f'{tag}:'):
            k += 1
        dropped.append(f"{tag or c['command']} (refused: {' '.join(errs)[-90:]})")
        del cmds[j:k]
        if not cmds:
            return False, ['nothing left to place'], dropped
    return os.path.exists(dest), ['too many refused placements'], dropped


class Designs:
    """every translated design under the export root, by (lower base name, kind)."""

    def __init__(self, export):
        self.export = export
        self.by = defaultdict(list)
        self._solid = {}
        for dp, dn, fn in os.walk(export):
            for f in fn:
                if f.endswith(ASM_SUFFIX):
                    self.add(os.path.relpath(os.path.join(dp, f[:-len(ASM_SUFFIX)]), export), 'iam')
                elif f.endswith('.sc') and not f.endswith('.sewn.sc'):
                    self.add(os.path.relpath(os.path.join(dp, f[:-3]), export), 'ipt')

    def add(self, rel, kind):
        key = (os.path.basename(rel).lower(), kind)
        if rel not in self.by[key]:
            self.by[key].append(rel)

    def path(self, rel, kind):
        return os.path.join(self.export, rel + (ASM_SUFFIX if kind == 'iam' else '.sc'))

    def find(self, name, kinds=('ipt', 'iam')):
        return [(rel, k) for k in kinds for rel in self.by.get((name.lower(), k), [])]

    def geometry(self, rel, kind):
        """how a design can be placed: 'link' (closed solids), 'step' / 'stl' file, or None."""
        key = (rel, kind)
        if key not in self._solid:
            how = None
            p = self.path(rel, kind)
            if kind == 'iam':
                how = ('link', p) if os.path.exists(p) else None
            elif os.path.exists(p):
                m = measure(p) or {}
                closed = (m.get('bodies') or 0) - len(m.get('open') or [])
                if closed > 0:
                    how = ('link', p)
                else:
                    step = os.path.join(self.export, rel + '.step')
                    rep = load(p + '.report.json') or {}
                    if rep.get('stl_used') and os.path.exists(rep['stl_used']):
                        how = ('stl', rep['stl_used'])
                    elif os.path.exists(step) and 'ADVANCED_FACE' in open(step, encoding='latin-1').read():
                        how = ('step', step)
            self._solid[key] = how
        return self._solid[key]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('work', help='assembly decode folder(s), separated by ";"')
    ap.add_argument('export')
    ap.add_argument('--only')
    a = ap.parse_args()
    designs = Designs(a.export)

    asm = {}
    for w in [w for w in a.work.split(';') if w]:
        with open(os.path.join(w, 'decode_report.csv'), encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                if not r['file'].lower().endswith('.iam'):
                    continue
                jd = os.path.join(w, 'j', r['job'])
                if not os.path.exists(os.path.join(jd, 'out', 'part.AmRxSegment.json')):
                    continue
                rel = os.path.splitext(r['file'])[0]
                comps, placements, counts = read_assembly(os.path.join(jd, 'out'))
                asm[rel] = {'comps': comps, 'placements': placements, 'browser': counts,
                            'refs': file_refs(os.path.join(jd, 'src', 'part.iam'))}
                designs.add(rel, 'iam')

    def nearest(asm_rel, cands):
        here = os.path.dirname(asm_rel)
        return max(cands, key=lambda c: len(os.path.commonpath([here, os.path.dirname(c[0])])) if here else 0)

    def resolve_all(asm_rel, d):
        """component display names -> (design rel, kind). Exact names first, then the files the
        assembly really references (with their extension) and the browser's file-based names,
        matched by name tokens and occurrence count. The assembly itself is never a candidate."""
        me = (os.path.basename(asm_rel).lower(), 'iam')
        ref_kinds = defaultdict(set)
        for base, kind in d['refs']:
            ref_kinds[base.lower()].add(kind)

        def cands(name):
            kinds = tuple(sorted(ref_kinds.get(name.lower(), ()))) or ('ipt', 'iam')
            return [c for c in designs.find(name, kinds) if (os.path.basename(c[0]).lower(), c[1]) != me]
        pool = {}
        for n in [b for b, _ in d['refs']] + list(d['browser']):
            if cands(n):
                pool.setdefault(n.lower(), n)
        out, used = {}, set()
        for i, c in enumerate(d['comps']):
            # an exact name only counts when the assembly really references that file
            # (e.g. "Tube Frame" in the top level is Frame.iam, not Tube Frame.iam)
            if cands(c['name']) and (not ref_kinds or c['name'].lower() in ref_kinds):
                out[i] = nearest(asm_rel, cands(c['name']))
                used.add(c['name'].lower())
        scored = []
        for i, c in enumerate(d['comps']):
            if i in out:
                continue
            for key, n in pool.items():
                if key in used:
                    continue
                s = similarity(c['name'], n) + (0.15 if d['browser'].get(n) == len(c['occurrences']) else 0)
                scored.append((s, i, key))
        for s, i, key in sorted(scored, reverse=True):
            if i in out or key in used or s < 0.45:
                continue
            out[i] = nearest(asm_rel, cands(pool[key]))
            used.add(key)
        # Content Center hardware that is not in the project: generate a stand-in from its name
        for i, c in enumerate(d['comps']):
            if i not in out:
                p = fasteners.make(c['name'], os.path.join(a.export, '_standard_parts'), run_script)
                if p:
                    rel = os.path.relpath(p[:-3], a.export)
                    designs.add(rel, 'ipt')
                    out[i] = (rel, 'ipt')
        return [(c, out.get(i)) for i, c in enumerate(d['comps'])]

    plan = {rel: resolve_all(rel, d) for rel, d in asm.items()}

    # Inventor models with Y up, SolidCraft with Z up: stand the top-level assemblies (the ones
    # no other assembly uses) upright; sub-assemblies keep their own coordinates
    used_as_child = {t[0] for items in plan.values() for _, t in items if t and t[1] == 'iam'}
    Y_UP_TO_Z_UP = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]

    def upright(m):
        R = Y_UP_TO_Z_UP
        rot = [[m[4 * r + c] for c in range(3)] for r in range(3)]
        t = [m[3], m[7], m[11]]
        nr = [[sum(R[i][k] * rot[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
        nt = [sum(R[i][k] * t[k] for k in range(3)) for i in range(3)]
        return [nr[0][0], nr[0][1], nr[0][2], nt[0], nr[1][0], nr[1][1], nr[1][2], nt[1],
                nr[2][0], nr[2][1], nr[2][2], nt[2]]

    order, done = [], set()
    pending = [r for r in plan if not a.only or a.only.lower() in r.lower()]
    while pending:
        progressed = False
        for rel in list(pending):
            deps = {t[0] for _, t in plan[rel] if t and t[1] == 'iam' and t[0] != rel}
            if not (deps & set(pending)):
                order.append(rel)
                done.add(rel)
                pending.remove(rel)
                progressed = True
        if not progressed:
            order += pending
            break

    report = []
    for rel in order:
        d = asm[rel]
        cmds, missing, unplaced, skipped, placed = [], [], [], [], 0
        for comp, target in plan[rel]:
            if not target:
                missing.append(f"{comp['name']} x{len(comp['occurrences'])}")
                continue
            how = designs.geometry(*target)
            if how is None:
                skipped.append(f"{comp['name']} (no geometry)")
                continue
            for occ in comp['occurrences']:
                m = d['placements'].get(occ)
                if not m and d['placements']:
                    unplaced.append(f"{comp['name']}#{occ}")      # placement record not decoded
                    continue
                if rel not in used_as_child:
                    m = upright(m or [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0])
                matrix = [round(x, 9) for x in m] if m else None
                if how[0] == 'link':
                    prm = {'path': how[1], 'reuse': True, 'name': os.path.basename(target[0])}
                    if matrix:
                        prm.update(to_matrix=matrix, tolerance=1e-3)
                    cmds.append({'command': 'component.insert_design', 'params': prm})
                else:
                    tag = f'{os.path.basename(target[0])} [{occ}]'
                    if how[0] == 'step':
                        cmds.append({'command': 'file.insert_step', 'params': {'path': how[1], 'flatten': True, 'name': tag}})
                    else:
                        cmds.append({'command': 'file.insert_mesh', 'params': {'path': how[1], 'name': tag}})
                    if matrix:
                        # the first part placed in a design is grounded: free it, then place it
                        cmds.append({'command': 'occurrence.ground', 'params': {'occurrence': f'{tag}:1', 'grounded': False}})
                        cmds.append({'command': 'occurrence.move', 'params': {
                            'occurrence': f'{tag}:1', 'to_matrix': matrix, 'tolerance': 1e-3}})
                        cmds.append({'command': 'occurrence.ground', 'params': {'occurrence': f'{tag}:1', 'grounded': True}})
                placed += 1
        dest = designs.path(rel, 'iam')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        ok, errs, dropped = build(cmds, dest) if cmds else (False, ['nothing to place'], [])
        skipped += dropped
        m = measure(dest) if ok else None
        report.append({'assembly': rel, 'ok': ok, 'occurrences_placed': placed,
                       'bodies': (m or {}).get('bodies', ''), 'components': len(plan[rel]),
                       'missing': '; '.join(missing), 'skipped': '; '.join(skipped),
                       'unplaced': '; '.join(unplaced),
                       'mapping': '; '.join(f"{c['name']} -> {t[0] + '.' + t[1] if t else None}" for c, t in plan[rel]),
                       'errors': ' | '.join(errs)[:400]})
        print(f"[{len(report)}/{len(order)}] {'ok' if ok else 'failed':<6} {placed:4} occ  {rel}"
              f"{'  missing: ' + '; '.join(missing)[:80] if missing else ''}", flush=True)
    if report:
        with open(os.path.join(a.export, 'assembly_report.csv'), 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=list(report[0].keys()))
            w.writeheader()
            w.writerows(report)


if __name__ == '__main__':
    main()
