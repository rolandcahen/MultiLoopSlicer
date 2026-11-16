"""Ligne de commande : python -m loopslicer fichier.wav [options]"""

import argparse

from .engine import Settings, DESCRIPTORS, SUBTYPES, run


def main():
    ap = argparse.ArgumentParser(
        prog="python -m loopslicer",
        description="Découpe un fichier audio en boucles (ou assemble plusieurs "
                    "fichiers-boucles), les classe selon un descripteur et écrit "
                    "un fichier multicanal.")
    ap.add_argument("inputs", nargs="+", help="1 fichier = découpage ; plusieurs = assemblage")
    ap.add_argument("-m", "--measures", type=int, default=2, help="mesures par boucle (2)")
    ap.add_argument("-b", "--beats", type=int, default=4, help="temps par mesure (4)")
    ap.add_argument("--bpm", type=float, default=None, help="tempo imposé (sinon auto)")
    ap.add_argument("-d", "--descriptor", choices=list(DESCRIPTORS), default="loudness")
    ap.add_argument("--order", choices=["up", "down"], default="up")
    ap.add_argument("-t", "--tracks", type=int, default=64,
                    help="nombre de pistes du fichier de sortie (64 ; 0 = auto)")
    ap.add_argument("--mono", action="store_true", help="1 piste par boucle au lieu de 2")
    ap.add_argument("--format", choices=["WAV", "AIFF"], default="WAV")
    ap.add_argument("--sr", type=int, default=48000)
    ap.add_argument("--depth", choices=list(SUBTYPES), default="32 bit float")
    ap.add_argument("-o", "--out", default=None, help="dossier de sortie (défaut : <fichier>_Loops)")
    g = ap.add_argument_group("réglages avancés (cf. loop_slicer.py)")
    g.add_argument("--phrase-bars", type=int, default=2, help="les boucles démarrent toutes les N mesures")
    g.add_argument("--downbeat-shift", type=int, default=0, help="correction du premier temps (en temps)")
    g.add_argument("--phrase-shift", type=int, default=0, help="correction de la grille de phrase (en mesures)")
    g.add_argument("--beat-offset-ms", type=float, default=0, help="décalage fin de toute la grille (ms)")
    g.add_argument("--preroll-ms", type=float, default=0)
    g.add_argument("--fade-ms", type=float, default=1, help="micro-fondus anti-clic (0 = coupe franche)")
    ap.add_argument("--similarity", type=float, default=80,
                    help="écarte les boucles ressemblant à plus de N %% à une boucle retenue (80)")
    ap.add_argument("--keep-similar", action="store_true",
                    help="ne pas exclure les boucles trop ressemblantes")
    g.add_argument("--min-dist", type=float, default=None,
                   help="seuil de distance absolu de loop_slicer.py (remplace --similarity)")
    g.add_argument("--loud-floor", type=float, default=-45, help="ignore les segments sous ce niveau (LUFS)")
    g.add_argument("--weights", type=float, nargs=4, default=[1, 1, 1, 0.7],
                   metavar=("TIMBRE", "HARMONIE", "TEXTURE", "LOUDNESS"))
    g.add_argument("--no-snap", action="store_true", help="ne pas arrondir le tempo au BPM entier")
    g.add_argument("--individual", action="store_true", help="exporte aussi chaque boucle séparément")
    a = ap.parse_args()
    s = Settings(measures_per_loop=a.measures, beats_per_measure=a.beats, bpm=a.bpm,
                 descriptor=a.descriptor, ascending=a.order == "up", n_tracks=a.tracks,
                 channels_per_loop=1 if a.mono else 2, file_format=a.format,
                 sample_rate=a.sr, subtype_label=a.depth, output_dir=a.out,
                 phrase_bars=a.phrase_bars, downbeat_shift=a.downbeat_shift,
                 phrase_shift=a.phrase_shift, beat_offset_ms=a.beat_offset_ms,
                 preroll_ms=a.preroll_ms, fade_ms=a.fade_ms, min_dist=a.min_dist,
                 exclude_similar=not a.keep_similar, similarity_max=a.similarity,
                 loud_floor=a.loud_floor, weights=tuple(a.weights), snap_bpm=not a.no_snap,
                 export_individual=a.individual)
    run(a.inputs, s)


if __name__ == "__main__":
    main()
