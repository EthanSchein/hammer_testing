Formal Verification
===============================

Hammer supports post-synthesis and post-P&R formal verification. It provides a simple API to provide a set of reference and implementation inputs (e.g. Verilog netlists) to perform formal verification checks such as logical equivalence checking (LEC).
This action requires a tool plugin to implement ``HammerFormalTool``.

Formal Verification Setup Keys
-------------------------------

* Namespace: ``vlsi.core``

    * ``formal_tool``
        * Python module of the formal verification tool e.g. ``hammer.formal.conformal``

Formal Verification Input Keys
-------------------------------

* Namespace: ``formal.inputs``

    * ``check`` (str)

        * Name of the formal verification check that is to be performed. Support varies based on the specific tool plugin. Potential check types/algorithms could be "lec", "power", "constraint", "cdc", "property", "eco", and more. At the moment, only "lec" is supported.

    * ``input_files`` ([])

        * A list of file paths to implementation source files (verilog, vhdl, spice, liberty, etc.) to be passed to the formal verification tool. For a LEC tool, this would be the sources for the "revised" design. The paths may be relative to the directory in which ``hammer-vlsi`` is called.

    * ``reference_files`` ([])

        * A list of file paths to reference source files (verilog, vhdl, spice, liberty, etc.) to be passed to the formal verification tool. For a LEC tool, this would be the sources for the "golden" design. The paths may be relative to the directory in which ``hammer-vlsi`` is called.

    * ``top_module`` (str)

        * Name of the top level module of the design to be verified.

.. note::
    In SledgeHammer, every DAG task runs Hammer from the directory the DAG was generated in, so relative paths in ``input_files`` and ``reference_files`` resolve against that directory wherever you run ``sledgehammer``.

Formal Verification Inputs
-------------------------------

Running the ``syn-to-formal`` action after running synthesis will automatically generate the Hammer IR required to pipe the synthesis outputs to the Hammer formal verification tool, and should be included in the Hammer call, as demonstrated in the "Post-Synthesis Formal Verification" command below.  The same goes for post-place-and-route formal verification.

At this time, only design files are passed to the formal verification tool: the RTL or a netlist as the reference, and a netlist as the implementation. Additional files (SDCs, CPFs, etc.) are needed for more advanced formal verification checks but are not yet currently supported. Similarly, formal verification on the behavioral RTL is also not yet supported.

Formal Verification Outputs
-------------------------------

The formal verification tool produces reports in ``OBJ_DIR/formal-rundir/``. Outputs from formal verification flows such as engineering change order (ECO) patches are not yet supported.

.. note::
    In SledgeHammer, the DAG passes ``--formal_rundir`` to each formal stage, so the reports land in ``OBJ_DIR/formal-syn-rundir/`` or ``OBJ_DIR/formal-par-rundir/`` (``OBJ_DIR/formal-syn-<module>/`` or ``OBJ_DIR/formal-par-<module>/`` in a hierarchical flow).

Formal Verification Commands
-------------------------------

* Synthesis to Formal Verification

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/syn-rundir/syn-output.json -o OBJ_DIR/syn-to-formal_input.json --obj_dir OBJ_DIR syn-to-formal``

* Post-Synthesis Formal Verification

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/syn-to-formal_input.json --obj_dir OBJ_DIR formal``

* P&R to Formal Verification 

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/par-rundir/par-output.json -o OBJ_DIR/par-to-formal_input.json --obj_dir OBJ_DIR par-to-formal``

* Post-P&R Formal Verification

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/par-to-formal_input.json --obj_dir OBJ_DIR formal``

.. note::
    In SledgeHammer, run ``sledgehammer formal-syn -t <design>`` or ``sledgehammer formal-par -t <design>`` instead of these commands.
    ``<design>`` is the name the DAG is registered under: by default the basename of its ``OBJ_DIR`` (such as ``gcd`` for ``build-sky130-cm/gcd``), not necessarily the top module.
    Register the DAG first by running ``sledgehammer buildfile`` from the vlsi directory, which prints the DAG it registered as ``sledgehammer_<design>_<user>``.
    A name with no registered DAG exits with ``no DAG registered for <design>`` and never falls back to the DAG of the directory you run it from.
    The DAG first runs the stages that formal verification depends on (each is skipped when unchanged), then ``syn-to-formal`` or ``par-to-formal``, so you do not run the conversion step yourself.
    Formal verification itself has no dependency check and is not cached, so it runs again every time a run includes it.
    See `CLI_REFERENCE.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_.
