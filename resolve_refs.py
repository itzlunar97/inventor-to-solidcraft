#!/usr/bin/env python3
"""
resolve_refs.py - find files that assemblies reference but the project's live folders lack,
and pick the newest OldVersions backup of each (Inventor keeps "<name>.<NNNN>.<ext>" copies).

  python resolve_refs.py <project_root> <out.csv> <asm work dir>[;<asm work dir>...] [--skip <decode_report.csv>;...]

Writes <out.csv> with columns source,file (file = the live-style relative path to decode it as),
for both parts and assemblies. Files already decoded (listed in a --skip report) are left out.
"""
import argparse, csv, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sc_assembly import file_refs

VERSION_RE = re.compile(r'^(?P<base>.+?)\.(?P<ver>\d{4})(?:_\d+)?\.(?P<ext>ipt|iam)$', re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('out')
    ap.add_argument('work')
    ap.add_argument('--skip', default='')
    a = ap.parse_args()
    root = os.path.abspath(a.root)

    live, backups = set(), {}
    for dp, dn, fn in os.walk(root):
        old = 'OldVersions' in dp.split(os.sep)
        for f in fn:
            if not f.lower().endswith(('.ipt', '.iam')):
                continue
            if old:
                m = VERSION_RE.match(f)
                if m:
                    key = (m.group('base').lower(), m.group('ext').lower())
                    ver = int(m.group('ver'))
                    if key not in backups or ver > backups[key][0]:
                        backups[key] = (ver, os.path.join(dp, f), m.group('base'))
            else:
                live.add((os.path.splitext(f)[0].lower(), os.path.splitext(f)[1][1:].lower()))
    decoded = set()
    for rep in [r for r in a.skip.split(';') if r]:
        with open(rep, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                b, e = os.path.splitext(os.path.basename(r['file']))
                decoded.add((b.lower(), e[1:].lower()))

    wanted = set()
    for w in [w for w in a.work.split(';') if w]:
        jroot = os.path.join(w, 'j')
        for job in os.listdir(jroot) if os.path.isdir(jroot) else []:
            iam = os.path.join(jroot, job, 'src', 'part.iam')
            if os.path.exists(iam):
                for base, kind in file_refs(iam):
                    wanted.add((base.lower(), kind))
    rows = []
    for key in sorted(wanted):
        if key in live or key in decoded or key not in backups:
            continue
        ver, src, real = backups[key]
        rel_dir = os.path.relpath(os.path.dirname(os.path.dirname(src)), root)
        rows.append({'source': src, 'file': os.path.join(rel_dir, f'{real}.{key[1]}')})
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['source', 'file'])
        w.writeheader()
        w.writerows(rows)
    print(f'{len(rows)} referenced files recovered from OldVersions')
    for r in rows:
        print('  ', r['file'])


if __name__ == '__main__':
    main()
