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
import argparse, math, os, queue, re, subprocess, sys, threading, time
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


class SpiderTrack(tk.Canvas):
    """progress as a spider crawling along a silk thread towards its web: the thread behind it is
    spun (lit), the way ahead is dotted, legs scuttle while it moves and it bobs while it waits."""

    def __init__(self, master, height=86):
        super().__init__(master, height=height, bg=BG, highlightthickness=1, highlightbackground=DIM, bd=0)
        self._value, self._max = 0.0, 1.0
        self.x = None            # drawn position (eases towards the target)
        self.phase = 0
        self.bind('<Configure>', lambda e: self.draw())
        self.after(90, self.tick)

    def configure(self, cnf=None, **kw):
        if 'value' in kw:
            self._value = float(kw.pop('value'))
        if 'maximum' in kw:
            self._max = max(float(kw.pop('maximum')), 1e-9)
        if kw or cnf:
            super().configure(cnf, **kw)

    config = configure

    def __getitem__(self, key):
        return self._max if key == 'maximum' else super().__getitem__(key)

    def geometry(self):
        w, h = max(self.winfo_width(), 120), max(self.winfo_height(), 40)
        left, right = 34, w - 80           # thread runs from the left post to the web
        y = h * 0.30
        return w, h, left, right, y

    def target_x(self):
        w, h, left, right, y = self.geometry()
        return left + (right - left) * min(self._value / self._max, 1.0)

    def tick(self):
        tx = self.target_x()
        if self.x is None:
            self.x = tx
        moving = abs(tx - self.x) > 0.6
        self.x += (tx - self.x) * 0.18 if moving else (tx - self.x)
        self.phase += 1
        self.draw(moving)
        self.after(90, self.tick)

    def draw(self, moving=False):
        self.delete('all')
        w, h, left, right, y = self.geometry()
        x = self.x if self.x is not None else left
        # anchor post and the web at the finish
        self.create_line(left - 14, 4, left - 14, h - 4, fill=DIM, width=2)
        cx, cy, r = right + 38, y, min(30, h * 0.45)
        for k in range(8):
            a = math.pi * 2 * k / 8
            self.create_line(cx, cy, cx + r * math.cos(a), cy + r * math.sin(a), fill=DIM)
        for rr in (r * 0.35, r * 0.65, r * 0.95):
            pts = []
            for k in range(9):
                a = math.pi * 2 * k / 8
                pts += [cx + rr * math.cos(a), cy + rr * math.sin(a)]
            self.create_line(*pts, fill=DIM)
        # silk: spun behind the spider, dotted ahead
        self.create_line(left - 14, y, right + 38 - r, y, fill=DARK, dash=(2, 4))
        self.create_line(left - 14, y, x, y, fill=GREEN, width=2)
        # the spider (S scales it)
        S = 1.7
        moving_now = moving or (self._value < self._max and self.phase % 40 < 3)
        bob = 0 if moving_now else math.sin(self.phase / 4.0) * 1.5 * S
        sy = y + 9 * S + bob                   # hangs just under the thread
        step = (self.phase % 2) if moving_now else 0
        for side in (-1, 1):                   # 4 legs a side, alternating gait
            for i in range(4):
                lift = ((3 if (i + step) % 2 else -3) * S) if moving_now else 0
                hip_x, hip_y = x + (-6 + i * 4) * S, sy
                knee_x = hip_x + (-5 + i * 3.5) * S
                knee_y = hip_y + (side * 9 - 4) * S
                foot_x = knee_x + (-4 + i * 3) * S + lift
                foot_y = hip_y + side * 15 * S
                self.create_line(hip_x, hip_y, knee_x, knee_y, foot_x, foot_y, fill=GREEN, width=2)
        self.create_line(x + 2 * S, y, x + 2 * S, sy - 6 * S, fill=GREEN)                       # dragline
        self.create_oval(x - 11 * S, sy - 7 * S, x + 5 * S, sy + 7 * S, fill=BG, outline=GREEN, width=2)   # abdomen
        self.create_line(x - 7 * S, sy, x + 1 * S, sy, fill=DIM)                                # back marking
        self.create_oval(x + 3 * S, sy - 5 * S, x + 13 * S, sy + 5 * S, fill=GREEN, outline=GREEN)         # head
        if self.phase % 30 != 0:                                                              # blinking eyes
            for ey in (-2, 2):
                self.create_rectangle(x + 9 * S, sy + (ey - 1) * S, x + 11 * S, sy + (ey + 1) * S, fill=BG, outline=BG)
        # percent tag riding with the spider
        pct = int(100 * min(self._value / self._max, 1.0))
        self.create_text(min(max(x, left + 20), right - 10), h - 9, text=f'{pct:3d}%', fill=AMBER, font=mono(9, True))
        for yy in range(0, h, 3):                # scanlines
            self.create_line(0, yy, w, yy, fill=BG, stipple='gray25')


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
        root.geometry('860x740')
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

        self.bar = SpiderTrack(frame, height=120)
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

        # 8-bit soundtrack toggle (top right)
        self.player = None
        self.music_lbl = tk.Label(frame, text='♪ MUSIC: OFF', fg=DIM, bg=BG, font=mono(9, True), cursor='hand2')
        self.music_lbl.place(relx=1.0, x=-14, y=12, anchor='ne')
        self.music_lbl.bind('<Button-1>', lambda e: self.toggle_music())

        self.t0 = None
        self.stage_index, self.stage_total = 0, 1
        self.stage_text = 'WAITING FOR JOB'
        root.after(150, self.pump)
        root.after(500, self.blink)

    def toggle_music(self, want=None):
        if self.player is None:
            import chiptune
            self.player = chiptune.Player()
        on = self.player.on if want is None else (not want)
        if on:
            self.player.stop()
        else:
            self.music_lbl.config(text='♪ LOADING...', fg=AMBER)
            # render off the UI thread (first time only), then start the loop
            def go():
                ok = self.player.start()
                self.root.after(0, lambda: self.music_lbl.config(
                    text='♪ MUSIC: ON' if ok else '♪ NO AUDIO', fg=GREEN if ok else RED))
            threading.Thread(target=go, daemon=True).start()
            return
        self.music_lbl.config(text='♪ MUSIC: OFF', fg=DIM)

    def close(self):
        if self.player:
            self.player.stop()

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


def demo(q, stop):
    """fake a 4-stage conversion so the interface can be seen without a real job."""
    import random
    rnd = random.Random()
    results = ['native'] * 2 + ['step+sketches'] * 7 + ['project-STL+sketches (no solid stored in Inventor file)',
               'parameters+sketches only (no geometry in Inventor file)']
    names = ['Frame Height Tube', 'Tube Frame Corner Joint', 'Radiator', 'Pump Holder', 'Mobo Duct',
             'Fan Knob', 'Split Loom End Cap', 'IO Box', 'M3 x 16 Socket Head Cap Screw', 'Reservoir Tube']
    stages = [('Decoding Inventor parts', 60, 0.05), ('Decoding Inventor assemblies', 20, 0.08),
              ('Translating parts to SolidCraft', 60, 0.25), ('Building SolidCraft assemblies', 15, 0.3)]
    while not stop.is_set():
        for k, (name, n, dt) in enumerate(stages, 1):
            q.put(f'STAGE|{k}|{len(stages)}|{name}')
            for i in range(1, n + 1):
                if stop.is_set():
                    return
                res = 'done' if k <= 2 else rnd.choice(results)
                q.put(f'[{i}/{n}] {res:<28} demo\\{rnd.choice(names)}.ipt')
                time.sleep(dt * rnd.uniform(0.5, 1.6))
        q.put('DONE - demo finished, starting again')
        time.sleep(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--watch')
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--project', help='Inventor project folder (with --run)')
    ap.add_argument('--work')
    ap.add_argument('--export')
    ap.add_argument('--title', default='Inventor to SolidCraft')
    ap.add_argument('--no-music', action='store_true', help='start with the 8-bit soundtrack off')
    ap.add_argument('--demo', action='store_true', help='simulate a conversion to show the interface')
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
    elif a.demo:
        threading.Thread(target=demo, args=(app.q, stop), daemon=True).start()
    if not a.no_music:
        root.after(300, lambda: app.toggle_music(True))
    root.protocol('WM_DELETE_WINDOW', lambda: (stop.set(), app.close(), root.destroy()))
    root.mainloop()


if __name__ == '__main__':
    main()
