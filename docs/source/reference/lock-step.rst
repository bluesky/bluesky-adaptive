=========================
Lock Step Implementations
=========================

As discussed in :doc:`../explanations/lock-step`, the synchronous approach to agent implementation can be
accomplished on a per-event or per-run basis in `bluesky-adaptive`.
The core of synchronous integration lies in the utilization of adaptive plans and factory functions.
The adaptive plans 

Per-Event
~~~~~~~~~

In cases where the computation we need to do to recommend the next step
is fast compared to the time it takes to collect a single data point (
aka an `~event_model.DocumentNames.event`), then it makes sense to run
the recommendation engine on every Event. 

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


.. autosummary::
   :toctree: api_gen
   :nosignatures:

   bluesky_adaptive.per_start.recommender_factory
   bluesky_adaptive.per_start.adaptive_plan
   bluesky_adaptive.on_stop.recommender_factory


Integrated Demonstrations
~~~~~~~~~~~~~~~~~~~~~~~~~

The following examaples are provided to illustrate the rational and mechanics behind the lock-step implementations.
They show how to build a reccomender factory from scratch, with the agent embedded in the factory, where the agent is a simple step function.


Per-event
---------

::

   def per_event_plan_sequence_factory(
       sequence, independent_keys, dependent_keys, *, max_count=10, queue=None
   ):
       """
       Generate the callback and queue for a naive recommendation engine.

       This returns the same sequence of points no matter what the
       measurements are.

       For each Event that the callback sees it will place either a
       recommendation or `None` into the queue.  Recommendations will be
       of a dict mapping the independent_keys to the recommended values and
       should be interpreted by the plan as a request for more data.  A `None`
       placed in the queue should be interpreted by the plan as in instruction to
       terminate the run.


       Parameters
       ----------
       sequence : iterable of positions
           This should be an iterable of positions vectors that match the motors

       independent_keys : List[str]
           The names of the independent keys in the events

       dependent_keys : List[str]
           The names of the dependent keys in the events

       max_count : int, optional
           The maximum number of measurements to take before poisoning the queue.

       queue : Queue, optional
           The communication channel for the callback to feedback to the plan.
           If not given, a new queue will be created.

       Returns
       -------
       callback : Callable[str, dict]
           This function must be subscribed to RunEngine to receive the
           document stream.

       queue : Queue
           The communication channel between the callback and the plan.  This
           is always returned (even if the user passed it in).

       """
       seq = iter(itertools.cycle(sequence))
       if queue is None:
           queue = Queue()

       # TODO handle multi-stream runs!
       def callback(name, doc):

           if name == "event":
               if doc["seq_num"] >= max_count:
                   # if at max number of points poison the queue and return early
                   queue.put(None)
                   return
               payload = doc["data"]
               inp, measurements = extract_arrays(
                   independent_keys, dependent_keys, payload
               )

               # call something to get next point!
               next_point = next(seq)
               queue.put({k: v for k, v in zip(independent_keys, next_point)})

       return callback, queue


   def per_event_plan_step_factory(
       step, independent_keys, dependent_keys, *, max_count=10, queue=None
   ):
       """
       Generate the callback and queue for a naive recommendation engine.

       This recommends a fixed step size independent of the measurement.

       For each Event that the callback sees it will place either a
       recommendation or `None` into the queue.  Recommendations will be
       of a dict mapping the independent_keys to the recommended values and
       should be interpreted by the plan as a request for more data.  A `None`
       placed in the queue should be interpreted by the plan as in instruction to
       terminate the run.


       Parameters
       ----------
       step : array[float]
           The delta step to take on each point

       independent_keys : List[str]
           The names of the independent keys in the events

       dependent_keys : List[str]
           The names of the dependent keys in the events

       max_count : int, optional
           The maximum number of measurements to take before poisoning the queue.

       queue : Queue, optional
           The communication channel for the callback to feedback to the plan.
           If not given, a new queue will be created.

       Returns
       -------
       callback : Callable[str, dict]
           This function must be subscribed to RunEngine to receive the
           document stream.

       queue : Queue
           The communication channel between the callback and the plan.  This
           is always returned (even if the user passed it in).

       """

       if queue is None:
           queue = Queue()

       def callback(name, doc):
           # TODO handle multi-stream runs!
           if name == "event_page":
               if doc["seq_num"][-1] > max_count:
                   # if at max number of points poison the queue and return early
                   queue.put(None)
                   return
               payload = doc["data"]
               # This is your "motor positions" and the "extracted measurements"
               independent, measurement = extract_arrays(
                   independent_keys, dependent_keys, payload
               )
               # call something to get next point!
               next_point = independent + step
               queue.put({k: v for k, v in zip(independent_keys, next_point)})

       rr = RunRouter([lambda name, doc: ([callback], [])])
       return rr, queue

and to run it:

::

       cb, queue = intra_plan_step_factory(np.asarray((5, 5)), ['ctrl_Ti', 'ctrl_temp'], ['rois_I_00', 'rois_I_01'] )
       intra_run_adaptive_plan([rois], {ctrl.Ti: 15, ctrl.temp: 300}, to_brains=cb, from_brains=queue)



Per-start
---------


::

   def per_start_step_factory(
       step, independent_keys, dependent_keys, *, max_count=10, queue=None
   ):
       """
       Generate the callback and queue for a naive recommendation engine.

       This recommends a fixed step size independent of the measurement.

       For each Run (aka Start) that the callback sees it will place
       either a recommendation or `None` into the queue.  Recommendations
       will be of a dict mapping the independent_keys to the recommended
       values and should be interpreted by the plan as a request for more
       data.  A `None` placed in the queue should be interpreted by the
       plan as in instruction to terminate the run.

       The StartDocuments in the stream must contain the key
       ``'batch_count'``.


       Parameters
       ----------
       step : array[float]
           The delta step to take on each point

       independent_keys : List[str]
           The names of the independent keys in the events

       dependent_keys : List[str]
           The names of the dependent keys in the events

       max_count : int, optional
           The maximum number of measurements to take before poisoning the queue.

       queue : Queue, optional
           The communication channel for the callback to feedback to the plan.
           If not given, a new queue will be created.

       Returns
       -------
       callback : Callable[str, dict]
           This function must be subscribed to RunEngine to receive the
           document stream.

       queue : Queue
           The communication channel between the callback and the plan.  This
           is always returned (even if the user passed it in).

       """

       if queue is None:
           queue = Queue()

       def callback(name, doc):
           # TODO handle multi-stream runs with more than 1 event!
           if name == 'start':
               if doc['batch_count'] > max_count:
                   queue.put(None)
                   return

           if name == "event_page":
               payload = doc["data"]
               # This is your "motor positions"
               independent = np.asarray([payload[k][-1] for k in independent_keys])
               # This is the extracted measurements
               measurement = np.asarray([payload[k][-1] for k in dependent_keys])
               # call something to get next point!
               next_point = independent + step
               queue.put({k: v for k, v in zip(independent_keys, next_point)})

       rr = RunRouter([lambda name, doc: ([callback], [])])
       return rr, queue

   def adaptive_factory_factory(
       adaptive_factory, independent_keys, dependent_keys, *, max_count=10, queue=None
   ):
       """
       Generate the callback and queue for a naive recommendation engine.

       This recommends a fixed step size independent of the measurement.

       For each Run (aka Start) that the callback sees it will place
       either a recommendation or `None` into the queue.  Recommendations
       will be of a dict mapping the independent_keys to the recommended
       values and should be interpreted by the plan as a request for more
       data.  A `None` placed in the queue should be interpreted by the
       plan as in instruction to terminate the run.

       The StartDocuments in the stream must contain the key
       ``'batch_count'``.


       Parameters
       ----------
       adaptive_factory : Callable[dict] -> adaptive.BaseLearner
           Function that when passed a Start document will return an
           `adaptive.BaseLearner` object ready to go

       independent_keys : List[str]
           The names of the independent keys in the events

       dependent_keys : List[str]
           The names of the dependent keys in the events

       max_count : int, optional
           The maximum number of measurements to take before poisoning the queue.

       queue : Queue, optional
           The communication channel for the callback to feedback to the plan.
           If not given, a new queue will be created.

       Returns
       -------
       callback : Callable[str, dict]
           This function must be subscribed to RunEngine to receive the
           document stream.

       queue : Queue
           The communication channel between the callback and the plan.  This
           is always returned (even if the user passed it in).

       """

       if queue is None:
           queue = Queue()

       last_batch_id = None
       adaptive_obj = None

       def callback(name, doc):
           nonlocal last_batch_id, adaptive_obj

           # TODO handle multi-stream runs with more than 1 event!
           if name == "start":
               if doc["batch_count"] > max_count:
                   queue.put(None)
                   return
               if last_batch_id != doc["batch_id"]:
                   last_batch_id = doc["batch_id"]
                   adaptive_obj = adaptive_factory(doc)

           if name == "event_page":
               payload = doc["data"]
               # This is your "motor positions"
               independent = np.asarray([payload[k][-1] for k in independent_keys])
               # This is the extracted measurements
               measurement = np.asarray([payload[k][-1] for k in dependent_keys])
               # push into the adaptive API
               adaptive_obj.ingest(independent, measurement)
               # pull the next point out of the adaptive API
               next_point = adaptive_obj.suggest(1)
               queue.put({k: v for k, v in zip(independent_keys, next_point)})

       rr = RunRouter([lambda name, doc: ([callback], [])])
       return rr, queue
