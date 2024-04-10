# Getting Started

This tutorial covers: 
- [Getting Started](#getting-started)
  - [Installation with pip](#installation-with-pip)
  - [Installation from source](#installation-from-source)
  - [Getting started wtih bluesky-pods](#getting-started-wtih-bluesky-pods)


## Installation with pip

To install bluesky-adaptive with pip, we suggest setting up a new environment with venv. 

```bash
python3 -m venv adaptive-env
source adaptive-env/bin/activate
pip install bluesky-adaptive
```

The `pip` line can be modified to include extra dependencies, such as those for the agents packaged with `bluesky-adaptive`. 
Valid commands include:

- `pip install "bluesky-adaptive[agents]"` to install all agents
- `pip install "bluesky-adaptive[dev]"` to install development dependencies
- `pip install "bluesky-adaptive[all]"` to install all of the above


## Installation from source

To install an editable installation for local development:

```bash
git clone https://github.com/bluesky/bluesky-adaptive
cd bluesky-adaptive
pip install -e ".[dev]"
```


## Getting started wtih bluesky-pods
To develop agents against the full Bluesky stack, we suggest getting started using [bluesky-pods](https://github.com/bluesky/bluesky-pods).
These use podman to run the full Bluesky stack in containers in a common pod.

```bash
git clone (https://github.com/bluesky/bluesky-pods)
cd bluesky-pods/compose/acq-pod
podman-compose --in-pod true up -d
```

To get a bluesky terminal in this pod run:

```bash
bash launch_bluesky.sh
```

From here, we will have an ipython session where we can import bluesky-adaptive and start developing agents.
