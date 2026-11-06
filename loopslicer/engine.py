"""
Moteur de MultiLoop Slicer.

Deux modes d'entrée :
  * « slice »  : un seul fichier audio à tempo fixe est découpé en boucles de
                 N mesures, calées sur la grille des mesures, toutes de durée
                 strictement identique (au sample près).
  * « merge »  : plusieurs fichiers (une boucle par fichier) sont mis à la même
                 longueur et rassemblés.

Les boucles sont ensuite classées selon un descripteur (sens croissant ou
décroissant) et écrites dans UN fichier multicanal : boucle 1 → canaux 1-2,
boucle 2 → canaux 3-4, etc. (ou 1 canal par boucle en mode mono).
"""

from __future__ import annotations

import csv
import json
import os
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
    measures_per_loop: int = 2
    beats_per_measure: int = 4
    bpm: Optional[float] = None          # None = détection automatique
    descriptor: str = "loudness"
    ascending: bool = True               # True = up (du plus faible au plus fort)
    n_tracks: int = 0                    # canaux du fichier de sortie ; 0 = auto
    channels_per_loop: int = 2           # 2 = stéréo, 1 = mono
    file_format: str = "WAV"
    sample_rate: int = 48000
    subtype_label: str = "32 bit float"
    fade_ms: float = 1.0
    skip_silent_db: float = -60.0        # boucles plus faibles ignorées (LUFS)
    output_dir: Optional[str] = None     # None = dossier du premier fichier


@dataclass
class Loop:
    index: int                 # position d'origine (1 = première)
    source: str                # nom du fichier source
    start_sample: int          # début dans le fichier source (sr d'origine)
    start_measure: float       # n° de mesure (1 = première mesure complète)
    audio: np.ndarray = field(repr=False)   # (n_samples, channels) au sr de sortie
    features: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------

def load_audio(path: str):
    """Lit wav / aiff / flac / mp3. Renvoie (signal (n, ch) float64, sr)."""
    try:
        y, sr = sf.read(path, dtype="float64", always_2d=True)
    except Exception:
        # repli : librosa / audioread (mp3 avec un vieux libsndfile, m4a…)
        y, sr = librosa.load(path, sr=None, mono=False)
        y = np.atleast_2d(y).T.astype(np.float64)
    return y, int(sr)


# --------------------------------------------------------------------------
# Tempo et grille de mesures
# --------------------------------------------------------------------------

def _refine_phase(mono: np.ndarray, sr: int, beat_dur: float, coarse: float) -> float:
    """Affine la phase de la grille à ~1 ms près.

    On calcule la montée d'énergie (dérivée positive du log-énergie, fenêtres
    courtes) et on la « replie » modulo la durée d'un temps : les attaques
    alignées sur la grille s'additionnent et forment un pic net. On cherche ce
    pic au voisinage (±40 ms) de l'estimation grossière.
    """
    hop, win = 32, 128
    n = (len(mono) - win) // hop
    if n < 10:
        return coarse
    frames = np.lib.stride_tricks.sliding_window_view(mono, win)[::hop][:n]
    e = np.log10((frames.astype(np.float64) ** 2).mean(axis=1) + 1e-10)
    d = np.maximum(0.0, np.diff(e))
    t = (np.arange(len(d)) * hop + win) / sr       # fin de fenêtre ≈ début de l'attaque
    nbins = max(8, int(beat_dur * sr / hop))
    hist = np.bincount(((t % beat_dur) / beat_dur * nbins).astype(int) % nbins,
                       weights=d, minlength=nbins)
    hist = np.convolve(np.r_[hist[-2:], hist, hist[:2]], np.ones(3) / 3, "same")[2:-2]
    centre = int(round(coarse / beat_dur * nbins)) % nbins
    span = int(0.04 / beat_dur * nbins)
    cand = (centre + np.arange(-span, span + 1)) % nbins
    best = cand[np.argmax(hist[cand])]
    return (best + 0.5) / nbins * beat_dur - win / sr / 2


def analyse_grid(y: np.ndarray, sr: int, beats_per_measure: int,
                 bpm: Optional[float], log: Logger):
    """Estime un tempo fixe et la position de la première mesure.

    Renvoie (bpm, t0) où t0 est l'instant (s) du premier temps fort.
    La grille est supposée strictement régulière (tempo fixe, type DAW/bounce).
    """
    mono = y.mean(axis=1).astype(np.float32)
    hop = 256
    onset_env = librosa.onset.onset_strength(y=mono, sr=sr, hop_length=hop)

    tempo_est, beats = librosa.beat.beat_track(
        onset_envelope=onset_env, sr=sr, hop_length=hop,
        start_bpm=bpm or 120.0, units="time", tightness=400)
    tempo_est = float(np.atleast_1d(tempo_est)[0])

    if bpm is None:
        if len(beats) >= 8:
            # régression linéaire sur les temps détectés : tempo fixe le plus
            # cohérent avec l'ensemble du morceau
            k = np.arange(len(beats))
            slope, _ = np.polyfit(k, beats, 1)
            bpm = 60.0 / slope
        else:
            bpm = tempo_est
        # arrondi au 1/100 de BPM si on en est très proche (tempos de DAW)
        if abs(bpm - round(bpm, 2)) < 0.005:
            bpm = round(bpm, 2)
        if abs(bpm - round(bpm)) < 0.05:
            bpm = float(round(bpm))
        log(f"Tempo détecté : {bpm:.2f} BPM")
    else:
        log(f"Tempo imposé : {bpm:.2f} BPM")

    beat_dur = 60.0 / bpm
    # phase de la grille des temps : moyenne circulaire des temps détectés
    if len(beats):
        phases = (beats % beat_dur) / beat_dur * 2 * np.pi
        phase = (np.angle(np.mean(np.exp(1j * phases))) % (2 * np.pi)) / (2 * np.pi) * beat_dur
    else:
        phase = 0.0
    phase = _refine_phase(mono, sr, beat_dur, phase)

    # quel temps de la mesure est le temps fort ? on cherche le décalage qui
    # maximise l'énergie des attaques dans le grave (grosse caisse, basse)
    S = np.abs(librosa.stft(mono, n_fft=2048, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low = librosa.onset.onset_strength(S=librosa.amplitude_to_db(S[freqs < 200]), sr=sr, hop_length=hop)
    full = onset_env / (onset_env.max() + 1e-12)
    low = low / (low.max() + 1e-12)
    strength = 0.6 * low + 0.4 * full[: len(low)]
    times = librosa.frames_to_time(np.arange(len(strength)), sr=sr, hop_length=hop)
    duration = len(mono) / sr

    best, best_score = 0, -np.inf
    for off in range(beats_per_measure):
        t = phase + off * beat_dur
        bar_times = np.arange(t, duration, beat_dur * beats_per_measure)
        idx = np.searchsorted(times, bar_times).clip(0, len(strength) - 1)
        score = strength[idx].mean() if len(idx) else -np.inf
        if score > best_score:
            best, best_score = off, score
    t0 = phase + best * beat_dur
    measure_dur = beat_dur * beats_per_measure
    # première mesure complète : on recule tant qu'on reste ≥ 0
    t0 = t0 % measure_dur
    log(f"Première mesure à {t0 * 1000:.1f} ms (mesure = {measure_dur:.4f} s)")
    return bpm, t0


# --------------------------------------------------------------------------
# Découpe / assemblage
# --------------------------------------------------------------------------

def _apply_fades(x: np.ndarray, sr: int, fade_ms: float):
    n = int(round(sr * fade_ms / 1000.0))
    if n < 1 or 2 * n >= len(x):
        return x
    ramp = np.linspace(0.0, 1.0, n, endpoint=False)[:, None]
    x = x.copy()
    x[:n] *= ramp
    x[-n:] *= ramp[::-1]
    return x


def _match_channels(x: np.ndarray, ch: int):
    if x.shape[1] == ch:
        return x
    if ch == 1:
        return x.mean(axis=1, keepdims=True)
    if x.shape[1] == 1:
        return np.repeat(x, ch, axis=1)
    return x[:, :ch]  # plus de 2 canaux source → on garde les 2 premiers


def _to_output(x: np.ndarray, sr_in: int, sr_out: int, length_out: int, s: Settings):
    x = _match_channels(x, s.channels_per_loop)
    if sr_in != sr_out:
        x = librosa.resample(x.T, orig_sr=sr_in, target_sr=sr_out, res_type="soxr_vhq").T
    if len(x) < length_out:
        x = np.vstack([x, np.zeros((length_out - len(x), x.shape[1]))])
    x = x[:length_out]
    return _apply_fades(x, sr_out, s.fade_ms)


def slice_file(path: str, s: Settings, log: Logger) -> tuple[List[Loop], dict]:
    y, sr = load_audio(path)
    log(f"Lecture : {os.path.basename(path)} — {y.shape[1]} canal(aux), {sr} Hz, "
        f"{len(y) / sr:.2f} s")
    bpm, t0 = analyse_grid(y, sr, s.beats_per_measure, s.bpm, log)

    measure_samples = 60.0 / bpm * s.beats_per_measure * sr          # flottant
    loop_samples_in = int(round(measure_samples * s.measures_per_loop))
    loop_seconds = measure_samples * s.measures_per_loop / sr
    loop_samples_out = int(round(loop_seconds * s.sample_rate))

    loops = []
    k = 0
    while True:
        start = int(round(t0 * sr + k * s.measures_per_loop * measure_samples))
        if start + loop_samples_in > len(y):
            break
        seg = y[start:start + loop_samples_in]
        loops.append(Loop(index=k + 1, source=os.path.basename(path),
                          start_sample=start,
                          start_measure=k * s.measures_per_loop + 1,
                          audio=_to_output(seg, sr, s.sample_rate, loop_samples_out, s)))
        k += 1
    log(f"{len(loops)} boucles de {s.measures_per_loop} mesure(s) "
        f"= {loop_seconds:.4f} s ({loop_samples_out} samples à {s.sample_rate} Hz)")
    info = dict(mode="slice", bpm=bpm, first_measure_s=t0,
                loop_seconds=loop_seconds, loop_samples=loop_samples_out)
    return loops, info


def merge_files(paths: Sequence[str], s: Settings, log: Logger) -> tuple[List[Loop], dict]:
    """Une boucle par fichier. Toutes sont ramenées à la longueur la plus
    fréquente (médiane), ce qui corrige les bounces à ±1 sample. Un fichier
    deux fois plus court que la référence est doublé (boucle répétée)."""
    raw = []
    for p in paths:
        y, sr = load_audio(p)
        raw.append((p, y, sr))
    secs = np.array([len(y) / sr for _, y, sr in raw])
    ref = float(np.median(secs))
    length_out = int(round(ref * s.sample_rate))
    log(f"{len(raw)} fichiers — longueur de référence {ref:.4f} s "
        f"({length_out} samples à {s.sample_rate} Hz)")
    loops = []
    for i, (p, y, sr) in enumerate(raw):
        d = len(y) / sr
        reps = int(round(ref / d)) if d > 0 else 1
        if reps >= 2 and abs(reps * d - ref) / ref < 0.01:
            y = np.tile(y, (reps, 1))
            log(f"  {os.path.basename(p)} : {d:.3f} s → répété ×{reps}")
        elif abs(d - ref) * s.sample_rate > 2:
            log(f"  {os.path.basename(p)} : {d:.4f} s → ajusté à {ref:.4f} s")
        loops.append(Loop(index=i + 1, source=os.path.basename(p), start_sample=0,
                          start_measure=float("nan"),
                          audio=_to_output(y, sr, s.sample_rate, length_out, s)))
    info = dict(mode="merge", loop_seconds=length_out / s.sample_rate,
                loop_samples=length_out)
    return loops, info


# --------------------------------------------------------------------------
# Descripteurs
# --------------------------------------------------------------------------

def _z(v):
    v = np.asarray(v, float)
    sd = v.std()
    return (v - v.mean()) / sd if sd > 1e-12 else np.zeros_like(v)


def compute_features(loops: List[Loop], sr: int, log: Logger, progress: Progress):
    meter = pyln.Meter(sr)
    for i, L in enumerate(loops):
        x = L.audio
        mono = x.mean(axis=1).astype(np.float32)
        f = {}
        try:
            lufs = meter.integrated_loudness(x if x.shape[1] <= 5 else mono)
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
                librosa.feature.spectral_centroid(S=S, sr=sr)[0],
                weights=S.sum(axis=0) + 1e-12))
            H, P = librosa.decompose.hpss(S)
            eh, ep = float((H ** 2).sum()), float((P ** 2).sum())
            flat = float(np.median(librosa.feature.spectral_flatness(S=S)))
            # harmonicité : part harmonique, pénalisée par la platitude spectrale
            f["harmonicity"] = float(eh / (eh + ep + 1e-12) * (1 - flat))
            # activité rythmique : 4 composantes
            env = librosa.onset.onset_strength(S=librosa.amplitude_to_db(S, ref=np.max), sr=sr)
            onsets = librosa.onset.onset_detect(onset_envelope=env, sr=sr, units="time")
            dur = len(mono) / sr
            f["onset_rate"] = float(len(onsets) / dur)                      # attaques / s
            f["impulsivity"] = float(np.percentile(env, 95) / (np.median(env) + 1e-6))
            f["percussive_ratio"] = float(ep / (eh + ep + 1e-12))
            frame_rms = librosa.feature.rms(y=mono, frame_length=1024, hop_length=256)[0]
            f["fluctuation"] = float(np.std(frame_rms) / (np.mean(frame_rms) + 1e-12))
        f["position"] = float(L.index)
        L.features = f
        progress((i + 1) / len(loops))

    # indice d'activité = moyenne des 4 composantes centrées-réduites
    comp = [_z([L.features[k] for L in loops])
            for k in ("onset_rate", "impulsivity", "percussive_ratio", "fluctuation")]
    act = np.mean(comp, axis=0)
    for L, a in zip(loops, act):
        L.features["activity"] = float(a)


# --------------------------------------------------------------------------
# Sélection, classement, écriture
# --------------------------------------------------------------------------

def order_and_select(loops: List[Loop], s: Settings, log: Logger) -> List[Loop]:
    kept = [L for L in loops if L.features["loudness"] > s.skip_silent_db]
    if len(kept) < len(loops):
        log(f"{len(loops) - len(kept)} boucle(s) silencieuse(s) ignorée(s) "
            f"(< {s.skip_silent_db:.0f} LUFS)")
    ranked = sorted(kept, key=lambda L: L.features[s.descriptor], reverse=not s.ascending)

    if s.n_tracks > 0:
        slots = s.n_tracks // s.channels_per_loop
        if s.n_tracks % s.channels_per_loop:
            log(f"Attention : {s.n_tracks} pistes n'est pas un multiple de "
                f"{s.channels_per_loop} — {slots} boucles utilisées")
        if len(ranked) > slots:
            # on garde des boucles réparties régulièrement sur tout l'éventail
            # du descripteur (de la plus faible à la plus forte)
            pick = np.unique(np.round(np.linspace(0, len(ranked) - 1, slots)).astype(int))
            log(f"{len(ranked)} boucles disponibles pour {slots} emplacements : "
                f"sélection répartie sur toute l'échelle du descripteur")
            ranked = [ranked[i] for i in pick]
        elif len(ranked) < slots:
            log(f"Seulement {len(ranked)} boucles pour {slots} emplacements : "
                f"les {slots - len(ranked)} derniers seront silencieux")
    return ranked


def write_output(ranked: List[Loop], info: dict, src_paths: Sequence[str],
                 s: Settings, log: Logger) -> dict:
    out_dir = s.output_dir or os.path.dirname(os.path.abspath(src_paths[0]))
    os.makedirs(out_dir, exist_ok=True)

    n_loops = len(ranked)
    slots = (s.n_tracks // s.channels_per_loop) if s.n_tracks > 0 else n_loops
    n_ch = slots * s.channels_per_loop
    length = info["loop_samples"]

    data = np.zeros((length, n_ch), dtype=np.float32)
    for i, L in enumerate(ranked):
        data[:, i * s.channels_per_loop:(i + 1) * s.channels_per_loop] = L.audio

    peak = float(np.abs(data).max()) if data.size else 0.0
    subtype = SUBTYPES[s.subtype_label]
    if peak > 1.0 and subtype != "FLOAT":
        log(f"Attention : crête à {20 * np.log10(peak):+.2f} dBFS — écrêtage en "
            f"format entier. Utilisez 32 bit float ou baissez le niveau source.")

    base = os.path.splitext(os.path.basename(src_paths[0]))[0]
    if len(src_paths) > 1:
        base = os.path.basename(os.path.dirname(os.path.abspath(src_paths[0]))) or "merge"
    sens = "up" if s.ascending else "down"
    name = f"{base}_{n_ch}ch_{s.descriptor}_{sens}"
    ext = ".wav" if s.file_format == "WAV" else ".aif"
    audio_path = os.path.join(out_dir, name + ext)

    fmt = s.file_format
    bytes_per = {"PCM_16": 2, "PCM_24": 3, "PCM_32": 4, "FLOAT": 4}[subtype]
    if fmt == "WAV" and length * n_ch * bytes_per > 4_000_000_000:
        fmt = "RF64"
        log("Fichier > 4 Go : écriture au format RF64")
    sf.write(audio_path, data, s.sample_rate, subtype=subtype, format=fmt)

    # index CSV
    csv_path = os.path.join(out_dir, name + "_index.csv")
    keys = ["loudness", "energy", "brightness", "harmonicity", "activity",
            "onset_rate", "impulsivity", "percussive_ratio", "fluctuation"]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rang", "canaux", "source", "position_origine", "mesure_debut",
                    "sample_debut_source", *keys])
        for i, L in enumerate(ranked):
            c0 = i * s.channels_per_loop + 1
            chans = f"{c0}-{c0 + s.channels_per_loop - 1}" if s.channels_per_loop > 1 else str(c0)
            w.writerow([i + 1, chans, L.source, L.index,
                        "" if np.isnan(L.start_measure) else int(L.start_measure),
                        L.start_sample,
                        *[f"{L.features.get(k, float('nan')):.4f}" for k in keys]])

    meta = dict(source=[os.path.basename(p) for p in src_paths], **info,
                settings={k: v for k, v in asdict(s).items() if k != "output_dir"},
                channels=n_ch, loops=n_loops, sample_rate=s.sample_rate,
                subtype=subtype, peak_dbfs=20 * np.log10(peak + 1e-12))
    with open(os.path.join(out_dir, name + "_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)

    log(f"✔ {os.path.basename(audio_path)} — {n_ch} canaux, "
        f"{length / s.sample_rate:.3f} s, {s.sample_rate} Hz, {s.subtype_label}")
    log(f"  Dossier : {out_dir}")
    return dict(audio=audio_path, csv=csv_path, channels=n_ch)


def run(paths: Sequence[str], s: Settings,
        log: Logger = print, progress: Progress = lambda p: None) -> dict:
    """Point d'entrée unique utilisé par l'interface et la ligne de commande."""
    paths = list(paths)
    if not paths:
        raise ValueError("Aucun fichier audio")
    progress(0.02)
    if len(paths) == 1:
        loops, info = slice_file(paths[0], s, log)
    else:
        loops, info = merge_files(paths, s, log)
    if not loops:
        raise ValueError("Aucune boucle complète : fichier trop court pour "
                         f"{s.measures_per_loop} mesure(s) ?")
    progress(0.3)
    log("Calcul des descripteurs…")
    compute_features(loops, s.sample_rate, log, lambda p: progress(0.3 + 0.6 * p))
    ranked = order_and_select(loops, s, log)
    label = DESCRIPTORS[s.descriptor][0]
    log(f"Classement : {label}, {'croissant (up)' if s.ascending else 'décroissant (down)'}")
    result = write_output(ranked, info, paths, s, log)
    progress(1.0)
    return result
