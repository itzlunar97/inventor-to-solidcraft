#!/usr/bin/env python3
"""
batch_translate.py - run sc_translate.py over every part batch_decode.py decoded.

  python batch_translate.py <work_dir> <dest_root> [--jobs 4] [--only substr]

<dest_root>/<relative path>.sc        SolidCraft design (native, or exact body + sketches)
<dest_root>/<relative path>.step      exact solid (InventorLoader ACIS -> STEP)
<dest_root>/<relative path>.sc.report.json
<dest_root>/translate_report.csv
"""
import argparse, csv, json, os, shutil, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_translate


STL_INDEX = {}


def index_stls(project_root):
    """live STLs in the Inventor project, by lower-case base name (used only for parts whose
    Inventor file stores no solid at all)."""
    for dp, dn, fn in os.walk(project_root):
        if 'OldVersions' in dp.split(os.sep):
            continue
        for f in fn:
            if f.lower().endswith('.stl'):
                STL_INDEX.setdefault(os.path.splitext(f)[0].lower(), os.path.join(dp, f))


def stl_for(part_base):
    key = part_base.lower()
    if key in STL_INDEX:
        return STL_INDEX[key]
    starts = sorted(k for k in STL_INDEX if k.startswith(key + ' '))
    return STL_INDEX[starts[0]] if len(starts) == 1 else None


def one(row, work, dest_root, scripts):
    rel = row['file']
    base = os.path.splitext(rel)[0]
    out_dir = os.path.join(work, 'j', row['job'], 'out')
    dest = os.path.join(dest_root, base + '.sc')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    step = os.path.join(out_dir, 'part.step')
    if os.path.exists(step):
        shutil.copyfile(step, os.path.join(dest_root, base + '.step'))
    try:
        rep = sc_translate.translate(out_dir, dest, os.path.basename(base), scripts,
                                     stl_hint=stl_for(os.path.basename(base)))
    except Exception as e:
        rep = {'result': 'failed', 'why_not_native': f'{type(e).__name__}: {e}'}
    return {
        'file': rel, 'result': rep.get('result'), 'why_not_native': rep.get('why_not_native', ''),
        'parameters': rep.get('parameters', ''), 'sketches': rep.get('sketches', ''),
        'features': '; '.join(rep.get('features', [])),
        'notes': ' | '.join(rep.get('notes', []))[:500],
        'native_errors': ' | '.join((rep.get('native') or {}).get('errors', []))[:500],
        'build_errors': ' | '.join((rep.get('step_build') or {}).get('errors', []))[:500],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('work')
    ap.add_argument('dest')
    ap.add_argument('--jobs', type=int, default=4)
    ap.add_argument('--only')
    ap.add_argument('--project', help='Inventor project root: its STL files are used for parts whose '
                                      'Inventor file stores no solid')
    a = ap.parse_args()
    if a.project:
        index_stls(a.project)
    with open(os.path.join(a.work, 'decode_report.csv'), encoding='utf-8-sig') as f:
        rows = [r for r in csv.DictReader(f) if r['stage'] == 'done']
    if a.only:
        rows = [r for r in rows if a.only.lower() in r['file'].lower()]
    scripts = os.path.join(a.work, 'scripts')
    os.makedirs(scripts, exist_ok=True)
    os.makedirs(a.dest, exist_ok=True)
    out = []
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = [ex.submit(one, r, a.work, a.dest, scripts) for r in rows]
        for i, fu in enumerate(as_completed(futs), 1):
            r = fu.result()
            out.append(r)
            print(f"[{i}/{len(rows)}] {r['result']:<28} {r['file']}  {r['why_not_native'][:70]}", flush=True)
    out.sort(key=lambda r: r['file'])
    with open(os.path.join(a.dest, 'translate_report.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    from collections import Counter
    print(Counter(r['result'] for r in out))


if __name__ == '__main__':
    main()
