Simulation
===============================

Hammer supports RTL, post-synthesis, and post-P&R simulation. It provides a simple API to add flags to the simulator call and automatically passes in collateral to the simulation tool from the synthesis and place-and-route outputs.
This action requires a tool plugin to implement ``HammerSimTool``.

Simulation Setup Keys
-------------------------------

* Namespace: ``vlsi.core``

    * ``sim_tool``
        * Module of the simulation tool, e.g. ``hammer.sim.vcs``

Simulation Input Keys
-------------------------------

* Namespace: ``sim.inputs``

    * ``input_files`` ([])

        * A list of file paths to source files (verilog sources, testharness/testbench, etc.) to be passed to the simulation tool (both verilog and any other source files needed). The paths may be relative to the directory in which ``hammer-vlsi`` is called.

    * ``top_module`` (str)

        * Name of the top level module of the design.

    * ``options`` ([str])

        *  Any options that are passed into this key will appear as plain text flags in the simulator call.

    * ``defines`` ([str])

        * Specifies define options that are passed to the simulator. e.g. when using VCS, this will be added as ``+define+{DEFINE}``.

    * ``compiler_cc_opts`` ([str])

        * Specifies C compiler options when generating the simulation executable. e.g. when using VCS, each compiler_cc_opt will be added as ``-CFLAGS {compiler_cc_opt}``.

    * ``compiler_ld_opts`` ([str])

        * Specifies C linker options when generating the simulation executable. e.g. when using VCS, each compiler_ld_opt will be added as ``-LDFLAGS {compiler_ld_opt}``.

    * ``timescale`` (str)

        * Plain string that specifies the simulation timescale. e.g. when using VCS, ``sim.inputs.timescale: '1ns/10ps'`` would be passed as ``-timescale=1ns/10ps``

    * ``benchmarks`` ([str])
    
        * A list of benchmark binaries that will be run with the simulator to test the design if the testbench requires it. For example, this may be RISC-V binaries. If unspecified or left empty, the simulation will execute as normal.

    * ``parallel_runs`` (int)

        * Maximum number of simulations to run in parallel. -1 denotes all in parallel. 0 or 1 denotes serial execution.

    * ``tb_name`` (str)

        * The name of the testbench/test driver in the simulation.

    * ``tb_dut`` (str)

        * Hierarchical path to the top level instance of the "dut" from the testbench.

    * ``level`` (``"rtl"``, ``"syn"``, or ``"par"``)

        * This defines whether the simulation being run is at the RTL level or at the gate level after synthesis or place-and-route.

    * ``all_regs`` (str)

        * Path to a list of all registers in the design, typically generated from the synthesis or P&R tool. This is used in gate level simulation to initialize register values.

    * ``seq_cells`` (str)

        * Path to a list of all sequential standard cells in the design, typically generated from the synthesis or P&R tool. This is used in gate level simulation.

    * ``sdf_file`` (str)

        * Path to Standard Delay Format file used in timing annotated simulations.

    * ``gl_register_force_value`` (0 or 1)

        * Defines what value all registers will be initialized to for gate level simulations.

    * ``timing_annotated`` (false or true)

        * Setting to false means that the simulation will be entirely functional. Setting to true means that the simulation will be time annotated based on synthesis or P&R results.

    * ``saif.mode`` (``"time"``, ``"trigger"``, ``"trigger_raw"``, ``"full"``, or ``"none"``)

        * ``"time"``: pair with ``saif.start_time`` and ``saif.end_time`` (TimeValue) to dump between 2 timestamps
        * ``"trigger"``: inserts a trigger into the simulator run script
        * ``"trigger_raw"``: inserts a given start/end trigger tcl script into the simulator run script. Specify scripts with ``saif.start_trigger_raw`` and ``saif.end_trigger_raw`` (str)
        * ``"full"``: dump the full simulation
        * ``"none"``: do not dump a SAIF (the default)

    * ``execution_flags`` ([str])

        * Each string in this list will be passed as an option when actually executing the simulation executable generated from the previous arguments.
        * Can also use ``execution_flags_prepend`` and ``execution_flags_append`` for additional execution flags

    * ``execute_sim`` (true or false)

        * Determines whether the simulation executable that is generated with the above inputs will actually be executed with the given flags or if the executable will just be generated.


Simulation Inputs
-------------------------------

There are no prerequisites to running an RTL simulation other than setting the keys that are described above. Running the ``syn-to-sim`` action after running synthesis will automatically generate the Hammer IR required to pipe the synthesis outputs to the Hammer simulation tool, and should be included in the Hammer call, as demonstrated in the "Post-Synthesis Gate Level Sim" command below.  The same goes for post-place-and-route simulations. The required files for these simulations
(netlist, SDF, etc.) are generated and piped to the simulation tool in the corresponding action's outputs.

.. note::
    In SledgeHammer, every DAG task runs Hammer from the directory the DAG was generated in, so relative paths in ``sim.inputs.input_files`` resolve against that directory wherever you run ``sledgehammer``.
    The ``sim_rtl`` task passes only the project configs the DAG was generated with, so a config that a Makefile adds for one target, such as ``configs-design/<design>/sim-rtl.yml`` for ``make sim-rtl`` in ``e2e/``, applies only if it is one of those configs.
    The ``sim_syn`` and ``sim_par`` tasks take only the output of ``syn-to-sim`` or ``par-to-sim`` as their project config, so they use the ``sim.inputs`` keys from the full config that synthesis (for ``sim_syn``) or place-and-route (for ``sim_par``) wrote the last time it was not skipped.

The Hammer simulation tool will initialize register values in the simulation, as that is of particular need when simulating Chisel-based designs, to deal with issues around x-pessimism.

Simulation Outputs
-------------------------------

The simulation tool is able to output waveforms for the simulation. All of the relevant outputs of the simulation can be found in ``OBJ_DIR/sim-rundir/``.

.. note::
    In SledgeHammer, the DAG passes ``--sim_rundir`` to each simulation stage, so the outputs land in ``OBJ_DIR/sim-rtl-rundir/``, ``OBJ_DIR/sim-syn-rundir/`` or ``OBJ_DIR/sim-par-rundir/`` (``OBJ_DIR/sim-rtl-<module>/``, ``OBJ_DIR/sim-syn-<module>/`` or ``OBJ_DIR/sim-par-<module>/`` in a hierarchical flow).

Simulation Commands
-------------------------------

* RTL Simulation Command

    * ``hammer-vlsi -e env.yml -p config.yml --obj_dir OBJ_DIR sim``

* Synthesis to Sim

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/syn-rundir/syn-output.json -o OBJ_DIR/syn-to-sim_input.json --obj_dir OBJ_DIR syn-to-sim``

* Post-Synthesis Gate Level Sim

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/syn-to-sim_input.json --obj_dir OBJ_DIR sim``

* P&R to Simulation

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/par-rundir/par-output.json -o OBJ_DIR/par-to-sim_input.json --obj_dir OBJ_DIR par-to-sim``

* Post-P&R Gate Level Sim

    * ``hammer-vlsi -e env.yml -p config.yml -p OBJ_DIR/par-to-sim_input.json --obj_dir OBJ_DIR sim``

.. note::
    In SledgeHammer, you run these with ``sledgehammer sim-rtl -t <design>``, ``sledgehammer sim-syn -t <design>`` or ``sledgehammer sim-par -t <design>``, which trigger the DAG registered for that design.
    ``<design>`` is the name the DAG was registered under, by default the basename of its ``OBJ_DIR`` (such as ``gcd`` for ``build-sky130-cm/gcd``), which is not always the top module. From the vlsi directory you can leave out ``-t`` when only one registered DAG belongs to that directory.
    With ``vlsi.core.build_system: sledgehammer`` in your project config, register that DAG first by running ``sledgehammer buildfile`` from the vlsi directory (it runs ``make buildfile``, or ``make build`` when the Makefile has a ``build`` rule and no ``buildfile`` rule, as in ``e2e/``).
    If no DAG is registered for ``<design>``, the command exits with ``no DAG registered for <design>`` and runs nothing, even when the current directory has a DAG of its own.
    You do not run ``syn-to-sim`` or ``par-to-sim`` yourself: for ``sim-syn`` and ``sim-par`` the DAG first brings synthesis (and, for ``sim-par``, place-and-route) up to date, skipping each one when it is unchanged, and then runs the conversion step.
    The simulation itself has no dependency check and is not cached, so it runs again every time a run includes it.
