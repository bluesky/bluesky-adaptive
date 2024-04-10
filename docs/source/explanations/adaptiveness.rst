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
- controlling the speed of a temperature ramp
- selecting what sample to measure next
- driving synthesis and simulation workloads

This package provides a set of reference tools for implementing in-the-loop
feedback on scientific signals on the seconds-to-minutes time scale in the
context of the `bluesky project <https://blueskyproject.io/>`__.

Bluesky Integration layer
-------------------------


Adaptive-ness can be inserted into the data collection process at several
levels, primarily driven by the timescale of the feedback.

1. below ``bluesky`` and in (or below) the control system
2. in ``bluesky`` plans, but without generating `~event_model.DocumentNames.event`
3. providing feedback on a per-`~event_model.DocumentNames.event` basis
4. providing feedback on a per-run / `~event_model.DocumentNames.start` basis
5. providing feedback across many runs



In or below the controls system
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you need to make decisions on very short time scales (and have a computation
than can fit in the time budget) doing "adaptive" in or below the control
system maybe a good choice.  One example of this is in the scaler devices that
are used as the backend electronics for integrating point detector on many
beamlines.  Typically they are configured to take a fixed length exposure,
however they can be configured to gate on any of the channels.  Thus by gating
on the I0 (incoming photon flux) channel your other wise fixed plan would
"adapt" the exposure time to account for upstream fluctuations in photon
intensity.

You could also imagine a scenario with an imaging detector where we have an
efficient way of telling if the image contains "good" data or not.  If we put
the logic in the image acquisition pipe line we could ask to take "N *good*
images" and the plan would adapt by taking as many frames as required until the
requested number of good frames were captured.

Configuring adaptiveness at this level can provide huge benefits, it
transparently works for any plan we run, but can be very time consuming to
develop and may be hardware-specific.  In general this level of adaptiveness is
out-of-scope for this package.

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

.. TODO(maffettone): Add a link to the example plans

Per-Run
~~~~~~~

In cases where the data we need to make a decision about what to do next
maps more closely to a Run, we do the same as the :ref:`per_event` case, but
only expect a recommendation once per-run.

An example of this could be a 2D map where at each point we take a
XANES scan and then focus on points of interest with in the map.

.. TODO(maffettone): Add a link to the example plans

Per-many-runs
~~~~~~~~~~~~~

At this scale we need to collect and process the results from many run before
making any recommendations as to the next step.  While this can be thought of
as a variation of the per-run level, this requires additional infrastructure
for reliable plan queuing, as implemented by `queueserver
<https://blueskyproject.io/bluesky-queueserver>`_, and is out of scope for this
project.
