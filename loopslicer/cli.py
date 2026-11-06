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
    ap.add_argument("-t", "--tracks", type=int, default=0,
                    help="nombre de pistes du fichier de sortie (0 = auto)")
    ap.add_argument("--mono", action="store_true", help="1 piste par boucle au lieu de 2")
    ap.add_argument("--format", choices=["WAV", "AIFF"], default="WAV")
    ap.add_argument("--sr", type=int, default=48000)
    ap.add_argument("--depth", choices=list(SUBTYPES), default="32 bit float")
    ap.add_argument("-o", "--out", default=None, help="dossier de sortie")
    a = ap.parse_args()
    s = Settings(measures_per_loop=a.measures, beats_per_measure=a.beats, bpm=a.bpm,
                 descriptor=a.descriptor, ascending=a.order == "up", n_tracks=a.tracks,
                 channels_per_loop=1 if a.mono else 2, file_format=a.format,
                 sample_rate=a.sr, subtype_label=a.depth, output_dir=a.out)
    run(a.inputs, s)


if __name__ == "__main__":
    main()
