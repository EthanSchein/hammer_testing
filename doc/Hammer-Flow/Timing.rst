Static Timing Analysis
===============================

Hammer supports post-synthesis and post-P&R static timing analysis. It provides a simple API to provide a set of design inputs (e.g. Verilog netlists, delay files, parasitics files) to perform static timing analysis (STA) for design signoff.
This action requires a tool plugin to implement ``HammerTimingTool``.

STA Setup Keys
-------------------------------

* Namespace: ``vlsi.core``

    * ``timing_tool``
        * Module of the STA tool, e.g. ``hammer.timing.tempus``

STA Input Keys
-------------------------------

* Namespace: ``timing.inputs``

    * ``input_files`` ([])

        * A list of file paths to the Verilog gate-level netlist to be passed to the STA tool. The paths may be relative to the directory in which ``hammer-vlsi`` is called.

    * ``top_module`` (str)

        * Name of the top level module of the design to be timed.

    * ``post_synth_sdc`` (str)

        * Post-synthesis generated SDC. Auto-populated after syn-to-timing and par-to-timing.

    * ``spefs`` ([str])

        * List of paths to all spef (parasitic extraction) files for the design. This list may include a spef file per MMMC corner. Paths may be relative to the directory in which hammer-vlsi is called. Auto-populated after par-to-timing.

    * ``sdf_file`` (str)

        * Path to Standard Delay Format file. Auto-populated after syn-to-timing.

    * ``max_paths`` (int)

        * Maximum number of timing paths to report from the STA tool. Large limits may hurt tool runtime.

STA Inputs
-------------------------------

Running the ``syn-to-timing`` action after running synthesis will automatically generate the Hammer IR required to pipe the synthesis outputs to the Hammer STA tool, and should be included in the Hammer call, as demonstrated in the "Post-Synthesis STA" command below.  The same goes for post-place-and-route STA.

.. note::
    In SledgeHammer, the DAG runs ``syn-to-timing`` or ``par-to-timing`` for you, and the STA task gets no project config except the input file that bridge writes from the full config that synthesis or place-and-route saved the last time it was not skipped.
    An edit to ``timing.*`` keys alone therefore reaches STA only after synthesis runs again (and place-and-route too, for ``timing-par``), for example by adding ``--forceall``, which reruns every stage the run executes.
    Every DAG task runs Hammer from the directory the DAG was generated in, so relative paths in ``timing.inputs`` resolve against that directory wherever you run ``sledgehammer``.
    The ``timing`` action also fingerprints the RTL first, so the files in ``synthesis.inputs.input_files`` must still exist.

STA Outputs
-------------------------------

The STA tool produces reports in ``OBJ_DIR/timing-rundir/``. Outputs from advanced STA flows such as engineering change order (ECO) patches are not yet supported.

.. note::
    In SledgeHammer, the DAG passes ``--timing_rundir`` to each STA stage, so the reports land in ``OBJ_DIR/timing-syn-rundir/`` or ``OBJ_DIR/timing-par-rundir/`` (``OBJ_DIR/timing-syn-<module>/`` or ``OBJ_DIR/timing-par-<module>/`` in a hierarchical flow).

STA Commands
-------------------------------

* Synthesis to STA 

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/syn-rundir/syn-output.json -o OBJ_DIR/syn-to-timing_input.json --obj_dir OBJ_DIR syn-to-timing``

* Post-Synthesis STA 

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/syn-to-timing_input.json --obj_dir OBJ_DIR timing``

* P&R to STA 

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/par-rundir/par-output.json -o OBJ_DIR/par-to-timing_input.json --obj_dir OBJ_DIR par-to-timing``

* Post-P&R STA 

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/par-to-timing_input.json --obj_dir OBJ_DIR timing``

.. note::
    In SledgeHammer, run ``sledgehammer timing-syn -t <design>`` or ``sledgehammer timing-par -t <design>`` instead (``sledgehammer run timing_syn timing_par -t <design>`` runs both), after registering the DAG by running ``sledgehammer buildfile`` from the vlsi directory.
    ``<design>`` is the name the DAG was registered under, by default the basename of its ``OBJ_DIR`` (such as ``gcd`` for ``build-sky130-cm/gcd``), which is not always the top module.
    A name with no registered DAG exits with ``no DAG registered for <design>`` and never falls back to the DAG of the current directory.
    From the vlsi directory you can leave out ``-t`` unless more than one registered DAG belongs to it.
    Before STA, the DAG runs synthesis (and, for ``timing-par``, ``syn-to-par`` and place-and-route), skipping each stage that is unchanged, then the ``syn-to-timing`` or ``par-to-timing`` bridge.
    STA itself has no dependency check and is not cached, so it runs again every time a run includes it.
    See `CLI_REFERENCE.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_.
