#!/usr/bin/env python3
"""
batch_decode.py - run il_decode.py (FreeCAD console + InventorLoader readers) over a whole
Inventor project. Works on COPIES (InventorLoader writes dump folders next to its input).

  python batch_decode.py <project_root> <work_dir> [--jobs 4] [--timeout 240] [--only substr]

* every live .ipt outside OldVersions
* plus, for components that assemblies reference but whose live file is missing, the newest
  OldVersions backup (e.g. "Frame Length Tube.0071.ipt" -> "Frame Length Tube.ipt")
Writes <work_dir>/decoded/<relative path>/<name>.{dc.json,step,status.json} and
<work_dir>/decode_report.csv
"""
import argparse, csv, json, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tool_paths import freecadcmd
FREECADCMD = freecadcmd()
VERSION_RE = re.compile(r'^(?P<base>.+?)\.(?P<ver>\d{4})(?:_\d+)?\.(?P<ext>ipt|iam)$', re.I)


def collect(root):
    """live: {relative path: abs path} for every live part (same-named parts in different
    folders are kept apart); backups: newest OldVersions copy per component name."""
    live, backups = {}, {}
    for dp, dn, fn in os.walk(root):
        in_old = 'OldVersions' in dp.split(os.sep)
        for f in fn:
            if not f.lower().endswith('.ipt'):
                continue
            p = os.path.join(dp, f)
            if in_old:
                m = VERSION_RE.match(f)
                if m:
                    key = m.group('base').lower()
                    ver = int(m.group('ver'))
                    if key not in backups or ver > backups[key][0]:
                        backups[key] = (ver, p)
            else:
                live[os.path.relpath(p, root)] = p
    return live, backups


def referenced_names(output_dir):
    """component names referenced by assemblies (from inv_extract.py's assembly_refs.csv)."""
    refs = os.path.join(output_dir, 'assembly_refs.csv')
    names = set()
    if os.path.exists(refs):
        with open(refs, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                if r.get('kind') == 'part':
                    names.add(r['component'].lower())
    return names


def run_one(src, rel, work, timeout, job_id):
    # short per-job folders keep InventorLoader's dump paths under Windows' 260-char limit;
    # job_map.csv maps each id back to the part's relative path
    jd = os.path.join(work, 'j', f'{job_id:04d}')
    dst_dir = os.path.join(jd, 'src')
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, 'part' + os.path.splitext(rel)[1].lower())
    shutil.copyfile(src, dst)
    out = os.path.join(jd, 'out')
    env = dict(os.environ, FC_IN=dst, FC_OUT=out, QT_QPA_PLATFORM='offscreen',
               FC_HANG_SECS=str(max(30, timeout - 20)))
    t0 = time.time()
    try:
        subprocess.run([FREECADCMD, os.path.join(HERE, 'il_decode.py')], env=env, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    st_path = os.path.join(out, 'part.status.json')
    st = {}
    if os.path.exists(st_path):
        with open(st_path, encoding='utf-8') as f:
            st = json.load(f)
    return {
        'job': f'{job_id:04d}', 'file': rel, 'source': src, 'stage': st.get('stage', 'no-status'),
        'error': st.get('error', ''), 'dc_nodes': st.get('dc_nodes', ''),
        'step': ';'.join(st.get('step', []) or []), 'step_error': st.get('step_error', ''),
        'seconds': round(time.time() - t0, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('work')
    ap.add_argument('--jobs', type=int, default=4)
    ap.add_argument('--timeout', type=int, default=240)
    ap.add_argument('--only', default=None)
    ap.add_argument('--assemblies', action='store_true', help='decode the live .iam files instead of parts')
    ap.add_argument('--extra', help='decode exactly the files in this CSV (source,file) - e.g. resolve_refs.py output')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if a.assemblies or a.extra:
        jobs = []
        if a.extra:
            with open(a.extra, encoding='utf-8') as f:
                jobs = [(r['source'], r['file']) for r in csv.DictReader(f)]
        else:
            for dp, dn, fn in os.walk(root):
                if 'OldVersions' in dp.split(os.sep):
                    continue
                jobs += [(os.path.join(dp, f), os.path.relpath(os.path.join(dp, f), root)) for f in fn if f.lower().endswith('.iam')]
        jobs.sort(key=lambda j: j[1])
        if not jobs:
            print('nothing to decode')
            return
        os.makedirs(a.work, exist_ok=True)
        rows = []
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = [ex.submit(run_one, src, rel, a.work, a.timeout, i) for i, (src, rel) in enumerate(jobs, 1)]
            for i, fu in enumerate(as_completed(futs), 1):
                rows.append(fu.result())
                print(f"[{i}/{len(jobs)}] {rows[-1]['stage']:<10} {rows[-1]['seconds']:>6}s  {rows[-1]['file']}", flush=True)
        rows.sort(key=lambda r: r['file'])
        with open(os.path.join(a.work, 'decode_report.csv'), 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return
    live, backups = collect(root)
    wanted = referenced_names(os.path.join(HERE, 'output'))

    jobs = []
    for rel, p in sorted(live.items()):
        jobs.append((p, rel))
    live_names = {os.path.splitext(os.path.basename(r))[0].lower() for r in live}
    recovered = []
    for key in sorted(wanted):
        if key not in live_names and key in backups:
            ver, p = backups[key]
            # decode it under the live name, in the folder its backup sits beside
            rel_dir = os.path.relpath(os.path.dirname(os.path.dirname(p)), root)
            rel = os.path.join(rel_dir, os.path.basename(VERSION_RE.match(os.path.basename(p)).group('base')) + '.ipt')
            jobs.append((p, rel))
            recovered.append((key, os.path.relpath(p, root)))
    if a.only:
        jobs = [j for j in jobs if a.only.lower() in j[1].lower()]
    print(f'{len(jobs)} parts to decode ({len(recovered)} recovered from OldVersions)')
    os.makedirs(a.work, exist_ok=True)
    with open(os.path.join(a.work, 'recovered_from_oldversions.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['component', 'backup_used'])
        w.writerows(recovered)

    rows = []
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(run_one, src, rel, a.work, a.timeout, i): rel for i, (src, rel) in enumerate(jobs, 1)}
        for i, fu in enumerate(as_completed(futs), 1):
            r = fu.result()
            rows.append(r)
            print(f"[{i}/{len(jobs)}] {r['stage']:<10} {r['seconds']:>6}s  {r['file']}  {r['error'][:80]}", flush=True)
    rows.sort(key=lambda r: r['file'])
    with open(os.path.join(a.work, 'decode_report.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    from collections import Counter
    print(Counter(r['stage'] for r in rows))
    print('steps:', sum(1 for r in rows if r['step']), 'of', len(rows))


if __name__ == '__main__':
    main()
