#!/usr/bin/env python3
"""
loop_slicer.py — découpe un morceau en boucles synchrones de N mesures,
choisit les plus différenciées et les classe par loudness.

Toutes les boucles d'un même jeu ont exactement la même longueur en
échantillons et commencent sur un premier temps de la même grille de
phrases : elles peuvent être permutées ou mixées à tout moment.

Usage :
    python loop_slicer.py morceau.wav
    python loop_slicer.py morceau.wav --bars 2 4 --max 32 --out ~/Desktop/Boucles
    python loop_slicer.py morceau.wav --bpm 110 --downbeat-shift 1

Voir README.md pour la méthode et les paramètres.

Librairies nécessaires :
    numpy
    scipy
    librosa
    soundfile
    pyloudnorm

"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf
import librosa
import pyloudnorm as pyln


# ---------------------------------------------------------------- lecture

def load_audio(path):
    """Renvoie (signal float64 stéréo, sr). Décode via ffmpeg si libsndfile
    ne sait pas lire le format (m4a, aac…)."""
    try:
        sf.info(path)
    except RuntimeError:
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False).name
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', path,
                        '-c:a', 'pcm_f32le', tmp], check=True)
        path = tmp
    y, sr = sf.read(path, dtype='float64', always_2d=True)
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    return y[:, :2], sr


# ---------------------------------------------------------------- tempo

def grid_score(oenv, period, phases, t_from, t_to):
    """Moyenne de l'enveloppe d'onsets sur une grille (période, phase) en trames."""
    n0 = np.ceil((t_from - phases) / period).clip(0)
    k = np.arange(int((t_to - t_from) / period) + 2)
    idx = phases[:, None] + (n0[:, None] + k[None, :]) * period
    idx = np.where(idx < t_to, idx, np.nan)
    vals = np.where(np.isnan(idx), np.nan, oenv[np.nan_to_num(idx).round().astype(int).clip(0, len(oenv) - 1)])
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
    return bpm, beat0, drift


# ---------------------------------------------------------------- mesures

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
    fi = np.searchsorted(tt, bt)

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
    db_scores = [z(nov)[beat_idx % beats_per_bar == p].mean() + 0.5 * z(kick)[beat_idx % beats_per_bar == p].mean()
                 for p in range(beats_per_bar)]
    db = int(np.argmax(db_scores))

    bar_beats = np.arange(db, nb - 1, beats_per_bar)
    bar_nov = nov[bar_beats]
    # les plus grands changements de section (moyenne des 3 plus forts) fixent le début de phrase
    ph_scores = [np.sort(bar_nov[np.arange(len(bar_nov)) % phrase_bars == p])[-3:].mean() for p in range(phrase_bars)]
    pb = int(np.argmax(ph_scores))
    return db, pb, [round(float(s), 3) for s in db_scores], [round(float(s), 3) for s in ph_scores]


# ---------------------------------------------------------------- sélection

def segment_features(seg, sr):
    m = seg.mean(1)
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
    side = np.sqrt(((seg[:, 0] - seg[:, 1]) ** 2).mean() / (((seg[:, 0] + seg[:, 1]) ** 2).mean() + 1e-12) + 1e-12)
    return dict(timbre=np.r_[mf[1:].mean(1), mf[1:].std(1)],
                harm=ch / (ch.sum() + 1e-9),
                tex=np.r_[con.mean(1), bands, on.mean(), on.std(), side])


def select(cands, feats, louds, nbars, n_max, min_dist, loud_floor, weights):
    def Z(k):
        X = np.array([f[k] for f in feats])
        X = (X - X.mean(0)) / (X.std(0) + 1e-9)
        return X / np.sqrt(X.shape[1])
    L = np.where(np.isfinite(louds), louds, -70.0)[:, None]
    blocks = [Z('timbre') * weights[0], Z('harm') * weights[1], Z('tex') * weights[2],
              (L - L.mean()) / (L.std() + 1e-9) * weights[3]]
    X = np.hstack(blocks)
    D = np.linalg.norm(X[:, None] - X[None], axis=2)
    bars = np.array([c[0] for c in cands])
    valid = np.isfinite(louds) & (louds > loud_floor)
    if not valid.any():
        return []
    first = int(np.argmax(np.where(valid, D.sum(1), -1)))
    sel, dmins = [first], [np.inf]
    while len(sel) < n_max:
        best, bi = -1, None
        for i in range(len(cands)):
            if not valid[i] or i in sel or any(abs(bars[i] - bars[j]) < nbars for j in sel):
                continue
            d = D[i, sel].min()
            if d > best:
                best, bi = d, i
        if bi is None or best < min_dist:
            break
        sel.append(bi)
        dmins.append(best)
    return list(zip(sel, dmins))


# ---------------------------------------------------------------- export

def export_set(y, sr, T0, BAR, nbars, phrase_bars, args, out_dir, stem):
    L = int(round(nbars * BAR * sr))
    pre = int(round(args.preroll_ms / 1000 * sr))
    cands, n = [], 0
    while True:
        s = int(round((T0 + n * BAR) * sr)) - pre
        if s + L > len(y):
            break
        if s >= 0 and n % phrase_bars == 0:
            cands.append((n, s))
        n += 1
    if not cands:
        print(f'  {nbars} mesures : morceau trop court'); return

    meter = pyln.Meter(sr)
    feats, louds = [], []
    for _, s in cands:
        seg = y[s:s + L]
        louds.append(meter.integrated_loudness(seg))
        feats.append(segment_features(seg, sr))
    louds = np.array(louds)

    picked = select(cands, feats, louds, nbars, args.max, args.min_dist, args.loud_floor, args.weights)
    picked.sort(key=lambda p: louds[p[0]])                      # du plus doux au plus fort

    d = os.path.join(out_dir, f'{nbars}_mesures')
    os.makedirs(d, exist_ok=True)
    F = int(round(args.fade_ms / 1000 * sr))
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, F)) if F > 1 else None
    rows = ['rang,fichier,mesure_debut,debut_s,debut_echantillon,longueur_echantillons,LUFS,distinctivite']
    stack, names = [], []
    for k, (i, dm) in enumerate(picked, 1):
        bar, s = cands[i]
        seg = y[s:s + L].copy()
        if ramp is not None:
            seg[:F] *= ramp[:, None]
            seg[-F:] *= ramp[::-1, None]
        seg = seg.astype(np.float32)
        name = f'{k:02d}_{louds[i]:+.1f}LUFS_mes{bar + 1:03d}.wav'
        sf.write(os.path.join(d, name), seg, sr, subtype=args.subtype)
        rows.append(f'{k},{name},{bar + 1},{s / sr:.4f},{s},{L},{louds[i]:.1f},{"" if np.isinf(dm) else round(dm, 2)}')
        stack.append(seg); names.append(name)
    with open(os.path.join(d, 'index.csv'), 'w') as f:
        f.write('\n'.join(rows) + '\n')

    if not args.no_multichannel and stack:
        mc = os.path.join(out_dir, f'{stem}_{nbars}mesures_{len(stack)}boucles_{2 * len(stack)}canaux.wav')
        sf.write(mc, np.hstack(stack), sr, subtype=args.subtype, format='WAV')
        with open(mc[:-4] + '_pistes.txt', 'w') as f:
            for j, nm in enumerate(names):
                f.write(f'canaux {2 * j + 1:2d}-{2 * j + 2:2d} : {nm}\n')
    print(f'  {nbars} mesures : {len(stack)} boucles de {L} échantillons ({L / sr:.4f} s) -> {d}')


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description='Découpe un morceau en boucles synchrones classées par loudness.')
    ap.add_argument('input')
    ap.add_argument('--out', help='dossier de sortie (défaut : <morceau>_Loops à côté du fichier)')
    ap.add_argument('--bars', type=int, nargs='+', default=[2, 4], help='longueur(s) de boucle en mesures')
    ap.add_argument('--max', type=int, default=32, help='nombre maximum de boucles par jeu')
    ap.add_argument('--min-dist', type=float, default=0.5, help='distinctivité minimale pour ajouter une boucle')
    ap.add_argument('--loud-floor', type=float, default=-45, help='ignore les segments sous ce niveau (LUFS)')
    ap.add_argument('--weights', type=float, nargs=4, default=[1, 1, 1, 0.7],
                    metavar=('TIMBRE', 'HARMONIE', 'TEXTURE', 'LOUDNESS'), help='poids des familles de descripteurs')
    ap.add_argument('--bpm', type=float, help='tempo imposé (sinon détecté)')
    ap.add_argument('--no-snap', action='store_true', help="ne pas arrondir le tempo détecté au BPM entier")
    ap.add_argument('--beats-per-bar', type=int, default=4)
    ap.add_argument('--phrase-bars', type=int, default=2, help='les boucles démarrent tous les N mesures (grille de phrase)')
    ap.add_argument('--downbeat-shift', type=int, default=0, help='correction manuelle du premier temps (en temps)')
    ap.add_argument('--phrase-shift', type=int, default=0, help='correction manuelle de la grille de phrase (en mesures)')
    ap.add_argument('--beat-offset-ms', type=float, default=0, help='décalage fin de toute la grille (ms)')
    ap.add_argument('--preroll-ms', type=float, default=0, help='démarre chaque boucle un peu avant le temps (ms)')
    ap.add_argument('--fade-ms', type=float, default=1, help='micro-fondus anti-clic (0 = coupe franche)')
    ap.add_argument('--no-multichannel', action='store_true', help='ne pas créer les fichiers multicanaux')
    ap.add_argument('--subtype', default='FLOAT', choices=['FLOAT', 'PCM_24', 'PCM_32', 'PCM_16'],
                    help='format des WAV exportés (défaut : 32 bits flottant)')
    args = ap.parse_args()

    stem = os.path.splitext(os.path.basename(args.input))[0]
    out_dir = os.path.expanduser(args.out or os.path.join(os.path.dirname(os.path.abspath(args.input)), stem + '_Loops'))
    os.makedirs(out_dir, exist_ok=True)

    print(f'Lecture de {args.input}')
    y, sr = load_audio(args.input)
    mono = y.mean(1).astype(np.float32)

    print('Analyse du tempo…')
    bpm, beat0, drift = detect_tempo(mono, sr, bpm_hint=args.bpm, snap=not args.no_snap, fixed=args.bpm is not None)
    beat0 += args.beat_offset_ms / 1000
    B = 60 / bpm
    BAR = args.beats_per_bar * B
    print(f'  tempo {bpm:.3f} BPM, premier temps détecté à {beat0:.4f} s')
    print(f'  écart de la grille par fenêtres de 15 s (ms) : {drift}')
    if drift and max(abs(d) for d in drift[1:-1] or drift) > 25:
        print('  ATTENTION : écarts importants, le tempo n\'est peut-être pas constant.')

    print('Recherche du premier temps de mesure…')
    db, pb, db_scores, ph_scores = detect_downbeat(mono, sr, bpm, beat0, args.beats_per_bar, args.phrase_bars)
    db = (db + args.downbeat_shift) % args.beats_per_bar
    pb = (pb + args.phrase_shift) % args.phrase_bars
    span = args.phrase_bars * BAR
    T0 = (beat0 + db * B + pb * BAR) % span
    print(f'  scores par temps {db_scores} -> temps {db}; scores de phrase {ph_scores} -> mesure {pb}')
    print(f'  mesure 1 à {T0:.4f} s, une mesure = {BAR:.4f} s')

    for nb in args.bars:
        export_set(y, sr, T0, BAR, nb, args.phrase_bars, args, out_dir, stem)

    with open(os.path.join(out_dir, 'analyse.json'), 'w') as f:
        json.dump(dict(source=os.path.abspath(args.input), sr=sr, bpm=bpm, beat0_s=beat0,
                       downbeat_beat=db, phrase_bar=pb, bar1_s=T0, bar_s=BAR, drift_ms=drift,
                       args=vars(args)), f, indent=2, ensure_ascii=False)
    print(f'Terminé : {out_dir}')


if __name__ == '__main__':
    main()
