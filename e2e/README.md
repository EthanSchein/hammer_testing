# Hammer End-to-End Integration Tests

The `hammer/e2e` folder contains an end-to-end (RTL -> GDS) smoketest flow using Hammer, either the Cadence or OpenROAD toolchain, and the ASAP7 or Skywater 130 PDKs.

## Setup

First, follow the [Hammer Power User Setup](https://hammer-vlsi.readthedocs.io/en/stable/Hammer-Basics/Hammer-Setup.html#power-user-setup) to clone Hammer and install/activate the virtual environment.

> **SledgeHammer:** That page sets up upstream Hammer. To set up this checkout instead, run `./scripts/uv_setup.sh` from its root (see [WORKSPACE_SETUP.md](https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/WORKSPACE_SETUP.md)). `sledgehammer buildfile`, run from `e2e/`, needs no activated venv: it puts the venv's python first on `PATH` before it runs `make build`. Run `source venv.sh` in the checkout root only before you run the `make` targets in `e2e/` yourself.

## Overview


### Flow Selection

The following variables in the Makefile select the target flow to run:

- `design`: RTL name
    - {`pass`, `gcd`, `sram_wrapper_sky130`}
- `pdk`: PDK name
    - {`sky130`, `asap7`}
- `tools`: CAD tool flow
    - {`cm` (commercial), `cm_plus` (commercial, like `cm` but with Innovus+ synthesis and Siemens Calibre DRC and LVS, which need the `hammer-mentor-plugins` submodule), `or` (OpenROAD)}
- `env`: compute environment
    - {`bwrc` (BWRC), `a` (Millennium), `inst` (instructional machines)}

The outputs of the flow by default reside in `OBJ_DIR=build-<pdk>-<tools>/<design>/`

### Configs

The Hammer configuration files consist of environment (`ENV_YML`) and project (`PROJ_YMLS`) configurations.
The project configs take precedence over the environment configs.
The order of precedence for the project configs reads from right to left (i.e. each file overrides all files to its left).
All configuration files are summarized below.

```shell
#         lowest precedence -------------------------------------------> highest precedence
PROJ_YMLS ?= $(PDK_CONF) $(TOOLS_CONF) $(DESIGN_CONF) $(DESIGN_PDK_CONF) $(SIM_CONF) $(POWER_CONF) $(extra)
```

- `ENV_YML`: Environment configs that specify CAD tool license servers and paths are in `configs-env`.
  Any project config that sets the same key overrides it
- `PDK_CONF`: PDK configs shared across all runs with this PDK are in `configs-pdk`
- `TOOLS_CONF`: Tool configs to select which CAD tool flow to use are in `configs-tool`
- Design-specific configs are located in `configs-design/<design>`, and are summarized below:
    - `DESIGN_CONF`: the common design config (design input files, anything else design-specific)
    - `DESIGN_PDK_CONF`: PDK-specific configs for this particular design (clock, placement, pin constraints)
    - `SIM_CONF`: Simulation configs for post-RTL, Synthesis, or PnR simulation
    - `POWER_CONF`: Power simulation configs for post-RTL, Synthesis, or PnR simulation
      (NOTE: The Makefile expects the power config filename for each simulation level + PDK to be in the format `power-{rtl,syn,par}-<pdk>.yml`,
      while the `joules.yml` and `voltus.yml` files serve as templates for the Cadence Joules/Voltus power tools)


## Run the Flow

First, use Hammer to construct a Makefile fragment with targets for all parts of the RTL -> GDS flow.
Specify the appropriate `env/tools/pdk/design` variables to select which configs will be used.

```shell
make build
# same as: `make env=bwrc tools=cm pdk=sky130 design=gcd build`
```

Hammer will generate a Makefile fragment in `OBJ_DIR/hammer.d`.

> **SledgeHammer:** The e2e tool and design configs set `vlsi.core.build_system: make`, so `make build` writes only `hammer.d` and registers no DAG for `sledgehammer` to run. Run `sledgehammer buildfile extra=sledge.yml` from `e2e/` instead, with the `sledge.yml` shown under Flow Customization: it runs `make build` with that config, which also writes `OBJ_DIR/hammer_dag.py` and links it into the dags folder, and then prints the DAG id it registered. Then, with the stack running (`sledgehammer`), run `sledgehammer syn`, `sledgehammer par`, `sledgehammer drc` and `sledgehammer lvs` from `e2e/`. That works while only one of your registered DAGs comes from `e2e/`. Once you have also generated the DAG for a second design, such as `pass`, they stop and ask for `-t`, so add `-t gcd` (the design name), which also runs them from any directory. The DAG id is `sledgehammer_<design>_<user>` whatever `pdk` and `tools` are, so `build-sky130-cm/gcd` and `build-asap7-cm/gcd` share one and only the build you generated last is registered. The build prints a NOTE when it takes the id over from the other one, and a run with `--obj_dir` set to the other build refuses until you regenerate the DAG from that build (delete its `hammer.d` first, as Flow Customization explains). See [CLI_REFERENCE.md](https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/CLI_REFERENCE.md).

Then run the rest of the flow, making sure to set the `env/tools/pdk/design` variables as needed:

```shell
make sim-rtl
make power-rtl

make syn
make sim-syn
make power-syn

make par
make sim-par
make power-par

make drc
make lvs
```

> **SledgeHammer:** The default design, `gcd`, has no `power-par-sky130.yml`, and its `power-syn-sky130.yml` sets no power tool, so `make power-syn` runs the Voltus plugin that `cm.yml` selects, which supports only post-PnR analysis. Run `make power-syn` and `make power-par` with `design=pass`.

These actions are summarized in more detail:

- SRAM generation
    - `make srams`
    - Generated SRAM configs in `OBJ_DIR/sram_generator-output.json` (empty unless `vlsi.inputs.sram_parameters` is specified)
- RTL simulation
    - `make sim-rtl`
    - Generated waveform in `OBJ_DIR/sim-rtl-rundir/output.fsdb`
- Post-RTL Power simulation
    - `make sim-rtl-to-power`
    - `make power-rtl`
    - Generated power reports in `OBJ_DIR/power-rtl-rundir/reports`
- Synthesis
    - `make syn`
    - Gate-level netlist in `OBJ_DIR/syn-rundir/<design>.mapped.v`
- Post-Synthesis simulation
    - `make syn-to-sim`
    - `make sim-syn`
    - Generated waveform and register forcing ucli script in `OBJ_DIR/sim-syn-rundir`
- Post-Synthesis Power simulation
    - `make syn-to-power`
    - `make sim-syn-to-power`
    - `make power-syn`
    - Generated power reports in `OBJ_DIR/power-syn-rundir/reports`
- PnR
    - `make syn-to-par`
    - `make par`
    - LVS netlist (`<design>.lvs.v`) and GDS (`<design>.gds`) in `OBJ_DIR/par-rundir`
- Post-PnR simulation
    - `make par-to-sim`
    - `make sim-par`
- Post-PnR Power simulation
    - `make par-to-power`
    - `make sim-par-to-power`
    - `make power-par`
    - Generated power reports in `OBJ_DIR/power-par-rundir`

### Flow Customization

If at any point you would like to use custom config files (that will override any previous configs), assign the `extra` Make variable to a space-separated list of these files.
For example, to also generate the SledgeHammer DAG, put `vlsi.core.build_system: sledgehammer` in a file such as `sledge.yml` in `e2e/`,
then run the following from `e2e/` (it runs `make extra="sledge.yml" build`):

```shell
sledgehammer buildfile extra="sledge.yml"
```

`make build` does nothing while `OBJ_DIR/hammer.d` is newer than `OBJ_DIR/sram_generator-output.json`, and neither does `sledgehammer buildfile`, so delete `hammer.d` first when you rebuild with different configs.
That includes the first `sledgehammer buildfile` after a plain `make build`. Skip the delete there and nothing is regenerated: `sledgehammer buildfile` then says the build registered no DAG and asks you to check `vlsi.core.build_system`, or, if another of your DAGs already comes from `e2e/`, lists that DAG as registered.

To use the [Hammer step flow control](https://hammer-vlsi.readthedocs.io/en/stable/Hammer-Use/Flow-Control.html), prepend `redo-` to any VLSI flow action,
then assign the `args` Make variable to the appropriate Hammer command line args.
For example, to only run the `report_power` step of the `power-rtl` action (i.e. bypass synthesis), run the following:

```shell
make args="--only_step report_power" redo-power-rtl
```

> **SledgeHammer:** `syn`, `par`, `drc` and `lvs` skip when their inputs have not changed, and the `redo-` targets do not force them, so add `--force` to `args` to rerun one (step flags also force `syn` and `par`). Through the DAG, `sledgehammer redo-syn` forces `syn`, and `sledgehammer syn --from_step <step>` passes a step flag (add `--steps-stage par` for `par`). [SUBSTEP_RESUME.md](https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/SUBSTEP_RESUME.md) explains which checkpoints `--from_step` and `--only_step` need and what happens to a run that stops early.

## Custom Setups

If you're not using a Berkeley EECS compute node, you can create your own environment setup.

- Create an environment config similar to those in `configs-env` for your node to specify the CAD tool license servers and paths/versions of CAD tools and the PDK, and set the `ENV_YML` variable to this file.
- The rest of the flow should be identical

### ASAP7 Install

Clone the [asap7 repo](https://github.com/The-OpenROAD-Project/asap7) somewhere and reference the path in your `ENV_YML` config.

### Sky130 Install

Refer to the [Hammer Sky130 plugin README](https://github.com/lawrencejrhee/hammer/blob/master/hammer/technology/sky130/README.md)
to install the Sky130 PDK, then reference the path in your `ENV_YML` config (the `technology.sky130.sky130A` key is required, and the Pegasus DRC and LVS that `cm` selects also need `technology.sky130.sky130_cds`).
The `sram_wrapper_sky130` design also needs the Sram22 macros in `technology.sky130.sram22_sky130_macros`.

## Pipecleaning

We have provided several reference designs of increasing complexity to aid with pipecleaning your Hammer VLSI flow setup. These are summarized as follows, in order of complexity:

- `pass`: single register
- `gcd`: computes greatest common divisor, good for testing more complex logic
- `sram_wrapper_sky130`: example wrapper for an SRAM instance in Sky130, useful for ensuring correct SRAM integration into flow
    - wraps any Sky130 SRAM instance available in `hammer/hammer/technology/sky130/sram-cache.json`
    - SRAM instance specified via defines in `configs-design/sram_wrapper_sky130/common.yml`
    - all SRAM configurations are loaded via these config fragments in `configs-design/sram_wrapper_sky130/sky130.yml`:
        - ```yaml
            # Compile all srams in our cache
            vlsi.inputs.sram_parameters: "../../../hammer/technology/sky130/sram-cache.json"
            vlsi.inputs.sram_parameters_meta: ["prependlocal", "transclude", "json2list"]
            ```



