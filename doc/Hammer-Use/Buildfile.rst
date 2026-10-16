.. _buildfile:

Hammer Buildfile
==========================================

Hammer natively supports a GNU Make-based build system to manage build dependencies.
To use this flow, ``vlsi.core.build_system`` must be set to ``make``.
``hammer`` writes the same ``hammer.d`` and also runs every action as plain legacy Hammer, with all SledgeHammer features (dependency check, PD cache, sub-step resume, error scan, LVS report verdict, time-saved ledger) off.
Stages after ``syn`` read ``vlsi.core.build_system`` from the stage before them, so after switching to or from ``hammer``, rerun from ``syn`` (``make redo-syn``).
Hammer will generate a Makefile include in the object directory named ``hammer.d`` after calling the ``build`` action:

.. code-block:: shell

    hammer-vlsi -e env.yml -p config.yml --obj_dir build build

.. note::
    In SledgeHammer, ``vlsi.core.build_system`` can also be ``sledgehammer`` (or its alias ``airflow``).
    The same ``build`` action then writes ``hammer.d`` plus an Airflow DAG, ``hammer_dag.py``, and links the DAG into the Airflow dags folder so ``sledgehammer <stage>`` can trigger it; with ``make``, only ``hammer.d`` is written.
    To run the action, run ``sledgehammer buildfile`` from the vlsi directory (Chipyard's ``vlsi``, or ``e2e/``).
    It runs ``make buildfile`` underneath, or ``make build`` when the Makefile has a ``build`` rule and no ``buildfile`` rule (as in ``e2e/``), with the SledgeHammer venv's python first on ``PATH``, so you do not need to activate a venv.
    Arguments such as ``VAR=val`` or ``-j 4`` go to make, and ``--target NAME`` runs another target.
    When make succeeds, it prints the DAGs registered for that directory and the command to run next, or says that none was registered (for example because ``vlsi.core.build_system`` is ``make``).
    By default the DAG id, which also names the link, is ``sledgehammer_<design>_<user>``, where ``<design>`` is the last path component of the ``obj_dir``; ``vlsi.core.airflow_dag_id`` overrides it, but ``sledgehammer <stage>`` only finds DAGs whose id has the form ``sledgehammer_<name>_<user>``, so keep that form when you override it.
    Two builds whose ``obj_dir`` ends in the same name (``build-sky130-cm/gcd`` and ``build-asap7-cm/gcd``, for example) therefore share the default DAG id: the one generated last replaces the other's link, and the build prints a NOTE when it does.
    See the `SledgeHammer CLI reference <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_ for that flow.

``build/hammer.d`` will contain environment variables needed by Hammer and a target for each major Hammer action (e.g. ``par``, ``syn``, etc. but not ``syn-to-par``, which is run automatically when calling ``make par``).
For a flat design, the dependencies are created between the major Hammer actions.
For hierarchical designs, Hammer will use the hierarchy to build a dependency graph and construct the Make target dependencies appropriately.

``hammer.d`` should be included in a higher-level Makefile.
While ``hammer.d`` defines all of the variables that it needs, there are often reasons to set these elsewhere.
Because ``hammer.d`` uses ``?=`` assignment, the settings created in the top-level Makefile will persist.
An example of this setup is found in `Chipyard <https://github.com/ucb-bar/chipyard/blob/main/vlsi/Makefile>`__.


To enable interactive usage, ``hammer.d`` also contains a set of companion "redo" targets (e.g. ``redo-par`` and ``redo-syn``).
These targets intentionally have no dependency information; they are for advanced users to make changes to the input config and/or edit the design manually, then continue the flow.
Additional arguments can be passed to the "redo" targets with the ``HAMMER_EXTRA_ARGS`` environment variable, for the following example uses:

* "Patching" the configuration for an action using ``HAMMER_EXTRA_ARGS="-p patch.yml"``.
* Flow control using ``--to_step/--until_step`` and ``--from_step/--after_step`` to stop a run at a particular step or resume one from a previous iteration.

.. note::
    In SledgeHammer, the ``syn``, ``par``, ``drc`` and ``lvs`` actions also run a dependency check, so a redo target skips the tool and exits successfully when the config, RTL, hooks and library collateral are unchanged since the last successful run in that ``obj_dir``, unless you pass step flags to ``syn`` or ``par``.
    The check ignores SledgeHammer's run-control keys (the ``vlsi.pd_cache``, ``vlsi.substep_resume`` and ``vlsi.error_scan`` keys, and ``vlsi.core.airflow_*``), so changing only those, for example adding a code to ``vlsi.error_scan.fail_on``, does not make the target rerun the tool.
    The check does not see hand edits to a stage's outputs in the ``obj_dir`` (a netlist or a tool database, for example), so after such an edit add ``--force`` to ``HAMMER_EXTRA_ARGS``.
    ``sledgehammer redo-<stage>`` runs that stage through the DAG with ``--force``, and the earlier stages it needs keep their normal check.

.. note::
    In SledgeHammer, when flow control stops a Genus, Innovus or OpenROAD run before it writes its final outputs, the run is not committed and the action exits with an error, so the target fails.
    Its checkpoints are kept, and with Genus or Innovus the next run without step flags or ``--force`` resumes from them if the inputs have not changed (see `SledgeHammer sub-step resume <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/SUBSTEP_RESUME.md>`_).
    ``sledgehammer`` takes ``--from_step``, ``--to_step`` and ``--only_step`` itself (with ``--steps-stage par`` for ``par``), but it does not read ``HAMMER_EXTRA_ARGS``, and the config files it passes are fixed when the DAG is generated.
    To apply a ``-p`` patch through it, add the patch to the configs your Makefile passes to ``build`` and rerun ``sledgehammer buildfile``, which hands its arguments to make; make does not rerun the ``build`` action while it finds ``hammer.d`` up to date, so delete ``hammer.d`` from the ``obj_dir`` first.
    In ``e2e/``, for example, ``hammer.d`` depends only on the SRAM generator output, so delete it and run ``sledgehammer buildfile extra="sledge.yml patch.yml"``, where ``sledge.yml`` sets ``vlsi.core.build_system: sledgehammer`` (the e2e tool and design configs set it to ``make``).
    The patch then applies to every run of that DAG until you regenerate it without the patch.

