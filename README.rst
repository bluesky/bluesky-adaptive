================
Bluesky Adaptive
================

|build_status| |pypi_version| |license|


Bluesky Adaptive is a component of the Bluesky ecosystem designed to introduce adaptivity and intelligent decision-making into experimental workflows.
It provides a flexible API that supports a wide range of adaptive algorithms, from simple rule-based adjustments to sophisticated artificial intelligence and machine learning models.
The package is designed to provide a framework and set of harnesses for "bring-your-own-agent" development.
It has no opinions on the algorithms that underpin your adaptivity, and it is designed to be agnostic to the specifics of the domain in which it is used.


============== ==============================================================
PyPI           ``pip install bluesky-adaptive``
Source code    https://github.com/bluesky/bluesky-adaptive
Documentation  https://blueskyproject.io/bluesky-adaptive
============== ==============================================================

Features
--------

* Adaptive harness for lockstep agents to interact with the RunEngine.
* Base classes for developing distributed agents to interact with the RunEngine Manager through Queue Server.
* Example agents using optional requirements (BoTorch, GPyTorch, Scikit Learn).
* Server for running managed agents with a FAST API.
* Adjudicators for acting as meta-agents that consume suggestions from many agents and gatekeep the Queue Server.

Getting Started
---------------

To get started with Bluesky-Adaptive, refer to the `Getting Started Tutorial <https://blueskyproject.io/bluesky-adaptive/tutorials/getting-started>`_. 
This tutorial will guide you through the installation process, setting up the Bluesky stack, and running your first adaptive experiment.

Usage
-----
Bluesky Adaptive is designed to be integrated into your experimental workflows to provide adaptive capabilities.
It offers two primary approaches:

- **Lockstep Approach**: For synchronous operations where each experimental step waits for the previous one to complete.
- **Asynchronous Approach**: Allows for independent operation from the main execution thread, suitable for complex setups.

Detailed guides on how to extend your existing Python tools for use with Bluesky Adaptive and how to run your agent as a service can be found in the documentation.

Contributing
------------

We welcome contributions from the community, whether it's adding new features, improving documentation, or reporting bugs. 
Please see our `contribution guidelines <https://github.com/bluesky/bluesky-adaptive/blob/main/CONTRIBUTING.rst>`_ for more information on how to get involved.

License
-------

Bluesky-Adaptive is distributed under the BSD-3-Clause license. See the `LICENSE <https://github.com/bluesky/bluesky-adaptive/blob/main/LICENSE>`_  file for more details.


.. |build_status| image:: https://github.com/bluesky/bluesky-adaptive/actions/workflows/tests.yml/badge.svg
    :target: https://github.com/bluesky/bluesky-adaptive/actions
    :alt: Build Status

.. |pypi_version| image:: https://img.shields.io/pypi/v/bluesky-adaptive.svg
        :target: https://pypi.python.org/pypi/bluesky-adaptive
        :alt: Latest PyPI version

.. |license| image:: https://img.shields.io/badge/License-BSD%203--Clause-blue.svg
    :target: https://opensource.org/licenses/BSD-3-Clause
    :alt: BSD 3-Clause License
