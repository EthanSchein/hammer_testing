# Introduction with Sky130 and OpenROAD

The following directions will get a simple ``pass`` design from RTL to GDS using the [OpenROAD tools](https://theopenroadproject.org) and the [Skywater 130nm PDK](https://github.com/google/skywater-pdk). These directions are meant to provide the minimal set of steps to do so, please reference the next section, [Hammer End-to-End Integration Tests](e2e.md), for more detailed descriptions of all files and commands.

## Prerequisites Setup

First, follow the [Hammer Power User Setup](https://hammer-vlsi.readthedocs.io/en/stable/Hammer-Basics/Hammer-Setup.html#power-user-setup) to clone Hammer and install/activate the virtual environment.

> **SledgeHammer:** The Power User Setup clones upstream Hammer, which has no `sledgehammer` CLI.
> Instead, run `./scripts/uv_setup.sh` from the root of your SledgeHammer checkout (see [Getting set up](https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md#getting-set-up)).
> `sledgehammer buildfile` (below) needs no activated venv, since it puts the venv's `python3` first on `PATH` for the `make` it runs.
> To run this page's `make` commands directly, run `source .venv/bin/activate` in the checkout first, because `example-driver` runs the first `python3` on your `PATH`.
> `SLEDGE_LAB=1 ./scripts/uv_setup.sh` installs Hammer alone, without Airflow or the Postgres driver: enough for the `make` commands on this page, but not for `sledgehammer`.

Next, run the setup script to install the OpenROAD tools and Skywater 130nm PDK using Conda.
You should supply a ``PREFIX`` path to a directory that will serve as the root of all PDK files and supporting tools (total size of all files is ~4GB),
otherwise the script will default to installing to your home directory (``~/``).

```shell
cd hammer/e2e
./scripts/setup-sky130-openroad.sh [PREFIX]
```

## Running Hammer

You should now have a file ``configs-env/my-env.yml`` containing all required tool and technology paths for this tutorial.
To point to your custom environment setup, set the Make variable ``env=my``.
Additionally, we set the ``design``, ``pdk``, and ``tools`` Make variables to the appropriate RTL design, PDK, and tools flow, respectively.
Now simply run the VLSI flow:

```shell
make design=pass pdk=sky130 tools=or env=my build
make design=pass pdk=sky130 tools=or env=my syn
make design=pass pdk=sky130 tools=or env=my par
make design=pass pdk=sky130 tools=or env=my drc
make design=pass pdk=sky130 tools=or env=my lvs
```

> **SledgeHammer:** To run these stages with `sledgehammer`, put `vlsi.core.build_system: sledgehammer` in a YAML file, since the e2e configs set it to `make`, which writes only `hammer.d`.
> Then, from `e2e/`, run `sledgehammer buildfile design=pass pdk=sky130 tools=or env=my extra=<that file>` in place of the `build` line (it runs `make build` there).
> It writes `build-sky130-or/pass/hammer_dag.py` as well as `hammer.d` and links the DAG into the dags folder; if `hammer.d` already exists, delete it first, or `sledgehammer buildfile` writes no DAG.
> With the stack running (start it with `sledgehammer`), run `sledgehammer run syn par drc lvs -t pass` in place of the four stage commands.
> The [SledgeHammer command reference](https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md) covers the other flags.

After Place-and-Route completes, the final database can be opened in an interactive OpenROAD session. Hammer generates a convenient script to launch these sessions:
```shell
cd ./build-sky130-or/pass/par-rundir
./generated-scripts/open_chip
```

After DRC and LVS complete, the final results may be viewed with the following scripts:

```shell
# View DRC results:
cd ./build-sky130-or/pass/drc-rundir
./generated-scripts/view_drc

# View LVS results:
cd ./build-sky130-or/pass/lvs-rundir
./generated-scripts/view_lvs
```

Congrats, you've just run your ``pass`` design from RTL to GDS with Hammer!

## Next Steps

At this point, you should go through the [Hammer End-to-End Integration Tests](e2e.md) documentation to learn how to configure Hammer further. We've also outlined some common next steps below.


### Commercial flow

This flow may be run with commercial tools by setting the Make variable ``tools=cm``, which selects the tool plugins via ``configs-tool/cm.yml``.
You will need to create a custom environment YAML file with your environment configuration, in ``configs-env/<my_custom>-env.yml``.
As a minimum, you must specify YAML keys below for the CAD tool licenses and versions/paths of the tools you are using,
see examples in ``configs-env/``.

```yaml
    # Commercial tool licenses/paths
    mentor.mentor_home: "" # Base path to where Mentor tools are installed
    mentor.MGLS_LICENSE_FILE: "" # Mentor license server/file
    
    cadence.cadence_home: "" # Base path to where Cadence tools are installed
    cadence.CDS_LIC_FILE: "" # Cadence license server/file
    
    synopsys.synopsys_home: "" # Base path to where Synopsys tools are installed
    synopsys.SNPSLMD_LICENSE_FILE: "" # Synopsys license server/files
    synopsys.MGLS_LICENSE_FILE: ""
    
    # Commercial tool versions/paths
    sim.vcs.version: ""
    synthesis.genus.version: "" # NOTE: for genus/innovus/joules, must specify binary path if version < 221
    par.innovus.version: ""
    power.joules.joules_bin: ""
```

Now re-run a similar flow as before, but pointing to your environment and the commercial tool plugins:

```shell
make design=pass pdk=sky130 tools=cm env=<my_custom> build
```
    

> **SledgeHammer:** To register this build's DAG, run `sledgehammer buildfile design=pass pdk=sky130 tools=cm env=<my_custom> extra=<file>` from `e2e/`, with the same `extra` file as the OpenROAD run.
> `build-sky130-cm/pass` and `build-sky130-or/pass` share one DAG id, `sledgehammer_pass_<user>`, and only the build generated last is registered.
> The commercial build prints a `NOTE` when it replaces the OpenROAD build's link, and `-t pass` then runs the commercial build.
> A run with `--obj_dir build-sky130-or/pass`, such as `sledgehammer run syn --obj_dir build-sky130-or/pass`, refuses until you register that build again: delete `build-sky130-or/pass/hammer.d`, then rerun the OpenROAD `sledgehammer buildfile` command.

Running DRC/LVS with commercial tools requires access to an NDA-version of the Skywater PDK.
We support running DRC/LVS with either Cadence Pegasus or Siemens Calibre.
See the [Sky130 documentation](../Technology/Sky130.md) for how to point to the NDA PDKs and run DRC/LVS with their respective tools.

> **SledgeHammer:** `configs-tool/cm.yml` selects Cadence Pegasus for both DRC and LVS.
> Pegasus reads its decks from the Cadence PDK at `technology.sky130.sky130_cds`, not from the NDA PDK, so add that key to your environment YAML.

### Chipyard flow

Follow [these directions in the Chipyard docs](https://chipyard.readthedocs.io/en/latest/VLSI/Sky130-OpenROAD-Tutorial.html) to build your own Chisel SoC design with OpenROAD and Sky130.

> **SledgeHammer:** To run that flow through SledgeHammer, set up SledgeHammer in your Chipyard tree as [Workspace setup](https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/WORKSPACE_SETUP.md) describes ([Chipyard integration](https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/setup-sledgehammer-chipyard.md) covers the conda and venv layering Chipyard needs).
> With `vlsi.core.build_system: sledgehammer` in your configs, run `sledgehammer buildfile` from the vlsi directory, with the variables you would give `make`, to register the DAG (it runs `make buildfile` with them), and `sledgehammer <stage>` replaces each `make <stage>`.
