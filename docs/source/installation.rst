Installation
============

For users
---------

Installation
^^^^^^^^^^^^

The package works with Python 3.11+ and can be installed from both PyPI and/or conda-forge.

The PyPI package is modular: ``pip install bluesky-adaptive`` installs only the core plans and their dependencies
(``bluesky``, ``networkx``, ``numpy``, ``rich``) and no optimizer backend. Importing a backend such as
``bluesky_adaptive.ax`` without its extra raises an ``ImportError`` that names the extra to install.
To install the package with every backend using the ``pip`` package manager, run the following command:

.. include:: _includes/installation-code-snippets.rst
   :start-after: .. snippet-pip-standard-start
   :end-before: .. snippet-pip-standard-end

To install the package using the ``conda`` package manager, run the following command:

.. include:: _includes/installation-code-snippets.rst
   :start-after: .. snippet-conda-standard-start
   :end-before: .. snippet-conda-standard-end

The conda-forge package includes the ``ax``, ``xopt``, ``scipy``, and ``queueserver`` dependencies.
For ``TiledRunPoller``, also install ``bluesky-tiled-plugins`` from conda-forge.

Optional Extras
^^^^^^^^^^^^^^^

Install only what you need by appending one or more extras to the ``pip`` requirement, for example
``pip install "bluesky-adaptive[ax,tiled]"``. Quote the requirement so that shells such as ``zsh`` do not
expand the brackets.

.. list-table::
   :header-rows: 1
   :widths: auto

   * - Extra
     - Installs
     - Notes
   * - ``bluesky-adaptive``
     - ``bluesky``, ``networkx``, ``numpy``, ``rich``
     - Core plans only; no optimizer backend
   * - ``bluesky-adaptive[ax]``
     - ``ax-platform``, ``botorch``, ``gpytorch``, ``torch``
     - Installs PyTorch; see below for CPU-only
   * - ``bluesky-adaptive[xopt]``
     - ``xopt`` (which requires ``botorch``, ``gpytorch``, ``torch``)
     - Installs PyTorch; see below for CPU-only
   * - ``bluesky-adaptive[scipy]``
     - ``scipy``
     - Needed by ``bluesky_adaptive.scipy``
   * - ``bluesky-adaptive[queueserver]``
     - ``bluesky-queueserver-api``
     - Transport layer only; pair with ``[ax]`` for ``QueueserverAgent``
   * - ``bluesky-adaptive[tiled]``
     - ``bluesky-tiled-plugins``
     - Client only; needed by ``TiledRunPoller``
   * - ``bluesky-adaptive[all]``
     - All backends + ``[queueserver]`` + ``[tiled]``
     - No dev tooling; will grow as new backends are added
   * - ``bluesky-adaptive[dev]``
     - ``bluesky-adaptive[all]`` + dev tooling
     - For contributors

The ``cpu`` extra in ``pyproject.toml`` is for development of this repository only: it routes ``torch`` to the
CPU-only PyTorch index through ``[tool.uv.sources]``, which ``uv`` and ``pixi`` honour only when installing from a
checkout. It has no effect when installing from PyPI.

PyTorch Acceleration Options
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The ``[ax]``, ``[xopt]``, and ``[all]`` extras install PyTorch. On Linux, the ``torch`` wheels on PyPI are built
with CUDA and pull in several gigabytes of NVIDIA libraries. For environments without a GPU, or to reduce the
installation size, install a CPU-only PyTorch build from the PyTorch index first; the second command then
keeps it:

.. include:: _includes/installation-code-snippets.rst
   :start-after: .. snippet-pip-cpu-start
   :end-before: .. snippet-pip-cpu-end

With `uv <https://docs.astral.sh/uv/>`_, a single command selects the CPU-only PyTorch index:

.. include:: _includes/installation-code-snippets.rst
   :start-after: .. snippet-uv-cpu-start
   :end-before: .. snippet-uv-cpu-end

This is particularly useful for:

- Containerized deployments without GPU access
- CI/CD pipelines
- Development environments on laptops without NVIDIA GPUs
- Edge computing scenarios

For conda users who want CPU-only PyTorch from conda-forge:

.. include:: _includes/installation-code-snippets.rst
   :start-after: .. snippet-conda-cpu-start
   :end-before: .. snippet-conda-cpu-end
