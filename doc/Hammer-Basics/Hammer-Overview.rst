Hammer Overview
================================

Hammer has a set of actions and automatically takes the output of one action and converts it into the input for another.  For instance, a synthesis action will output a mapped verilog file which will then automatically be piped to the place-and-route input when a place-and-route action is called.

A user's Hammer environment is typically separated into four different components: core Hammer, one or more tool plugins, a technology plugin, and a set of project-specific Hammer input files. Hammer is meant to expose a set of generalized APIs that are then implemented by tool- and technology-specific plugins.

.. note::
    In SledgeHammer, there is one more component: an Airflow stack that runs each design's flow as a DAG, with Postgres holding Airflow's run state, the PD cache of stage results and sub-step checkpoints. See `Workspace setup <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/WORKSPACE_SETUP.md>`_ to set it up.

Hammer is included in a larger project called `Chipyard <https://github.com/ucb-bar/chipyard>`__ which is the unified repo for an entire RTL, simulation, emulation, and VLSI flow from Berkeley Architecture Research. There is an in-depth Hammer demo there, and it is a great place to look at a typical Hammer setup.

Main Hammer
-------------------------------

Hammer provides the Python backend for a Hammer project and exposes a set of APIs that are typical of modern VLSI flows. These APIs are then implemented by a tool plugin and a technology plugin of the designer's choice. The structure of Hammer is meant to enable re-use and portability between technologies.

Hammer takes its inputs and serializes its state in the form of YAML and JSON files, called Hammer IR. The designer sets a variety of settings in the form of keys in different namespaces that are designated in Hammer to control its functionality. 

.. note::
    Supported keys are contained in `defaults.yml <https://github.com/ucb-bar/hammer/blob/master/hammer/config/defaults.yml>`__. This file provides sensible defaults that may be overridden or are set to null if they must be provided by the designer.

.. note::
    In SledgeHammer, ``hammer/config/defaults.yml`` also declares the keys added on top of Hammer (``vlsi.pd_cache.enabled``, ``vlsi.pd_cache.ledger_enabled``, ``vlsi.pd_cache.project``, ``vlsi.substep_resume.*``, ``vlsi.error_scan.*`` and ``vlsi.core.airflow_*``). They choose how a stage runs, not what it produces, so the dependency check and the PD cache key leave them out. `ENVIRONMENT_VARIABLES.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/ENVIRONMENT_VARIABLES.md>`_ describes most of them.

Here is an example of a snippet that would be included in the user's input configuration.

.. _library-example:
.. code-block:: yaml

    vlsi.core.technology: "hammer.technology.asap7"
    vlsi.inputs.supplies:
        VDD: "0.7 V"
        GND: "0 V"

This demonstrates two different namespaces, ``vlsi.core`` and ``vlsi.inputs``, and then two different keys, ``technology`` and ``supplies``, which are set to the ``asap7`` technology and 0.7 Volts supply voltage, respectively.

Further details about these keys and how they are manipulated are found in the :ref:`config` section.

Tech Plugins
-------------------------------

A technology plugin consists of a ``defaults.yml`` and a technology configuration: either a ``*.tech.json`` (or ``*.tech.yml``) file, or one the plugin builds in Python in its ``gen_config()`` method, as ``sky130`` does.

The technology configuration contains pointers to relevant PDK files and fundamental technology constants.  These values are not meant to be overridden. As a last resort, ``vlsi.technology.manually_override_pdk_collateral: true`` with a list of replacement files in ``vlsi.technology.override_libraries`` replaces individual PDK files (see the ``sky130`` ``defaults.yml``).

``defaults.yml`` sets default technology variables for Hammer to consume, which may be specific to this technology or generic to all. These values may be overridden by design-specific configurations. An example of this is shown in the open-source technology plugins in ``hammer/technology/``, such as ``asap7``, and how to set up a technology plugin is documented in more detail in the :ref:`technology` section.

.. note:: Unless you are a UCB BAR or BWRC affiliate or have set up a 3-way technology NDA with us, we cannot share pre-built proprietary technology plugin repositories.

Tool Plugins
-------------------------------

A Hammer tool plugin actually implements tool-specific steps of the VLSI flow in Hammer in a template-like fashion.
The TCL commands input to the tool are created using technology and design settings provided by the designer.


The Cadence and Synopsys tool plugins are included in ``hammer/`` under their respective actions (for example ``hammer/synthesis/genus``, ``hammer/par/innovus`` and ``hammer/sim/vcs``). The plugins for Mentor (now Siemens) tools, such as Calibre DRC and LVS, are in a separate repository, ``hammer-mentor-plugins``, which users must request access to.

.. _plugins-access:
.. note:: If you are not a UCB BAR or BWRC affiliate and have access to Mentor Graphics (now Siemens) tools, please email hammer-plugins-access@lists.berkeley.edu with a request for access to the hammer-mentor-plugins repository. MAKE SURE TO INCLUDE YOUR GITHUB ID IN YOUR EMAIL AND YOUR ASSOCIATION TO SHOW YOU HAVE LICENSED ACCESS TO THE TOOLS. There will be no support guarantee for the plugin repositories, but users are encouraged to file issues and contribute patches where needed.

There are also a set of open-source tools (e.g. Yosys, OpenROAD, Magic, Netgen) provided in ``hammer/`` under their respective actions.

These plugins implement many of the common steps of a modern physical design flow. However, a real chip flow will require many custom settings and steps that may not be generalizable across technology nodes.
Because of this, Hammer has an "escape-hatch" mechanism, called a hook, that allows the designer to inject custom steps between the default steps provided by the CAD tool plugin.
Hooks are python methods that emit TCL code and may be inserted before or after an existing step or replace the step entirely.
This allows the designer to leverage the APIs built into Hammer while easily inserting custom steps into the flow.
Hooks are discussed in more detail in the :ref:`hooks` section.

Calling Hammer
-------------------------------

To use Hammer on the command line, the designer will invoke the ``hammer-vlsi`` utility.
This calls the ``main()`` method of the ``CLIDriver`` class, which exits with the action's return code. An example invocation is below:

.. _call-example:
.. code-block:: bash

    hammer-vlsi -e env.yml -p config.yml --obj_dir build par

Using hooks requires the designer to extend the ``CLIDriver`` class. A good example exists in the `Chipyard <https://github.com/ucb-bar/chipyard>`__ repository (``chipyard/vlsi/example-vlsi``). This would change the invocation to something like the following:

.. _call-example-hooks:
.. code-block:: bash

    example-vlsi -e env.yml -p config.yml --obj_dir build par

.. note::
    In SledgeHammer, you run stages with the ``sledgehammer`` command instead of calling the driver for each one.
    With ``vlsi.core.build_system: sledgehammer`` in your project config, the ``build`` action writes an Airflow DAG next to ``hammer.d`` and links it into the Airflow dags folder.
    You run that action with ``sledgehammer buildfile`` from the vlsi directory (or ``e2e/``): underneath, it runs ``make buildfile`` (``make build`` in ``e2e/``) with the ``sledgehammer`` venv's Python first on ``PATH``, so you don't activate a venv, then prints the registered DAG id and the command to run next.
    With the stack running (``sledgehammer`` with no arguments starts it), ``sledgehammer par`` or ``sledgehammer run syn par`` runs stages through that DAG, and each task runs the driver that ran ``build`` with the flags described below.
    See the `command reference <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md>`_.

Hammer configuration files must be in YML or JSON format, 
and are divided into environment and project configurations.
The environment configuration file, which in this case is ``env.yml``, is passed to Hammer using the ``-e`` flag.
``env.yml`` contains pointers to the required tool licenses and environment variables. 
The project configuration file is passed in using the ``-p`` flag.
In this case, there is only one, ``config.yml``, and it needs to set all the required keys for the step of the flow being run.
Passing in multiple files looks like ``-p config1.yml -p config2.yml``. Refer to the :ref:`config` section for the implications of multiple config files.
Project configurations take precedence over the environment settings, and environment settings are not propagated to the output configuration files after each action.
The order of precedence for the project configs reads from right to left (i.e. each file overrides all files to its left in the command line).


``--obj_dir build`` designates what directory Hammer should use as a working directory.
All default action run directories and output files will be placed here.

Finally, ``par`` designates that this is a place-and-route action.

In this case, Hammer will write outputs to the path ``$PWD/build/par-rundir``.

.. note::
    In SledgeHammer, every tool action first fingerprints the RTL in ``synthesis.inputs.input_files``, so those files must exist even for ``par``.
    ``syn``, ``par``, ``drc`` and ``lvs`` then compare the merged configuration (environment settings included) and the RTL, hook and library collateral fingerprints with ``build/master_database.json``: if nothing the stage depends on has changed, the action prints ``Database unchanged, can skip par`` and exits 0 without running the tool, unless you pass ``--force``.
    After a failed Genus or Innovus run, the next ``syn`` or ``par`` with unchanged inputs resumes from the newest checkpoint the tool confirmed writing, and with the PD cache on (the default under the ``sledgehammer`` launcher) a matching stored result is restored instead of running the tool.
    See `SUBSTEP_RESUME.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/SUBSTEP_RESUME.md>`_ and `PD_STORE_README.md <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/PD_STORE_README.md>`_.

For the full list of Hammer command-line arguments, run ``hammer-vlsi --help`` or take a peek in the ``hammer/vlsi/cli_driver.py`` file.

Summary
-------

The software architecture as described above is shown in the diagram below, which is taken from `the Hammer DAC paper <https://dl.acm.org/doi/abs/10.1145/3489517.3530672>`_.

.. image:: arch.png
    :width: 600
