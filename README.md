# Bluesky Adaptive

[![Testing](https://github.com/bluesky/bluesky-adaptive/actions/workflows/ci.yml/badge.svg)](https://github.com/bluesky/bluesky-adaptive/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/bluesky/bluesky-adaptive/branch/main/graph/badge.svg)](https://codecov.io/gh/bluesky/bluesky-adaptive)
[![PyPI](https://img.shields.io/pypi/v/bluesky-adaptive.svg)](https://pypi.python.org/pypi/bluesky-adaptive)
[![Conda](https://img.shields.io/conda/vn/conda-forge/bluesky-adaptive.svg)](https://anaconda.org/conda-forge/bluesky-adaptive)

- Free software: 3-clause BSD license
- Documentation: <https://blueskyproject.io/bluesky-adaptive>.

Bluesky Adaptive was formerly developed as Blop ([github.com/bluesky/blop](https://github.com/bluesky/blop)).
The 0.x series of `bluesky-adaptive` is discontinued; to keep using its APIs, pin `bluesky-adaptive<1`.

## Installation

```bash
pip install bluesky-adaptive
```

or with conda:

```bash
conda install -c conda-forge bluesky-adaptive
```

## Citation

If you use this package in your work, please cite the following paper:

```bibtex
@Article{Morris2024,
  author   = {Morris, Thomas W. and Rakitin, Max and Du, Yonghua and Fedurin, Mikhail and Giles, Abigail C. and Leshchev, Denis and Li, William H. and Romasky, Brianna and Stavitski, Eli and Walter, Andrew L. and Moeller, Paul and Nash, Boaz and Islegen-Wojdyla, Antoine},
  journal  = {Journal of Synchrotron Radiation},
  title    = {{A general Bayesian algorithm for the autonomous alignment of beamlines}},
  year     = {2024},
  month    = {Nov},
  number   = {6},
  pages    = {1446--1456},
  volume   = {31},
  doi      = {10.1107/S1600577524008993},
  keywords = {Bayesian optimization, automated alignment, synchrotron radiation, digital twins, machine learning},
  url      = {https://doi.org/10.1107/S1600577524008993},
}
```
