#!/usr/bin/env python3
"""
progress_gui.py - Inventor to SolidCraft: loading-bar window for the conversion.

Watch a run that is already going (follows its log):
    pythonw progress_gui.py --watch <log file>

Run the whole pipeline with a bar per stage:
    pythonw progress_gui.py --run --project <Inventor project> --work <work dir> --export <export dir>
      stages: decode parts -> decode assemblies -> translate parts -> build assemblies

The batch scripts print "[i/N] result  file" lines (and "ok/ERR ... assembly" for assemblies);
this window turns those into progress, counts per result and a log.
"""
import argparse, os, queue, re, subprocess, sys, threading, time
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

HERE = os.path.dirname(os.path.abspath(__file__))
LINE = re.compile(r'^\[(\d+)/(\d+)\]\s+(.*?)\s{2,}(\S.*?)(?:\s{2,}(.*))?$')
COUNT = re.compile(r'^(\d+) (?:parts|Inventor files)? ?to decode|^(\d+) parts to decode')
KNOWN_RESULTS = sorted([
    'native', 'step+sketches (unverified)', 'step+sketches', 'sewn-solid+sketches',
    'mesh+sketches (surfaces only in Inventor file)',
    'project-STL+sketches (no solid stored in Inventor file)',
    'parameters+sketches only (no geometry in Inventor file)',
    'parameters+sketches only (geometry could not be imported)',
    'failed', 'done', 'imported', 'no-status'], key=len, reverse=True)
RESULT_GROUPS = [
    ('native', 'Native (editable features)', '#2e7d32'),
    ('step', 'Exact solid + sketches', '#1565c0'),
    ('mesh', 'Mesh / STL + sketches', '#6a1b9a'),
    ('parameters', 'Parameters + sketches only', '#ef6c00'),
    ('failed', 'Failed', '#c62828'),
]


def group_of(result):
    r = (result or '').lower()
    if r.startswith('native'):
        return 'native'
    if r.startswith(('step', 'sewn')):
        return 'step'
    if 'stl' in r or r.startswith('mesh'):
        return 'mesh'
    if r.startswith('parameters'):
        return 'parameters'
    if r.startswith(('failed', 'err')):
        return 'failed'
    return None


# ---- retro CRT palette ("green phosphor")
BG, PANEL = '#050805', '#0b120b'
GREEN, DIM, DARK = '#39ff6a', '#1f9e45', '#0f3d1c'
AMBER, RED = '#ffb000', '#ff4f4f'
GROUP_COLORS = {'native': GREEN, 'step': '#7dffb0', 'mesh': AMBER, 'parameters': '#c8ff3d', 'failed': RED}


def mono(size, bold=False):
    for fam in ('Fixedsys', 'Terminal', 'Consolas', 'Courier New'):
        if fam in tkfont.families():
            return (fam, size, 'bold') if bold else (fam, size)
    return ('Courier', size)


class BlockBar(tk.Canvas):
    """chunky segmented bar: ▮▮▮▮▮▯▯▯ drawn as phosphor blocks with a glow edge."""

    def __init__(self, master, segments=40, height=26, color=GREEN):
        super().__init__(master, height=height, bg=BG, highlightthickness=1,
                         highlightbackground=DIM, bd=0)
        self.segments, self.color = segments, color
        self._value, self._max = 0.0, 1.0
        self.bind('<Configure>', lambda e: self.redraw())

    def configure(self, cnf=None, **kw):
        if 'value' in kw:
            self._value = float(kw.pop('value'))
        if 'maximum' in kw:
            self._max = max(float(kw.pop('maximum')), 1e-9)
        if kw or cnf:
            super().configure(cnf, **kw)
        self.redraw()

    config = configure

    def __getitem__(self, key):
        return self._max if key == 'maximum' else super().__getitem__(key)

    def redraw(self):
        self.delete('all')
        w, h = max(self.winfo_width(), 50), max(self.winfo_height(), 10)
        gap, pad = 3, 4
        seg_w = (w - 2 * pad - gap * (self.segments - 1)) / self.segments
        lit = int(round(self.segments * min(self._value / self._max, 1.0)))
        for i in range(self.segments):
            x0 = pad + i * (seg_w + gap)
            on = i < lit
            self.create_rectangle(x0, pad, x0 + seg_w, h - pad,
                                  fill=self.color if on else DARK, outline=DIM if on else '#0a1f0f')
        for y in range(0, h, 3):                       # scanlines
            self.create_line(0, y, w, y, fill=BG, stipple='gray25')


class App:
    BANNER = (
        r" ___ _   ___   __    ___  ___    ___ ___  _  ___   __", "\n",
        r"|_ _| \ | \ \ / /__ / __|/ __|  / __/ _ \| \| \ \ / /", "\n",
        r" | ||  \| |\ V /___|\__ \ (__  | (_| (_) | .` |\ V / ", "\n",
        r"|___|_|\__| \_/     |___/\___|  \___\___/|_|\_| \_/  ",
    )

    def __init__(self, root, title):
        self.root = root
        root.title(title)
        root.geometry('860x680')
        root.minsize(680, 520)
        root.configure(bg=BG)
        self.q = queue.Queue()
        F = mono(10)
        lab = lambda parent, var=None, text=None, fg=GREEN, font=F, **kw: tk.Label(
            parent, textvariable=var, text=text, fg=fg, bg=kw.pop('bg', BG), font=font, anchor='w', justify='left', **kw)

        frame = tk.Frame(root, bg=BG, highlightthickness=2, highlightbackground=DIM)
        frame.pack(fill='both', expand=True, padx=10, pady=10)

        tk.Label(frame, text=''.join(self.BANNER), fg=GREEN, bg=BG, font=mono(9), justify='left').pack(anchor='w', padx=12, pady=(10, 0))
        lab(frame, text='INVENTOR TO SOLIDCRAFT  ::  PART TRANSLATION TERMINAL  v1.0', fg=DIM).pack(anchor='w', padx=12)
        lab(frame, text='═' * 92, fg=DIM).pack(anchor='w', padx=12)

        self.stage = tk.StringVar(value='> WAITING FOR JOB')
        self.cursor_on = True
        lab(frame, self.stage, font=mono(14, True)).pack(anchor='w', padx=12, pady=(6, 2))

        self.bar = BlockBar(frame, segments=40, height=30)
        self.bar.pack(fill='x', padx=12, pady=(2, 2))
        self.pct = tk.StringVar(value='')
        lab(frame, self.pct, font=mono(11, True)).pack(anchor='w', padx=12)
        self.current = tk.StringVar(value='')
        lab(frame, self.current, fg=DIM).pack(anchor='w', padx=12, pady=(0, 6))

        self.overall_lbl = tk.StringVar(value='')
        lab(frame, self.overall_lbl, fg=AMBER).pack(anchor='w', padx=12)
        self.overall = BlockBar(frame, segments=24, height=18, color=AMBER)
        self.overall.pack(fill='x', padx=12, pady=(0, 8))

        box = tk.Frame(frame, bg=PANEL, highlightthickness=1, highlightbackground=DIM)
        box.pack(fill='x', padx=12, pady=4)
        lab(box, text=' RESULTS ', fg=BG, bg=GREEN, font=mono(9, True)).grid(row=0, column=0, sticky='w', padx=6, pady=(6, 4))
        self.counts, self.count_vars = {}, {}
        for i, (key, label, _) in enumerate(RESULT_GROUPS):
            r, c = 1 + i // 2, (i % 2) * 2
            col = GROUP_COLORS[key]
            lab(box, text=f'■ {label.upper():<28}', fg=col, bg=PANEL).grid(row=r, column=c, sticky='w', padx=(8, 0), pady=1)
            v = tk.StringVar(value='000')
            lab(box, v, fg=col, bg=PANEL, font=mono(10, True)).grid(row=r, column=c + 1, sticky='w', padx=(0, 30))
            self.counts[key], self.count_vars[key] = 0, v
        tk.Frame(box, bg=PANEL, height=6).grid(row=9, column=0)

        self.eta = tk.StringVar(value='')
        lab(frame, self.eta, fg=DIM).pack(anchor='w', padx=12, pady=(4, 0))

        logf = tk.Frame(frame, bg=BG)
        logf.pack(fill='both', expand=True, padx=12, pady=(4, 6))
        self.log = tk.Text(logf, height=10, wrap='none', font=mono(9), bg=BG, fg=DIM,
                           insertbackground=GREEN, relief='flat', highlightthickness=1,
                           highlightbackground=DARK, selectbackground=DARK)
        sb = tk.Scrollbar(logf, command=self.log.yview, bg=DARK, troughcolor=BG,
                          activebackground=DIM, relief='flat', width=12)
        self.log.configure(yscrollcommand=sb.set, state='disabled')
        self.log.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        for key, col in GROUP_COLORS.items():
            self.log.tag_configure(key, foreground=col)

        self.clock = tk.StringVar(value='')
        lab(frame, self.clock, fg=DIM, font=mono(9)).pack(anchor='e', padx=12, pady=(0, 6))

        self.t0 = None
        self.stage_index, self.stage_total = 0, 1
        self.stage_text = 'WAITING FOR JOB'
        root.after(150, self.pump)
        root.after(500, self.blink)

    def blink(self):
        self.cursor_on = not self.cursor_on
        self.stage.set('> ' + self.stage_text + (' █' if self.cursor_on else '  '))
        self.clock.set(time.strftime('SYS %H:%M:%S'))
        self.root.after(500, self.blink)

    # ------------------------------------------------------------------ updates
    def write(self, text, tag=None):
        self.log.configure(state='normal')
        self.log.insert('end', text.rstrip() + '\n', tag or ())
        self.log.see('end')
        self.log.configure(state='disabled')

    def set_stage(self, name, index, total):
        self.stage_text = name.upper()
        self.stage_index, self.stage_total = index, total
        self.bar.configure(value=0, maximum=1)
        self.pct.set('')
        self.t0 = time.time()
        self.overall.configure(maximum=total, value=index - 1)
        self.overall_lbl.set(f'PIPELINE  STAGE {index}/{total}')
        self.write(f'*** {name.upper()} ***', 'native')

    def progress(self, i, n, result, path):
        if self.t0 is None:
            self.t0 = time.time()
        self.bar.configure(maximum=n, value=i)
        self.pct.set(f'{100 * i // max(n, 1):3d}%   [{i:03d}/{n:03d}]')
        self.current.set(f'  LOADING: {path}')
        g = group_of(result)
        if g:
            self.counts[g] += 1
            self.count_vars[g].set(f'{self.counts[g]:03d}')
        el = time.time() - self.t0
        if i and i < n:
            left = el / i * (n - i)
            self.eta.set(f'  T+{int(el // 60):02d}:{int(el % 60):02d}   ETA {int(left // 60):02d}:{int(left % 60):02d}')
        elif i >= n:
            self.eta.set(f'  STAGE COMPLETE IN {int(el // 60):02d}:{int(el % 60):02d}')
        self.overall.configure(maximum=max(self.stage_total, 1),
                               value=max(self.stage_index - 1, 0) + i / max(n, 1))
        self.write(f'{i:03d}/{n:03d} {result.upper()[:34]:<34} {path}', g)

    def handle(self, line):
        line = line.rstrip()
        if not line:
            return
        m = re.match(r'^\[(\d+)/(\d+)\]\s+(.*)$', line)
        if m:
            i, n, rest = int(m.group(1)), int(m.group(2)), m.group(3)
            result = next((r for r in KNOWN_RESULTS if rest.startswith(r)), rest.split()[0])
            tail = rest[len(result):].strip()
            pm = re.match(r'^(.*?\.(?:ipt|iam))\b', tail, re.I)
            self.progress(i, n, result, pm.group(1) if pm else tail)
            return
        if line.startswith('STAGE|'):
            _, idx, tot, name = line.split('|', 3)
            self.set_stage(name, int(idx), int(tot))
            return
        if line.startswith('DONE'):
            self.stage_text = 'ALL JOBS COMPLETE'
            self.overall.configure(value=self.overall['maximum'])
            self.write(line)
            return
        am = re.match(r'^(ok |ERR)\s+(\d+) occ\s+(.*)$', line)
        if am:
            self.write(line, 'step' if am.group(1).startswith('ok') else 'failed')
            return
        self.write(line)

    def pump(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self.pump)


# ---------------------------------------------------------------------- sources
def follow(path, q, stop):
    """tail -f: replay the log so far, then follow new lines."""
    while not os.path.exists(path) and not stop.is_set():
        time.sleep(0.5)
    with open(path, encoding='utf-8', errors='replace') as f:
        while not stop.is_set():
            line = f.readline()
            if line:
                q.put(line)
            else:
                time.sleep(0.4)


def run_pipeline(a, q):
    py = sys.executable.replace('pythonw.exe', 'python.exe')
    project = a.project
    stages = [
        ('Decoding Inventor parts', [py, '-u', os.path.join(HERE, 'batch_decode.py'), project, a.work]),
        ('Decoding Inventor assemblies', [py, '-u', os.path.join(HERE, 'batch_decode.py'), project,
                                          os.path.join(a.work, 'asm'), '--assemblies']),
        ('Translating parts to SolidCraft', [py, '-u', os.path.join(HERE, 'batch_translate.py'), a.work, a.export,
                                             '--project', project]),
        ('Building SolidCraft assemblies', [py, '-u', os.path.join(HERE, 'sc_assembly.py'),
                                            os.path.join(a.work, 'asm'), a.export]),
    ]
    for k, (name, cmd) in enumerate(stages, 1):
        q.put(f'STAGE|{k}|{len(stages)}|{name}')
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding='utf-8', errors='replace',
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        n_seen = 0
        for line in p.stdout:
            # the decode stages print "done   0.7s  file" lines; number them for the bar
            dm = re.match(r'^\[(\d+)/(\d+)\]\s+(\S+)\s+([\d.]+)s\s+(.*)$', line.strip())
            if dm:
                line = f'[{dm.group(1)}/{dm.group(2)}] {dm.group(3):<10}  {dm.group(5)}'
            q.put(line)
            n_seen += 1
        p.wait()
        q.put(f'{name}: finished (exit {p.returncode})')
    q.put('DONE - see translate_report.csv and assembly_report.csv in the export folder')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--watch')
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--project', help='Inventor project folder (with --run)')
    ap.add_argument('--work')
    ap.add_argument('--export')
    ap.add_argument('--title', default='Inventor to SolidCraft')
    a = ap.parse_args()
    root = tk.Tk()
    try:
        ttk.Style().theme_use('vista')
    except tk.TclError:
        pass
    app = App(root, a.title)
    stop = threading.Event()
    if a.watch:
        app.set_stage('Translating parts to SolidCraft', 1, 1)
        threading.Thread(target=follow, args=(a.watch, app.q, stop), daemon=True).start()
    elif a.run:
        threading.Thread(target=run_pipeline, args=(a, app.q), daemon=True).start()
    root.protocol('WM_DELETE_WINDOW', lambda: (stop.set(), root.destroy()))
    root.mainloop()


if __name__ == '__main__':
    main()
