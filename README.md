# MultiLoop Slicer

Small standalone application for **interactive sound design**: it cuts an audio piece into loops of equal length aligned to the measure grid, **sorts them by a sound descriptor** (loudness, energy, brightness, harmonicity, rhythmic activity…) and writes **a single multichannel file** (loop 1 → channels 1-2, loop 2 → channels 3-4, …).

The file can then be played with the Max for Live device **`MultiChannelLoop-Browser_03.amxd`** (in the `max/` folder), which lets you move continuously from one loop to the next, for example from calmest to most active, according to a control parameter (gesture, sensor, interaction…).

---

## User guide

### 1. Install (once)

You need **Python 3.10 or later** ([python.org](https://www.python.org/downloads/)) and an internet connection the first time.

1. Download this repository (green **Code → Download ZIP** button) and unzip it.
2. Double-click **`MultiLoop Slicer.command`**.
   - The first time, macOS may block the file: **right-click → Open**, then confirm.
   - The first launch installs what is needed (a few minutes). Later launches are immediate.

> Without the launcher (Linux, Windows or Terminal):
> ```bash
> python3 -m venv .venv
> .venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
> .venv/bin/python -m loopslicer
> ```

### 2. Use

1. **Drag and drop** an audio file (wav, aiff, flac, mp3) onto the dotted area, or click it to browse.
   - **One file** → *slice* mode: the piece is cut into loops.
   - **Several files, or a folder** → *merge* mode: each file is one loop (for example bounces from Logic or Live).
2. **Slicing**: number of **measures per loop** (2 by default), **beats per measure** (4), **tempo** detected automatically, or entered by hand if detection goes wrong.
3. **Sorting**: choose the **descriptor** and the **direction**:
   - **↑ Up**: from the weakest value to the strongest (channels 1-2 = the softest, calmest, darkest… loop),
   - **↓ Down**: the reverse.
4. **Output file**:
   - **Number of tracks** = number of channels in the file (64 by default, so 32 stereo loops). With *auto*, all loops are kept.
   - **Loops**: stereo (2 tracks per loop) or mono (1 track).
   - **Format**: WAV or AIFF, sample rate, bit depth. **Default: WAV 48 kHz 32-bit float.**
   - **Folder**: where to write the files (by default, next to the source file). The application remembers your settings from one session to the next.
5. Click **Generate**. At the end, the file is shown in the Finder.

### 3. What you get

For a source `piece.wav` sorted by activity, in rising order, on 64 tracks:

| File | Contents |
|---|---|
| `piece_64ch_activity_up.wav` | the multichannel file to load into the Max for Live device |
| `piece_64ch_activity_up_index.csv` | one row per loop: rank, channels, original position, starting measure, exact sample in the source, values of every descriptor |
| `piece_64ch_activity_up_meta.json` | detected tempo, loop length, settings used |

The CSV opens in Excel / Numbers. It lets you find which passage of the piece each pair of channels comes from.

### 4. Play with Ableton Live

1. Put **`max/MultiChannelLoop-Browser_03.amxd`** on an audio track in Live.
2. Load the generated multichannel file into it.
3. Since the channels are sorted by descriptor, browsing the loops with the device means travelling through the sound along that descriptor: from the softest to the loudest, from the calmest to the most active, etc.

<!-- TODO: describe the device's controls here (parameters, MIDI / OSC mapping). -->

---

## Descriptors

| Descriptor | What it measures | Method |
|---|---|---|
| **Loudness (LUFS)** | perceived loudness | integrated loudness, ITU-R BS.1770 / EBU R128 (`pyloudnorm`) |
| **Energy (RMS dB)** | raw signal energy, without perceptual weighting | RMS level in dBFS |
| **Brightness** | bright / dark timbre | spectral centroid weighted by energy (Hz) |
| **Harmonicity** | tonal content vs. noise and percussion | harmonic share after harmonic/percussive separation (HPSS), × (1 − spectral flatness) |
| **Rhythmic activity** | calm ↔ busy, impulsive | average of 4 standardized components: attack density (onsets/s), impulsivity (peaks / median of the attack envelope), percussive share (HPSS), envelope fluctuation (std/mean of the RMS) |
| **Original position** | — | chronological order in the source file |

The **rhythmic activity** index is relative: it is computed with respect to the set of loops in the file (each component is centred and scaled). The detailed components are in the CSV, so you can build your own sort.

---

## Method

**Slice mode (one file)**

1. **Tempo**: beat detection (`librosa`), then a linear regression over all detected beats → one fixed tempo for the whole piece (rounded when it is very close to a whole number, the typical case for a DAW export).
2. **Grid phase**: the onset times are folded modulo one beat, which gives a sharp peak, accurate to about a millisecond.
3. **Downbeat**: among the 4 possible positions, the one that maximizes the low-frequency attacks (kick, bass) is kept.
4. **Slicing**: consecutive loops of N measures, non-overlapping, starting at the first complete measure. The start of each loop is computed from the theoretical grid (no drift), and **all loops have exactly the same number of samples**.
5. **Conversion** to the requested format (high-quality *soxr* resampling), mono/stereo, **1 ms fades** at both ends to avoid clicks.
6. Loops that are almost silent (< −60 LUFS) are ignored.

**Merge mode (several files)**

- The reference length is the median of the lengths: files that differ by a few samples (typical of bounces) are padded or trimmed; a file **exactly half as long** is played twice (loop doubled).

**Sorting and selection**

- The loops are sorted by the descriptor. If there are **more loops than slots** (e.g. 40 loops for 64 tracks = 32 slots), the selection is **spread evenly across the whole range of the descriptor**, from the weakest to the strongest. If there are fewer, the last channels stay silent.

---

## Limitations

- The piece must have a **fixed tempo** (DAW export, programmed music). Rubato, accelerando or live music without a click will give an approximate grid: enter the tempo by hand, or prepare the loops in a DAW and use *merge* mode.
- The downbeat is guessed from the low-frequency attacks: on music without a kick or bass, check the first loop by listening (the starting measure is in the CSV).
- WAV is limited to 4 GB: beyond that, the file is written as RF64 automatically.
- In 16 / 24 / 32-bit integer formats, a level above 0 dBFS clips (a warning is shown). The 32-bit float default avoids this problem.

---

## Command line

The same engine can be scripted, for example to process a batch of files:

```bash
.venv/bin/python -m loopslicer piece.wav -m 2 -d activity --order up -t 64
.venv/bin/python -m loopslicer bounces/*.wav -d loudness --order down --mono
.venv/bin/python -m loopslicer --help
```

## Build a macOS application (.app)

```bash
./build_app.sh        # → dist/MultiLoop Slicer.app
```

The `.app` contains Python and all dependencies (about 300-500 MB). It can be distributed without installing anything, but since it is not signed, colleagues have to open it the first time with **right-click → Open**.

## Contents of the repository

```
loopslicer/engine.py   analysis, slicing, descriptors, export
loopslicer/gui.py      graphical interface (PySide6)
loopslicer/cli.py      command line
max/                   Max for Live device MultiChannelLoop-Browser_03.amxd
MultiLoop Slicer.command   macOS launcher (double-click)
build_app.sh           builds the .app with PyInstaller
```

## Credits

Roland Cahen — ENSCi Les Ateliers, CRD, as part of Louise Grebel's thesis.
