.. snippet-pip-standard-start
.. code:: bash

   $ pip install "bluesky-adaptive[all]"
.. snippet-pip-standard-end

.. snippet-conda-standard-start
.. code:: bash

   $ conda install -c conda-forge bluesky-adaptive
.. snippet-conda-standard-end

.. snippet-pip-cpu-start
.. code:: bash

   $ pip install torch --index-url https://download.pytorch.org/whl/cpu
   $ pip install "bluesky-adaptive[all]"
.. snippet-pip-cpu-end

.. snippet-uv-cpu-start
.. code:: bash

   $ uv pip install "bluesky-adaptive[all]" --torch-backend=cpu
.. snippet-uv-cpu-end

.. snippet-conda-cpu-start
.. code:: bash

   $ conda install -c conda-forge bluesky-adaptive "pytorch=*=cpu*"
.. snippet-conda-cpu-end
