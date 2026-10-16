.. _flow-control:

Flow Control
=======================================

Physical design is necessarily an iterative process, and designers will often require fine control of the flow within any given action.
This allows for rapid testing of new changes in the Hammer IR to improve the quality of results (QoR) after certain steps.

Given the flow defined by tool steps and hooks (described in the next section), users can select ranges of these to run.
The tool script will then be generated with commands corresponding to only the steps that are to be run.

Command-line Interface
----------------------

Flow control is specified with the following optional Hammer command-line flags, which must always target a valid step/hook:

* ``--start_before_step <target>``: this starts the tool from (inclusive) the target step. Alternate flag: ``--from_step``
* ``--start_after_step <target>``: this starts the tool from (exclusive) the target step. Alternate flag: ``--after_step``
* ``--stop_after_step``: this stops the tool at (inclusive) the target step. Alternate flag: ``--to_step``
* ``--stop_before_step``: this stops the tool at (exclusive) the target step. Alternate flag: ``--until_step``
* ``--only_step``: this only runs the target step

.. note::
    In SledgeHammer, ``sledgehammer run`` and ``sledgehammer <stage>`` accept only ``--start_before_step`` (``--from_step``), ``--stop_after_step`` (``--to_step``) and ``--only_step``.
    The flags apply to one stage, ``syn`` by default, and are not passed to any other stage in the run, so a par step flag needs ``--steps-stage par``.

As ``hammer-vlsi`` is parsing through the steps for a given tool, it will print debugging information indicating which steps are skipped and which are run.

.. note::
    In SledgeHammer, a step flag makes the chosen stage run even when its inputs have not changed, and its result is neither restored from nor stored in the PD cache.
    Starting from any step but the first needs that step's ``pre_<step>`` checkpoint in the run directory or in the SledgeHammer database, which is fetched automatically; if there is none, the run fails and lists the checkpoints that exist.
    A Genus, Innovus or OpenROAD run whose steps stop before the tool writes its final outputs (``write_outputs`` in synthesis, ``write_design`` in place-and-route) is not committed and exits with an error, so the DAG reports it as failed.
    With Genus or Innovus, a plain rerun without step flags then resumes from the newest confirmed checkpoint if the inputs have not changed, as it does after a failed syn or par run (see `SledgeHammer sub-step resume <https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/SUBSTEP_RESUME.md>`_).

Certain combinations are not supported:

* ``--only_step`` is not compatible with any of the other flags
* ``--start_before_step`` and ``--start_after_step`` may not be specified together
* ``--stop_after_step`` and ``--stop_before_step`` may not be specified together
* ``--start_before_step`` and ``--stop_before_step`` may not be the same step
* ``--start_after_step`` and ``--stop_after_step`` may not be the same step
* ``--start_after_step`` and ``--stop_before_step`` may not be the same or adjacent steps

Hammer does not reject these combinations.
In the first three, the flag named first takes precedence; the last three run nothing.

Logically, a target of ``stop_{before|after}_step`` before ``start_{before|after}_step`` will also not run anything (though it is not explicitly checked).
