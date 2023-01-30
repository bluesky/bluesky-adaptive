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
   distributed

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
- controlling the speed of a temperature ramp
- selecting what sample to measure next
- driving synthesis and simulation workloads

This package provides a set of reference tools for implementing in-the-loop
feedback on scientific signals on the seconds-to-minutes time scale in the
context of the `bluesky project <https://blueskyproject.io/>`__.


Dimensions of Adaptive-ness
---------------------------

Given the breadth of what can be considered "adaptive", we propose breaking
these down along three axis: the coordination motif, the source of the signal
we are using for the feedback and the timescale of the feedback loop.

The communication between the plan and the recommendation engine can either be
synchronous, "in-the-loop", or asynchronous.  In the synchronous case we block
progress on the scan while we wait for the next recommendation (based on the
freshest data!).  This is best for cases where the cost of generating the
recommendation is low and the cost of taking the wrong measurement is high.
For example, this motif is very natural when using AI to drive the scanning
of a 2D sample.  However, synchronous communication implies a very tight coupling
between the plan and the recommendation engine which is not always desirable.  We
can achieve looser coupling by using asynchronous communication where the
plan is able to continue running and periodically checks for input from the
recommendation engines.   Examples of this include the suspenders concept
built in the RunEnigne which can be used to pause and resume the experiment if
the beam dumps.  Another example is a temperature ramp where the ramp rate is
dynamically controlled by machine (or human) intervention.

The information that we want to feedback on can be roughly classified into two
categories: engineering values and scientific values.  Engineering values are
things like the current ring current, the flux of x-rays in an ion chamber, or
if the shutters are open.  These are things that the control systems provide
and if they are in bad states will result is useless data, however interpreting
them does not require knowing anything about the sample or science being done.
On the other hand science signal are very tightly coupled to the current
experiment and their interpretation is context dependent.  In the case of
studying a phase change a sudden drop in intensity may indicate that you are
near the transition point or grain boundary and should slow down to collect
more data about this region of phase space.  On the other hand, if you are
trying to map a sample with fluorescence a drop in intensity may indicate you
have scanned off the sample and need to scan back as this part of the phase
space has no scientific value!

The final dimension of adaptive experiments is timescale on which the feedback
needs to happen.  This is controlled by both how long the computations take and
how quickly the system can respond to the feedback.  For example, a PID loop
that maintains the alignment of a mirror is an adaptive experiment, the control
system is changing the hardware based of measurements, but the need to respond
on the sub-ms timescale leaves very little time budget for computation!  On the
other hand, if you want to use experimental results to guide what chemical
synthesis to run, and the synthesis takes a day to complete, you have a
significantly bigger computation budget.  There are natural scales at sub-ms,
best handled in the control system, sub-second for per-step adaptive and
suspender logic, second-to-minute for inter-run, and minutes-to-longer.

Each combination of communication motif, signal class, and timescale has a use
and which ones to pick will depend on the requirements and constraints on a
per-facility, per-beamline, and per-experiment basis.  A given experiment may
even make use of adaptivness from multiple levels!  There is abundant prior art
in this space, being able to feedback measurements to acquisition is not a
novel idea.


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


.. autosummary::
   :nosignatures:
   :toctree: api_gen

   bluesky_adaptive.per_event.recommender_factory
   bluesky_adaptive.per_event.adaptive_plan

Per-Run
~~~~~~~

In cases where the data we need to make a decision about what to do next
maps more closely to a Run, we do the same as the :ref:`per_event` case, but
only expect a recommendation once per-run.

An example of this could be a 2D map where at each point we take a
XANES scan and then focus on points of interest with in the map.


.. autosummary::
   :toctree: api_gen
   :nosignatures:

   bluesky_adaptive.per_start.recommender_factory
   bluesky_adaptive.per_start.adaptive_plan
   bluesky_adaptive.on_stop.recommender_factory




Per-many-runs
~~~~~~~~~~~~~

At this scale we need to collect and process the results from many run before
making any recommendations as to the next step.  While this can be thought of
as a variation of the per-run level, this requires additional infrastructure
for reliable plan queuing, as implemented by `queueserver
<https://blueskyproject.io/bluesky-queueserver>`_, and is out of scope for this
project.



Asynchronous and decoupled feedback
-----------------------------------

In some cases we do want or need a tightly coupled synchronous coordination
between data collection and the computation.  In both the per-event and per-run
levels, the plan is expecting a 1:1 response from the recommendation engine for
each piece of data collected.  In general, the plan has to wait for the
response from algorithm before continuing.

Alternatively, we may instead want have an agent watching the data and
assessing the quality.  If we detect we have collected enough data on the
sample and further exposure would waste beamtime and put excess dose on the
sample we want to complete the plan early.  Conversely, if we detect that the
data is junk (like the sample is no longer in the beam) we want to abort the
plan.  While this watch dog process can save us time, we do not want to slow
down data collection to wait for permission to continue at every point!  In
this case we only need to convey 3 possible states to the RunEngine and plan
``{'keep going', 'you have enough data', 'this data is garbage please stop'}``.
In an analogy to `Suspenders
<https://blueskyproject.io/bluesky/state-machine.html#automated-suspension>`__,
this can be done at the RunEnigne level (as has been done at APS) so the plan
does not need to even be aware of the watch dog to benefit from it.

A slightly more coupled example of asynchronous feedback is a 1D scan that
looks at a PV (or other shared state) for what its step size is.  If the step
size is set by a user to a fixed value an the plan run it behaves as a standard
`~bluesky.plans.scan` with a fixed step size.  However, there could be an agent
(or a human!) watching the data and adjusting the step size based on the how
"interesting" the data currently looks.  By this mechanism we would spend more
time collecting data in "interesting" parts of phase space and extract more
science for a given amount of beamtime.

In both of these cases the feedback is adding value without imposing a cost to
the plans and the failure mode of the computation failing is the current status
quo.

An increasingly desirable operating paradigm for autonomous experiments considers
the directives of many agents, or even networks of agents,
including multiple human agents. The previously described lock-step approaches to
experiment and analysis, leave no room for human experts to engage in the loop,
incorporation of information from complementary techniques, or the integration of
multiple computational agents. In this more complex paradigm, various agents must be
able to process the captured data stream, suggest plans to be executed,
and create reports for human consumption. This is exemplified in the case where
multiple passive agents are performing dataset factorization or AI-based compression
algorithms that provide visualization tools for users,  multiple active learning
agents are providing suggestions for next experiments as they complete their
computation, and human agents are also guiding the experiment
(see this `NeurIPS Workshop Paper <https://arxiv.org/abs/2301.09177>`_).

Here, the same :code:`tell`, :code:`report`, :code:`ask` grammar can be used in conjunction with
Kafka, Tiled, and the RunManager. This adds some additional dependencies including
``bluesky-queueserver``, ``tiled``, and ``bluesky-kafka``.
Examples of these agents are provided in :ref:`distributed`.
Furthermore, using Kafka for distributed communication, one can construct meta-agents,
or *adjudicators*, which coordinate between a collection of agents in more sophisticated ways
than the RunManager priority queue and provide an additional avenue for human intervention.
