.. Packaging Scientific Python documentation master file, created by
   sphinx-quickstart on Thu Jun 28 12:35:56 2018.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

================================
 bluesky-adaptive Documentation
================================

.. toctree::
   :titlesonly:

   installation
   usage
   release-history
   min_versions
   examples

.. warning::

   This is currently under rapid development, the API may change at
   any time.


Adaptive-ness in plans
======================

Fixed plans (ex `~bluesky.plans.count` or `~bluesky.plans.scan`) are
sufficient for many science use-cases, but in some cases having the
plan respond (adapt) to the data as it is being taken can provide
significant improvements to the quality of the data collected while
simultaneously reducing the collection time.  The feedback between the data
and the plan can be at many levels of fidelity:

- detecting if the data looks "bad" (the sample fell out of the beam)
  and stopping acquisition
- detecting when the data is at a sufficient signal to noise
- beamline alignment / tuning / sample centering
- auto-exposure
- selecting points in phase space to measure next
- selecting what sample to measure next

Adaptive-ness can be inserted into the data collection process at several levels

1. below ``bluesky`` and in (or below) the control system
2. in ``bluesky`` plans, but without generating `~event_model.DocumentNames.event`
3. providing feedback on a per-`~event_model.DocumentNames.event` basis
4. providing feedback on a per-run / `~event_model.DocumentNames.start` basis
5. asynchronous and decoupled feedback
6. providing feedback across many runs

Each of these level of fidelity and interaction has a use and which
ones to pick will depend on the requirements and constraints on a
per-facility, per-beamline, and per-experiment basis.  A given
experiment may even make use of adaptivness from multiple levels!

There is abundant prior art in this space, being able to feedback
measurements to acquisition is not a novel idea.  This package
provides a set of reference tools for implementing levels 3 and 4 of
these feedback loops in the context of the `bluesky project
<https://blueskyproject.io/>`__ for any fidelity or application.

Integration layer
-----------------

In or below the controls system
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you need to make decisions on very short time scales (and have a
computation than can fit in the time budget) doing "adaptive" in or
below the control system maybe a good choice.  One example of this is
in the scaler devices that are used as the backend electronics for
integrating point detector on many beamlines.  Typically they are
configured to take a fixed length exposure, however they can be
configured to gate on any of the channels.  Thus by gating on the I0
(incoming photon flux) channel your other wise fixed plan would
"adapt" the exposure time to account for upstream fluctuations in
photon intensity.

You could also imagine a scenario with an imaging detector where we
have an efficient way of telling if the image contains "good" data or
not.  If we put the logic in the image acquisition pipe line we loud
ask to take "N *good* images" and the plan would adapt by taking as
many frames as required until the requested number of good frames were
captured.

Configuring adaptiveness at this level can provide huge benefits, it
transparently works for any plan we run, but can be very time
consuming to develop and may be hardware-specific.  In general this
level of adaptiveness is out-of-scope for this package.

In plans, but below Events
~~~~~~~~~~~~~~~~~~~~~~~~~~

At the most granular level ``bluesky`` gives the plan author access to
the data extracted from the control system before it is processed
through the `event_model <https://blueskyproject.io/event_model>`_
documents.  This is the level that we use in the
`~bluesky.plans.adaptive_scan` which is bundled with ``bluesky``.
This level has also been used at LCLS who implement the frame dropping
logic described above at the plan level (via
`~bluesky.plan_stubs.drop`).

This level gives the author a tremendous amount of flexibility and can
be used to prevent "bad" data from entering the document stream, but quickly
becomes very plan-specific and difficult to generalize and re-use.  This level
is documented else where and out of scope for this project.

.. _per_event:

Per-Event
~~~~~~~~~

In cases where the computation we need to do to recommend the next step
is fast compared to the time it takes to collect a single data point (
aka an `~event_model.DocumentNames.event`), then it makes sense to run
the recommendation engine on every Event.  At the end of the plan we will
have 1 run who's path through phase space was driven by the data.

Examples of this are a 1D scan that samples more finely around the center
of a peak or a 2D scan across gradient sample that samples more finely
at phase boundaries.  In these cases there is a 1:1 mapping between an
event collected and a recommendation for the next point to collect.


Per-Run
~~~~~~~

In cases where the data we need to make a decision about what to do next
maps more closely to a Run, we do the same as the :ref:`per_event` case, but
only expect a recommendation once per-run.

An example of this could be a 2D map where at each point we take a
XANES scan and then focus on points of interest with in the map.

Asynchronous and decoupled feedback
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In some cases we do want or need a tightly coupled synchronous
coordination between data collection and the computation.  In both the
per-event and per-run levels, the plan is expecting a 1:1 response
from the recommendation engine for each piece of data collected.  In
general, the plan has to wait for the response from algorithm before
continuing and the communication channel between the plan.  Further, the
communication between the algorithm and the plan necessarily very rich so
the plan and the brains need to be fairly well coordinated.

If instead we wanted to have an agent watching the data and assessing
the quality.  If we detect we have collected enough data on the sample
and further exposure would waste beamtime and put excess dose on the
sample we want to complete the plan early.  Conversely, if we detect
that the data is junk (like the sample is no longer in the beam) we
want to abort the plan.  While this watch dog process can save us
time, we do not want to slow down data collection to wait for
permission to continue at every point.  In this case we only need to
convey 3 possible states to the RunEngine and plan ``{'keep going',
'you have enough data', 'this data is garbage please stop'}``.  In an
analogy to `Suspenders
<https://blueskyproject.io/bluesky/state-machine.html#automated-suspension>`__,
this can be done at the RunEnigne level (as has been done at APS) so
the plan does not need to even be aware of the watch dog to benefit
from it.

A slightly more coupled example of asynchronous feedback is a 1D scan
that looks at a PV (or other shared state) for what its step size is.
If the step size is set by a user to a fixed value an the plan run it behaves
as a normal `~bluesky.plans.scan` with a fixed step size.  However,
there could be an agent (or a human!) watching the data and adjusting
the step size based on the how "interesting" the data currently looks.

In both of these cases the feedback is adding value without imposing
a cost to the plans and the failure mode of the computation failing is
the current status quo.

There are many ways that asynchronous feedback can be configured and
implemented and is out of scope for this package.



Per-many-runs
~~~~~~~~~~~~~

At this scale we need to collect and process the results from many run
before making any recommendations as to the next step.  While this can
be thought of as a variation of the per-run level, this requires
significant additional infrastructure (such as a reliable plan queuing
system) and is out of scope for this project.



Implementation and deployment concerns
--------------------------------------

Details that need to be worked out

 - make sure the plan and agent agree on the Document schema
 - how the documents get to the Agent (and any pre-processing / data
   reduction that needs to be done
 - how the data will be routed back to the plan
 - the schema of the data going from the recommendation engine to the plan
 - how to handle back-pressure
 - how to handle the plan / agent getting "out of sync"
 - how to handle the agent going quiet
