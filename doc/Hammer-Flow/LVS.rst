LVS
===============================

Hammer has an action for running layout-versus-schematic (LVS) on a post-place-and-route GDS and gate-level netlist.
This action requires a tool plugin to implement ``HammerLVSTool``.

LVS Setup Keys
--------------

* Namespace: ``vlsi.core``

    * ``lvs_tool``
        
        * Module of the lvs tool, e.g. ``hammer.lvs.calibre``

LVS Input Keys
--------------

* Namespace: ``lvs``

    * ``inputs.top_module`` (str)

        * Name of top RTL module to run LVS on. Auto-populated after par-to-lvs.

    * ``inputs.layout_file`` (str)

        * GDSII file from P&R. Auto-populated after par-to-lvs.

    * ``inputs.schematic_files`` ([str])

        * Netlists from P&R'd design and libraries. Auto-populated after par-to-lvs.

    * ``inputs.additional_lvs_text_mode`` (str)

        * Chooses what custom LVS commands to add to the run file. ``auto`` selects the one provided in the :ref:`tech-json`.

    * ``submit`` (dict)

        * Can override global settings for submitting jobs to a workload management platform.

LVS Inputs 
--------------
There are no other prerequisites to running LVS other than setting the keys described above.

.. note::
    In SledgeHammer, the DAG's ``lvs`` task gets your project configuration only through ``lvs-input.json``, which carries the full config that synthesis wrote the last time it was not skipped.
    An ``lvs.*`` edit therefore reaches LVS only on a run where synthesis is not skipped as unchanged, for example with ``sledgehammer lvs -t <design> --forceall``, which also reruns ``syn`` and ``par``.
    The ``lvs`` action also fingerprints the RTL first, so the files listed in ``synthesis.inputs.input_files`` must still exist.

LVS Outputs
--------------
* LVS results report and database in ``{OBJ_DIR}/lvs-rundir``
* A run file: ``{OBJ_DIR}/lvs-rundir/lvs_run_file``
* A script to interactively view the LVS results: ``{OBJ_DIR}/lvs-rundir/generated-scripts/view_lvs``

LVS Commands
--------------

* LVS Command (after par-to-lvs is run)

    * ``hammer-vlsi -e env.yml -p {OBJ_DIR}/lvs-input.json --obj_dir OBJ_DIR lvs``

.. note::
    In SledgeHammer, run ``sledgehammer lvs -t <design>`` instead: it triggers the DAG registered for ``<design>``, which runs ``syn``, ``syn-to-par``, ``par`` and ``par-to-lvs`` before LVS (see `CLI_REFERENCE.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_).
    ``<design>`` is the name the DAG was registered under, by default the obj_dir basename (``build-<pdk>-<tools>/<design>``), which is not always the top module.
    If no DAG is registered for ``<design>``, the command exits with ``no DAG registered for <design>`` and does not fall back to the DAG of your current directory.
    Register it first by running ``sledgehammer buildfile`` from the vlsi directory, which prints the DAG it registered as ``sledgehammer_<design>_<user>``.
    From the vlsi directory you can leave out ``-t`` unless more than one registered DAG belongs to it.
    LVS is skipped when nothing it depends on changed since its last successful run, so add ``--force`` to rerun it.
    The action also fails when ``lvs-rundir`` holds a Calibre-style ``lvs_results.rpt`` that reports ``INCORRECT`` or ``NOT COMPARED``, even if the tool exited cleanly.
    SledgeHammer reads no other report format, so with other tools (Pegasus included) check the tool's own report for the comparison result.
