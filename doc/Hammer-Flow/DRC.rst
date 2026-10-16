DRC
===============================

Hammer has an action for running design rule check (DRC) on a post-place-and-route GDS.
This action requires a tool plugin to implement ``HammerDRCTool``.

DRC Setup Keys
--------------

* Namespace: ``vlsi.core``

    * ``drc_tool``
        
        * Module of the DRC tool e.g. ``hammer.drc.calibre``

DRC Input Keys
--------------

* Namespace: ``drc``

    * ``inputs.top_module`` (str)

        * Name of top RTL module to run DRC on. Auto-populated after par-to-drc.

    * ``inputs.layout_file`` (str)

        * GDSII file from place-and-route. Auto-populated after par-to-drc.

    * ``inputs.additional_drc_text_mode`` (str)

        * Chooses what custom DRC commands to add to the run file. ``auto`` selects the one provided in the :ref:`tech-json`.

    * ``inputs.drc_rules_to_run`` ([str])

        * This selects a subset of the rules given in the technology's Design Rule Manual (DRM). The format of these rules will be technology- and tool-specific.

    * ``submit`` (dict)

        * Can override global settings for submitting jobs to a workload management platform.

DRC Inputs 
--------------
There are no other prerequisites to running DRC other than setting the keys described above.

.. note::
    In SledgeHammer, the ``drc`` action fingerprints the RTL before it runs, so the files in ``synthesis.inputs.input_files`` must still exist.
    The DAG passes the ``drc`` task no project config except ``drc-input.json``, which ``par-to-drc`` builds from the par output, so it carries the full config that synthesis wrote the last time it was not skipped.
    An edit to ``drc.*`` keys in your project config therefore reaches DRC only after synthesis runs again, for example with ``sledgehammer drc -t <design> --forceall``, which also reruns ``syn`` and ``par``.

DRC Outputs
--------------

* DRC results report and database in ``{OBJ_DIR}/drc-rundir``
* A run file: ``{OBJ_DIR}/drc-rundir/drc_run_file``
* A script to interactively view the DRC results: ``{OBJ_DIR}/drc-rundir/generated-scripts/view_drc``

DRC Commands
--------------

* DRC Command (after par-to-drc is run)

    * ``hammer-vlsi -e env.yml -p {OBJ_DIR}/drc-input.json --obj_dir OBJ_DIR drc``

.. note::
    In SledgeHammer, run ``sledgehammer drc -t <design>`` instead: the DAG runs ``syn``, ``syn-to-par``, ``par`` and ``par-to-drc`` before DRC.
    ``<design>`` is the obj_dir basename (``gcd`` for ``build-sky130-cm/gcd``), which is not always the top module.
    Running ``sledgehammer buildfile`` from the vlsi directory registers the DAG as ``sledgehammer_<design>_<user>`` and prints that name.
    A name with no registered DAG exits with ``no DAG registered for <design>``; it never falls back to the DAG of the current directory.
    From the vlsi directory you can leave out ``-t`` unless more than one registered DAG belongs to that directory.
    The ``drc`` action exits successfully without running the tool or touching ``drc-rundir`` when the config, RTL, hooks and library collateral are unchanged and neither ``syn`` nor ``par`` has rerun since the last successful DRC run.
    Add ``--force`` to rerun DRC anyway.
    See `CLI_REFERENCE.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_.
