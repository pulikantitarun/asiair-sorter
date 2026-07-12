# ASIAIR Session Sorter

A Windows desktop app that organises your **ZWO ASIAIR** SD card data into a clean, date-and-target folder structure — with a built-in **PHD2 guiding log viewer** showing RA/Dec error graphs and session statistics.

![ASIAIR Session Sorter](https://raw.githubusercontent.com/pulikantitarun/asiair-sorter/main/docs/screenshot.png)

---

## Features

- **One-click sorting** — picks up the Asiair's own folder structure (Light/Dark/Flat/Bias, filter subfolders, target names) so nothing gets misclassified
- **JPG preview cleanup** — deletes Asiair's preview JPEGs from the SD card without copying them to your archive
- **PHD2 log viewer** — load any PHD2 guide log and see interactive RA/Dec error graphs, star SNR, dither markers, and per-session RMS/peak-error stats
- **Move or copy** — copy keeps the SD card intact; move clears it out
- **Dry run** — preview every action before touching a single file
- **Dark themed UI** — built with customtkinter
- **Remembers** your last-used folders

### Output structure

```
[Destination]/
  Sessions/
    2026-07-11/
      PHD2_Logs/
      M31/
        Lights/
          Ha/
          OIII/
        Darks/
        Flats/
      NGC7000/
        Lights/
          SII/
```

---

## Installation

### Pre-built Windows installer (recommended)

Download `ASIAIR_Sorter_Setup_v*.exe` from the [Releases](https://github.com/pulikantitarun/asiair-sorter/releases) page and run it. No Python required.

### Run from source

Requires Python 3.8+.

```bash
pip install customtkinter matplotlib numpy
python asiair_sorter.py
```

### Build the exe yourself

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --collect-all customtkinter ^
            --collect-all matplotlib --hidden-import numpy ^
            --icon asiair_sorter.ico --name ASIAIR_Sorter ^
            asiair_sorter.py
```

To rebuild the Windows installer, open `installer/asiair_sorter_setup.iss` in [Inno Setup 6](https://jrsoftware.org/isinfo.php).

---

## Usage

1. Click **Browse…** next to *SD Card / Asiair Folder* and select your SD card root (e.g. `E:\ASIAIR`)
2. Click **Browse…** next to *Main Storage Folder* and select your archive destination
3. Optionally tick **Dry Run** to preview without touching files
4. Click **Copy Files** or **Move Files**
5. Switch to the **PHD2 Viewer** tab to load and graph any PHD2 log file

---

## Customising folder names

Near the top of `asiair_sorter.py` there is a `CONSTANTS` block. Edit these to rename output folders:

```python
SESSIONS_ROOT = "Sessions"
LIGHTS_FOLDER = "Lights"
DARKS_FOLDER  = "Darks"
FLATS_FOLDER  = "Flats"
BIAS_FOLDER   = "Bias"
PHD2_FOLDER   = "PHD2_Logs"
KNOWN_FILTERS = ["Ha", "OIII", "SII", "Hb", "Lum", "R", "G", "B", "L", "RGB"]
```

---

## Contributing

Contributions are very welcome! Ideas for future features:

- macOS / Linux support
- ASTAP plate-solve integration to auto-detect targets
- Session statistics export (CSV / HTML report)
- Support for other guide software logs (NINA, PHD1)
- Automatic SD card detection

### How to contribute

1. Fork the repo and create a feature branch
2. Make your changes, test against a real Asiair SD card dump if possible
3. Open a pull request with a clear description of what you changed and why

Please keep the single-file design (`asiair_sorter.py`) for ease of distribution, unless a change genuinely requires splitting.

---

## License

**GNU Affero General Public License v3.0** — see [LICENSE](LICENSE).

In short: you are free to use, modify, and distribute this software, but any modified version you make available (including as a hosted service) must also be released under the AGPL with source code available.
