Hammer Actions
===================================

Hammer has a set of actions including synthesis, place-and-route, DRC, LVS, simulation, SRAM generation, and PCB collateral generation.
All of the Hammer actions and their associated key inputs can be found in ``hammer/config/defaults.yml`` and are documented in detail in each action's documentation.

.. note::
    In SledgeHammer, ``defaults.yml`` also declares the keys SledgeHammer adds to control how a stage runs (``vlsi.pd_cache.*``, ``vlsi.substep_resume.*``, ``vlsi.error_scan.*`` and ``vlsi.core.airflow_*``).
    They never change what a stage produces, so they stay out of the dependency check and the PD cache key, and setting one never reruns a stage.
    `ENVIRONMENT_VARIABLES.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/ENVIRONMENT_VARIABLES.md>`_ lists most of them.

Hammer will automatically pass the output files of one action to subsequent actions if those actions require the files.
Hammer does this with conversion steps (e.g. ``syn-to-par`` for synthesis outputs to place-and-route inputs), which map the outputs from one tool into the inputs of another (e.g. ``synthesis.outputs.output_files`` maps to ``par.inputs.input_files``).
The Hammer make infrastructure builds these conversion rules automatically, so using the Make infrastructure is recommended.
See the :ref:`buildfile` section for more details.

.. note::
    In SledgeHammer, set ``vlsi.core.build_system: sledgehammer`` and run ``sledgehammer buildfile`` from the vlsi directory (Chipyard's ``vlsi``, or ``e2e/``).
    Underneath, it runs ``make buildfile`` (``make build`` in ``e2e/``), and the ``build`` action that target runs writes an Airflow DAG next to ``hammer.d`` and links it into the dags folder.
    ``sledgehammer buildfile`` then prints the registered DAG id and the command to run next.
    ``sledgehammer <stage>`` or ``sledgehammer run <stages>`` runs that DAG, which runs the conversion steps for you as tasks between the stages.
    See `CLI_REFERENCE.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_.

Hammer actions are implemented using tools. See the :ref:`cad-tools` section for details about how these tools are set up.
