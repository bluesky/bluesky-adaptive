.. warning::

   bluesky-adaptive 0.x is discontinued. Starting with 1.0, bluesky-adaptive adopts the
   `blop <https://github.com/bluesky/blop>`_ codebase and continues its development under the
   bluesky-adaptive name at `bluesky-adaptive <https://github.com/bluesky/bluesky-adaptive>`_.
   The 1.0+ API differs from 0.x: the Kafka/Tiled Agent base, adjudicators,
   server, and lockstep recommenders are not carried forward. Pin
   ``bluesky-adaptive<1`` to keep this package.

   0.4.0 is the final 0.x release.

Bluesky Adaptive
----------------

The documentation is split into 4 sections:

.. grid:: 2
    :gutter: 4

    .. grid-item-card:: :material-regular:`lightbulb;4em`
        :link: explanations/index
        :link-type: doc

        **Explanations**

        Understand the concepts behind bluesky-adaptive. Ideal for gaining a deeper understanding of the library's principles.

    .. grid-item-card:: :material-regular:`school;4em`
        :link: tutorials/index
        :link-type: doc

        **Tutorials**

        Step-by-step guides to help you get started with bluesky-adaptive and learn through doing.

    .. grid-item-card:: :material-regular:`directions;4em`
        :link: how-to/index
        :link-type: doc

        **How-to guides**

        Practical guides and recipes for accomplishing specific tasks with bluesky-adaptive.

    .. grid-item-card:: :material-regular:`menu_book;4em`
        :link: reference/index
        :link-type: doc

        **Reference**

        Comprehensive API documentation for bluesky-adaptive. Reference material for classes, methods, and functionalities.


For detailed release notes and version history, see the :doc:`reference/release-history`.

.. toctree::
   :hidden:
   :caption: Contents

   explanations/index
   how-to/index
   tutorials/index
   reference/index


.. include:: ../../README.md
