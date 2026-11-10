# Functional ECO after place and route

A bug found after `par` normally means rerunning synthesis and place and route
from scratch. The `eco` stage avoids that. It takes the placed-and-routed
netlist, compares it with a netlist synthesized from the corrected RTL, and
has Cadence Conformal generate a patch that makes the implemented design match
the fix. It then applies the patch, maps it onto library cells, proves the
patched netlist equivalent to the corrected one, and writes the patched netlist
plus Innovus scripts with the same edits. The `eco_par` stage then places and
routes the patch incrementally on `par`'s database.

```text
par ─┬→ par_to_sim / par_to_power / par_to_formal / par_to_timing / par_to_drc / par_to_lvs
     └→ par_to_eco → eco → eco_to_par → eco_par
```

Re-running signoff on the patched layout is not part of it yet; see [What is
not covered yet](#what-is-not-covered-yet).

## Running it

The stage needs one input `par` does not produce: the corrected design.
Synthesize the fixed RTL in a **separate** build directory (the build you are
patching keeps its original RTL), with the same technology, tools and
constraints, for example:

```bash
cd e2e
make syn design=gcd OBJ_DIR=$PWD/build-sky130-cm/gcd-fixed extra=configs-design/gcd/fixed-rtl.yml
```

where `fixed-rtl.yml` points `synthesis.inputs.input_files` at the corrected
sources. Then name that netlist in the design's `eco.yml`:

```yaml
# e2e/configs-design/gcd/eco.yml
formal.inputs.eco_reference_files: ["build-sky130-cm/gcd-fixed/syn-rundir/gcd.mapped.v"]
```

and run the stage:

| Flow | Command |
|---|---|
| Make (`e2e/`) | `make eco`; the Makefile adds `configs-design/$(design)/eco.yml` for any goal with `eco` in it |
| DAG | tick **Functional ECO** on the trigger form, or `sledgehammer run eco` |
| Rerun just the ECO | `make redo-eco`; in the DAG `eco` reruns whenever it is asked for, like the other formal stages |

`studio make-dag --design <design>` puts every `configs-design/<design>/*.yml`
in the DAG, so rerun it after adding `eco.yml`.

Asking for `eco` pulls in `syn` and `par` like any later stage, and both skip.
`formal.inputs.eco_reference_files` lives under `formal.*`, which the
dependency check and the fingerprints scope to the formal stages, so adding it
after `par` changes nothing `syn` or `par` depend on. A key outside the stage
namespaces would count as global and rerun every stage.

Conformal settings come from the formal tool's keys:

- `formal.conformal.license` picks the binary mode: `GXL` (default) runs
  `lec -ECOGXL`, `XL` runs `lec -ECO`. On the BWRC license servers
  `Conformal_ECO_GXL` is available and `Conformal_ECO` is not, so keep `GXL`.
- `formal.conformal.version` picks the install. The default `212` segfaults on
  the RHEL 9 BWRC machines; `261` works: `formal.conformal.version: "261"`.
- `formal.conformal.eco_method` picks how the patch is found and where it goes:
  `hierarchical` (default), `flat_localized` or `flat_global`. See
  [Choosing the ECO method](#choosing-the-eco-method).

## What the stage reads and writes

`par_to_eco` (`HammerDriver.par_output_to_eco_input`) turns `par`'s output
into a formal run with `formal.inputs.check: eco`:

| ECO input | Comes from | Conformal side |
|---|---|---|
| `formal.inputs.input_files` | `par.outputs.output_sim_netlist` (`<top>.sim.v`) | golden; the design that gets patched |
| `formal.inputs.reference_files` | `formal.inputs.eco_reference_files` | revised; what the patch must match |

The implemented design is golden because Conformal's ECO commands patch the
golden side: `ANALYZE ECO` "analyzes the ECO change in the Revised root module
when compared to the Golden root module" (Conformal Reference, LEC ECO
commands). This is the reverse of the plain `lec` check, where the reference is
golden. In the reports, `(G)` is the implementation and `(R)` the reference.
| `formal.inputs.top_module` | `par.inputs.top_module` | root module of both |
| Liberty | every library of the setup MMMC corner, from the technology | `read_library -liberty` |

The Liberty is one corner, so the patch maps onto one characterization of each
cell, and every library at that corner, so pads and macros in the netlist have
models (Conformal stops on a cell it has none for). On sky130 that corner lists
several supply variants of each IO pad. Conformal keeps the first copy (warning
IGN3.2), and the two verification reports gain a "Design ambiguity: Duplicate
module definition" line. Neither changes the patch or the verdicts.

The `eco` stage is the `formal` action run in its own rundir
(`--formal_rundir eco-rundir`), the same way `formal_par` runs in
`formal-par-rundir`. Everything lands in `eco-rundir/`:

| File | What it is |
|---|---|
| `eco.tcl`, `eco.log` | the generated dofile and Conformal's log |
| `01_pre_eco_verification.rpt`, `02_pre_eco_nonequivalent.rpt` | compare before the patch (expected to fail). `hierarchical`: the module-by-module result and the non-equivalent modules |
| `03_eco_check.rpt`, `04_eco_hierarchy_noneq.rpt` | `flat_localized` only: ECO setup checks and the modules that need patching |
| `patch.v`, `ecopin.do`, `hier.eco.do` | the patch, the ECO pins it needs, the hierarchical compare dofile. `hierarchical` also keeps one `patch_<module>.v` per patched module; `patch.v` collects them |
| `golden_flat.v` | `flat_global` only: the implemented netlist flattened, which the patch is applied to |
| `eco_genus/` | `optimize_patch`'s Genus run: `cfm_eco_genus.tcl`, `genus.log` and the mapped patches |
| `05_post_eco_verification.rpt`, `06_post_eco_compare_data.rpt` | the written `<top>.eco.v`, read back and compared flat against the reference (must pass) |
| `<top>.eco.v`, `07_ECOprelogics.rpt` | the patched netlist, in library cells, and its report |
| `eco.invs` | the mapped patch as Innovus netlist-edit commands (legacy syntax) |
| `eco_stylus.tcl` | the same edits in Stylus Common UI syntax (`create_inst`, `connect`); what `eco_par` applies |

`analyze_eco` writes the patch in generic gates (`and`, `not`, muxes), which no
layout tool can place. After applying it, the stage runs Conformal's
`optimize_patch`, which has Genus map the patch onto the same setup-corner
Liberty, leaving out the technology's dont-use cells (`-avoid`). The Genus is
`formal.conformal.eco_genus_bin`, by default the DDI Genus of
`synthesis.genus.version`, the one synthesis runs. On Test2 the 17 generic gates
became 9 `sky130_scl_9T` cells (`AOI21X1`, `OAI22X1`, `CLKMX2X2`, ...); the
final compare checks the mapped netlist. `optimize_patch` needs the patches
applied with `-keephierarchy` and, after a hierarchical compare, the compare's
black boxes deleted (`delete_black_box -all -hier -both`) first.

The stage fails unless `05_post_eco_verification.rpt` reports `PASS`.
Conformal's exit status cannot decide this: it flags the non-equivalent points
of the first compare, which an ECO always has. The final compare reads the
written netlist back rather than comparing in the same session: after a
hierarchical compare, an in-session compare still sees the submodules as black
boxes and checks only the top level (on Test2: 214 points, no flip-flops,
against 2532). When the designs already match
before the patch, the stage passes with a warning that there was nothing to
fix.

`eco-rundir/formal-output.json` exports the artifacts for later stages:
`formal.outputs.eco_netlist`, `formal.outputs.eco_patch`,
`formal.outputs.eco_innovus_script` and `formal.outputs.eco_stylus_script`.

The dofile has the two steps of the plugin's `lec` check, with a Conformal
checkpoint after each: `setup_eco_designs` reads the designs, and
`patch_eco_designs` does the rest of the original dofile. Each checkpoint
costs about 27 s and 195 MB even on gcd, about as much as the ECO itself, so
the ECO is not split further. From the command line, `--to_step
setup_eco_designs` stops after reading (in `e2e/`, `make redo-eco args="..."`)
and `--from_step patch_eco_designs` restarts from that checkpoint. The DAG
trigger form's step fields apply to `syn` and `par` only. A run stopped early
is judged only on the steps it ran.

## Choosing the ECO method

`formal.conformal.eco_method` (default `hierarchical`) picks how Conformal
finds the logic to change and where it puts the patch. All three end with the
same check: the written `<top>.eco.v` is read back and compared flat against
the reference.

| Method | Finds the mismatch by | Patch goes | Conformal steps |
|---|---|---|---|
| `hierarchical` | comparing module by module (`write_hier_compare_dofile`, `run_hier_compare`) | into each non-equivalent module, generated from that module's own compare | per module: `set_root_module`, `compare`, `analyze_eco patch_<module>.v`; then `apply_patch <module> <module>_eco -keephierarchy` |
| `flat_localized` | one flat compare (`set_eco_option -flat`, `analyze_hier_compare -eco_aware`) | into each module the failing logic sits in | `compare_eco_hierarchy`, `analyze_eco -hierarchical`, `apply_patch -auto -keephierarchy`. Cadence's "flattened ECO flow" |
| `flat_global` | one flat compare of the flattened netlists | one patch at the top; the patched netlist comes out flat | `flatten`, `write_design golden_flat.v`, read back; `analyze_eco`, `apply_patch -auto -keephierarchy` |

`flat_global` reads a flattened copy of the implementation (`golden_flat.v`)
because Conformal applies a patch only to the netlist structure it read
(`ECO1.12` otherwise), and flattens the reference too, or `analyze_eco` finds
no implementation nets for its gates. On a hierarchical netlist left as it is,
a top-level patch tries to re-implement most of the design (Conformal warns
"Please use hierarchical method").

Measured on `gatelevelgurus_Test2` (bugs in `Branch_comp` and `Imm_gen`), all
three through the plugin, Conformal 26.1, before `optimize_patch` was added
(so the patch is `analyze_eco`'s, unmapped):

| | `hierarchical` | `flat_localized` | `flat_global` |
|---|---:|---:|---:|
| Stage time | 241 s | 166 s | 322 s |
| Patch | 1 library cell, 18 primitives, in 2 modules | 0 cells, 17 primitives, in 2 modules | 49 library cells, 510 primitives, at the top |
| Innovus edits | 5 pins detached, 23 connected | 3 pins detached, 16 connected | 58 instances deleted, 319 pins detached, 259 connected |
| Patched netlist | hierarchy kept | hierarchy kept | flat (`riscv_top` + `riscv_top_eco`) |
| Final check | PASS, 2532 points | PASS, 2532 points | PASS, 2530 points |

A flat patched netlist no longer matches the module structure of `par`'s
Innovus database, so `eco_par` cannot apply a `flat_global` patch: its edits
name nets of the flattened netlist (on Test2, `cpu/Immgen_main/n_11`), and
Innovus stops at the first one (`IMPTCM-162`). Use `hierarchical` or
`flat_localized` when the patch is to be placed and routed.

## Placing and routing the patch (`eco_par`)

`eco_par` is an incremental Innovus run on `par`'s database:

| Flow | Command |
|---|---|
| Make (`e2e/`) | `make eco-par` (after `eco`); `make redo-eco-par` reruns just it |
| DAG | tick **ECO Place and Route**, or `sledgehammer run eco_par`; asking for it pulls in `eco`, `syn` and `par`. Tick **Reuse existing syn/par outputs** as well to apply the ECO to the layout you already have (below) |

Without that option the DAG runs `syn` and `par` as Hammer actions, and Hammer
reruns them whenever its dependency check sees a change, including a change to
Hammer's own code (`vlsi.framework_fingerprint_sha256`), not only when their
outputs are missing. With **Reuse existing syn/par outputs**
(`reuse_upstream`), a `syn` or `par` that runs only because a later stage needs
it calls no Hammer action. It checks its last completed outputs and passes them
on, and `syn_to_par` leaves `par-input.json` alone. Before reusing, the task
checks that:

- its `syn-output-full.json` / `par-output-full.json` reads, and the netlists it
  lists exist;
- every input it records (the RTL, `par`'s synthesized netlist) still exists and
  is not newer than it;
- `master_database.json` does not mark the stage `needsToRerun` (a run that did
  not complete).

Any failure stops the task with the reason and does not fall back to running
the stage. Reusing skips Hammer's dependency check, fingerprints included, and
writes nothing to the master database, so the next normal run judges `syn` and
`par` as it would have. A stage ticked on the form runs as usual.

A `hammer.d` or DAG generated before this stage has no `eco_par`; regenerate
it (`studio make-dag --design <design>`, or `sledgehammer buildfile`).

`eco_to_par` (`HammerDriver.eco_output_to_par_input`) sets
`par.inputs.eco_script` to `formal.outputs.eco_stylus_script`. With that key
set, the Innovus plugin replaces its full flow with these steps, in
`eco-par-rundir/`:

| Step | Innovus |
|---|---|
| `eco_restore_design` | `read_db` of `par`'s `<top>_FINAL`, found beside `par.outputs.output_netlist` |
| `eco_apply_patch` | `source eco_stylus.tcl`; the new cells come in unplaced, already on VDD/VSS through the database's global-net rules |
| `eco_place` | `delete_filler` (the technology's decap and filler cells), `place_eco`, `check_place`, then the same `add_fillers` as `par` |
| `eco_route` | `route_eco`: routes the new and changed nets, keeps the rest |
| `eco_opt_design` | `opt_design -post_route -setup -hold`, as `par` ends on Innovus 25.1 |
| `write_regs`, `write_design` | `par`'s own: `<top>_FINAL`, the netlists, DEF, GDS, SDF and SPEF |

`eco-par-rundir/par-output.json` exports the usual `par.outputs.*`, pointing at
the patched layout. A failed restore or patch exits Innovus (instead of leaving
it at its prompt), and the run fails when its netlist is older than the script,
so a rerun that stopped early never passes on an earlier run's outputs.

`eco_par` keeps `par` cached. The `eco-par` action runs the par tool in its own
rundir without the dependency check, PD cache or substep resume, so it never
writes `par`'s master database. `par.inputs.eco_script` has no default: a new
`par.*` default would reach every `par` config and rerun `par`. The run applies
the Innovus plugin's own hooks only, not the technology's or the driver's
`par` hooks, which target full-flow steps (`init_design`, `power_straps`) that
the ECO run does not have; the restored database already carries their
settings. It emits Tcl only (`par.innovus.use_python: false`, the default).

Measured with the default `hierarchical` method (Test2 also with
`flat_localized`), Innovus 25.1, `sky130_scl`:

| | Test1 (`A ^ B` → `A \| B`) | Test2 | Test2, `flat_localized` |
|---|---:|---:|---:|
| `eco` (with `optimize_patch`) | 295 s | 331 s | 212 s |
| Mapped patch | 234 cells | 9 cells | 9 cells |
| `eco_par` | 24 min | 23 min | 23 min |
| Original cells untouched (DEF placement and cell) | 98.45% | 99.06% | 99.14% |
| Setup WNS: `par` → after `eco_par` | +0.134 → +0.075 ns | +0.165 → +0.147 ns | |
| Hold WNS: `par` → after `eco_par` | −0.026 → −0.117 ns (one I/O path, violating in `par` already) | → +0.008 ns | |
| `check_place` | no violations | no violations | no violations |
| Final netlist vs the reference (Conformal) | PASS, 2532 points | PASS, 2532 points | PASS, 2532 points |

Against a full `syn` + `par` of about 71–73 minutes. Without `eco_opt_design`,
Test2's setup WNS after `route_eco` was −1.446 ns (the patch sits on a
critical path and Genus mapped it without timing); `opt_design` resized or
moved 182 cells to recover it. `check_drc` counts stay at `par`'s own level
(Test2: 1618 before, 1621 after; the same macro-blockage and power-stripe
shorts, none on an ECO net).

## Where this came from

The flow is the EECS 144 ECO project
([anshthakkarr/eecs144-proj](https://github.com/anshthakkarr/eecs144-proj)),
`eco_patch_generation/conformal_eco.do`, which ran Conformal by hand on
netlists Hammer had produced. What changed:

| EECS 144 project | SledgeHammer |
|---|---|
| `lec -dofile conformal_eco.do`, run by hand | the `eco` DAG stage / `make eco` |
| `cadencerc` puts `CONFRML201/bin` on `PATH` | `formal.conformal.version` and `cadence.*` from the env config |
| `./gcd_coprocessor.sim.v` | `par.outputs.output_sim_netlist`, via `par_to_eco` |
| `./gcd_coprocessor_correct.mapped.v` | `formal.inputs.eco_reference_files` |
| `./sky130_ss_1.62_125_nldm.lib.gz` | the technology's setup-corner Liberty; with `technology.sky130.stdcell_library: sky130_scl` it includes that same file |
| `set root module gcd_coprocessor` | `par.inputs.top_module` |
| native dofile syntax | Conformal Tcl mode, which the plugin always uses: `report verification > f` becomes `redirect f {report_verification}`, `add compare points` becomes `add_compared_points` |
| `conformal_eco_out/` | `eco-rundir/` (report names unchanged) |
| `gcd_coprocessor.pre.G3.v` | `<top>.eco.v` |
| `clean.sh` | `make redo-eco` / `--force` regenerate the rundir |
| reading `05_post_eco_verification.rpt` by eye | the stage fails unless it says `PASS` |

Running the project's own netlists through the stage (sky130 with
`stdcell_library: sky130_scl`, Conformal 26.1) gives the same `patch.v` (apart
from the tool-version comment), the same patched netlist and the same verdicts
(`FAIL:NONEQ`, then `PASS`) as the project's committed outputs. Against its
original dofile rerun on the same Conformal version, the Innovus script and
the compare-data, ECO-check and hierarchy reports are identical. The two
verification reports differ only by the duplicate-module line above, and
`07_ECOprelogics.rpt` only by printing the netlist's absolute path.

On the e2e `gcd`, a change that zeroes the result bus until B reaches zero
(`result_bits_data = B_zero ? A_reg : 0`) patched in as 16
`sky130_fd_sc_hd__and2_0` cells, and a separate Conformal compare of the
patched netlist against the changed RTL passes.

## What is not covered yet

- **Signoff on the patched layout.** Timing, DRC, LVS and a final
  equivalence check of `eco-par-rundir/<top>.sim.v` would hang off `eco_par`
  the way they hang off `par` today. The equivalence results above were run by
  hand.
- **Physical-aware patch mapping.** `optimize_patch` maps with the Liberty
  only; with the SDC, LEF and `par`'s DEF it can map for timing and placement
  (`-premaskphysical`), as the archived flow did. Here `eco_opt_design`
  recovers the timing afterwards.
- **`flat_global` patches in `eco_par`** (see above).
- **Hierarchical designs.** The DAG gets an `eco` and an `eco_par` node per
  module, but the flow has only been run on flat designs.
