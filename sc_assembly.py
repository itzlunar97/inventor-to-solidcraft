#!/usr/bin/env python3
"""
sc_assembly.py - rebuild Inventor assemblies (.iam) as SolidCraft assemblies.

  python sc_assembly.py <asm work dir> <export root> [--only substr]

Reads what batch_decode.py --assemblies decoded for each .iam (AmRxSegment: component
definitions, their occurrence ids and each occurrence's placement matrix; AmBrowserSegment:
occurrence names), finds each component's translated design (.sc) under <export root>, and
writes <export root>/<relative path>.sc with one linked component per occurrence, placed with
Inventor's exact matrix. Sub-assemblies are built before the assemblies that use them.
Writes <export root>/assembly_report.csv.
"""
import argparse, csv, json, os, re, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sc_translate import parse_matrix, run_script, measure


def load(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return json.load(f)


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
            for v in (a or {}).get('props', {}).get('lst0') or []:
                if isinstance(v, int):
                    occ.append(v)
        if not occ and isinstance(n['props'].get('u32_0'), int):
            # sub-assembly occurrences of a top-level assembly use a record that does not
            # decode; the definition's own number is its (first) occurrence id
            occ = [n['props']['u32_0']]
        key = (n['name'], tuple(occ))
        if any((c['name'], tuple(c['occurrences'])) == key for c in comps):
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
            placements[occ_id] = [m[0][0], m[0][1], m[0][2], m[0][3],
                                  m[1][0], m[1][1], m[1][2], m[1][3],
                                  m[2][0], m[2][1], m[2][2], m[2][3]]
    names = []
    for n in br:
        nm = n.get('name')
        if isinstance(nm, str):
            m = re.match(r'^(.*):(\d+)$', nm)
            if m:
                names.append(m.group(1))
    counts = defaultdict(int)
    for b in names:
        counts[b] += 1
    return comps, placements, dict(counts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('work')
    ap.add_argument('export')
    ap.add_argument('--only')
    a = ap.parse_args()
    with open(os.path.join(a.work, 'decode_report.csv'), encoding='utf-8-sig') as f:
        jobs = [r for r in csv.DictReader(f) if r['stage'] == 'done']

    # every design that exists or will exist: parts already exported + assemblies to build
    designs = defaultdict(list)    # lower base name -> [relative path without extension]
    for dp, dn, fn in os.walk(a.export):
        for f in fn:
            if f.endswith('.sc') and not f.endswith('.sewn.sc'):
                rel = os.path.relpath(os.path.join(dp, f[:-3]), a.export)
                designs[os.path.basename(rel).lower()].append(rel)
    asm = {}
    for r in jobs:
        rel = os.path.splitext(r['file'])[0]
        comps, placements, counts = read_assembly(os.path.join(a.work, 'j', r['job'], 'out'))
        asm[rel] = {'comps': comps, 'placements': placements, 'browser': counts}
        if rel not in designs[os.path.basename(rel).lower()]:
            designs[os.path.basename(rel).lower()].append(rel)

    def resolve(asm_rel, comp, browser_left):
        """component display name -> design path (nearest folder wins for duplicates)."""
        cands = designs.get(comp['name'].lower(), [])
        if not cands:
            # renamed in the assembly: the browser shows file-based names; match by count
            # (the browser also lists constraints such as "Flush:1": only names with a design count)
            same = [b for b, c in browser_left.items()
                    if c == len(comp['occurrences']) and designs.get(b.lower())]
            if len(same) == 1:
                cands = designs.get(same[0].lower(), [])
                browser_left.pop(same[0], None)
        else:
            browser_left.pop(comp['name'], None)
        if not cands:
            return None
        here = os.path.dirname(asm_rel)
        return max(cands, key=lambda c: len(os.path.commonpath([here, os.path.dirname(c)])) if here else 0)

    plan = {}
    for rel, d in asm.items():
        left = dict(d['browser'])
        exact = [c for c in d['comps'] if designs.get(c['name'].lower())]
        renamed = [c for c in d['comps'] if not designs.get(c['name'].lower())]
        items = []
        for c in exact + renamed:
            items.append((c, resolve(rel, c, left)))
        plan[rel] = items

    order, done = [], set()
    pending = [r for r in plan if not a.only or a.only.lower() in r.lower()]
    while pending:
        progressed = False
        for rel in list(pending):
            deps = {t for _, t in plan[rel] if t in plan and t != rel}
            if deps <= done or not (deps & set(pending)):
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
        cmds, missing, placed = [], [], 0
        for comp, target in plan[rel]:
            path = os.path.join(a.export, target + '.sc') if target else None
            if not path or not os.path.exists(path):
                missing.append(f"{comp['name']} x{len(comp['occurrences'])}")
                continue
            for k, occ in enumerate(comp['occurrences'], 1):
                m = d['placements'].get(occ)
                prm = {'path': path, 'reuse': True, 'name': comp['name']}
                if m:
                    prm['to_matrix'] = [round(x, 9) for x in m]
                    prm['tolerance'] = 1e-3
                cmds.append({'command': 'component.insert_design', 'params': prm})
                placed += 1
        dest = os.path.join(a.export, rel + '.sc')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        ok, errs = run_script(cmds, dest) if cmds else (False, ['nothing to place'])
        m = measure(dest) if ok else None
        report.append({'assembly': rel, 'ok': ok, 'occurrences_placed': placed,
                       'components': len(plan[rel]), 'missing': '; '.join(missing),
                       'bodies': (m or {}).get('bodies', ''), 'errors': ' | '.join(errs)[:400]})
        print(f"{'ok ' if ok else 'ERR'} {placed:4} occ  {rel}  {('missing: ' + '; '.join(missing))[:90] if missing else ''}", flush=True)
    with open(os.path.join(a.export, 'assembly_report.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(report[0].keys()))
        w.writeheader()
        w.writerows(report)


if __name__ == '__main__':
    main()
