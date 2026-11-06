"""Voice changer for calls (laptop only).

Your microphone → a live voice effect → a virtual microphone ("CABLE Input", from the free
VB-CABLE driver). In WhatsApp Desktop, Meet, Zoom, Teams… you pick "CABLE Output" as the
microphone, and the other side hears the changed voice.

The effects are plain numpy, run in small blocks (about 10 ms) so the delay stays low.
"""
import threading
import time
from collections import deque

import numpy as np

RATE = 48000
BLOCK = 480  # 10 ms

# name -> (semitones, voice body, effect). Pitch is the note of your voice; "voice body" is the
# size of the throat and mouth it seems to come from (below 1 = bigger/deeper, above 1 = smaller).
# Changing both together, in natural proportions, is what makes it sound like another person
# rather than a sped-up or slowed-down recording.
PRESETS = {
    "Normal (no change)": (0, 1.00, None),
    "Deeper man": (-3, 0.92, None),
    "Very deep man": (-5, 0.86, None),
    "Older man": (-2, 0.95, "older"),
    "Younger / lighter": (2, 1.06, None),
    "Female voice": (5, 1.17, None),
    "Male voice (from a woman's voice)": (-5, 0.86, None),
    "Kid": (7, 1.25, None),
    "Custom (use the sliders)": (0, 1.00, None),
    "Effect: robot": (0, 1.00, "robot"),
    "Effect: radio / old phone": (0, 1.00, "radio"),
    "Effect: echo hall": (0, 1.00, "hall"),
}
# Names from earlier versions -> today's
OLD_NAMES = {"Deeper": "Deeper man", "Much deeper (monster)": "Very deep man", "Higher": "Younger / lighter",
             "Chipmunk": "Kid", "Robot": "Effect: robot", "Radio / old phone": "Effect: radio / old phone",
             "Echo hall": "Effect: echo hall", "Custom (use the slider)": "Custom (use the sliders)"}


class VoiceShifter:
    """Natural-sounding voice change, done in three steps every 256 samples (5 ms):

    1. Phase-vocoder time-stretch by the pitch ratio, with "phase locking" (each harmonic keeps
       its neighbouring frequencies in step, which is what removes the metallic/robotic sound).
    2. Voice body: the spectral envelope (the formants: throat and mouth) is measured with a
       cepstrum and re-applied where it should be after the pitch change, scaled by the body factor.
    3. Resample back to normal speed, which moves the pitch.

    About 45 ms of delay."""

    def __init__(self, n=2048, hop=256, lifter=48):
        self.N, self.ha, self.L = n, hop, lifter
        self.win = np.hanning(n)
        self.bins = n // 2 + 1
        self.k = np.arange(self.bins)
        self.omega = 2 * np.pi * self.k * hop / n
        self.fin = np.zeros(n)
        self.pending = np.zeros(0)
        self.last_ph = np.zeros(self.bins)
        self.syn_ph = np.zeros(self.bins)
        self.ola = np.zeros(4 * n)
        self.stretched = np.zeros(0)
        self.rpos = 0.0
        self.out = np.zeros(n - hop)     # start-up delay
        self.first = True

    def _envelope(self, mag):
        """Smooth spectral envelope (formants) via the cepstrum."""
        c = np.fft.irfft(np.log(mag + 1e-9), self.N)
        c[self.L:self.N - self.L] = 0
        return np.exp(np.fft.rfft(c, self.N).real)

    def _frame(self, ratio, body):
        N, ha = self.N, self.ha
        hs = max(1, int(round(ha * ratio)))
        X = np.fft.rfft(self.fin * self.win)
        mag, ph = np.abs(X), np.angle(X)
        # true frequency of each bin
        d = ph - self.last_ph - self.omega
        d = (d + np.pi) % (2 * np.pi) - np.pi
        self.last_ph = ph
        inst = (self.omega + d) / ha
        # phase locking: peaks advance on their own, every other bin follows its nearest peak
        peaks = np.where((mag[1:-1] > mag[:-2]) & (mag[1:-1] >= mag[2:]) & (mag[1:-1] > mag.max() * 1e-4))[0] + 1
        if self.first or len(peaks) == 0:
            self.syn_ph = ph.copy() if self.first else self.syn_ph + inst * hs
            self.first = False
        else:
            pk_ph = self.syn_ph[peaks] + inst[peaks] * hs
            mids = (peaks[:-1] + peaks[1:]) / 2.0
            owner = peaks[np.searchsorted(mids, self.k)]
            new = np.empty(self.bins)
            new[:] = pk_ph[np.searchsorted(mids, self.k)] + (ph - ph[owner])
            self.syn_ph = new
        # voice body: flatten the old formants, apply them where they belong after resampling
        if abs(ratio - 1) > 1e-4 or abs(body - 1) > 1e-4:
            env = self._envelope(mag)
            want = np.interp(self.k * ratio / body, self.k, env, right=env[-1])
            mag = mag / env * want
        y = np.fft.irfft(mag * np.exp(1j * self.syn_ph), N) * self.win
        y /= (3.0 * N / 8.0) / hs
        self.ola[:N] += y
        res = self.ola[:hs].copy()
        self.ola[:-hs] = self.ola[hs:]
        self.ola[-hs:] = 0
        return res

    def process(self, x, ratio, body=1.0):
        n = len(x)
        if abs(ratio - 1) < 1e-4 and abs(body - 1) < 1e-4:
            buf = np.concatenate([self.out, x.astype(np.float64)])
            self.out = buf[n:]
            return buf[:n].astype(np.float32)
        self.pending = np.concatenate([self.pending, x.astype(np.float64)])
        parts = [self.stretched]
        while len(self.pending) >= self.ha:
            self.fin[:-self.ha] = self.fin[self.ha:]
            self.fin[-self.ha:] = self.pending[:self.ha]
            self.pending = self.pending[self.ha:]
            parts.append(self._frame(ratio, body))
        st = np.concatenate(parts)
        # Resample by exactly the stretch that was applied (hop sizes are whole samples), so
        # the delay stays steady instead of drifting.
        ratio = max(1, int(round(self.ha * ratio))) / self.ha
        made = []
        pos = self.rpos
        avail = len(st) - 1
        if avail > 0:
            count = int(max(0, np.floor((avail - pos) / ratio)))
            if count > 0:
                p = pos + ratio * np.arange(count)
                i0 = np.floor(p).astype(np.int64)
                fr = p - i0
                made.append(st[i0] * (1 - fr) + st[i0 + 1] * fr)
                pos = pos + ratio * count
        drop = int(np.floor(pos))
        self.stretched = st[drop:]
        self.rpos = pos - drop
        buf = np.concatenate([self.out] + made)
        if len(buf) < n:
            buf = np.concatenate([np.zeros(n - len(buf)), buf])
        self.out = buf[n:]
        # don't let the delay creep up
        if len(self.out) > 2 * self.N:
            self.out = self.out[-self.N:]
        return buf[:n].astype(np.float32)


class PitchShifter:
    """Phase-vocoder pitch shifter (frequency-domain): every 512 samples it looks at the last
    2048, moves each frequency up or down by the pitch ratio while keeping it in phase, and
    overlaps the frames back together. Clean pitch change; about 40 ms of delay."""

    def __init__(self, n=2048, osamp=4):
        self.N = n
        self.step = n // osamp
        self.osamp = osamp
        self.win = np.hanning(n).astype(np.float64)
        self.k = np.arange(n // 2 + 1)
        self.expct = 2 * np.pi * self.step / n
        self.fin = np.zeros(n)
        self.pending = np.zeros(0)
        self.last_phase = np.zeros(n // 2 + 1)
        self.sum_phase = np.zeros(n // 2 + 1)
        self.accum = np.zeros(2 * n)
        self.out = np.zeros(n - self.step)  # the delay, filled with silence at the start

    def _frame(self, ratio):
        N, step = self.N, self.step
        X = np.fft.rfft(self.fin * self.win)
        mag = np.abs(X)
        ph = np.angle(X)
        d = ph - self.last_phase
        self.last_phase = ph
        d -= self.k * self.expct
        d = (d + np.pi) % (2 * np.pi) - np.pi
        true_bin = self.k + d * self.osamp / (2 * np.pi)
        # move every bin to bin * ratio
        tgt = np.floor(self.k * ratio + 0.5).astype(np.int64)
        ok = tgt <= N // 2
        syn_mag = np.zeros_like(mag)
        syn_bin = np.zeros_like(mag)
        np.add.at(syn_mag, tgt[ok], mag[ok])
        syn_bin[tgt[ok]] = true_bin[ok] * ratio
        dev = syn_bin - self.k
        self.sum_phase += dev * 2 * np.pi / self.osamp + self.k * self.expct
        y = np.fft.irfft(syn_mag * np.exp(1j * self.sum_phase), N) * self.win / 1.5
        self.accum[:N] += y
        res = self.accum[:step].copy()
        self.accum[:-step] = self.accum[step:]
        self.accum[-step:] = 0
        return res

    def process(self, x, ratio):
        if abs(ratio - 1.0) < 1e-4:
            # keep the same delay so switching effects doesn't jump
            buf = np.concatenate([self.out, x.astype(np.float64)])
            self.out = buf[len(x):]
            return buf[:len(x)].astype(np.float32)
        self.pending = np.concatenate([self.pending, x.astype(np.float64)])
        made = [self.out]
        while len(self.pending) >= self.step:
            self.fin[:-self.step] = self.fin[self.step:]
            self.fin[-self.step:] = self.pending[:self.step]
            self.pending = self.pending[self.step:]
            made.append(self._frame(ratio))
        buf = np.concatenate(made)
        n = len(x)
        if len(buf) < n:
            buf = np.concatenate([buf, np.zeros(n - len(buf))])
        self.out = buf[n:]
        return buf[:n].astype(np.float32)


class Effects:
    def __init__(self):
        self.ps = VoiceShifter()
        self.t = 0                      # samples so far (robot, older)
        self.echo = np.zeros(RATE, dtype=np.float32)
        self.ew = 0
        self.lp = 0.0                   # radio filters
        self.hp_x = 0.0
        self.hp_y = 0.0
        self.rin = 1e-4
        self.rout = 1e-4

    def run(self, x, semitones, effect, body=1.0):
        y = self.ps.process(x, 2.0 ** (semitones / 12.0), body)
        if semitones or abs(body - 1) > 1e-4:
            # Keep the changed voice as loud as the real one (smoothed, so it doesn't pump).
            self.rin = 0.9 * self.rin + 0.1 * float(np.sqrt(np.mean(x * x)) + 1e-6)
            self.rout = 0.9 * self.rout + 0.1 * float(np.sqrt(np.mean(y * y)) + 1e-6)
            y = y * float(np.clip(self.rin / self.rout, 0.5, 4.0))
        if effect == "older":
            # a slight, slow wobble, like an older voice
            n = len(y)
            t = (self.t + np.arange(n)) / RATE
            self.t += n
            y = y * (1 + 0.06 * np.sin(2 * np.pi * 5.5 * t)).astype(np.float32)
        elif effect == "robot":
            n = len(y)
            t = (self.t + np.arange(n)) / RATE
            self.t += n
            y = y * np.sin(2 * np.pi * 55.0 * t).astype(np.float32) * 1.6
        elif effect == "radio":
            y = self._band(y)
            y = np.tanh(y * 3.0) * 0.5
        elif effect == "hall":
            y = self._echo(y, 0.18, 0.38)
        return np.clip(y, -1.0, 1.0).astype(np.float32)

    def _band(self, y):
        # crude telephone band: one-pole high-pass (~300 Hz) then low-pass (~3 kHz), vectorised per block
        a_hp = 0.96
        out = np.empty_like(y)
        px, py = self.hp_x, self.hp_y
        for i, v in enumerate(y):  # 480 samples per block: cheap enough
            py = a_hp * (py + v - px)
            px = v
            out[i] = py
        self.hp_x, self.hp_y = px, py
        a_lp = 0.33
        lp = self.lp
        for i, v in enumerate(out):
            lp += a_lp * (v - lp)
            out[i] = lp
        self.lp = lp
        return out

    def _echo(self, y, seconds, decay):
        d = int(seconds * RATE)
        n = len(y)
        N = len(self.echo)
        idx = (self.ew + np.arange(n)) % N
        past = self.echo[(idx - d) % N]
        out = y + decay * past
        self.echo[idx] = out
        self.ew = (self.ew + n) % N
        return out * 0.8


def find_cable(devices):
    """The virtual microphone's playback side (what we play into). VB-CABLE names it
    "CABLE Input (VB-Audio Virtual Cable)"; newer versions use "CABLE In 16ch …"; Windows may cut
    names short. Prefer the 2-channel one."""
    best, score = None, 0
    for i, d in enumerate(devices):
        if d.get("max_output_channels", 0) <= 0:
            continue
        n = d.get("name", "").lower()
        s = 0
        if "cable input" in n:
            s = 4
        elif n.startswith("cable in"):
            s = 3
        elif "vb-audio" in n and "cable" in n and "output" not in n:
            s = 2
        elif n.startswith("cable") and "output" not in n:
            s = 1
        if "16" in n:
            s -= 0.5  # the 16-channel one also works, but the stereo one is simpler
        if s > score:
            best, score = i, s
    return best


class VoiceChanger:
    def __init__(self, settings, log=lambda m: None):
        self.settings = settings
        self.log = log
        self.running = False
        self.level = 0.0
        self.error = ""
        self.fx = Effects()
        self.lock = threading.Lock()
        self.q_out = deque()
        self.q_mon = deque()
        self.streams = []
        self.used_mic = ""
        self.warning = ""
        self.hist = deque()
        self.mute_until = 0.0

    @staticmethod
    def sd():
        import sounddevice
        return sounddevice

    def rescan(self):
        """Sound devices are read once when the audio library starts; this reads them again
        (e.g. after installing VB-CABLE while the app was open)."""
        try:
            sd = self.sd()
            if not self.running:
                sd._terminate()
                sd._initialize()
        except Exception:
            pass

    def devices(self):
        try:
            return list(self.sd().query_devices())
        except Exception as e:
            self.error = f"Couldn't list sound devices: {e}"
            return []

    def _api_order(self):
        """Windows lists each device once per audio system; MME is the most forgiving (it
        converts sample rates for us), then DirectSound, then WASAPI. WDM-KS is skipped."""
        try:
            apis = list(self.sd().query_hostapis())
        except Exception:
            return {}
        rank = {}
        for i, a in enumerate(apis):
            n = a.get("name", "")
            rank[i] = 0 if "MME" in n else 1 if "DirectSound" in n else 2 if "WASAPI" in n else 9
        return rank

    def mics(self):
        """(index, name) of each microphone, once, using the most reliable copy."""
        rank = self._api_order()
        best = {}
        for i, d in enumerate(self.devices()):
            n = d.get("name", "")
            low = n.lower()
            if d.get("max_input_channels", 0) <= 0 or "cable" in low or low.startswith("pc speaker") \
                    or "sound mapper" in low or "primary sound capture" in low:
                continue
            r = rank.get(d.get("hostapi"), 9)
            if r >= 9:
                continue
            key = low[:31]  # MME cuts names to 31 characters
            if key not in best or r < best[key][0]:
                best[key] = (r, i, n)
        return [(i, n) for _, i, n in sorted(best.values(), key=lambda x: x[2].lower())]

    def cables(self):
        """Every copy of VB-CABLE's playback side, most reliable first."""
        rank = self._api_order()
        devs = self.devices()
        out = []
        for i, d in enumerate(devs):
            if d.get("max_output_channels", 0) > 0 and find_cable([d]) == 0 and rank.get(d.get("hostapi"), 9) < 9:
                out.append((rank.get(d.get("hostapi"), 9), "16" in d.get("name", ""), i))
        return [i for _, _, i in sorted(out)]

    def cable(self):
        c = self.cables()
        return c[0] if c else None

    def _settings(self):
        preset = self.settings.get("voice_preset") or "Deeper man"
        preset = OLD_NAMES.get(preset, preset)
        semis, body, effect = PRESETS.get(preset, (0, 1.0, None))
        semis += float(self.settings.get("voice_pitch") or 0)
        body *= 1 + float(self.settings.get("voice_body") or 0) / 100.0
        return semis, body, effect

    def _in(self, indata, frames, t, status):
        x = indata[:, 0].astype(np.float32)
        self.level = float(np.sqrt(np.mean(x * x)) * 4)
        semis, body, effect = self._settings()
        on = self.settings.get("voice_on")
        if self._howling(x):
            # Feedback: the changer is hearing itself. Go silent for a moment so the loop dies.
            self.mute_until = time.time() + 1.5
            if self.settings.get("voice_monitor"):
                self.settings.set("voice_monitor", False)
                self.warning = ("Feedback (whistling) detected, so “Hear myself” was turned off — the laptop "
                                "speakers were feeding back into the microphone. Use headphones to hear yourself.")
            else:
                self.warning = ("Feedback (whistling) detected and stopped. Check that the microphone isn't "
                                "“CABLE Output” and that no app plays the call into the microphone.")
        if time.time() < self.mute_until:
            x = np.zeros_like(x)
            self.fx = Effects()   # clear the echo/pitch memory of the howl
        y = self.fx.run(x, semis, effect, body) if on is None or on else x
        with self.lock:
            self.q_out.append(y)
            while len(self.q_out) > 6:      # keep the delay short
                self.q_out.popleft()
            if self.settings.get("voice_monitor"):
                self.q_mon.append(y)
                while len(self.q_mon) > 6:
                    self.q_mon.popleft()

    def _howling(self, x):
        """Feedback is a loud sound that never pauses; speech pauses between syllables several
        times a second. True after 1.2 s of loud sound with no pause in it."""
        rms = float(np.sqrt(np.mean(x * x)))
        self.hist.append(rms)
        if len(self.hist) > 120:
            self.hist.popleft()
        if len(self.hist) < 120:
            return False
        h = np.fromiter(self.hist, dtype=np.float32)
        med = float(np.median(h))
        if med < 0.03:
            return False
        dips = int(np.sum(h < 0.35 * med))
        if dips == 0 and float(h.min()) > 0.5 * med:
            self.hist.clear()
            return True
        return False

    def _pull(self, q, outdata, frames):
        need = frames
        out = np.zeros(frames, dtype=np.float32)
        pos = 0
        with self.lock:
            while need > 0 and q:
                b = q[0]
                take = min(len(b), need)
                out[pos:pos + take] = b[:take]
                if take < len(b):
                    q[0] = b[take:]
                else:
                    q.popleft()
                pos += take
                need -= take
        for c in range(outdata.shape[1]):
            outdata[:, c] = out

    def _default_name(self, kind):
        try:
            d = self.sd().query_devices(kind=kind)
            return d.get("name", "") if d else ""
        except Exception:
            return ""

    def _open(self, kind, candidates, **kw):
        """Opens the first device that works; returns (stream, index)."""
        sd = self.sd()
        last = None
        for idx in candidates:
            try:
                st = (sd.InputStream if kind == "in" else sd.OutputStream)(device=idx, samplerate=RATE, blocksize=BLOCK,
                                                                          dtype="float32", latency="low", **kw)
                return st, idx
            except Exception as e:
                last = e
        raise RuntimeError(str(last) if last else "no device")

    def start(self):
        if self.running:
            return
        sd = self.sd()
        if not self.cables():
            self.rescan()
        cables = self.cables()
        if not cables:
            outs = sorted({d.get("name", "") for d in self.devices() if d.get("max_output_channels", 0) > 0})
            raise RuntimeError("Can't find VB-CABLE's “CABLE Input” device. If you just installed it, restart the "
                               "laptop.\n\nSound outputs this laptop has:\n• " + "\n• ".join(outs[:20]))
        mics = self.mics()
        want = self.settings.get("voice_mic")
        # Windows often makes "CABLE Output" the default microphone when VB-CABLE is installed.
        # Recording from it would mean listening to our own output (feedback), so never do that.
        default_in = self._default_name("input")
        default_ok = "cable" not in default_in.lower()
        if not default_ok:
            self.warning = ("Windows' default microphone is “CABLE Output” (VB-CABLE set that when it installed). "
                            "That's right for call apps; the voice changer uses your real microphone instead.")
        real = sorted(mics, key=lambda m: (0 if any(w in m[1].lower() for w in ("microphone", "mic array", "mic ")) else 1))
        order = [i for i, n in mics if n == want] + ([None] if default_ok else []) + [i for i, n in real if n != want]
        self.fx = Effects()
        self.q_out.clear()
        self.q_mon.clear()
        devs = self.devices()
        self.streams = []
        try:
            ins, used = self._open("in", order, channels=1, callback=self._in)
            self.used_mic = "Windows default microphone" if used is None else devs[used].get("name", "")
            if want and self.used_mic != want:
                self.log(f"Voice changer: couldn't open “{want}”, using “{self.used_mic}” instead.")

            def outs_for(ch_idx):
                return min(2, devs[ch_idx].get("max_output_channels", 2) or 1)
            outs = None
            last = None
            for c in cables:
                try:
                    outs, _ = self._open("out", [c], channels=outs_for(c),
                                         callback=lambda o, f, t, s: self._pull(self.q_out, o, f))
                    break
                except Exception as e:
                    last = e
            if outs is None:
                raise RuntimeError(f"Couldn't open VB-CABLE: {last}")
            self.streams = [ins, outs]
            # "Hear myself" goes to your speakers/headphones, never into the cable.
            if "cable" not in self._default_name("output").lower():
                try:
                    mon, _ = self._open("out", [None], channels=1, callback=lambda o, f, t, s: self._pull(self.q_mon, o, f))
                    self.streams.append(mon)
                except Exception:
                    pass  # no speakers to hear yourself on: fine
            for st in self.streams:
                st.start()
        except Exception:
            self.stop()
            raise
        self.running = True
        self.error = ""
        self.log(f"Voice changer on (microphone: {self.used_mic}).")

    def stop(self):
        for s in self.streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.streams = []
        if self.running:
            self.log("Voice changer off.")
        self.running = False
        self.level = 0.0


def _vowel(f0, formants, seconds=1.0):
    """A buzzy vowel: a pulse train at f0 through resonances at the given frequencies."""
    n = int(RATE * seconds)
    x = np.zeros(n)
    x[::int(RATE / f0)] = 1.0
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / RATE)
    H = np.zeros_like(f)
    for i, F in enumerate(formants):
        H += (1.0 / (i + 1)) / (1 + ((f - F) / (F * 0.08)) ** 2)
    y = np.fft.irfft(X * H, n)
    return (0.4 * y / np.abs(y).max()).astype(np.float32)


def _formant1(x):
    """Frequency of the first big hump in the spectral envelope (300-1500 Hz)."""
    seg = x[: (len(x) // 4096) * 4096].reshape(-1, 4096)
    mag = np.mean(np.abs(np.fft.rfft(seg * np.hanning(4096), axis=1)), axis=0)
    c = np.fft.irfft(np.log(mag + 1e-9))
    c[40:-40] = 0
    env = np.exp(np.fft.rfft(c).real)
    f = np.fft.rfftfreq(4096, 1 / RATE)
    band = (f > 300) & (f < 1500)
    return float(f[band][np.argmax(env[band])])


def _f0(x):
    """Pitch by autocorrelation (60-500 Hz)."""
    seg = x[:8192].astype(np.float64)
    seg = seg - seg.mean()
    ac = np.fft.irfft(np.abs(np.fft.rfft(seg, 16384)) ** 2)[:8192]
    lo, hi = int(RATE / 500), int(RATE / 60)
    lag = lo + int(np.argmax(ac[lo:hi]))
    # refine with a parabola through the peak
    a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
    shift = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0
    return RATE / (lag + shift)


def selftest():
    """Checks the effects without any sound device: a 220 Hz tone shifted up 12 semitones comes
    out near 440 Hz, down 12 near 110 Hz, and every preset runs."""
    t = np.arange(RATE) / RATE
    tone = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    def peak(semis):
        fx = Effects()
        out = np.concatenate([fx.run(tone[i:i + BLOCK], semis, None) for i in range(0, len(tone), BLOCK)])
        seg = out[RATE // 4:]
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        return np.fft.rfftfreq(len(seg), 1 / RATE)[np.argmax(spec)]
    up, down, same = peak(12), peak(-12), peak(0)
    assert abs(up - 440) < 15, up
    assert abs(down - 110) < 10, down
    assert abs(same - 220) < 5, same
    for semis in (-8, -4, 4, 9):
        fx = Effects()
        out = np.concatenate([fx.run(tone[i:i + BLOCK], semis, None) for i in range(0, len(tone), BLOCK)])
        ratio = float(np.sqrt(np.mean(out[RATE // 2:] ** 2)) / np.sqrt(np.mean(tone ** 2)))
        assert 0.6 < ratio < 1.6, (semis, ratio)
    for name, (semis, body, eff) in PRESETS.items():
        fx = Effects()
        for i in range(0, RATE // 2, BLOCK):
            y = fx.run(tone[i:i + BLOCK], semis, eff, body)
            assert y.shape == (BLOCK,) and np.isfinite(y).all(), name
    names = [{"name": "Speakers (Realtek)", "max_output_channels": 2},
             {"name": "CABLE Output (VB-Audio Virtual Cable)", "max_output_channels": 0, "max_input_channels": 2},
             {"name": "CABLE In 16ch (VB-Audio Virtual Cable)", "max_output_channels": 16}]
    assert find_cable(names) == 2
    names.append({"name": "CABLE Input (VB-Audio Virtual C", "max_output_channels": 2})
    assert find_cable(names) == 3
    assert find_cable(names[:1]) is None
    # Voice body: a synthetic vowel ("ah": 120 Hz buzz shaped by formants near 700/1200/2600 Hz).
    vowel = _vowel(120, (700, 1200, 2600))
    f1_in, f0_in = _formant1(vowel), _f0(vowel)
    for semis, body, want_f0, want_f1 in ((5, 1.0, 120 * 2 ** (5 / 12), f1_in),
                                          (-4, 1.0, 120 * 2 ** (-4 / 12), f1_in),
                                          (5, 1.17, 120 * 2 ** (5 / 12), f1_in * 1.17),
                                          (-5, 0.86, 120 * 2 ** (-5 / 12), f1_in * 0.86)):
        fx = Effects()
        out = np.concatenate([fx.run(vowel[i:i + BLOCK], semis, None, body) for i in range(0, len(vowel), BLOCK)])
        out = out[RATE // 4:]
        f0, f1 = _f0(out), _formant1(out)
        assert abs(f0 / want_f0 - 1) < 0.04, ("pitch", semis, body, f0, want_f0)
        assert abs(f1 / want_f1 - 1) < 0.12, ("voice body", semis, body, f1, want_f1, f1_in)
    # the delay stays steady (no slow drift into clicks or lag)
    for semis, body in ((-3, 0.92), (5, 1.17)):
        fx = Effects()
        held = []
        long_v = np.tile(vowel, 6)
        for i in range(0, len(long_v), BLOCK):
            fx.run(long_v[i:i + BLOCK], semis, None, body)
            held.append(len(fx.ps.out))
        assert max(held[100:]) - min(held[100:]) < 1200, ("delay drifts", semis, min(held[100:]), max(held[100:]))
    # Feedback guard. Simulate the real problem: the changer's output leaks back into its own
    # microphone (as from speakers, or a microphone set to the cable). That must be caught...
    class _S2(dict):
        def get(self, k): return dict.get(self, k)
        def set(self, k, v): self[k] = v
    vc = VoiceChanger(_S2(voice_preset="Deeper man", voice_monitor=True))
    seed = _vowel(120, (700, 1200, 2600), 0.3)
    loop = deque([np.zeros(BLOCK, dtype=np.float32)] * 8)   # 80 ms from speaker to microphone
    caught_at = None
    for b in range(400):                                      # 4 seconds
        src = seed[b * BLOCK:(b + 1) * BLOCK] if (b + 1) * BLOCK <= len(seed) else np.zeros(BLOCK, dtype=np.float32)
        mic = np.clip(src + 1.6 * loop.popleft(), -1, 1).astype(np.float32)
        vc._in(mic.reshape(-1, 1), BLOCK, None, None)
        o = np.zeros((BLOCK, 1), dtype=np.float32)
        vc._pull(vc.q_out, o, BLOCK)
        loop.append(o[:, 0])
        if caught_at is None and vc.mute_until > 0:
            caught_at = b * BLOCK / RATE
    assert caught_at is not None and caught_at < 2.5, ("feedback loop not caught", caught_at)
    assert vc.settings.get("voice_monitor") is False and vc.warning
    # ...while talking (syllables with gaps) must not be.
    vc = VoiceChanger(_S2(voice_preset="Deeper man"))
    syl = _vowel(120, (700, 1200, 2600), 4.0)
    env = (np.sin(2 * np.pi * 4 * np.arange(len(syl)) / RATE) > -0.2).astype(np.float32)
    talk = syl * env
    for i in range(0, len(talk), BLOCK):
        vc._in(talk[i:i + BLOCK].reshape(-1, 1), BLOCK, None, None)
    assert vc.mute_until == 0, "talking was mistaken for feedback"
    # the live audio step, with the app's real settings object (get() takes one argument)
    class _Strict:
        def __init__(self, d): self.d = d
        def get(self, k): return self.d.get(k)
    vc = VoiceChanger(_Strict({"voice_preset": "Female voice", "voice_pitch": 2.0, "voice_body": 5, "voice_monitor": True}))
    for i in range(0, RATE // 10, BLOCK):
        vc._in(tone[i:i + BLOCK].reshape(-1, 1), BLOCK, None, None)
    o = np.zeros((BLOCK, 2), dtype=np.float32)
    vc._pull(vc.q_out, o, BLOCK)
    assert np.abs(o).max() > 0 and vc.level > 0 and vc.q_mon, "audio step produced nothing"
    # speed: one 10 ms block of every preset well under 10 ms
    t0 = time.perf_counter()
    fx = Effects()
    for _ in range(100):
        for name, (semis, body, eff) in PRESETS.items():
            fx.run(tone[:BLOCK], semis, eff, body)
    per = (time.perf_counter() - t0) / (100 * len(PRESETS)) * 1000
    assert per < 5, f"too slow: {per:.2f} ms per block"
    return f"VOICE OK (up {up:.0f} Hz, down {down:.0f} Hz, {per:.2f} ms per block)"


if __name__ == "__main__":
    print(selftest())
