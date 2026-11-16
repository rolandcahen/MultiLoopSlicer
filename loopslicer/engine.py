"""
Moteur de MultiLoop Slicer.

Reprend loop_slicer.py (R. Cahen) : analyse de la grille de tempo, sélection
des boucles les plus différenciées, longueurs exactes, fondus anti-clic.
Ajouts : classement par descripteur (sonie, énergie, brillance, harmonicité,
activité rythmique), mode assemblage multi-fichiers, choix du format de sortie
et du nombre de pistes.

Deux modes d'entrée :
  * « découpage »  : un fichier à tempo fixe est découpé en boucles de N
                     mesures, calées sur la grille de mesures (et de phrases),
                     toutes de durée strictement identique.
  * « assemblage » : plusieurs fichiers (une boucle par fichier) sont mis à la
                     même longueur et rassemblés.

Les boucles sont classées selon un descripteur (croissant ou décroissant) et
écrites dans UN fichier multicanal : boucle 1 → canaux 1-2, boucle 2 → 3-4…
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Optional, Sequence

import numpy as np
import soundfile as sf
import librosa
import pyloudnorm as pyln

# --------------------------------------------------------------------------
# Paramètres
# --------------------------------------------------------------------------

DESCRIPTORS = {
    # clé : (libellé affiché, description courte)
    "loudness": ("Sonie (LUFS)", "Volume perçu, norme EBU R128 / ITU-R BS.1770"),
    "energy": ("Énergie (RMS dB)", "Énergie du signal, non pondérée"),
    "brightness": ("Brillance (centroïde)", "Centre de gravité spectral, en Hz"),
    "harmonicity": ("Harmonicité", "Part tonale / harmonique vs bruit et percussions"),
    "activity": ("Activité rythmique", "Densité d'attaques, impulsivité, part percussive, fluctuation"),
    "distinctiveness": ("Distinctivité", "Ordre de choix par la sélection (les plus différenciées d'abord)"),
    "position": ("Position d'origine", "Ordre chronologique dans le fichier source"),
}

SAMPLE_RATES = [44100, 48000, 88200, 96000]
SUBTYPES = {  # libellé : sous-type soundfile
    "16 bit": "PCM_16",
    "24 bit": "PCM_24",
    "32 bit int": "PCM_32",
    "32 bit float": "FLOAT",
}
FORMATS = {"WAV": "WAV", "AIFF": "AIFF"}

Logger = Callable[[str], None]
Progress = Callable[[float], None]


@dataclass
class Settings:
    # découpage
    measures_per_loop: int = 2
    beats_per_measure: int = 4
    bpm: Optional[float] = None          # None = détection automatique
    snap_bpm: bool = True                # arrondi au BPM entier si très proche
    phrase_bars: int = 2                 # les boucles démarrent toutes les N mesures
    downbeat_shift: int = 0              # correction manuelle (en temps)
    phrase_shift: int = 0                # correction manuelle (en mesures)
    beat_offset_ms: float = 0.0          # décalage fin de toute la grille
    preroll_ms: float = 0.0              # démarre chaque boucle un peu avant le temps
    # sélection
    exclude_similar: bool = True         # écarte les boucles trop ressemblantes
    similarity_max: float = 80.0         # % : ressemblance au-delà de laquelle on écarte
    min_dist: Optional[float] = None     # seuil absolu (expert, cf. loop_slicer.py --min-dist) ;
                                         # s'il est donné, il remplace similarity_max
    loud_floor: float = -45.0            # ignore les segments sous ce niveau (LUFS)
    weights: tuple = (1.0, 1.0, 1.0, 0.7)  # timbre, harmonie, texture, loudness
    # classement
    descriptor: str = "loudness"
    ascending: bool = True               # True = up (du plus faible au plus fort)
    # sortie
    n_tracks: int = 64                   # canaux du fichier de sortie ; 0 = auto
    channels_per_loop: int = 2           # 2 = stereo, 1 = mono
    file_format: str = "WAV"
    sample_rate: int = 48000
    subtype_label: str = "32 bit float"
    fade_ms: float = 1.0
    export_individual: bool = False      # écrit aussi un fichier par boucle
    output_dir: Optional[str] = None     # None = <source>_Loops à côté du fichier


@dataclass
class Loop:
    index: int                 # position d'origine (1 = première)
    source: str                # nom du fichier source
    start_sample: int          # début dans le fichier source (sr d'origine)
    start_measure: float       # n° de mesure (1 = mesure 1 de la grille)
    raw: np.ndarray = field(repr=False)     # segment stéréo au sr source
    sr_in: int = 0
    audio: Optional[np.ndarray] = field(default=None, repr=False)  # au sr de sortie
    distinct: float = float("nan")          # distance min. aux boucles déjà choisies
    similarity: float = float("nan")        # % de ressemblance avec la plus proche des autres retenues
    pick_order: int = 0
    features: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Lecture (loop_slicer.py)
# --------------------------------------------------------------------------

def load_audio(path: str):
    """Renvoie (signal float64 stéréo, sr). Décode via ffmpeg si libsndfile
    ne sait pas lire le format (m4a, aac…)."""
    tmp = None
    try:
        sf.info(path)
    except Exception:
        if shutil.which("ffmpeg"):
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path,
                            "-c:a", "pcm_f32le", tmp], check=True)
            path = tmp
    try:
        y, sr = sf.read(path, dtype="float64", always_2d=True)
    except Exception:
        y, sr = librosa.load(path, sr=None, mono=False)
        y = np.atleast_2d(y).T.astype(np.float64)
    finally:
        if tmp:
            os.unlink(tmp)
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    return y[:, :2], int(sr)


# --------------------------------------------------------------------------
# Tempo (loop_slicer.py)
# --------------------------------------------------------------------------

def grid_score(oenv, period, phases, t_from, t_to):
    """Moyenne de l'enveloppe d'onsets sur une grille (période, phase) en trames."""
    n0 = np.ceil((t_from - phases) / period).clip(0)
    k = np.arange(int((t_to - t_from) / period) + 2)
    idx = phases[:, None] + (n0[:, None] + k[None, :]) * period
    idx = np.where(idx < t_to, idx, np.nan)
    vals = np.where(np.isnan(idx), np.nan,
                    oenv[np.nan_to_num(idx).round().astype(int).clip(0, len(oenv) - 1)])
    return np.nanmean(vals, axis=1)


def detect_tempo(mono, sr, bpm_hint=None, snap=True, fixed=False):
    hop = sr // 200                                   # trames de 5 ms
    fps = sr / hop
    oenv = librosa.onset.onset_strength(y=mono, sr=sr, hop_length=hop, n_fft=2048)
    oenv = oenv / (oenv.max() + 1e-12)
    a, b = int(0.05 * len(oenv)), int(0.95 * len(oenv))   # ignore les bords

    if bpm_hint is None:
        y22 = librosa.resample(mono, orig_sr=sr, target_sr=22050)
        bpm_hint = float(np.atleast_1d(librosa.beat.beat_track(y=y22, sr=22050)[0])[0])

    def search(bpms, step):
        best = (-1, None, None)
        for bpm in bpms:
            period = 60 / bpm * fps
            ph = np.arange(0, period, step)
            s = grid_score(oenv, period, ph, a, b)
            i = int(np.argmax(s))
            if s[i] > best[0]:
                best = (s[i], bpm, ph[i])
        return best

    if fixed:
        bpm = bpm_hint
    else:
        _, bpm, _ = search(np.arange(bpm_hint * 0.95, bpm_hint * 1.05, 0.05), 0.5)
        _, bpm, _ = search(np.arange(bpm - 0.06, bpm + 0.06, 0.002), 0.5)
    if snap and not fixed and abs(bpm - round(bpm)) < 0.05:
        bpm = float(round(bpm))
    period = 60 / bpm * fps
    ph = np.arange(0, period, 0.1)
    phase = ph[int(np.argmax(grid_score(oenv, period, ph, a, b)))]
    beat0 = phase / fps                               # premier temps (s), < 1 temps

    # dérive : décalage de phase optimal par fenêtres de 15 s
    drift = []
    for t in np.arange(a / fps, b / fps - 15, 15):
        s = grid_score(oenv, period, ph, t * fps, (t + 15) * fps)
        d = (ph[int(np.argmax(s))] - phase + period / 2) % period - period / 2
        drift.append(round(float(d / fps * 1000), 1))
    return float(bpm), beat0, drift


# --------------------------------------------------------------------------
# Mesures (loop_slicer.py)
# --------------------------------------------------------------------------

def detect_downbeat(mono, sr, bpm, beat0, beats_per_bar, phrase_bars):
    """Phase du premier temps (en temps) puis phase de phrase (en mesures),
    par la nouveauté spectrale beat-synchrone + attaques graves."""
    B = 60 / bpm
    hop = 480
    S = np.abs(librosa.stft(mono, n_fft=4096, hop_length=hop))
    fr = librosa.fft_frequencies(sr=sr, n_fft=4096)
    tt = librosa.frames_to_time(np.arange(S.shape[1]), sr=sr, hop_length=hop)
    nb = int((len(mono) / sr - beat0) / B)
    bt = beat0 + np.arange(nb) * B
    fi = np.searchsorted(tt, bt).clip(0, S.shape[1] - 1)

    low = S[(fr > 30) & (fr < 120)].sum(0)
    lowon = np.maximum(0, np.diff(low, prepend=low[0]))
    kick = np.array([lowon[max(0, i - 2):i + 3].max() for i in fi])

    P = S ** 2
    chroma = librosa.feature.chroma_stft(S=P, sr=sr)
    mf = librosa.feature.mfcc(S=librosa.power_to_db(librosa.feature.melspectrogram(S=P, sr=sr)), n_mfcc=13)
    F = np.vstack([chroma, mf / 50])
    bf = np.array([F[:, fi[i]:max(fi[i] + 1, fi[i + 1])].mean(1) for i in range(nb - 1)])
    nov = np.r_[0, np.linalg.norm(np.diff(bf, axis=0), axis=1)]
    kick = kick[:nb - 1]

    z = lambda v: (v - v.mean()) / (v.std() + 1e-12)
    beat_idx = np.arange(nb - 1)
    db_scores = [z(nov)[beat_idx % beats_per_bar == p].mean()
                 + 0.5 * z(kick)[beat_idx % beats_per_bar == p].mean()
                 for p in range(beats_per_bar)]
    db = int(np.argmax(db_scores))

    bar_beats = np.arange(db, nb - 1, beats_per_bar)
    bar_nov = nov[bar_beats]
    # les plus grands changements de section (moyenne des 3 plus forts) fixent le début de phrase
    ph_scores = [np.sort(bar_nov[np.arange(len(bar_nov)) % phrase_bars == p])[-3:].mean()
                 for p in range(phrase_bars)]
    pb = int(np.argmax(ph_scores))
    return db, pb, [round(float(s), 3) for s in db_scores], [round(float(s), 3) for s in ph_scores]


# --------------------------------------------------------------------------
# Sélection des boucles les plus différenciées (loop_slicer.py)
# --------------------------------------------------------------------------

def segment_features(seg, sr):
    m = seg.mean(1).astype(np.float32)
    Sx = np.abs(librosa.stft(m, n_fft=2048, hop_length=512))
    P = Sx ** 2
    mel = librosa.power_to_db(librosa.feature.melspectrogram(S=P, sr=sr, n_mels=64))
    mf = librosa.feature.mfcc(S=mel, n_mfcc=20)
    ch = librosa.feature.chroma_stft(S=P, sr=sr).mean(1)
    con = librosa.feature.spectral_contrast(S=Sx, sr=sr)
    on = librosa.onset.onset_strength(S=mel, sr=sr)
    fr = librosa.fft_frequencies(sr=sr, n_fft=2048)
    bands = [P[(fr >= lo) & (fr < hi)].sum() for lo, hi in
             [(20, 120), (120, 500), (500, 2000), (2000, 6000), (6000, sr / 2 + 1)]]
    bands = 10 * np.log10(np.array(bands) / (P.sum() + 1e-12) + 1e-12)
    side = np.sqrt(((seg[:, 0] - seg[:, 1]) ** 2).mean()
                   / (((seg[:, 0] + seg[:, 1]) ** 2).mean() + 1e-12) + 1e-12)
    return dict(timbre=np.r_[mf[1:].mean(1), mf[1:].std(1)],
                harm=ch / (ch.sum() + 1e-9),
                tex=np.r_[con.mean(1), bands, on.mean(), on.std(), side])


def similarity_threshold(D, s):
    """Distance en dessous de laquelle deux boucles sont « trop ressemblantes ».

    La ressemblance est exprimée en % par rapport à la distance médiane entre
    toutes les boucles du morceau : 100 % = identiques, 0 % = aussi différentes
    que deux boucles prises au hasard. 80 % correspond à peu près au seuil
    --min-dist 0.5 de loop_slicer.py."""
    if s.min_dist is not None:
        return float(s.min_dist)
    if not s.exclude_similar:
        return 0.0
    off = D[~np.eye(len(D), dtype=bool)]
    med = float(np.median(off)) if off.size else 0.0
    return (1 - s.similarity_max / 100.0) * med


def distance_matrix(feats, louds, weights):
    def Z(k):
        X = np.array([f[k] for f in feats])
        X = (X - X.mean(0)) / (X.std(0) + 1e-9)
        return X / np.sqrt(X.shape[1])
    L = np.where(np.isfinite(louds), louds, -70.0)[:, None]
    blocks = [Z("timbre") * weights[0], Z("harm") * weights[1], Z("tex") * weights[2],
              (L - L.mean()) / (L.std() + 1e-9) * weights[3]]
    X = np.hstack(blocks)
    return np.linalg.norm(X[:, None] - X[None], axis=2)


def select(bars, D, louds, nbars, n_max, min_dist, loud_floor):
    """Sélection gloutonne max-min : on part de la boucle la plus éloignée de
    toutes les autres, puis on ajoute à chaque fois la plus éloignée de celles
    déjà choisies, sans chevauchement (nbars), tant que la distance ≥ min_dist.
    Renvoie ([(indice, distance)], raison de l'arrêt)."""
    bars = np.asarray(bars)
    valid = np.isfinite(louds) & (louds > loud_floor)
    if not valid.any():
        return [], "aucune boucle assez forte"
    first = int(np.argmax(np.where(valid, D.sum(1), -1)))
    sel, dmins = [first], [np.inf]
    reason = "plus de candidates disponibles"
    while len(sel) < n_max:
        best, bi = -1, None
        for i in range(len(bars)):
            if not valid[i] or i in sel or any(abs(bars[i] - bars[j]) < nbars for j in sel):
                continue
            d = D[i, sel].min()
            if d > best:
                best, bi = d, i
        if bi is None:
            break
        if best < min_dist:
            reason = "les autres candidates ressemblent trop aux boucles retenues"
            break
        sel.append(bi)
        dmins.append(best)
    else:
        reason = "nombre de pistes atteint"
    return list(zip(sel, dmins)), reason


def _similarities(D, idx):
    """% de ressemblance de chaque boucle retenue avec la plus proche des autres."""
    off = D[~np.eye(len(D), dtype=bool)]
    med = float(np.median(off)) if off.size else 1.0
    out = []
    for i in idx:
        others = [D[i, j] for j in idx if j != i]
        out.append(100 * max(0.0, 1 - min(others) / med) if others and med > 0 else float("nan"))
    return out


# --------------------------------------------------------------------------
# Conversion de format
# --------------------------------------------------------------------------

def _fade(x: np.ndarray, sr: int, fade_ms: float):
    F = int(round(fade_ms / 1000 * sr))
    if F <= 1 or 2 * F >= len(x):
        return x
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, F))   # cosinus surélevé
    x = x.copy()
    x[:F] *= ramp[:, None]
    x[-F:] *= ramp[::-1, None]
    return x


def _to_output(x: np.ndarray, sr_in: int, length_out: int, s: Settings):
    if s.channels_per_loop == 1:
        x = x.mean(axis=1, keepdims=True)
    if sr_in != s.sample_rate:
        x = librosa.resample(x.T, orig_sr=sr_in, target_sr=s.sample_rate, res_type="soxr_vhq").T
    if len(x) < length_out:
        x = np.vstack([x, np.zeros((length_out - len(x), x.shape[1]))])
    return _fade(x[:length_out], s.sample_rate, s.fade_ms)


def _slots(s: Settings, available: int) -> int:
    return s.n_tracks // s.channels_per_loop if s.n_tracks > 0 else available


# --------------------------------------------------------------------------
# Mode découpage
# --------------------------------------------------------------------------

def slice_file(path: str, s: Settings, log: Logger, progress: Progress):
    y, sr = load_audio(path)
    mono = y.mean(1).astype(np.float32)
    log(f"Lecture : {os.path.basename(path)} — {sr} Hz, {len(y) / sr:.2f} s")

    log("Analyse du tempo…")
    bpm, beat0, drift = detect_tempo(mono, sr, bpm_hint=s.bpm, snap=s.snap_bpm,
                                     fixed=s.bpm is not None)
    beat0 += s.beat_offset_ms / 1000
    B = 60 / bpm
    BAR = s.beats_per_measure * B
    log(f"  tempo {bpm:.3f} BPM{' (imposé)' if s.bpm else ''}, premier temps à {beat0:.4f} s")
    if drift:
        log(f"  écart de la grille par fenêtres de 15 s (ms) : {drift}")
        if max(abs(d) for d in (drift[1:-1] or drift)) > 25:
            log("  ATTENTION : écarts importants, le tempo n'est peut-être pas constant.")
    progress(0.15)

    log("Recherche du premier temps de mesure…")
    db, pb, db_scores, ph_scores = detect_downbeat(mono, sr, bpm, beat0,
                                                   s.beats_per_measure, s.phrase_bars)
    db = (db + s.downbeat_shift) % s.beats_per_measure
    pb = (pb + s.phrase_shift) % s.phrase_bars
    T0 = (beat0 + db * B + pb * BAR) % (s.phrase_bars * BAR)
    log(f"  scores par temps {db_scores} -> temps {db} ; scores de phrase {ph_scores} -> mesure {pb}")
    log(f"  mesure 1 à {T0:.4f} s, une mesure = {BAR:.4f} s")
    progress(0.25)

    nbars = s.measures_per_loop
    L = int(round(nbars * BAR * sr))
    pre = int(round(s.preroll_ms / 1000 * sr))
    cands, n = [], 0
    while True:
        st = int(round((T0 + n * BAR) * sr)) - pre
        if st + L > len(y):
            break
        if st >= 0 and n % s.phrase_bars == 0:
            cands.append((n, st))
        n += 1
    if not cands:
        raise ValueError(f"Morceau trop court pour des boucles de {nbars} mesure(s)")

    log(f"{len(cands)} positions candidates — analyse des timbres…")
    meter = pyln.Meter(sr)
    feats, louds = [], []
    for i, (_, st) in enumerate(cands):
        seg = y[st:st + L]
        louds.append(meter.integrated_loudness(seg))
        feats.append(segment_features(seg, sr))
        progress(0.25 + 0.3 * (i + 1) / len(cands))
    louds = np.array(louds)

    n_max = _slots(s, len(cands))
    D = distance_matrix(feats, louds, s.weights)
    thr = similarity_threshold(D, s)
    picked, reason = select([c[0] for c in cands], D, louds, nbars, n_max, thr, s.loud_floor)
    sims = _similarities(D, [i for i, _ in picked])
    log(f"Sélection : {len(picked)} boucles différenciées (sur {n_max} demandées), "
        f"sans chevauchement, ≥ {s.loud_floor:.0f} LUFS — arrêt : {reason}")
    if s.exclude_similar and s.min_dist is None:
        log(f"  boucles trop ressemblantes écartées (ressemblance > {s.similarity_max:.0f} %)")

    loop_seconds = nbars * BAR
    length_out = int(round(loop_seconds * s.sample_rate))
    loops = []
    for order, ((i, dm), sim) in enumerate(zip(picked, sims), 1):
        bar, st = cands[i]
        loops.append(Loop(index=bar // s.phrase_bars + 1, source=os.path.basename(path),
                          start_sample=st, start_measure=bar + 1, raw=y[st:st + L], sr_in=sr,
                          distinct=float(dm), similarity=sim, pick_order=order))
    log(f"Boucles de {nbars} mesure(s) = {loop_seconds:.4f} s "
        f"({L} samples à {sr} Hz → {length_out} à {s.sample_rate} Hz)")
    info = dict(mode="slice", bpm=bpm, beat0_s=beat0, downbeat_beat=db, phrase_bar=pb,
                bar1_s=T0, bar_s=BAR, drift_ms=drift, candidates=len(cands),
                loop_seconds=loop_seconds, loop_samples=length_out)
    return loops, info


# --------------------------------------------------------------------------
# Mode assemblage
# --------------------------------------------------------------------------

def merge_files(paths: Sequence[str], s: Settings, log: Logger, progress: Progress):
    """Une boucle par fichier. Toutes sont ramenées à la longueur médiane (ce qui
    corrige les bounces à ±1 sample). Un fichier deux fois plus court que la
    référence est doublé (boucle répétée)."""
    raw = [(p, *load_audio(p)) for p in paths]
    secs = np.array([len(y) / sr for _, y, sr in raw])
    ref = float(np.median(secs))
    length_out = int(round(ref * s.sample_rate))
    log(f"{len(raw)} fichiers — longueur de référence {ref:.4f} s "
        f"({length_out} samples à {s.sample_rate} Hz)")
    loops, feats, louds = [], [], []
    for i, (p, y, sr) in enumerate(raw):
        d = len(y) / sr
        reps = int(round(ref / d)) if d > 0 else 1
        if reps >= 2 and abs(reps * d - ref) / ref < 0.01:
            y = np.tile(y, (reps, 1))
            log(f"  {os.path.basename(p)}: {d:.3f} s → répété ×{reps}")
        elif abs(d - ref) * sr > 2:
            log(f"  {os.path.basename(p)}: {d:.4f} s → ajusté à {ref:.4f} s")
        loops.append(Loop(index=i + 1, source=os.path.basename(p), start_sample=0,
                          start_measure=float("nan"), raw=y, sr_in=sr))
        louds.append(pyln.Meter(sr).integrated_loudness(y))
        feats.append(segment_features(y, sr))
        progress(0.05 + 0.5 * (i + 1) / len(raw))

    n_max = _slots(s, len(loops))
    louds = np.array(louds)
    D = distance_matrix(feats, louds, s.weights)
    if len(loops) > n_max or s.exclude_similar or s.min_dist is not None:
        thr = similarity_threshold(D, s)
        picked, reason = select(np.arange(len(loops)), D, louds, 0, n_max, thr, s.loud_floor)
        dropped = sorted(set(range(len(loops))) - {i for i, _ in picked})
        log(f"{len(picked)} fichiers retenus sur {len(loops)} — arrêt : {reason}")
        for i in dropped:
            if not (np.isfinite(louds[i]) and louds[i] > s.loud_floor):
                why = "trop faible"
            else:
                j = min((k for k, _ in picked), key=lambda k: D[i, k])
                med = float(np.median(D[~np.eye(len(D), dtype=bool)]))
                why = (f"ressemble à {loops[j].source} "
                       f"({100 * max(0, 1 - D[i, j] / med):.0f} %)")
            log(f"  écarté : {loops[i].source} — {why}")
    else:
        picked = [(i, np.nan) for i in range(len(loops))]
    sims = _similarities(D, [i for i, _ in picked])
    chosen = []
    for order, ((i, dm), sim) in enumerate(zip(picked, sims), 1):
        loops[i].distinct, loops[i].pick_order, loops[i].similarity = float(dm), order, sim
        chosen.append(loops[i])
    info = dict(mode="merge", loop_seconds=length_out / s.sample_rate, loop_samples=length_out)
    return chosen, info


# --------------------------------------------------------------------------
# Descripteurs de classement
# --------------------------------------------------------------------------

def _z(v):
    v = np.asarray(v, float)
    sd = v.std()
    return (v - v.mean()) / sd if sd > 1e-12 else np.zeros_like(v)


def compute_features(loops: List[Loop], sr: int, progress: Progress):
    meter = pyln.Meter(sr)
    for i, L in enumerate(loops):
        x = L.audio
        mono = x.mean(axis=1).astype(np.float32)
        f = {}
        try:
            lufs = meter.integrated_loudness(x)
        except Exception:
            lufs = -np.inf
        f["loudness"] = float(lufs) if np.isfinite(lufs) else -100.0
        rms = np.sqrt(np.mean(x ** 2))
        f["energy"] = float(20 * np.log10(rms + 1e-12))
        if rms < 1e-7:
            f.update(brightness=0.0, harmonicity=0.0, onset_rate=0.0,
                     impulsivity=0.0, percussive_ratio=0.0, fluctuation=0.0)
        else:
            S = np.abs(librosa.stft(mono, n_fft=2048, hop_length=512))
            f["brightness"] = float(np.average(
                librosa.feature.spectral_centroid(S=S, sr=sr)[0], weights=S.sum(axis=0) + 1e-12))
            H, P = librosa.decompose.hpss(S)
            eh, ep = float((H ** 2).sum()), float((P ** 2).sum())
            flat = float(np.median(librosa.feature.spectral_flatness(S=S)))
            f["harmonicity"] = float(eh / (eh + ep + 1e-12) * (1 - flat))
            env = librosa.onset.onset_strength(S=librosa.amplitude_to_db(S, ref=np.max), sr=sr)
            onsets = librosa.onset.onset_detect(onset_envelope=env, sr=sr, units="time")
            f["onset_rate"] = float(len(onsets) / (len(mono) / sr))
            f["impulsivity"] = float(np.percentile(env, 95) / (np.median(env) + 1e-6))
            f["percussive_ratio"] = float(ep / (eh + ep + 1e-12))
            frame_rms = librosa.feature.rms(y=mono, frame_length=1024, hop_length=256)[0]
            f["fluctuation"] = float(np.std(frame_rms) / (np.mean(frame_rms) + 1e-12))
        f["position"] = float(L.start_sample if np.isfinite(L.start_measure) else L.index)
        f["distinctiveness"] = float(L.pick_order)
        L.features = f
        progress((i + 1) / len(loops))
    # activité rythmique = moyenne des 4 composantes centrées-réduites
    comp = [_z([L.features[k] for L in loops])
            for k in ("onset_rate", "impulsivity", "percussive_ratio", "fluctuation")]
    for L, a in zip(loops, np.mean(comp, axis=0)):
        L.features["activity"] = float(a)


# --------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------

FEATURE_KEYS = ["loudness", "energy", "brightness", "harmonicity", "activity",
                "onset_rate", "impulsivity", "percussive_ratio", "fluctuation"]


def write_output(ranked: List[Loop], info: dict, src_paths: Sequence[str],
                 s: Settings, log: Logger) -> dict:
    src0 = os.path.abspath(src_paths[0])
    if len(src_paths) == 1:
        stem = os.path.splitext(os.path.basename(src0))[0]
    else:
        stem = os.path.basename(os.path.dirname(src0)) or "merge"
    out_dir = os.path.expanduser(s.output_dir or os.path.join(os.path.dirname(src0), stem + "_Loops"))
    os.makedirs(out_dir, exist_ok=True)

    cpl = s.channels_per_loop
    slots = _slots(s, len(ranked))
    if s.n_tracks > 0 and s.n_tracks % cpl:
        log(f"Remarque : {s.n_tracks} pistes n'est pas un multiple de {cpl} — {slots} boucles")
    if len(ranked) < slots:
        hint = (" (relevez le seuil de ressemblance, ou décochez « exclure les boucles "
                "trop ressemblantes », pour en obtenir plus)"
                if s.exclude_similar or s.min_dist is not None
                else " (le morceau ne contient pas plus de boucles complètes)")
        log(f"Seulement {len(ranked)} boucles pour {slots} emplacements : "
            f"les {slots - len(ranked)} derniers seront silencieux{hint}")
    n_ch = slots * cpl
    length = info["loop_samples"]
    data = np.zeros((length, n_ch), dtype=np.float32)
    for i, L in enumerate(ranked):
        data[:, i * cpl:(i + 1) * cpl] = L.audio

    subtype = SUBTYPES[s.subtype_label]
    peak = float(np.abs(data).max()) if data.size else 0.0
    if peak > 1.0 and subtype != "FLOAT":
        log(f"Attention : crête à {20 * np.log10(peak):+.2f} dBFS — écrêtage en "
            f"format entier. Utilisez 32 bit float ou baissez le niveau source.")

    sens = "up" if s.ascending else "down"
    mes = f"{s.measures_per_loop}mes_" if info["mode"] == "slice" else ""
    name = f"{stem}_{mes}{len(ranked)}boucles_{n_ch}ch_{s.descriptor}_{sens}"
    ext = ".wav" if s.file_format == "WAV" else ".aif"
    audio_path = os.path.join(out_dir, name + ext)
    fmt = s.file_format
    bytes_per = {"PCM_16": 2, "PCM_24": 3, "PCM_32": 4, "FLOAT": 4}[subtype]
    if fmt == "WAV" and length * n_ch * bytes_per > 4_000_000_000:
        fmt = "RF64"
        log("Fichier > 4 Go : écriture au format RF64")
    sf.write(audio_path, data, s.sample_rate, subtype=subtype, format=fmt)

    # un fichier par boucle (optionnel), nommé comme dans loop_slicer.py
    loop_names = []
    if s.export_individual:
        d = os.path.join(out_dir, name)
        os.makedirs(d, exist_ok=True)
        for k, L in enumerate(ranked, 1):
            tag = f"mes{int(L.start_measure):03d}" if np.isfinite(L.start_measure) \
                else os.path.splitext(L.source)[0]
            nm = f"{k:02d}_{L.features['loudness']:+.1f}LUFS_{tag}{ext}"
            sf.write(os.path.join(d, nm), L.audio.astype(np.float32), s.sample_rate,
                     subtype=subtype, format=s.file_format)
            loop_names.append(nm)
        log(f"  + {len(ranked)} boucles séparées dans {name}/")

    csv_path = os.path.join(out_dir, name + "_index.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rang", "canaux", "source", "mesure_debut", "debut_s",
                    "debut_echantillon", "distinctivite", "ressemblance_max_%", "ordre_selection",
                    *FEATURE_KEYS]
                   + (["fichier"] if loop_names else []))
        for i, L in enumerate(ranked):
            c0 = i * cpl + 1
            chans = f"{c0}-{c0 + cpl - 1}" if cpl > 1 else str(c0)
            w.writerow([i + 1, chans, L.source,
                        int(L.start_measure) if np.isfinite(L.start_measure) else "",
                        f"{L.start_sample / L.sr_in:.4f}", L.start_sample,
                        "" if not np.isfinite(L.distinct) else round(L.distinct, 2),
                        "" if not np.isfinite(L.similarity) else round(L.similarity),
                        L.pick_order,
                        *[f"{L.features.get(k, float('nan')):.4f}" for k in FEATURE_KEYS]]
                       + ([loop_names[i]] if loop_names else []))

    meta = dict(source=[os.path.abspath(p) for p in src_paths], **info,
                settings={k: v for k, v in asdict(s).items() if k != "output_dir"},
                channels=n_ch, loops=len(ranked), sample_rate=s.sample_rate,
                subtype=subtype, peak_dbfs=round(20 * np.log10(peak + 1e-12), 2))
    with open(os.path.join(out_dir, name + "_analyse.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False, default=float)

    log(f"✔ {os.path.basename(audio_path)} — {n_ch} canaux, "
        f"{length / s.sample_rate:.4f} s, {s.sample_rate} Hz, {s.subtype_label}")
    log(f"  Dossier : {out_dir}")
    return dict(audio=audio_path, csv=csv_path, channels=n_ch, out_dir=out_dir)


def run(paths: Sequence[str], s: Settings,
        log: Logger = print, progress: Progress = lambda p: None) -> dict:
    """Point d'entrée unique utilisé par l'interface et la ligne de commande."""
    paths = list(paths)
    if not paths:
        raise ValueError("Aucun fichier audio")
    progress(0.02)
    if len(paths) == 1:
        loops, info = slice_file(paths[0], s, log, progress)
    else:
        loops, info = merge_files(paths, s, log, progress)
    if not loops:
        raise ValueError("Aucune boucle exploitable (toutes trop faibles ?)")
    for L in loops:
        L.audio = _to_output(L.raw, L.sr_in, info["loop_samples"], s)
    progress(0.6)
    log("Calcul des descripteurs de classement…")
    compute_features(loops, s.sample_rate, lambda p: progress(0.6 + 0.3 * p))
    ranked = sorted(loops, key=lambda L: L.features[s.descriptor], reverse=not s.ascending)
    log(f"Classement : {DESCRIPTORS[s.descriptor][0]}, "
        f"{'croissant (up)' if s.ascending else 'décroissant (down)'}")
    result = write_output(ranked, info, paths, s, log)
    progress(1.0)
    return result
