"""Interface graphique de MultiLoop Slicer (PySide6)."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback

from PySide6.QtCore import Qt, QObject, QThread, Signal, QSettings
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton,
    QLineEdit, QFileDialog, QPlainTextEdit, QProgressBar, QMessageBox,
)

from . import engine
from .engine import Settings, DESCRIPTORS, SAMPLE_RATES, SUBTYPES, FORMATS

AUDIO_EXT = (".wav", ".wave", ".aif", ".aiff", ".flac", ".mp3")


class DropZone(QLabel):
    filesDropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(110)
        self.setWordWrap(True)
        self._idle_style = ("QLabel{border:2px dashed #888;border-radius:10px;"
                            "padding:12px;font-size:14px;}")
        self.setStyleSheet(self._idle_style)
        self.setText("Glissez-déposez ici un fichier audio (wav, aiff, mp3)\n"
                     "— ou plusieurs fichiers-boucles à assembler —\n\n"
                     "ou cliquez pour parcourir")

    def mousePressEvent(self, e):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choisir un ou plusieurs fichiers audio", "",
            "Audio (*.wav *.wave *.aif *.aiff *.flac *.mp3)")
        if files:
            self.filesDropped.emit(sorted(files))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setStyleSheet(self._idle_style.replace("#888", "#3a7bd5"))

    def dragLeaveEvent(self, e):
        self.setStyleSheet(self._idle_style)

    def dropEvent(self, e):
        self.setStyleSheet(self._idle_style)
        files = []
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isdir(p):  # un dossier → tous ses fichiers audio
                files += [os.path.join(p, f) for f in os.listdir(p)
                          if f.lower().endswith(AUDIO_EXT) and not f.startswith(".")]
            elif p.lower().endswith(AUDIO_EXT):
                files.append(p)
        if files:
            self.filesDropped.emit(sorted(files))


class Worker(QObject):
    log = Signal(str)
    progress = Signal(float)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, files, settings):
        super().__init__()
        self.files, self.settings = files, settings

    def run(self):
        try:
            res = engine.run(self.files, self.settings, self.log.emit, self.progress.emit)
            self.done.emit(res)
        except Exception as exc:
            self.log.emit(traceback.format_exc())
            self.failed.emit(str(exc))


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MultiLoop Slicer")
        self.setMinimumWidth(560)
        self.files: list[str] = []
        self.prefs = QSettings("ENSCi-CRD", "MultiLoopSlicer")
        self.thread = None

        root = QVBoxLayout(self)

        # --- entrée ---------------------------------------------------------
        self.drop = DropZone()
        self.drop.filesDropped.connect(self.set_files)
        root.addWidget(self.drop)

        # --- découpage ------------------------------------------------------
        g_cut = QGroupBox("Découpage")
        f = QFormLayout(g_cut)
        self.measures = QComboBox()
        self.measures.addItems(["1", "2", "4", "8", "16"])
        self.measures.setCurrentText("2")
        f.addRow("Mesures par boucle", self.measures)
        self.beats = QSpinBox(); self.beats.setRange(1, 16); self.beats.setValue(4)
        f.addRow("Temps par mesure", self.beats)
        row = QHBoxLayout()
        self.bpm_auto = QCheckBox("auto"); self.bpm_auto.setChecked(True)
        self.bpm = QDoubleSpinBox(); self.bpm.setRange(20, 400); self.bpm.setDecimals(2)
        self.bpm.setValue(120); self.bpm.setEnabled(False)
        self.bpm_auto.toggled.connect(lambda on: self.bpm.setEnabled(not on))
        row.addWidget(self.bpm_auto); row.addWidget(self.bpm); row.addStretch()
        f.addRow("Tempo (BPM)", row)
        root.addWidget(g_cut)

        # --- classement -----------------------------------------------------
        g_ord = QGroupBox("Classement")
        f = QFormLayout(g_ord)
        self.descriptor = QComboBox()
        for key, (label, tip) in DESCRIPTORS.items():
            self.descriptor.addItem(label, key)
            self.descriptor.setItemData(self.descriptor.count() - 1, tip, Qt.ToolTipRole)
        f.addRow("Descripteur", self.descriptor)
        self.order = QComboBox()
        self.order.addItem("↑ Up — du plus faible au plus fort", True)
        self.order.addItem("↓ Down — du plus fort au plus faible", False)
        f.addRow("Sens", self.order)
        sim_row = QHBoxLayout()
        self.excl = QCheckBox("Exclure les boucles trop ressemblantes")
        self.excl.setChecked(True)
        self.excl.setToolTip("Quand le morceau offre plus de boucles possibles que nécessaire, "
                             "ou contient des passages répétés, on ne garde qu'une boucle par "
                             "famille de boucles qui se ressemblent.")
        self.sim = QSpinBox(); self.sim.setRange(30, 99); self.sim.setValue(80)
        self.sim.setSuffix(" %"); self.sim.setPrefix("au-delà de ")
        self.sim.setToolTip("Ressemblance (timbre, harmonie, texture, sonie) au-delà de laquelle "
                            "une boucle est écartée. 100 % = identiques. Plus bas = plus sévère, "
                            "donc moins de boucles.")
        self.excl.toggled.connect(self.sim.setEnabled)
        sim_row.addWidget(self.excl); sim_row.addWidget(self.sim); sim_row.addStretch()
        f.addRow("Sélection", sim_row)
        root.addWidget(g_ord)

        # --- sortie ---------------------------------------------------------
        g_out = QGroupBox("Fichier de sortie")
        f = QFormLayout(g_out)
        self.tracks = QSpinBox(); self.tracks.setRange(0, 1024); self.tracks.setValue(64)
        self.tracks.setSpecialValueText("auto (toutes les boucles)")
        self.tracks.setSingleStep(2)
        f.addRow("Nombre de pistes", self.tracks)
        self.chans = QComboBox()
        self.chans.addItem("Stéréo (2 pistes par boucle)", 2)
        self.chans.addItem("Mono (1 piste par boucle)", 1)
        f.addRow("Boucles", self.chans)
        fmt_row = QHBoxLayout()
        self.fmt = QComboBox(); self.fmt.addItems(list(FORMATS))
        self.sr = QComboBox(); self.sr.addItems([str(r) for r in SAMPLE_RATES])
        self.sr.setCurrentText("48000")
        self.depth = QComboBox(); self.depth.addItems(list(SUBTYPES))
        self.depth.setCurrentText("32 bit float")
        for w in (self.fmt, self.sr, QLabel("Hz"), self.depth):
            fmt_row.addWidget(w)
        f.addRow("Format", fmt_row)
        dir_row = QHBoxLayout()
        self.out_dir = QLineEdit()
        self.out_dir.setPlaceholderText("<nom du fichier>_Loops, à côté du fichier source")
        b = QPushButton("Choisir…"); b.clicked.connect(self.choose_dir)
        dir_row.addWidget(self.out_dir); dir_row.addWidget(b)
        f.addRow("Dossier", dir_row)
        root.addWidget(g_out)

        # --- réglages avancés (repliables) ---------------------------------
        self.adv = QGroupBox("Réglages avancés")
        self.adv.setCheckable(True)
        self.adv.setChecked(False)
        f = QFormLayout()
        self.phrase = QSpinBox(); self.phrase.setRange(1, 16); self.phrase.setValue(2)
        self.phrase.setToolTip("Les boucles ne démarrent que toutes les N mesures")
        f.addRow("Grille de phrase (mesures)", self.phrase)
        self.db_shift = QSpinBox(); self.db_shift.setRange(-15, 15)
        self.db_shift.setToolTip("Si la boucle ne démarre pas sur le premier temps")
        f.addRow("Correction du 1er temps (temps)", self.db_shift)
        self.ph_shift = QSpinBox(); self.ph_shift.setRange(-15, 15)
        f.addRow("Correction de phrase (mesures)", self.ph_shift)
        self.offset = QDoubleSpinBox(); self.offset.setRange(-500, 500); self.offset.setSuffix(" ms")
        self.offset.setToolTip("Décale toute la grille (si les attaques sont coupées ou en retard)")
        f.addRow("Décalage fin de la grille", self.offset)
        self.preroll = QDoubleSpinBox(); self.preroll.setRange(0, 200); self.preroll.setSuffix(" ms")
        f.addRow("Pré-roll", self.preroll)
        self.fade = QDoubleSpinBox(); self.fade.setRange(0, 50); self.fade.setValue(1); self.fade.setSuffix(" ms")
        f.addRow("Micro-fondus anti-clic", self.fade)
        self.floor = QDoubleSpinBox(); self.floor.setRange(-90, 0); self.floor.setValue(-45)
        self.floor.setSuffix(" LUFS")
        f.addRow("Ignorer en dessous de", self.floor)
        self.individual = QCheckBox("Exporter aussi chaque boucle dans un fichier séparé")
        f.addRow(self.individual)
        inner = QWidget(); inner.setLayout(f)
        lay = QVBoxLayout(self.adv); lay.addWidget(inner)
        inner.setVisible(False)
        self.adv.toggled.connect(inner.setVisible)
        root.addWidget(self.adv)

        # --- action ---------------------------------------------------------
        self.go = QPushButton("Générer le fichier multicanal")
        self.go.setMinimumHeight(36)
        self.go.setEnabled(False)
        self.go.clicked.connect(self.start)
        root.addWidget(self.go)
        self.bar = QProgressBar(); self.bar.setRange(0, 100)
        root.addWidget(self.bar)
        self.logbox = QPlainTextEdit(); self.logbox.setReadOnly(True)
        self.logbox.setMinimumHeight(140)
        root.addWidget(self.logbox)

        self.restore_prefs()

    # ------------------------------------------------------------------
    PREF_WIDGETS = ("measures", "descriptor", "order", "chans", "fmt", "sr", "depth")

    def restore_prefs(self):
        p = self.prefs
        for name in self.PREF_WIDGETS:
            v = p.value(name)
            if v is not None:
                getattr(self, name).setCurrentIndex(int(v))
        self.beats.setValue(int(p.value("beats", 4)))
        self.tracks.setValue(int(p.value("tracks", 64)))
        self.out_dir.setText(p.value("out_dir", ""))
        self.phrase.setValue(int(p.value("phrase", 2)))
        self.excl.setChecked(p.value("exclude_similar", "true") in (True, "true"))
        self.sim.setValue(int(p.value("similarity", 80)))
        self.floor.setValue(float(p.value("floor", -45)))
        self.individual.setChecked(p.value("individual", "false") in (True, "true"))

    def save_prefs(self):
        p = self.prefs
        for name in self.PREF_WIDGETS:
            p.setValue(name, getattr(self, name).currentIndex())
        p.setValue("beats", self.beats.value())
        p.setValue("tracks", self.tracks.value())
        p.setValue("out_dir", self.out_dir.text())
        p.setValue("phrase", self.phrase.value())
        p.setValue("exclude_similar", self.excl.isChecked())
        p.setValue("similarity", self.sim.value())
        p.setValue("floor", self.floor.value())
        p.setValue("individual", self.individual.isChecked())

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Dossier de sortie", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def set_files(self, files):
        self.files = files
        if len(files) == 1:
            txt = f"🎵 {os.path.basename(files[0])}\n\nMode : découpage en boucles"
        else:
            txt = (f"🎵 {len(files)} fichiers\n({os.path.basename(files[0])} … "
                   f"{os.path.basename(files[-1])})\n\nMode : assemblage (1 fichier = 1 boucle)")
        self.drop.setText(txt + "\n\n(cliquez ou déposez pour changer)")
        self.go.setEnabled(True)

    def log(self, msg):
        self.logbox.appendPlainText(msg)

    def settings(self) -> Settings:
        return Settings(
            measures_per_loop=int(self.measures.currentText()),
            beats_per_measure=self.beats.value(),
            bpm=None if self.bpm_auto.isChecked() else self.bpm.value(),
            descriptor=self.descriptor.currentData(),
            ascending=self.order.currentData(),
            n_tracks=self.tracks.value(),
            channels_per_loop=self.chans.currentData(),
            file_format=self.fmt.currentText(),
            sample_rate=int(self.sr.currentText()),
            subtype_label=self.depth.currentText(),
            output_dir=self.out_dir.text().strip() or None,
            phrase_bars=self.phrase.value(),
            downbeat_shift=self.db_shift.value(),
            phrase_shift=self.ph_shift.value(),
            beat_offset_ms=self.offset.value(),
            preroll_ms=self.preroll.value(),
            fade_ms=self.fade.value(),
            exclude_similar=self.excl.isChecked(),
            similarity_max=float(self.sim.value()),
            loud_floor=self.floor.value(),
            export_individual=self.individual.isChecked(),
        )

    def start(self):
        self.save_prefs()
        self.go.setEnabled(False)
        self.bar.setValue(0)
        self.logbox.clear()
        self.thread = QThread()
        self.worker = Worker(self.files, self.settings())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(lambda p: self.bar.setValue(int(p * 100)))
        self.worker.done.connect(self.finished)
        self.worker.failed.connect(self.failed)
        self.worker.done.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.start()

    def finished(self, res):
        self.go.setEnabled(True)
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", res["audio"]])  # montre le fichier dans le Finder

    def failed(self, msg):
        self.go.setEnabled(True)
        QMessageBox.warning(self, "MultiLoop Slicer", f"Erreur : {msg}")


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
