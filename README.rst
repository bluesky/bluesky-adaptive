================
bluesky-adaptive
================

|build_status| |pypi_version| |license|


Tools for writing adaptive plans

* Free software: 3-clause BSD license
* Documentation: https://bluesky.github.io/bluesky-adaptive

Features
--------

* Adaptive harness for lockstep agents to interact with the RunEngine.
* Base classes for developing distributed agents to interact with the RunEngine Manager through Queue Server.
* Example agents using optional requirements (BoTorch, GPyTorch, Scikit Learn).
* Server for running managed agents with a FAST API.
* Adjudicators for acting as meta-agents that consume suggestions from many agents and gatekeep the Queue Server.

.. |build_status| image:: https://github.com/bluesky/bluesky-adaptive/actions/workflows/tests.yml/badge.svg
    :target: https://github.com/bluesky/bluesky-adaptive/actions
    :alt: Build Status

.. |pypi_version| image:: https://img.shields.io/pypi/v/bluesky-adaptive.svg
        :target: https://pypi.python.org/pypi/bluesky-adaptive
        :alt: Latest PyPI version

.. |license| image:: https://img.shields.io/badge/License-BSD%203--Clause-blue.svg
    :target: https://opensource.org/licenses/BSD-3-Clause
    :alt: BSD 3-Clause License
