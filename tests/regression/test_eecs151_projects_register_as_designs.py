import gzip
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from hammer.shell import design_register, pd_store_cli
from hammer.vlsi import rtl_check

SRAM = "sram22_64x32m4w8"
SRAM_PORTS = "clk, rstb, ce, we, wmask, addr, din, dout"

# Shaped like the EECS151 skeleton: the Makefile names the configs, syn.yml lists the RTL under YAML
# anchors, sky130.yml carries the class machines' installs, par.yml places the SRAM.
PROJECT = {
    "Makefile": "vlsi_dir=$(abspath .)\n"
                "TECH_CONF           ?= $(vlsi_dir)/sky130.yml\n"
                "DESIGN_SYN_CONF     ?= $(vlsi_dir)/syn.yml\n"
                "DESIGN_PAR_CONF     ?= $(vlsi_dir)/par.yml\n",
    "sky130.yml": "vlsi.core.technology: hammer.technology.sky130\n"
                  "technology.sky130:\n"
                  '  sky130A: "/home/ff/eecs151/sky130A"\n'
                  '  stdcell_library: "sky130_scl"\n'
                  "vlsi.core.max_threads: 4\n"
                  'synthesis.genus.genus_bin: "/share/instsww/cadence/genus"\n'
                  'synthesis.genus.version: "221"\n'
                  'cadence.cadence_home: "/home/ff/eecs151/cadence"\n'
                  'vlsi.inputs.sram_parameters: "/home/ff/eecs151/hammer/sram-cache.json"\n'
                  'vlsi.inputs.sram_parameters_meta: ["transclude", "json2list"]\n'
                  "par.inputs.gds_merge: true\n",
    "syn.yml": "clockPeriod: &CLK \"10ns\"\n"
               "verilogSrc: &SRC\n"
               '  - "src/pkg.sv"\n'
               '  - "src/reg.sv"\n'
               '  - "src/mem.sv"\n'
               '  - "src/top.sv"\n'
               'vlsi.inputs.clocks: [{name: "clk", period: *CLK, uncertainty: "0.1ns"}]\n'
               "synthesis.inputs:\n"
               '  top_module: "top"\n'
               "  input_files: *SRC\n",
    "par.yml": "vlsi.inputs.placement_constraints:\n"
               "  - {path: top, type: toplevel, x: 0, y: 0, width: 100, height: 100,\n"
               "     margins: {left: 0, right: 0, top: 0, bottom: 0}}\n"
               "  - {path: top/m/bank, type: hardmacro, x: 10, y: 10}\n",
    "src/pkg.sv": "package widths_pkg;\n  localparam int W = 8;\nendpackage\n",
    "src/reg.sv": "module Reg #(parameter N = 1) (input logic clk, input logic [N-1:0] d,\n"
                  "                                output logic [N-1:0] q);\n"
                  "  always_ff @(posedge clk) q <= d;\nendmodule\n",
    "src/mem.sv": "module mem(input logic clk);\n"
                  f"  {SRAM} bank (.clk(clk), .rstb(1'b1), .ce(1'b1), .we(1'b0), .wmask(4'b0),\n"
                  "                  .addr(6'b0), .din(32'b0), .dout());\nendmodule\n",
    # A `define the configs never set leaves `ghost` dead, and a parameterized instance is still reached.
    "src/top.sv": "import widths_pkg::*;\n"
                  "module top(input logic clk, input logic [W-1:0] d, output logic [W-1:0] q);\n"
                  "`ifdef USE_GHOST\n  ghost g (.clk(clk));\n`else\n"
                  "  Reg #(.N(W)) r (.clk(clk), .d(d), .q(q));\n`endif\n"
                  "  mem m (.clk(clk));\nendmodule\n",
    # Never listed in syn.yml, so never read.
    "src/top_tb.sv": "module top_tb; top dut(); undefined_thing u(); endmodule\n",
}


@pytest.fixture
def slang() -> None:
    try:
        rtl_check._checked_binary()
    except rtl_check.SlangNotFound as e:
        pytest.skip(f"needs slang {rtl_check.SLANG_VERSION}: {e}")


def _write(root: Path, files: Dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """
    A checkout with one project under src/original and an sram22 install that ships its GDS gzipped.
    Loading sky130 for real needs the PDK, so Hammer's check is replaced by one that binds the SRAM the
    way its sram_generator does, from the macro directory the generated configs point at.
    """
    site = tmp_path / "sram22"
    macro = site / SRAM
    macro.mkdir(parents=True)
    (macro / f"{SRAM}.lef").write_text("MACRO x\n")
    (macro / f"{SRAM}_ss_100C_1v60.lib").write_text("library (x) {}\n")
    (macro / f"{SRAM}.v").write_text(f"module {SRAM}(\n`ifdef USE_POWER_PINS\n  vdd, vss,\n`endif\n"
                                     f"  {SRAM_PORTS}\n);\nendmodule\n")
    with gzip.open(macro / f"{SRAM}.gds.gz", "wb") as f:
        f.write(b"GDS bytes")
    (site / "sram22.spice").write_text("* all macros\n")
    _write(tmp_path / "repo", {
        "e2e/configs-env/bwrc-env.yml": f'technology.sky130.sram22_sky130_macros: "{site}"\n',
    })
    _write(tmp_path / "repo" / "src" / "original" / "proj", PROJECT)

    def check(config_dir: Path, files, e2e_dir, env, pdk, tools, obj_dir, srams) -> List[dict]:
        tech = yaml.safe_load((config_dir / "sky130.yml").read_text())
        root = os.path.normpath(config_dir / tech["technology.sky130.sram22_sky130_macros"])
        base = f"{root}/{SRAM}/{SRAM}"
        return [{"library": {"name": SRAM, "nldm_liberty_file": f"{base}_ss_100C_1v60.lib", "lef_file": f"{base}.lef",
                             "gds_file": f"{base}.gds", "verilog_sim": f"{base}.v"}}] if srams else []

    monkeypatch.setattr(design_register, "_check_with_hammer", check)
    return tmp_path / "repo"


def _register(repo: Path, project: str = "proj") -> design_register.ProjectRegistration:
    return design_register.register_project(repo / "src" / "original" / project, e2e_dir=repo / "e2e")


def _generated(repo: Path, name: str, design: str = "proj") -> dict:
    return yaml.safe_load((repo / "e2e" / "configs-design" / design / name).read_text())


class TestRegistration:
    def test_project_becomes_a_design_from_its_own_configs(self, slang, repo) -> None:
        before = _snapshot(repo / "src")
        reg = _register(repo)
        assert _snapshot(repo / "src") == before

        design_dir = repo / "e2e" / "configs-design" / "proj"
        assert reg.design_dir == design_dir and reg.top_module == "top"
        common = _generated(repo, "common.yml")
        assert common["synthesis.inputs.top_module"] == "top"
        # syn.yml's order, which Genus reads in, through paths relative to the generated config
        assert common["synthesis.inputs.input_files"] == [
            "../../../src/original/proj/src/pkg.sv", "../../../src/original/proj/src/reg.sv",
            "../../../src/original/proj/src/mem.sv", "../../../src/original/proj/src/top.sv"]
        assert common["synthesis.inputs.input_files_meta"] == "prependlocal"
        assert all((design_dir / f).is_file() for f in common["synthesis.inputs.input_files"])
        assert common["vlsi.inputs.clocks"] == [{"name": "clk", "period": "10ns", "uncertainty": "0.1ns"}]
        assert [c["path"] for c in common["vlsi.inputs.placement_constraints"]] == ["top", "top/m/bank"]

        tech = _generated(repo, "sky130.yml")
        assert tech["technology.sky130.stdcell_library"] == "sky130_scl"
        assert tech["vlsi.core.max_threads"] == 4 and tech["par.inputs.gds_merge"] is True
        # only the SRAM the RTL instantiates, from Hammer's own catalog
        assert [p["name"] for p in tech["vlsi.inputs.sram_parameters"]] == [SRAM]

        record = json.loads((design_dir / "registration.json").read_text())
        assert record["source"] == "src/original/proj"
        assert record["srams"] == {SRAM: ["top/m/bank"]}
        for key in ("technology.sky130.sky130A", "synthesis.genus.genus_bin", "synthesis.genus.version",
                    "cadence.cadence_home", "vlsi.core.technology", "vlsi.inputs.sram_parameters",
                    "vlsi.inputs.sram_parameters_meta", "clockPeriod", "verilogSrc"):
            assert key in record["dropped_settings"]
        for name in ("common.yml", "sky130.yml", "sky130-extras.yml"):
            text = (design_dir / name).read_text()
            assert "/home/ff" not in text and "/share/instsww" not in text, name
        assert not reg.warnings

    def test_srams_are_bound_through_a_mirror_when_the_gds_is_gzipped(self, slang, repo) -> None:
        _register(repo)
        mirror = repo / "e2e" / "configs-design" / design_register.SRAM_MIRROR_DIR
        assert (mirror / SRAM / f"{SRAM}.gds").read_bytes() == b"GDS bytes"
        assert (mirror / SRAM / f"{SRAM}.lef").is_symlink()
        assert (mirror / "sram22.spice").is_symlink()
        tech = _generated(repo, "sky130.yml")
        assert tech["technology.sky130.sram22_sky130_macros"] == "../" + design_register.SRAM_MIRROR_DIR
        extras = _generated(repo, "sky130-extras.yml")
        assert extras["vlsi.technology.extra_libraries_meta"] == "append"
        assert extras["vlsi.technology.extra_libraries"][0]["library"]["gds_file"] == \
            str(mirror / SRAM / f"{SRAM}.gds")

    def test_registering_again_changes_nothing(self, slang, repo) -> None:
        assert _register(repo).written
        design_dir = repo / "e2e" / "configs-design" / "proj"
        (design_dir / "eco.yml").write_text("formal.inputs.eco_reference_files: []\n")
        before = _snapshot(design_dir)
        assert _register(repo).written == []
        assert _snapshot(design_dir) == before

    def test_a_design_it_did_not_write_is_left_alone(self, slang, repo) -> None:
        design_dir = repo / "e2e" / "configs-design" / "proj"
        design_dir.mkdir(parents=True)
        (design_dir / "common.yml").write_text("mine\n")
        with pytest.raises(design_register.RegistrationError, match="holds common.yml that register-projects did not write"):
            _register(repo)
        assert (design_dir / "common.yml").read_text() == "mine\n"

    def test_augment_leaves_a_registered_design_alone(self, slang, repo) -> None:
        _register(repo)
        design_dir = repo / "e2e" / "configs-design" / "proj"
        before = _snapshot(design_dir)
        srams, warnings = design_register.augment_existing_design(design_dir)
        assert srams == [] and "register-projects" in warnings[0]
        assert _snapshot(design_dir) == before


class TestErrors:
    def _fails(self, repo: Path, files: Dict[str, str], match: str) -> None:
        _write(repo / "src" / "original" / "proj", files)
        with pytest.raises(design_register.RegistrationError, match=match):
            _register(repo)
        assert not (repo / "e2e" / "configs-design" / "proj").exists()

    def test_no_top_module_names_the_candidates(self, slang, repo) -> None:
        syn = PROJECT["syn.yml"].replace('  top_module: "top"\n', "")
        self._fails(repo, {"syn.yml": syn}, r"does not guess it\. The modules nothing instantiates are: top")

    def test_a_module_nothing_defines_is_named_with_its_instance(self, slang, repo) -> None:
        top = PROJECT["src/top.sv"].replace("  mem m (.clk(clk));\n", "  mem m (.clk(clk));\n  pll p (.clk(clk));\n")
        self._fails(repo, {"src/top.sv": top}, r"pll: top/p")

    def test_a_package_listed_after_its_use(self, slang, repo) -> None:
        syn = PROJECT["syn.yml"].replace('  - "src/pkg.sv"\n', "").replace(
            '  - "src/top.sv"\n', '  - "src/top.sv"\n  - "src/pkg.sv"\n')
        self._fails(repo, {"syn.yml": syn}, "src/top.sv uses widths_pkg, which src/pkg.sv declares later")

    def test_a_placement_for_an_instance_that_is_not_there(self, slang, repo) -> None:
        self._fails(repo, {"par.yml": PROJECT["par.yml"].replace("top/m/bank", "top/m/bank0")},
                    "top/m/bank0.*place_inst")

    def test_a_missing_input_file(self, slang, repo) -> None:
        (repo / "src" / "original" / "proj" / "src" / "reg.sv").unlink()
        self._fails(repo, {}, "do not exist: src/reg.sv")

    def test_a_path_from_the_class_machines(self, slang, repo) -> None:
        par = PROJECT["par.yml"] + 'vlsi.inputs.custom_sdc_files: ["/home/ff/eecs151/extra.sdc"]\n'
        self._fails(repo, {"par.yml": par}, "/home/ff/eecs151/extra.sdc, a path on the machine")

    def test_an_sram_port_the_macro_does_not_have(self, slang, repo) -> None:
        mem = PROJECT["src/mem.sv"].replace(".ce(1'b1)", ".csb(1'b1)")
        self._fails(repo, {"src/mem.sv": mem}, r"top/m/bank connects csb, which SRAM .* does not have")


def test_one_broken_project_does_not_stop_the_others(slang, repo, capsys) -> None:
    _write(repo / "src" / "original" / "broken", {"syn.yml": "synthesis.inputs.top_module: x\n"})
    rc = pd_store_cli.main(["register-projects", "--repo-root", str(repo), "--no-dag"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "broken                   FAILED     synthesis.inputs.input_files is not set" in err
    assert "proj                     registered" in err
    assert (repo / "e2e" / "configs-design" / "proj" / "common.yml").is_file()
    assert not (repo / "e2e" / "configs-design" / "broken").exists()


def test_make_designs_runs_register_projects_and_nothing_for_the_default_design() -> None:
    e2e = Path(__file__).resolve().parents[2] / "e2e"
    out = subprocess.run(["make", "-n", "-C", str(e2e), "designs", "project=gatelevelgurus",
                          "OBJ_DIR=/nonexistent/build"],
                         capture_output=True, text=True, check=True).stdout
    commands = [line for line in out.splitlines() if not line.startswith("make")]
    assert len(commands) == 1
    studio, args = commands[0].split(" ", 1)
    assert studio == "studio" or studio.endswith("/.venv/bin/studio")
    assert args == "register-projects --env bwrc --pdk sky130 --tools cm --project gatelevelgurus"


class TestModifiedCopies:
    def test_a_copy_registers_beside_its_original_and_patches_toward_it(self, slang, repo, capsys) -> None:
        copy = repo / "src" / "Test1" / "proj"
        _write(copy, PROJECT)
        _write(copy, {"src/reg.sv": PROJECT["src/reg.sv"].replace("q <= d", "q <= ~d")})
        assert pd_store_cli.main(["register-projects", "--repo-root", str(repo), "--no-dag"]) == 0
        assert "proj_Test1               registered" in capsys.readouterr().err

        design_dir = repo / "e2e" / "configs-design" / "proj_Test1"
        common = _generated(repo, "common.yml", "proj_Test1")
        assert "../../../src/Test1/proj/src/reg.sv" in common["synthesis.inputs.input_files"]
        eco = _generated(repo, "eco.yml", "proj_Test1")
        assert eco["formal.inputs.eco_reference_files"] == ["../../build-sky130-cm/proj/syn-rundir/top.mapped.v"]
        assert eco["formal.inputs.eco_reference_files_meta"] == "prependlocal"
        record = json.loads((design_dir / "registration.json").read_text())
        assert record["source"] == "src/Test1/proj" and record["reference"] == "src/original/proj"
        # the original gets no eco.yml of its own
        assert not (repo / "e2e" / "configs-design" / "proj" / "eco.yml").exists()

    def test_a_copy_without_its_original_fails_alone(self, slang, repo, capsys) -> None:
        _write(repo / "src" / "Test1" / "orphan", PROJECT)
        assert pd_store_cli.main(["register-projects", "--repo-root", str(repo), "--no-dag"]) == 1
        err = capsys.readouterr().err
        assert "orphan_Test1             FAILED     it is a copy of orphan, but src/original/orphan does not exist" in err
        assert "proj                     registered" in err

    def _register_copy(self, repo: Path) -> design_register.ProjectRegistration:
        return design_register.register_project(
            repo / "src" / "Test1" / "proj", e2e_dir=repo / "e2e", design="proj_Test1",
            reference=repo / "src" / "original" / "proj")

    def test_an_edited_eco_yml_is_never_rewritten(self, slang, repo) -> None:
        _write(repo / "src" / "Test1" / "proj", PROJECT)
        first = self._register_copy(repo)
        assert "eco.yml (created; edit freely)" in first.written
        eco = repo / "e2e" / "configs-design" / "proj_Test1" / "eco.yml"
        assert "never" in eco.read_text() and "overwrites this file" in eco.read_text()
        mine = "formal.inputs.eco_reference_files: [/elsewhere/top.mapped.v]\nformal.conformal.version: '212'\n"
        eco.write_text(mine)
        again = self._register_copy(repo)
        assert eco.read_text() == mine
        assert not any("eco.yml" in w for w in again.written)
        assert any("eco.yml: kept" in n for n in again.notes)

    def test_an_eco_yml_written_before_registering_is_used(self, slang, repo) -> None:
        _write(repo / "src" / "Test1" / "proj", PROJECT)
        design_dir = repo / "e2e" / "configs-design" / "proj_Test1"
        design_dir.mkdir(parents=True)
        mine = "formal.inputs.eco_reference_files: [/elsewhere/top.mapped.v]\n"
        (design_dir / "eco.yml").write_text(mine)
        reg = self._register_copy(repo)
        assert (design_dir / "eco.yml").read_text() == mine
        assert (design_dir / "common.yml").is_file() and (design_dir / "registration.json").is_file()
        assert "eco.yml" not in json.loads((design_dir / "registration.json").read_text())["files"]
        assert not reg.warnings

    def test_an_eco_yml_without_a_reference_is_a_warning(self, slang, repo) -> None:
        _write(repo / "src" / "Test1" / "proj", PROJECT)
        design_dir = repo / "e2e" / "configs-design" / "proj_Test1"
        design_dir.mkdir(parents=True)
        (design_dir / "eco.yml").write_text("formal.conformal.version: '261'\n")
        reg = self._register_copy(repo)
        assert any("sets no formal.inputs.eco_reference_files" in w for w in reg.warnings)
