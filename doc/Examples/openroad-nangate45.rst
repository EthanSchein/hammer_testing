OpenROAD and nangate45
=======================================

The `OpenROAD-flow <https://github.com/The-OpenROAD-Project/OpenROAD-flow>`__ is an open-source, technology-independent VLSI toolchain. As of this writing, it has the ability to run synthesis, place-and-route, and drc through various open-source tools. 

The OpenROAD-flow repository contains the nangate45 example pdk, which you might find useful for high-level architectural exploration.

Hammer has the ability to target the OpenROAD toolchain and its nangate45 pdk. So you can now simply push a button in `Chipyard <https://github.com/ucb-bar/chipyard>`__ to go from your Chisel design to a somewhat reasonable gds at an example 45-nm node.

Instructions
---------------------------
In addition to following the chipyard and hammer setup instructions, you must also set up OpenROAD-flow before using it. The steps to run the default RocketConfig through OpenROAD's drc are:

1) install OpenROAD-flow (follow the repo's instructions) on your machine
2) ``cd /work/OpenROAD-flow && source setup_env.sh``
3) ``cd /work/chipyard/vlsi && make tech_name=nangate45 drc``

This assumes you already have chipyard's ``env.sh`` and then hammer's ``venv.sh`` sourced into your shell.

.. note::
   In SledgeHammer, DAG tasks inherit the environment of the shell that launches the stack with ``sledgehammer`` (or of your edge worker), not of the shell you trigger runs from, so source ``setup_env.sh`` before you launch the stack or the worker. With ``vlsi.core.build_system: sledgehammer`` set in your project config, step 3 becomes ``sledgehammer buildfile tech_name=nangate45`` from ``/work/chipyard/vlsi`` (it runs ``make buildfile tech_name=nangate45`` with the SledgeHammer venv's python first on ``PATH``, so you do not need to activate a venv, and that registers the DAG), followed by ``sledgehammer drc`` in the same directory. See `CLI_REFERENCE.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_.
