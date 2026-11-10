# EECS151 projects as SledgeHammer designs

A finished EECS151 ASIC project already knows how to build itself: its
Makefile and Hammer YAML name the RTL, the top module, the clock, the
floorplan and the SRAMs. Registration reads that and turns each project into
an ordinary SledgeHammer design, the same kind as `gcd`, so it runs through
the DAG (syn, par, and from par the `par_to_eco` → `eco` branch) from the
Airflow UI.

```text
src/original/<project>/        the project as handed in; only ever read
        │  make designs  (studio register-projects)
        ▼
e2e/configs-design/<project>/  the design: what the e2e Makefile and make-dag read
dags/sledgehammer_<project>_<user>.py  →  the DAG Airflow lists
```

## Running it

```bash
cd e2e
make designs                          # every project under ../src/original, and its modified copies
make designs project=gatelevelgurus   # just one (with its copies)
```

`make designs` runs `studio register-projects`, which registers each project
and then runs `studio make-dag` for it. Within a few minutes Airflow's
dag-processor lists the design as `sledgehammer_<project>_<user>`; trigger it
from there like any other design. The Make flow works too:
`make syn design=gatelevelgurus`. It runs the same commands with the same
configs as the DAG (the e2e Makefile adds only `extra=` and `args=` for a
design with `registration.json`), so stages either flow ran stay cached in the
other.

Each project registers on its own. One that cannot become a valid design is
reported by name with the reason, the others still register, and the command
exits non-zero. Running it again with nothing changed rewrites nothing.

## What registration reads

The project's own configuration is the source of truth.

| What | Where it comes from |
|---|---|
| Which configs | the Makefile's `TECH_CONF`, `DESIGN_SYN_CONF`, `DESIGN_PAR_CONF` (the skeleton's `sky130.yml`, `syn.yml`, `par.yml` when it does not set them) |
| RTL, in order | `synthesis.inputs.input_files`, exactly as listed; Genus reads them in this order. A file the list leaves out (testbenches, alternatives, copies) is never read |
| Top module | `synthesis.inputs.top_module`. Never guessed: without it registration stops and lists the modules nothing instantiates |
| Defines, include dirs | `synthesis.inputs.defines`, `synthesis.inputs.include_dirs`, plus the directory of every `` `include `` the RTL reaches |
| Clock, delays, floorplan, pins, power straps, corners, stdcell library | the matching `vlsi.*`, `par.*` and `technology.*` settings |
| SRAMs | the modules the elaborated RTL instantiates but does not define (below) |

The RTL is elaborated from the top module with slang (the binary the RTL
fingerprint already uses), with the configured defines, so `` `ifdef``
branches, parameters and generate blocks resolve the way synthesis sees them.
Registration fails when:

- an input file is missing, listed twice, or outside the project;
- slang cannot elaborate the design (duplicate modules, a missing package or
  `` `include``, the top module not found);
- a package comes after a file that uses it in `input_files`;
- the design instantiates a module that no input defines and that is not an
  SRAM in Hammer's catalog;
- a hardmacro placement constraint names an instance that is not an SRAM in
  the design (Innovus would stop at `place_inst`);
- a setting it would carry over holds an absolute path, or a meta directive it
  cannot translate.

## What it writes

Everything lands in `e2e/configs-design/<project>/`, which git ignores:

| File | What it holds |
|---|---|
| `common.yml` | from the syn and par configs: top module, RTL (through `prependlocal` paths relative to this file, so it works from any checkout), clocks, delays, placement, pins |
| `sky130.yml` | from the tech config, plus `vlsi.inputs.sram_parameters` for the SRAMs the design uses |
| `sky130-extras.yml` | the SRAM libraries, as Hammer's `sram_generator` binds them. The DAG and the Makefile flow both read them from here: for a registered design, `make` uses the `hammer.d` that `studio make-dag` writes, with the DAG's configs, and runs no `make srams` |
| `registration.json` | where the design came from: the configs read, the RTL, every SRAM instance, and each project setting dropped and why |

Settings that described the class machines are dropped, since SledgeHammer's
`configs-env`, `configs-pdk` and `configs-tool` supply its own: tool versions
and binaries, PDK install paths, licenses, and the tool and build-system
choice (`vlsi.core.*`, except `max_threads`). Keys that are not Hammer
settings, like the skeleton's YAML anchor holders (`verilogSrc`,
`clockPeriod`), are dropped too.

Registration only writes a design directory it created
(`registration.json` marks it), never one with the same name it did not. It
leaves other files there alone, such as the `eco.yml` the [ECO
stage](ECO.md) needs. `studio augment`, which make-dag runs for other designs,
leaves a registered design alone.

## SRAMs

An SRAM is a module the elaborated design instantiates without defining,
whose name is in Hammer's catalog,
[`hammer/technology/sky130/sram-cache.json`](../hammer/technology/sky130/sram-cache.json).
Only instances the top module actually reaches count, with their real
hierarchy paths. Registration then:

1. sets `vlsi.inputs.sram_parameters` to those catalog entries, nothing else
   from the catalog;
2. runs Hammer's `sram_generator` on the generated configs, as `make srams`
   does, which binds the LEF, GDS, Verilog model and a Liberty file per MMMC
   corner from `technology.sky130.sram22_sky130_macros` in `configs-env`;
3. checks every bound file exists, every port an instance connects exists on
   the macro, and every SRAM instance matches a hardmacro placement constraint
   (an SRAM without one is a warning: par then has to place it).

Synthesis resolves the SRAMs from their Liberty, as in the EECS151 flow, so no
behavioral model or blackbox stub reaches Genus.

The BWRC install (`sram22_sky130_macros_2026`) ships each GDS only gzipped,
while the generator names `<macro>.gds` and par merges every macro GDS into
its output. So the design reads the macros through
`e2e/configs-design/.sram22_sky130_macros/`, shared by all registered designs,
which links each file of the install and holds the GDS decompressed. When the
install has a plain `.gds`, the design uses it directly.

## Modified copies for ECO tests

Any directory beside `src/original/` holds modified copies: a copy of a
project with a deliberate functional bug, for example
`src/Test1/gatelevelgurus/`. `make designs` registers it as its own design,
`<project>_<tree>` (`gatelevelgurus_Test1`), next to the original, and gives
it one more file, `eco.yml`:

```yaml
formal.inputs.eco_reference_files:
- ../../build-sky130-cm/gatelevelgurus/syn-rundir/riscv_top.mapped.v
formal.inputs.eco_reference_files_meta: prependlocal
formal.conformal.eco_method: hierarchical   # or flat_localized, flat_global
formal.conformal.version: '261'
```

`eco.yml` is yours. Registration creates this default only when the design
has none, and never rewrites or deletes an existing one, so edit it freely;
delete it to get the default back. You can also write
`e2e/configs-design/<design>/eco.yml` before the design is ever registered,
and registration builds the design around it. The ECO methods are described in
[ECO.md](ECO.md#choosing-the-eco-method).

The copy is the implementation the bug went through syn and par in; the
original is the corrected design. The [eco stage](ECO.md) patches the copy's
par netlist until Conformal proves it equivalent to the netlist syn makes
from the original. So:

1. run `syn` on the original's DAG (`sledgehammer_gatelevelgurus_<user>`);
2. run the copy's DAG (`sledgehammer_gatelevelgurus_Test1_<user>`) with syn,
   par and **Functional ECO** ticked.

A copy registers only when `src/original/<project>` exists and has the same
top module. An `eco.yml` without `formal.inputs.eco_reference_files` registers
with a warning: the eco stage would stop at `par_to_eco` for lack of a
reference.

## Not covered yet

- **Simulation configs** (`sim-rtl.yml` and the gate-level ones) are not
  registered; their testbenches need the course's RISC-V test programs.
- **sky130 only**, and only SRAMs from Hammer's catalog as macros. A project
  that instantiates standard cells or other IP directly fails with the
  module's name.
- SRAMs under **generate blocks** get approximate paths, so placement
  mismatches there are warnings rather than errors.
- The class flow used Genus and Innovus 22.1; SledgeHammer's environment runs
  Genus 23.1 and Innovus 25.1, so results can differ from the project's own.
