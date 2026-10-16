.. _par:

Place-and-Route 
===============================

Hammer has an action for placing and routing a synthesized design.
This action requires a tool plugin to implement ``HammerPlaceAndRouteTool``.

P&R Setup Keys
--------------

* Namespace: ``vlsi.core``

    * ``par_tool``
        
        * Module of the P&R tool, e.g. ``hammer.par.innovus``


P&R Input Keys
--------------

* Namespace: ``vlsi`` 
  
    * These are built-in Hammer APIs covered in :ref:`hammer-apis`

* Namespace: ``par``

    * ``inputs.input_files`` ([])
        
        * List of paths to post-synthesis netlists. Auto-populated after syn-to-par.

    * ``inputs.top_module`` (str)

        * Name of top RTL module to P&R. Auto-populated after syn-to-par.

    * ``inputs.post_synth_sdc`` (str)

        * Post-synthesis generated SDC. Auto-populated after syn-to-par.

    * ``inputs.gds_map_mode`` (str)
    
        * Specify which GDS layermap file to use. ``auto`` uses the technology-supplied file, whereas ``manual`` requires a file to be specified via ``inputs.gds_map_file``.

    * ``inputs.gds_merge`` (bool)

        * ``True`` tells the P&R tool to merge all library & macro GDS before streamout. Otherwise, only references will exist and merging needs to be done later, by a tool such as ``Calibre``, ``gdstk``, or ``gdspy``.

    * ``inputs.physical_only_cells_mode`` (str)

        * Specifies which set of cells to exclude from SPICE netlist because they have no logical function. ``auto`` uses the technology-supplied list, whereas ``manual`` and ``append`` override and append to the supplied list, respectively.

    * ``submit`` (dict)

        * Can override global settings for submitting jobs to a workload management platform.

    * ``power_straps_mode`` (str)
        
        * Power straps configuration. ``generate`` enables Hammer's power straps API, whereas ``manual`` requires a TCL script in ``power_straps_script_contents``.

    * ``blockage_spacing`` (Decimal)

        * Global obstruction around every hierarchical sub-block and hard macro

    * ``generate_power_straps_options`` (dict)

        * If ``generate_power_straps_method`` is ``by_tracks``, this struct specifies all the options for the power straps API. See :ref:`hammer-apis` for more detail.

P&R Inputs
----------
There are no other prerequisites to running place & route other than setting the keys described above.

.. note::
    In SledgeHammer, par also fingerprints the RTL, so every file in ``synthesis.inputs.input_files`` must still exist or par fails with ``RTL file not found``.
    In the DAG, par reads its keys from the ``par-input.json`` that ``syn-to-par`` builds out of ``{OBJ_DIR}/syn-rundir/syn-output-full.json``, the config synthesis saved the last time it was not skipped.
    A skipped synthesis leaves that file as it was, so an edit to ``par.*`` keys alone reaches par only after synthesis runs again, for example with ``sledgehammer par -t <design> --forceall``.

P&R Outputs
-----------

* Hierarchical (e.g. Cadence ILMs) and between-step snapshot databases in ``OBJ_DIR/par-rundir``
* GDSII file with final design: ``{OBJ_DIR}/par-rundir/{TOP_MODULE}.gds``
* Verilog gate-level netlist: ``{OBJ_DIR}/par-rundir/{TOP_MODULE}.lvs.v``
* SDF file for post-par simulation: ``{OBJ_DIR}/par-rundir/{TOP_MODULE}.par.sdf``
* Timing reports: ``{OBJ_DIR}/par-rundir/timingReports``
* A script to open the final chip: ``{OBJ_DIR}/par-rundir/generated-scripts/open_chip``

P&R output Hammer IR ``{OBJ_DIR}/par-rundir/par-output.json`` is converted to inputs for the DRC, LVS, and simulation tools by the ``par-to-drc``, ``par-to-lvs``, and ``par-to-sim`` actions, respectively.

P&R Commands
------------

* P&R Command (after syn-to-par is run)

    * ``hammer-vlsi -e env.yml -p {OBJ_DIR}/par-input.json --obj_dir OBJ_DIR par``

* P&R to DRC

    * ``hammer-vlsi -e env.yml -p config.yml -p {OBJ_DIR}/par-rundir/par-output.json --obj_dir OBJ_DIR par-to-drc``

* P&R to LVS

    * ``hammer-vlsi -e env.yml -p config.yml -p {OBJ_DIR}/par-rundir/par-output.json --obj_dir OBJ_DIR par-to-lvs``

* P&R to Simulation

    * ``hammer-vlsi -e env.yml -p config.yml -p {OBJ_DIR}/par-rundir/par-output.json --obj_dir OBJ_DIR par-to-sim``

.. note::
    In SledgeHammer, run ``sledgehammer par -t <design>`` instead: the DAG runs synthesis (skipped when unchanged) and ``syn-to-par`` before par, and runs ``par-to-drc``, ``par-to-lvs`` and ``par-to-sim`` for you when you ask for ``drc``, ``lvs`` and ``sim_par``, respectively.
    ``<design>`` is the name the DAG is registered under: by default the obj_dir basename, which need not be the top module. ``sledgehammer buildfile``, run from the vlsi directory, prints it at the end as part of the registered dag_id, ``sledgehammer_<design>_<user>``.
    A name with no registered DAG exits with ``no DAG registered for <design>`` and never falls back to the DAG of the current directory. From the vlsi directory you can leave out ``-t`` when only one registered DAG builds under that directory or was generated there.
    Par is skipped when nothing changed since its last successful run recorded in ``{OBJ_DIR}/master_database.json``, and with the PD cache on (the default under the ``sledgehammer`` launcher), a result already stored for the same inputs is restored instead of running the tool.
    If par fails or is killed under Innovus, the next run with unchanged inputs and without ``--force`` or step flags resumes from the newest confirmed ``pre_<step>`` checkpoint; ``--force`` reruns par from scratch.
    Step flags such as ``--from_step`` apply to synthesis unless you add ``--steps-stage par``; see the `CLI reference <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_ and `SUBSTEP_RESUME.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/SUBSTEP_RESUME.md>`_.
