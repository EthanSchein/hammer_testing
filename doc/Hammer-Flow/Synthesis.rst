Synthesis 
===============================

Hammer supports synthesizing Verilog-based RTL designs to gate-level netlists.
This action requires a tool plugin to implement ``HammerSynthesisTool``.

Synthesis Setup Keys
-------------------------------

* Namespace: ``vlsi.core``
  
    * ``synthesis_tool``
        * Module of the synthesis tool, e.g. ``hammer.synthesis.genus``

Synthesis Input Keys
-------------------------------

* Namespace: ``synthesis``

    * ``inputs.input_files`` ([])

        * A list of file paths to source files to be passed to the synthesis tool. The paths may be relative to the directory in which ``hammer-vlsi`` is called.

    * ``inputs.top_module`` (str)

        * Name of the top level verilog module of the design. 

    * ``clock_gating_mode`` (str)

        * ``auto``: turn on automatic clock gating inference in CAD tools

        * ``empty``: do not do any clock gating

Synthesis Inputs
-------------------------------

There are no prerequisites to running synthesis other than setting the keys that are described above.

.. note::
    In SledgeHammer, every DAG task runs Hammer from the directory the DAG was generated in, so relative paths in ``synthesis.inputs.input_files`` resolve against that directory wherever you run ``sledgehammer``.
    A listed file that does not exist fails the stage with ``RTL file not found`` before the tool starts.
    SledgeHammer fingerprints these files with slang 11.0 to decide whether the RTL changed, so edits to comments or whitespace alone do not rerun synthesis.
    If slang is missing, reports another version, or cannot parse the RTL, SledgeHammer hashes the file bytes instead and logs a warning.


Synthesis Outputs
------------------------------

* Mapped verilog file: ``obj_dir/syn-rundir/{TOP_MODULE}.mapped.v``
* Mapped design SDF: ``obj_dir/syn-rundir/{TOP_MODULE}.mapped.sdf``
* Synthesis output Hammer IR is contained in ``obj_dir/syn-rundir/syn-output.json``
* Synthesis reports for gates, area, and timing are output in ``obj_dir/syn-rundir/reports``

The synthesis output Hammer IR is converted to inputs for the P&R tool and the simulation tool by the Hammer ``syn-to-par`` and ``syn-to-sim`` commands, respectively.
    

Synthesis Commands
-----------------------------

* Synthesis Command

    * ``hammer-vlsi -e env.yml -p config.yml --obj_dir OBJ_DIR syn``

* Synthesis to Place-and-route

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/syn-rundir/syn-output.json --obj_dir OBJ_DIR syn-to-par``

* Synthesis to Simulation

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/syn-rundir/syn-output.json --obj_dir OBJ_DIR syn-to-sim`` 

.. note::
    In SledgeHammer, you run synthesis with ``sledgehammer syn`` from the vlsi directory where you ran ``sledgehammer buildfile``.
    It triggers the DAG that ``sledgehammer buildfile`` registered.
    From another directory, or when more than one registered DAG belongs to that directory, add ``-t <design>``, where ``<design>`` is the name the DAG was registered under (by default the basename of its ``OBJ_DIR``, such as ``gcd`` for ``build-sky130-cm/gcd``).
    A name with no registered DAG exits with ``no DAG registered for <design>`` and never falls back to the DAG of the current directory.
    You do not run ``syn-to-par`` or ``syn-to-sim`` yourself, because the DAG runs them when you ask for a later stage such as ``sledgehammer par``.
    Synthesis is skipped when its config, RTL, hooks and library collateral are unchanged since the last successful run recorded in ``OBJ_DIR/master_database.json``.
    With the PD cache on (the default under the ``sledgehammer`` launcher), a result already stored for the same inputs is restored instead of running the tool.
    Add ``--force`` to rerun synthesis anyway, or ``--local`` to run the tool instead of restoring a cached result, as described in the `CLI reference <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_.
