"""
chiptune.py - a small original 8-bit loop in the style of old keygen / crack-intro music,
synthesised from scratch (pulse-wave lead, square bass, noise drums) and written as a WAV.
No samples, no third-party tunes. Played looping by progress_gui.py.
"""
import array, math, os, random, struct, tempfile, wave

SR = 22050
BPM = 140


def _freq(midi):
    return 440.0 * 2 ** ((midi - 69) / 12.0)


def _pulse(phase, duty):
    return 1.0 if (phase % 1.0) < duty else -1.0


def render(path=None):
    """render the loop once (cached in the temp folder) and return the WAV path."""
    path = path or os.path.join(tempfile.gettempdir(), 'inv2sc_chiptune_v1.wav')
    if os.path.exists(path):
        return path
    beat = 60.0 / BPM
    s16 = beat / 4
    # i - VI - III - VII in A minor: Am, F, C, G (twice; the second pass adds a hook)
    chords = [(57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62)] * 2
    arp = [0, 1, 2, 3, 2, 1, 0, 1, 2, 3, 4, 3, 2, 1, 2, 3]       # 16ths over chord tones
    hook = [76, None, 74, 76, None, 79, 76, None, 72, None, 71, 72, None, 74, 71, None]
    bars = len(chords)
    n = int(SR * bars * 4 * beat)
    mix = [0.0] * n
    rnd = random.Random(7)
    noise = [rnd.uniform(-1, 1) for _ in range(SR)]

    def add_note(start, dur, f, amp, duty, decay, vib=0.0):
        a, b = int(start * SR), min(n, int((start + dur) * SR))
        ph = 0.0
        for i in range(a, b):
            t = (i - a) / SR
            ff = f * (1 + vib * math.sin(2 * math.pi * 6 * t))
            ph += ff / SR
            env = math.exp(-t * decay)
            mix[i] += amp * env * _pulse(ph, duty)

    for bar, chord in enumerate(chords):
        t0 = bar * 4 * beat
        tones = list(chord) + [chord[0] + 12, chord[1] + 12]
        for k, idx in enumerate(arp):                          # lead arpeggio (25% pulse)
            add_note(t0 + k * s16, s16 * 0.95, _freq(tones[idx] + 12), 0.16, 0.25, 9.0, vib=0.004)
        if bar >= 4:                                           # second pass: hook on top (12.5% pulse)
            for k, m in enumerate(hook):
                if m is not None:
                    add_note(t0 + k * s16, s16 * 1.8, _freq(m), 0.14, 0.125, 4.0, vib=0.008)
        for k in range(8):                                     # bass, octave-jumping 8ths
            m = chord[0] - 24 + (12 if k % 2 else 0)
            add_note(t0 + k * beat / 2, beat / 2 * 0.9, _freq(m), 0.22, 0.5, 6.0)
        for k in range(4):                                     # drums
            tb = t0 + k * beat
            if k in (0, 2):                                    # kick: pitch-dropping sine
                a = int(tb * SR)
                for i in range(a, min(n, a + int(0.14 * SR))):
                    t = (i - a) / SR
                    mix[i] += 0.45 * math.exp(-t * 22) * math.sin(2 * math.pi * (55 + 90 * math.exp(-t * 40)) * t)
            else:                                              # snare: noise burst
                a = int(tb * SR)
                for i in range(a, min(n, a + int(0.12 * SR))):
                    t = (i - a) / SR
                    mix[i] += 0.22 * math.exp(-t * 28) * noise[(i * 7) % SR]
            a = int((tb + beat / 2) * SR)                       # off-beat hi-hat
            for i in range(a, min(n, a + int(0.03 * SR))):
                t = (i - a) / SR
                mix[i] += 0.08 * math.exp(-t * 90) * noise[(i * 13) % SR]

    out = array.array('B', (max(0, min(255, int(128 + 110 * math.tanh(v)))) for v in mix))
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(1)          # 8-bit, of course
        w.setframerate(SR)
        w.writeframes(out.tobytes())
    return path


class Player:
    """loop the tune in the background (Windows winsound); silently does nothing elsewhere."""

    def __init__(self):
        self.on = False
        try:
            import winsound
            self.ws = winsound
        except ImportError:
            self.ws = None

    def start(self):
        if not self.ws:
            return False
        path = render()
        self.ws.PlaySound(path, self.ws.SND_FILENAME | self.ws.SND_ASYNC | self.ws.SND_LOOP)
        self.on = True
        return True

    def stop(self):
        if self.ws:
            self.ws.PlaySound(None, 0)
        self.on = False

    def toggle(self):
        self.stop() if self.on else self.start()
        return self.on


if __name__ == '__main__':
    print(render())
