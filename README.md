# Bluesky Adaptive

[![Build Status]][Actions] [![PyPI Version]][PyPI] [![License Badge]][License]

**Bluesky Adaptive** is a component of the Bluesky ecosystem designed to introduce adaptivity and intelligent decision-making into experimental workflows.  
It provides a flexible API that supports a wide range of adaptive algorithms, from simple rule-based adjustments to sophisticated artificial intelligence and machine learning models.  
The package is designed to provide a framework and set of harnesses for "bring-your-own-agent" development.  
It has no opinions on the algorithms that underpin your adaptivity, and it is designed to be agnostic to the specifics of the domain in which it is used.

| Resource        | Link                      |
| --- | --- |
| **PyPI**        | `pip install bluesky-adaptive` |
| **Source Code** | [GitHub][]                |
| **Docs**        | [Documentation][]         |

## Features

- Adaptive harness for lockstep agents to interact with the RunEngine.
- Base classes for developing distributed agents to interact with the RunEngine Manager through Queue Server.
- Example agents using optional requirements (BoTorch, GPyTorch, Scikit Learn).
- Server for running managed agents with a FAST API.
- Adjudicators for acting as meta-agents that consume suggestions from many agents and gatekeep the Queue Server.

## Getting Started

To get started with Bluesky-Adaptive, refer to the [Getting Started Tutorial][Tutorial].  
This tutorial will guide you through the installation process, setting up the Bluesky stack, and running your first adaptive experiment.

## Usage

Bluesky Adaptive is designed to be integrated into your experimental workflows to provide adaptive capabilities.  
It offers two primary approaches:

- **Lockstep Approach**: For synchronous operations where each experimental step waits for the previous one to complete.
- **Asynchronous Approach**: Allows for independent operation from the main execution thread, suitable for complex setups.

Detailed guides on how to extend your existing Python tools for use with Bluesky Adaptive and how to run your agent as a service can be found in the [documentation][].

## Development Environment

To set up a development environment for Bluesky Adaptive, you can use the provided containerized setup with `bluesky-pods`.
A minimal compose file is available in `bluesky_adaptive/tests/podman/docker-compose.yaml` to run the unit tests.
For more complete integration testing, you can use the `bluesky-pods` repository, which provides a full stack including the RunEngine, Queue Server, and Tiled server.

```bash
podman pull ghcr.io/bluesky/bluesky-pods-bluesky:latest
podman tag ghcr.io/bluesky/bluesky-pods-bluesky:latest bluesky:latest
git clone (https://github.com/bluesky/bluesky-pods)
cd bluesky-pods/compose/acq-pod
podman-compose --in-pod true up -d
```

## Contributing

We welcome contributions from the community, whether it's adding new features, improving documentation, or reporting bugs.  
Please see our [contribution guidelines][] for more information on how to get involved.

## License

Bluesky-Adaptive is distributed under the BSD-3-Clause license.  
See the [LICENSE file][] for more details.

---

[Build Status]: https://github.com/bluesky/bluesky-adaptive/actions/workflows/tests.yml/badge.svg
[Actions]: https://github.com/bluesky/bluesky-adaptive/actions
[PyPI Version]: https://img.shields.io/pypi/v/bluesky-adaptive.svg
[PyPI]: https://pypi.python.org/pypi/bluesky-adaptive
[License Badge]: https://img.shields.io/badge/License-BSD%203--Clause-blue.svg
[License]: https://opensource.org/licenses/BSD-3-Clause

[GitHub]: https://github.com/bluesky/bluesky-adaptive
[Documentation]: https://blueskyproject.io/bluesky-adaptive
[Tutorial]: https://blueskyproject.io/bluesky-adaptive/tutorials/getting-started
[contribution guidelines]: https://github.com/bluesky/bluesky-adaptive/blob/main/CONTRIBUTING.rst
[LICENSE file]: https://github.com/bluesky/bluesky-adaptive/blob/main/LICENSE
