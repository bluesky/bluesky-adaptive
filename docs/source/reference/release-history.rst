===============
Release History
===============

v0.4.0 (2026-10-20)
-------------------
+ Final 0.x release; starting with 1.0, bluesky-adaptive adopts the blop codebase and continues
  its development under the bluesky-adaptive name. The 1.0+ API differs from 0.x.
+ Add the Tsuchinoko agent and ``tsuchinoko`` extra (#49, #57).
+ Maintain pyproject.toml and ruff configuration (#53).
+ Update tests for the new bluesky-pods (#52).
+ Add a documentation publishing workflow (#59).
+ Fix isolated sdist/wheel builds using setuptools_scm and generated package versions.
+ Announce end of life in the README and documentation, with a ``FutureWarning`` on import.

v0.3.2 (2025-06-12)
-------------------
+ Update the Databroker development dependency (#51).
+ Fix caproto, Databroker, and Kafka container test configuration in CI (#50).
+ Clarify the grammar of agent and lockstep APIs (#48).
+ Remove the strict Databroker dependency for typing (#47).
+ Repair test and documentation CI workflows (#45).
+ Reduce pod and communication dependencies in tests with offline helpers (#43).
+ Refresh Python and Tiled dependencies and CI configuration (#40).

v0.3.1 (2023-04-26)
-------------------
+ MANIFEST updates for packaging and documentation

v0.3.0 (2023-04-26)
-------------------
+ Add distributed agents: base classes, simple, sklearn, and gpytorch agents
+ First implementation of a server and FAST API
+ Add adjudicators (meta-agents)
+ Updates to development workflow in-line with scientific cookiecutter

v0.2.1 (2021-10-26)
-------------------
+ updates to documentation

v0.2.0 (2021-04-20)
-------------------
+ add on_stop factory
+ add scipy optimizer

v0.1.0 (2021-03-18)
-------------------
initial release
