"""
Generate Hammer configs from a directory of RTL.

Point this at a project, tell it the top module and clock, and you get back
the four YAML files (common, syn, sky130, par) plus blackbox stubs for any
SRAMs the design uses. The goal is to skip the part where you spend a few
hours discovering that Genus needs the SRAM as a blackbox, par needs the
LEF/lib wired up via extra_libraries, the behavioral SRAM .v file will get
synthesized into a wall of flip-flops if you're not careful, and so on.
Everything in here came out of debugging spring_asic_final_project the hard
way over six attempts.

The flow when you run design-register:

1. Walk the RTL and find which modules the design defines vs which ones
   it just instantiates. The difference is what needs a stub.
2. Recognize sram22_<depth>x<width>m<mux>w<wmask> names and look up the
   matching directory under BWRC's sram22 install. Each one ships a LEF,
   a lib (.lib), a GDS, and a Verilog sim model.
3. Write a blackbox Verilog stub for each SRAM. Just the port list, no
   body. Genus then leaves the SRAM instance alone instead of trying to
   compile its `reg mem[...]` storage into flip-flops.
4. Emit the four configs. sky130.yml gets a vlsi.technology.extra_libraries
   block so Innovus actually sees the SRAM macros at place_inst time.
   par.yml only contains a toplevel placement; you still need to drop in
   x/y for each macro instance by hand.

Stuff this doesn't handle yet:

  - Auto-floorplanning macros. The tool warns when there are macros in
    the design but doesn't try to pick coordinates. That's the next
    feature (design-suggest-placements).
  - PDKs other than sky130.
  - OpenRAM-style SRAMs (sky130_sram_*). Only sram22 today.
  - Generating the Airflow DAG file. You still copy an existing one and
    sed the design name through it. Solved by the planned DAG factory.

Finished EECS151 projects take a different route: `studio register-projects`
(`make designs` in e2e/) reads src/original/<project>/ as its own flow would
and writes e2e/configs-design/<project>/ plus the design's DAG. See
"Registering EECS151 projects" below and readme_documents/EECS151_PROJECTS.md.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# The sram22 macros live under one common root with one directory per macro.
# Each directory has the LEF / lib / GDS / Verilog file we need. Override the
# default install root with the SRAM22_ROOT environment variable if your site
# keeps the macros elsewhere.
SRAM22_ROOT = os.environ.get(
    "SRAM22_ROOT", "/tools/commercial/skywater/local/sram22_sky130_macros"
)

# sram22 names look like sram22_<depth>x<width>m<mux>w<wmask>, for example
# sram22_256x32m4w8. We parse them out so the resolver can find the right
# directory without the caller having to know the site layout.
SRAM22_NAME_RE = re.compile(r"^sram22_(\d+)x(\d+)m(\d+)w(\d+)$")


@dataclass
class DiscoveredModules:
    """What an RTL scan found: modules defined, modules used, files walked."""
    user_defined: Set[str]      # modules the user's RTL declares
    instantiated: Set[str]      # modules the user's RTL references
    rtl_files: List[Path]       # every .v / .sv / .vh, in sorted order

    @property
    def undefined_references(self) -> Set[str]:
        """Modules used but not declared. These are the ones that need stubs."""
        return self.instantiated - self.user_defined


# ----- RTL scanning -----

# Matches a module declaration: "module foo" or "module foo (..." or
# "module foo #(...)". The trailing character class is what tells us we hit
# a real declaration and not, say, "modules:" in a comment.
MODULE_DECL_RE = re.compile(r"^\s*module\s+(\w+)\s*[#(\s]", re.MULTILINE)

# Verilog and SystemVerilog keywords that look enough like module names to
# trip our instantiation regex. We match the regex, then drop anything that
# turns out to be one of these. The "assert property(...)" case is what
# motivated the SV expansion at the bottom of the set.
VERILOG_KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg",
    "logic", "assign", "always", "always_ff", "always_comb", "always_latch",
    "initial", "begin", "end", "if", "else",
    "case", "endcase", "for", "while", "function", "endfunction", "task",
    "endtask", "generate", "endgenerate", "parameter", "localparam",
    "integer", "genvar", "default", "posedge", "negedge", "and", "or", "not",
    "nand", "nor", "xor", "xnor", "buf", "supply0", "supply1", "tri",
    "return", "automatic", "static", "void", "real", "time", "string",
    "typedef", "struct", "union", "enum", "package", "endpackage",
    "import", "export", "extern", "virtual", "class", "endclass",
    # SystemVerilog assertion grammar: "assert property(...);" looks like
    # a module instantiation if you squint. Same for cover/assume/expect.
    "assert", "assume", "cover", "expect", "property", "endproperty",
    "sequence", "endsequence", "checker", "endchecker", "restrict",
    "disable", "bind",
    # System tasks like $display, $finish. The regex strips the $ so
    # they look like bare identifiers, hence ending up in this set.
    "display", "write", "monitor", "strobe", "finish", "stop",
    "fopen", "fclose", "fwrite", "readmemh", "readmemb",
}
# An instantiation looks like "<module_name> <instance_name>(". The
# instance name part is loose because generate blocks (sram_data[0].sram)
# and escaped identifiers (\foo.bar) make Verilog instance names messy.
# Anything we grab here gets cross-checked against VERILOG_KEYWORDS.
INSTANTIATION_RE = re.compile(
    r"^\s*(\w+)\s+(?:\\?\w+(?:\[[^\]]*\])?\.?\w*|\w+)\s*\(",
    re.MULTILINE,
)


# Things we drop on the floor by default when walking a directory. The list
# is what bit me on spring_asic_final_project: testbenches that aren't
# synthesizable, "Cache (copy).v" macOS duplicates that re-declare the same
# modules, etc. Override with --exclude on the CLI, or kill the default
# list entirely with --no-default-excludes.
DEFAULT_EXCLUDE_PATTERNS = [
    "*Testbench.v", "*Testbench.sv",
    "*TestBench.v", "*TestBench.sv",
    "*_tb.v", "*_tb.sv",
    "*_test.v",
    "* (copy).v",       # macOS Finder-style duplicates
    "* (*).v",          # anything with " (foo).v" suffix
    "test_*.v",
]


def _strip_ifdef_blocks(text: str) -> str:
    """
    Drop everything between `ifdef and `endif so dead code doesn't lie to us.

    Why bother: a file like Memory151.v might have an instantiation of
    no_cache_mem guarded by `ifdef no_cache_mem. Without that define set
    (and we have no way of knowing what defines will be set at compile
    time), the instantiation is dead code. If we don't strip it, the
    scanner reports no_cache_mem as a missing module and the user has
    to chase a phantom error.

    The conservative move is to assume no defines: drop every conditional
    branch and only look at the unconditional code. That can lose real
    instantiations if the design relies on a default-on `define, but in
    practice those are rare and the error message is clearer than the
    alternative.

    This is a dumb line-based scanner, not a real preprocessor. Nested
    `ifdefs work via depth counting. `define inside an `ifdef can confuse
    it, but I haven't hit that case yet.
    """
    out: List[str] = []
    depth = 0
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("`ifdef") or stripped.startswith("`ifndef"):
            depth += 1
            continue
        if stripped.startswith("`endif"):
            depth = max(0, depth - 1)
            continue
        if stripped.startswith("`else") or stripped.startswith("`elsif"):
            # The depth counter is already keeping us inside the block,
            # so we just skip the directive line itself.
            continue
        if depth == 0:
            out.append(line)
    return "\n".join(out)


def _is_excluded(path: Path, patterns: List[str]) -> bool:
    """
    Does this path match any of the user's exclude patterns?

    We try a couple of different matching strategies so the user can write
    either "basename" patterns like "*Testbench.v" or "subpath" patterns
    like "Cache/*.v" without having to think about which the tool expects.
    Pathlib's match() is anchored to the end of the path; fnmatch lets us
    do a substring-style match against the full path string.
    """
    import fnmatch
    name = path.name
    full = str(path)
    for pat in patterns:
        if Path(name).match(pat):
            return True
        try:
            if path.match(pat):
                return True
        except Exception:
            pass
        if fnmatch.fnmatch(full, pat) or fnmatch.fnmatch(full, f"*/{pat}"):
            return True
    return False


def scan_rtl_directory(
    rtl_paths: List[Path],
    exclude_patterns: Optional[List[str]] = None,
    use_default_excludes: bool = True,
) -> DiscoveredModules:
    """
    Walk the RTL paths and figure out which modules are defined vs used.

    Directory inputs get walked recursively but filtered through the
    default exclude list (testbenches, copies, etc.) plus anything the
    caller adds via exclude_patterns. File inputs are always included
    verbatim: if you hand the tool a specific .v file, we don't second
    guess you.

    The dedup step is the other thing worth knowing about: if two files
    declare the same module name, we keep the first one in sorted order
    and silently drop the others. That's how we handle "Cache.v" coexisting
    with "Cache/Cache (copy).v" or similar; the user shouldn't have to
    explicitly --exclude every duplicate.
    """
    user_defined: Set[str] = set()
    instantiated: Set[str] = set()
    rtl_files: List[Path] = []

    excludes: List[str] = []
    if use_default_excludes:
        excludes.extend(DEFAULT_EXCLUDE_PATTERNS)
    if exclude_patterns:
        excludes.extend(exclude_patterns)

    skipped: List[Path] = []
    for p in rtl_paths:
        if p.is_dir():
            for candidate in sorted(list(p.rglob("*.v")) + list(p.rglob("*.sv"))):
                if _is_excluded(candidate, excludes):
                    skipped.append(candidate)
                else:
                    rtl_files.append(candidate)
        elif p.is_file():
            # User passed a specific file; trust them and skip filtering.
            rtl_files.append(p)

    # First pass: dedup by module name. If two files declare the same
    # module, we keep the one that sorts first and drop the rest. The
    # canonical case this handles is "Cache.v" + "Cache/Cache (copy).v".
    seen_modules: Set[str] = set()
    deduplicated: List[Path] = []
    skipped_dup: List[Tuple[Path, Set[str]]] = []
    for f in rtl_files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = _strip_ifdef_blocks(text)
        modules_in_file = {m.group(1) for m in MODULE_DECL_RE.finditer(text)}
        dup = modules_in_file & seen_modules
        if dup:
            skipped_dup.append((f, dup))
            continue
        seen_modules.update(modules_in_file)
        deduplicated.append(f)

    if skipped:
        print(f"  Excluded by patterns: {len(skipped)} files "
              f"(e.g. {[str(s.name) for s in skipped[:3]]})",
              file=sys.stderr)
    if skipped_dup:
        print(f"  Excluded as duplicates: {len(skipped_dup)} files "
              f"(e.g. {[str(s.name) for s, _ in skipped_dup[:3]]})",
              file=sys.stderr)

    rtl_files = deduplicated

    # Second pass: now that we know which files we're keeping, find all the
    # module declarations and instantiations across the final set. Comments
    # and `ifdef'd-out code get stripped so we don't pick up false hits.
    for f in rtl_files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = _strip_ifdef_blocks(text)

        for m in MODULE_DECL_RE.finditer(text):
            user_defined.add(m.group(1))

        for m in INSTANTIATION_RE.finditer(text):
            candidate = m.group(1)
            # The regex is loose; the keyword set is how we filter the
            # false positives back out.
            if candidate in VERILOG_KEYWORDS:
                continue
            instantiated.add(candidate)

    return DiscoveredModules(
        user_defined=user_defined,
        instantiated=instantiated,
        rtl_files=rtl_files,
    )


# ----- SRAM22 metadata + path resolution -----

@dataclass
class Sram22Info:
    """Everything we need to know about one sram22 macro to wire it up."""
    name: str           # e.g. sram22_64x32m4w8
    depth: int          # number of words (64)
    width: int          # word width in bits (32)
    mux: int            # the m# part of the name (column mux factor)
    wmask: int          # the w# part of the name (write mask granularity)
    addr_width: int     # log2(depth), needed for the blackbox stub
    base_dir: Path      # macro's directory under SRAM22_ROOT
    lef_file: Path
    lib_file: Path
    gds_file: Path
    verilog_sim: Path


def resolve_sram22(name: str, sram22_root: str = SRAM22_ROOT) -> Optional[Sram22Info]:
    """
    Turn a module name like sram22_64x32m4w8 into the paths Hammer needs.

    Returns None if the name doesn't look like a sram22 macro or if any of
    the deliverables are missing on disk. The caller can use that signal
    to decide whether to fall back to a different resolver (OpenRAM, etc.)
    or to warn the user.
    """
    m = SRAM22_NAME_RE.match(name)
    if not m:
        return None
    depth, width, mux, wmask = (int(x) for x in m.groups())
    base = Path(sram22_root) / name
    if not base.is_dir():
        return None
    # Check that all four deliverables are present. If the macro was only
    # half-installed we'd rather fail here than emit a config that points
    # at a missing file and watch Innovus blow up later.
    lef = base / f"{name}.lef"
    gds = base / f"{name}.gds"
    vsim = base / f"{name}.v"
    # The .lib file is per-corner; tt_025C_1v80 is the only one we care
    # about for the typical sky130 flow. It may have a .rc / .rcc / .c
    # suffix depending on extraction fidelity; pick whichever exists.
    lib_candidates = sorted(base.glob(f"{name}_tt_025C_1v80*.lib"))
    if not (lef.is_file() and gds.is_file() and vsim.is_file() and lib_candidates):
        return None
    addr_width = (depth - 1).bit_length()
    return Sram22Info(
        name=name,
        depth=depth, width=width, mux=mux, wmask=wmask,
        addr_width=addr_width,
        base_dir=base,
        lef_file=lef,
        lib_file=lib_candidates[0],
        gds_file=gds,
        verilog_sim=vsim,
    )


# ----- Blackbox stub generation -----

def generate_blackbox_stub(info: Sram22Info) -> str:
    """
    Make a port-only Verilog stub Genus can elaborate without synthesizing.

    The vendor's .v file is a full behavioral model: it has a `reg` array
    for storage and `always` blocks for the read/write logic. If you hand
    that to Genus it'll compile it just like any other RTL, which means
    your nice 64x32 SRAM becomes 2048 flip-flops sprinkled into the
    surrounding cache module. Worse, the macro instance vanishes from
    the netlist hierarchy, so par has nothing to call place_inst on.

    What we want instead is a stub that declares only the port list and
    no body. Genus treats it as a hard macro (since LEF/lib show up via
    extra_libraries in sky130.yml), preserves the instance in the netlist,
    and lets par handle the physical side.
    """
    return f"""// AUTO-GENERATED blackbox stub for {info.name}
// DO NOT EDIT. Regenerate via `studio design-register`.
// Sourced from {info.verilog_sim}

module {info.name}(
`ifdef USE_POWER_PINS
    vdd,
    vss,
`endif
  clk, we, wmask, addr, din, dout
);

  parameter DATA_WIDTH  = {info.width};
  parameter ADDR_WIDTH  = {info.addr_width};
  parameter WMASK_WIDTH = {info.wmask};
  parameter RAM_DEPTH   = 1 << ADDR_WIDTH;

`ifdef USE_POWER_PINS
  inout vdd;
  inout vss;
`endif
  input                       clk;
  input                       we;
  input  [WMASK_WIDTH-1:0]    wmask;
  input  [ADDR_WIDTH-1:0]     addr;
  input  [DATA_WIDTH-1:0]     din;
  output reg [DATA_WIDTH-1:0] dout;

  // No body. par/Innovus uses the LEF/lib instead.
endmodule
"""


# ----- Config file emission -----

def emit_common_yml(
    design: str,
    top_module: str,
    rtl_files: List[Path],
    blackbox_stubs: List[Path],
) -> str:
    """Build the common.yml that lists the top module and all input files."""
    lines: List[str] = []
    lines.append(f"# AUTO-GENERATED by studio design-register for '{design}'.")
    lines.append("# Regenerate (will overwrite) by running design-register again.")
    lines.append("")
    lines.append("vlsi.core.build_system: make")
    lines.append("")
    lines.append('vlsi.inputs.power_spec_type: "cpf"')
    lines.append('vlsi.inputs.power_spec_mode: "auto"')
    lines.append("")
    lines.append("synthesis.inputs:")
    lines.append(f'  top_module: "{top_module}"')
    lines.append("  input_files:")
    for f in rtl_files:
        lines.append(f'    - "{f}"')
    if blackbox_stubs:
        lines.append("    # Blackbox stubs for the macros the design instantiates.")
        lines.append("    # Do NOT swap these for the vendor's behavioral .v files. Genus")
        lines.append("    # will treat the behavioral model as RTL, compile its `reg mem[...]`")
        lines.append("    # storage into flip-flops, and quietly destroy the macro hierarchy.")
        lines.append("    # par then can't place the SRAM because there's no SRAM instance left.")
        for s in blackbox_stubs:
            lines.append(f'    - "{s}"')
    lines.append("")
    lines.append("sim.inputs:")
    lines.append(f'  top_module: "{top_module}"')
    lines.append("")
    lines.append("power.inputs:")
    lines.append(f'  top_module: "{top_module}"')
    lines.append("")
    return "\n".join(lines)


def emit_syn_yml(design: str, clock_ns: float) -> str:
    """Build syn.yml with the clock spec and a starting-point input delay."""
    # A common rule of thumb is to budget about 20% of the clock period for
    # input arrival, which gives synthesis some margin. Designs that need
    # tighter or looser numbers can edit this after generation.
    fifth = clock_ns / 5.0
    return f"""# AUTO-GENERATED by studio design-register for '{design}'.
# Regenerate (will overwrite) by running design-register again.

vlsi.inputs.clocks:
  - {{name: "clk", period: "{clock_ns}ns", uncertainty: "0.1ns"}}

# Starting point: input arrival = clock_period / 5.
# Tighten or loosen per design once you have real timing data.
vlsi.inputs.delays:
  - {{name: "mem*", clock: "clk", direction: "input", delay: "{fifth:.2f}ns"}}
"""


def emit_sky130_yml(design: str, clock_ns: float, srams: List[Sram22Info]) -> str:
    """Build sky130.yml: stackup, sim defines, and one extra_libraries entry per macro."""
    lines: List[str] = []
    lines.append(f"# AUTO-GENERATED by studio design-register for '{design}'.")
    lines.append("# Regenerate (will overwrite) by running design-register again.")
    lines.append("")
    lines.append('technology.core.stackup: "sky130_fd_sc_hd"')
    lines.append('vlsi.technology.placement_site: "unithd"')
    lines.append("")
    lines.append("sim.inputs:")
    lines.append(f'  defines: ["CLOCK_PERIOD={clock_ns}"]')
    lines.append('  defines_meta: "append"')
    lines.append("")

    if srams:
        # This block is the key piece. Without it, Innovus has no LEF/lib
        # for the SRAM macros and place_inst fails with IMPTCM-162. The
        # sky130 SRAM generator in hammer/technology/sky130/sram_compiler/
        # has the logic to produce these entries, but the combined syn_par
        # action doesn't invoke it, so we just build them directly.
        lines.append("# SRAM macro library bindings, one per macro the design uses.")
        lines.append("# These feed each macro's LEF + .lib + GDS through Hammer's tech")
        lines.append("# plugin into Innovus, which is what lets par's place_inst find")
        lines.append("# the macros and the timing engine model them as cells.")
        lines.append("vlsi.technology.extra_libraries:")
        for s in srams:
            lines.append("  - library:")
            lines.append(f'      name: "{s.name}"')
            lines.append(f'      nldm_liberty_file: "{s.lib_file}"')
            lines.append(f'      lef_file: "{s.lef_file}"')
            lines.append(f'      gds_file: "{s.gds_file}"')
            lines.append(f'      verilog_sim: "{s.verilog_sim}"')
            lines.append('      corner: {nmos: typical, pmos: typical, temperature: "25 C"}')
            lines.append('      supplies: {VDD: "1.80 V", GND: "0 V"}')
            lines.append('      provides: [{lib_type: sram, vt: svt}]')
    lines.append("")
    return "\n".join(lines)


def emit_par_yml(
    design: str,
    top_module: str,
    srams: List[Sram22Info],
    fp_width: int = 1600,
    fp_height: int = 1600,
) -> str:
    """
    Build par.yml with the toplevel floorplan + a skeleton for macro placements.

    We can't auto-place the macros yet (that's the design-suggest-placements
    feature on the roadmap), so for every SRAM we just leave a commented-out
    TODO block with the right shape. The user needs to:

      1. Run syn once to see how the netlist hierarchy actually names the
         macro instances. Generate blocks turn into things like
         "sram_data[0].sram" or "data_srams[0].dsram" depending on the RTL.
      2. Uncomment one entry per instance and fill in x/y/orientation.
      3. Re-run par.
    """
    lines: List[str] = []
    lines.append(f"# AUTO-GENERATED by studio design-register for '{design}'.")
    lines.append("# Re-running design-register will overwrite this file. If you've added")
    lines.append("# custom placement constraints, copy them out first.")
    lines.append("")
    lines.append("vlsi.inputs.placement_constraints:")
    lines.append(f'  - path: "{top_module}"')
    lines.append("    type: toplevel")
    lines.append(f"    margins: {{left: 0, right: 0, top: 0, bottom: 0}}")
    lines.append("    x: 0")
    lines.append("    y: 0")
    lines.append(f"    width: {fp_width}")
    lines.append(f"    height: {fp_height}")
    if srams:
        lines.append("")
        lines.append("  # ==== MACRO PLACEMENTS — YOU NEED TO FILL THESE IN ====")
        lines.append("  # Every macro instance the design has must be placed somewhere,")
        lines.append("  # or routing will die with NRIG-91 'not placed on the manufacturing")
        lines.append("  # grid' once it hits the unplaced macro.")
        lines.append("  #")
        lines.append("  # The skeleton below has one entry per macro module, but the design")
        lines.append("  # probably has multiple instances of each (generate blocks etc.).")
        lines.append("  # Easiest workflow: run syn once, then grep the *.mapped.v for the")
        lines.append("  # macro module names to find every instance path, then add one")
        lines.append("  # placement entry per instance.")
        for s in srams:
            lines.append("")
            lines.append("  # TODO: copy this block per instance and fill in path + x + y.")
            lines.append(f'  # - path: "{top_module}/<instance_path>/{s.name}_inst"')
            lines.append("  #   type: hardmacro")
            lines.append("  #   x: 100")
            lines.append("  #   y: 100")
            lines.append('  #   orientation: "r0"')
            lines.append('  #   top_layer: "met3"')
    lines.append("")
    lines.append("vlsi.inputs.pin_mode: generated")
    lines.append("vlsi.inputs.pin.generate_mode: semi_auto")
    lines.append("vlsi.inputs.pin.assignments:")
    lines.append('  - {pins: "*", layers: ["met4"], side: "bottom"}')
    lines.append("")
    return "\n".join(lines)


# ----- Top-level orchestration -----

def register_design(
    name: str,
    top_module: str,
    clock_ns: float,
    rtl_paths: List[Path],
    pdk: str = "sky130",
    out_dir: Optional[Path] = None,
    sram22_root: str = SRAM22_ROOT,
    exclude_patterns: Optional[List[str]] = None,
    use_default_excludes: bool = True,
) -> Tuple[Path, List[str]]:
    """
    Scan the RTL, work out the macros, and write all four configs to disk.

    Returns (output_dir, warnings) where warnings is a list of human-readable
    strings the caller should surface to the user, mostly about manual steps
    they still need to take (macro placements, undefined modules we don't
    recognize, etc.).
    """
    if pdk != "sky130":
        raise NotImplementedError(f"PDK '{pdk}' not supported yet; only sky130")

    if out_dir is None:
        # If we're running from a Hammer repo checkout, drop into the same
        # e2e/configs-design/ layout the rest of the project uses. Otherwise
        # fall back to a relative path; the caller can override with --out-dir.
        cwd = Path.cwd()
        if (cwd / "e2e").is_dir():
            out_dir = cwd / "e2e" / "configs-design" / name
        else:
            out_dir = Path("e2e/configs-design") / name

    out_dir.mkdir(parents=True, exist_ok=True)
    stubs_dir = out_dir / "sram_blackbox_stubs"
    stubs_dir.mkdir(parents=True, exist_ok=True)

    warnings: List[str] = []

    # Step 1: walk the RTL and figure out the module landscape.
    print(f"Scanning RTL under: {[str(p) for p in rtl_paths]}", file=sys.stderr)
    discovered = scan_rtl_directory(
        rtl_paths,
        exclude_patterns=exclude_patterns,
        use_default_excludes=use_default_excludes,
    )
    print(f"  Found {len(discovered.rtl_files)} RTL files", file=sys.stderr)
    print(f"  Defined modules: {len(discovered.user_defined)}", file=sys.stderr)
    print(f"  Undefined references: {sorted(discovered.undefined_references)}", file=sys.stderr)

    # Step 2: classify the undefined references. The ones that match the
    # sram22 naming pattern go to the resolver. Anything else gets a warning;
    # Genus will refuse to elaborate it unless the user does something about it.
    srams: List[Sram22Info] = []
    other_undefined: List[str] = []
    for mod in sorted(discovered.undefined_references):
        info = resolve_sram22(mod, sram22_root=sram22_root)
        if info is not None:
            srams.append(info)
        elif SRAM22_NAME_RE.match(mod):
            # Name parses as sram22 but the macro dir isn't here. Probably
            # a different cluster, or a typo, or a half-installed macro.
            warnings.append(
                f"Module '{mod}' looks like a sram22 macro but the directory "
                f"{Path(sram22_root) / mod} doesn't exist on this host."
            )
        else:
            other_undefined.append(mod)

    if other_undefined:
        warnings.append(
            f"Undefined module(s) that aren't recognized macro types: "
            f"{other_undefined}. Genus will fail at elaborate unless you "
            f"provide stubs for these or set hdl_error_on_blackbox=false."
        )

    print(f"  Resolved {len(srams)} sram22 macros: {[s.name for s in srams]}", file=sys.stderr)

    # Step 3: write a blackbox stub for each macro we resolved. These are
    # what Genus sees during syn (so it doesn't synthesize the macro's
    # storage into FFs).
    stub_paths: List[Path] = []
    for s in srams:
        stub_path = stubs_dir / f"{s.name}.v"
        stub_path.write_text(generate_blackbox_stub(s))
        stub_paths.append(stub_path)
        print(f"  Wrote stub: {stub_path}", file=sys.stderr)

    # Step 4: write the four config files.
    (out_dir / "common.yml").write_text(
        emit_common_yml(name, top_module, discovered.rtl_files, stub_paths)
    )
    (out_dir / "syn.yml").write_text(emit_syn_yml(name, clock_ns))
    (out_dir / "sky130.yml").write_text(emit_sky130_yml(name, clock_ns, srams))
    (out_dir / "par.yml").write_text(emit_par_yml(name, top_module, srams))

    print(f"\nWrote configs to: {out_dir}", file=sys.stderr)

    if srams:
        warnings.append(
            f"par.yml needs hand-edits: add placement constraints for the "
            f"{len(srams)} SRAM macro instance(s) before triggering par. "
            f"Use a syn run to find the actual hierarchical instance names "
            f"(grep '*.mapped.v' for the macro module names)."
        )

    return out_dir, warnings


def lint_par_yml(design_dir: Path) -> List[str]:
    """
    Scan par.yml for commented-out placement constraints that look like SRAM
    macros. These are the classic "user uncommented some but forgot others"
    bug pattern that leads to PAR's NRIG-91 (off-grid macro) routing abort.

    Returns a list of warning lines. Empty list if everything looks clean.
    """
    par_yml = design_dir / "par.yml"
    if not par_yml.exists():
        return []

    warnings: List[str] = []
    suspicious: List[Tuple[int, str]] = []
    pattern = re.compile(r'^\s*#\s*-\s*path:\s*["\']?.*sram', re.IGNORECASE)
    for lineno, raw in enumerate(par_yml.read_text().splitlines(), start=1):
        if pattern.match(raw):
            suspicious.append((lineno, raw.strip()))

    if suspicious:
        warnings.append(
            f"{par_yml.name} has {len(suspicious)} SRAM placement constraint(s) "
            f"still commented out. PAR routing will fail with NRIG-91 if these "
            f"macros are actually instantiated. Review or remove:"
        )
        for lineno, line in suspicious:
            warnings.append(f"    L{lineno}: {line}")

    return warnings


def _emit_sky130_extras_yml(srams: List[Sram22Info]) -> str:
    lines: List[str] = []
    lines.append("# AUTO-GENERATED by studio augment.")
    lines.append("# SRAM macro library bindings only. The user's other sky130")
    lines.append("# config (stackup, placement_site, etc.) stays in sky130.yml.")
    lines.append("# Include both files in your hammer-vlsi run.")
    lines.append("")
    if not srams:
        lines.append("# No SRAM macros detected. This file is intentionally empty.")
        return "\n".join(lines) + "\n"
    lines.append("vlsi.technology.extra_libraries:")
    for s in srams:
        lines.append("  - library:")
        lines.append(f'      name: "{s.name}"')
        lines.append(f'      nldm_liberty_file: "{s.lib_file}"')
        lines.append(f'      lef_file: "{s.lef_file}"')
        lines.append(f'      gds_file: "{s.gds_file}"')
        lines.append(f'      verilog_sim: "{s.verilog_sim}"')
        lines.append('      corner: {nmos: typical, pmos: typical, temperature: "25 C"}')
        lines.append('      supplies: {VDD: "1.80 V", GND: "0 V"}')
        lines.append('      provides: [{lib_type: sram, vt: svt}]')
    lines.append("")
    return "\n".join(lines)


def augment_existing_design(
    design_dir: Path,
    sram22_root: str = SRAM22_ROOT,
) -> Tuple[List[Sram22Info], List[str]]:
    """
    Read configs-design/<name>/common.yml, detect SRAMs in the listed RTL,
    and write blackbox stubs + sky130-extras.yml. Leaves the user's existing
    common.yml / syn.yml / sky130.yml / par.yml untouched.
    """
    import yaml

    warnings: List[str] = []
    # A registered project's SRAMs are already bound through Hammer's sram_generator, in the
    # sky130-extras.yml this would overwrite with a single-corner guess.
    if (design_dir / REGISTRATION_FILE).is_file():
        warnings.append(
            f"{design_dir.name} is registered from a project (see {REGISTRATION_FILE}); its SRAMs are "
            f"bound by `studio register-projects`, so augment left it unchanged."
        )
        return [], warnings

    common_yml = design_dir / "common.yml"
    if not common_yml.is_file():
        raise FileNotFoundError(
            f"{common_yml} does not exist. augment expects the design's "
            f"common.yml to already be in place (with synthesis.inputs.input_files)."
        )

    with common_yml.open("r") as f:
        cfg = yaml.safe_load(f) or {}

    syn_in = (cfg.get("synthesis.inputs")
              or cfg.get("synthesis", {}).get("inputs", {}))
    input_files = syn_in.get("input_files", []) if isinstance(syn_in, dict) else []
    if not input_files:
        input_files = cfg.get("synthesis.inputs.input_files", []) or []

    if not input_files:
        raise ValueError(
            f"Couldn't find synthesis.inputs.input_files in {common_yml}. "
            f"Make sure the design's common.yml lists its RTL."
        )

    rtl_paths = [Path(p) for p in input_files]
    print(f"Scanning {len(rtl_paths)} RTL files listed in {common_yml.name}...",
          file=sys.stderr)
    discovered = scan_rtl_directory(rtl_paths, use_default_excludes=False)
    print(f"  Undefined references: {sorted(discovered.undefined_references)}",
          file=sys.stderr)

    srams: List[Sram22Info] = []
    for mod in sorted(discovered.undefined_references):
        info = resolve_sram22(mod, sram22_root=sram22_root)
        if info is not None:
            srams.append(info)
        elif SRAM22_NAME_RE.match(mod):
            warnings.append(
                f"'{mod}' looks like a sram22 macro but isn't installed at "
                f"{Path(sram22_root) / mod}."
            )

    print(f"  Resolved {len(srams)} sram22 macros: {[s.name for s in srams]}",
          file=sys.stderr)

    stubs_dir = design_dir / "sram_blackbox_stubs"
    stubs_dir.mkdir(parents=True, exist_ok=True)
    stub_paths: List[Path] = []
    for s in srams:
        p = stubs_dir / f"{s.name}.v"
        p.write_text(generate_blackbox_stub(s))
        stub_paths.append(p)
        print(f"  Wrote stub: {p}", file=sys.stderr)

    extras_path = design_dir / "sky130-extras.yml"
    extras_path.write_text(_emit_sky130_extras_yml(srams))
    print(f"  Wrote: {extras_path}", file=sys.stderr)

    if stub_paths:
        stubs_listed = any(
            str(stubs_dir) in str(Path(p)) or "sram_blackbox_stubs" in str(p)
            for p in input_files
        )
        if not stubs_listed:
            warnings.append(
                f"Blackbox stubs were written to {stubs_dir}, but common.yml's "
                f"synthesis.inputs.input_files doesn't list them. Add these to "
                f"that list so Genus reads them:\n    "
                + "\n    ".join(f'- "{p}"' for p in stub_paths)
            )

    if srams:
        warnings.append(
            f"Add 'sky130-extras.yml' to your hammer-vlsi -p list so par "
            f"picks up the SRAM LEF/lib bindings."
        )

    warnings.extend(lint_par_yml(design_dir))

    return srams, warnings


# ----- Registering EECS151 projects -----
#
# Everything above starts from a bare directory of RTL. What follows starts
# from a finished EECS151 project (src/original/<project>/, with its own
# Makefile and Hammer YAML) and turns it into an ordinary SledgeHammer design:
# e2e/configs-design/<design>/, which the e2e Makefile and `studio make-dag`
# read like any other design. The project tree is only ever read.
#
# The project's own configs are the source of truth. Its Makefile names the
# tech, syn and par configs it feeds syn and par, and those give the top
# module, the RTL in the order Genus reads it, defines, clocks, delays,
# floorplan, power straps and stdcell library. What described the class
# machines (tool versions and binaries, PDK install paths, licenses, the tool
# choice) is dropped: configs-env, configs-pdk and configs-tool supply
# SledgeHammer's own. Paths into the project are rewritten relative to the
# generated config with prependlocal, so the configs hold from any checkout.
#
# SRAMs are what the RTL instantiates without defining. slang (the binary the
# RTL fingerprint already runs) elaborates the design from the top module, so
# only instances the design actually reaches count, with their real hierarchy
# paths and `ifdef branches resolved. Each must be an SRAM in Hammer's
# sram-cache.json, and Hammer's own sram_generator binds it at every MMMC
# corner, exactly as `make srams` does, instead of the stubs above: Genus
# resolves the instance from the macro's Liberty, as in the EECS151 flow.

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTS_DIR = REPO_ROOT / "src" / "original"
# Marks configs-design/<design>/ as written from a project, and records which.
REGISTRATION_FILE = "registration.json"
# The design's eco stage settings. Registration creates a default for a modified copy once and
# otherwise leaves it to the user.
ECO_FILE = "eco.yml"
# Under configs-design/, shared by every registered design: the sram22 macros
# they use, with any GDS the install ships only gzipped decompressed.
SRAM_MIRROR_DIR = ".sram22_sky130_macros"

# The EECS151 skeleton's Makefile variables for the configs syn and par read,
# and the file each one defaults to.
_MAKEFILE_CONFIG_VARS = {"TECH_CONF": "tech", "DESIGN_SYN_CONF": "syn", "DESIGN_PAR_CONF": "par"}
_SKELETON_CONFIGS = {"tech": "sky130.yml", "syn": "syn.yml", "par": "par.yml"}
# The order the skeleton passes them in, later ones winning.
_CONFIG_ROLES = ("tech", "syn", "par")

_HAMMER_NAMESPACES = {"vlsi", "technology", "synthesis", "par", "drc", "lvs", "sim", "power",
                      "formal", "timing", "pcb", "sram_generator"}
_SITE_NAMESPACES = {"cadence", "synopsys", "mentor"}
_TOOL_INSTALL_KEY = re.compile(
    r"^(?:synthesis|par|drc|lvs|sim|power|timing|formal)\.[^.]+\.(?:version|\w*_bin|\w*_home)$")
# vlsi.core.* picks the tools, technology and build system; max_threads is the
# one flow setting in it (the default is a single thread).
_KEPT_CORE_KEYS = {"vlsi.core.max_threads"}
# Rewritten by registration rather than carried over.
_REWRITTEN_KEYS = {"synthesis.inputs.input_files", "synthesis.inputs.include_dirs",
                   "vlsi.inputs.sram_parameters"}
_PATH_SUFFIXES = (".v", ".sv", ".vh", ".svh", ".sdc", ".tcl", ".yml", ".yaml", ".json", ".lef",
                  ".lib", ".gds", ".cpf", ".upf", ".def")
# A design name becomes part of the Airflow dag id and make's design=.
_DESIGN_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAKE_ASSIGNMENT = re.compile(r"^\s*(\w+)\s*(?:\?|:{1,2}|\+)?=\s*(.*?)\s*$")


class RegistrationError(Exception):
    """Why a project cannot become a design. The message says what to fix."""


@dataclass
class ElaboratedRtl:
    """What slang's elaboration of the top module says about the RTL."""
    # module -> one (instance path, connected port names, under a generate scope) per instance
    undefined: Dict[str, List[Tuple[str, List[str], bool]]]
    packages: Dict[str, str]        # package -> file that declares it
    includes: List[str]             # `include files slang read
    unused_files: List[str]         # input files nothing in the elaborated design came from


@dataclass
class ProjectRegistration:
    """One registered project, for the caller's report."""
    project_dir: Path
    design: str
    design_dir: Path
    configs: Dict[str, Path]
    top_module: str
    input_files: List[Path]
    srams: Dict[str, List[str]]     # macro -> instance paths
    clocks: List[Any]
    dropped: Dict[str, str]         # project setting -> why it was not carried over
    unused_files: List[str]
    written: List[str]              # generated files that changed
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def discover_projects(projects_dir: Path = PROJECTS_DIR) -> List[Path]:
    """Every project under projects_dir, one per directory, in name order. Hidden directories are skipped."""
    if not projects_dir.is_dir():
        raise RegistrationError(f"{projects_dir} does not exist; put each project in its own directory there.")
    return sorted(p for p in projects_dir.iterdir() if p.is_dir() and not p.name.startswith("."))


def discover_variants(projects_dir: Path = PROJECTS_DIR) -> List[Tuple[str, Path]]:
    """
    Every modified copy of a project: (tree, project directory) for each
    src/<tree>/<project>, where <tree> is any directory beside original/
    (Test1, test_2, ...) and src/original/<project> is the copy's original.
    """
    found: List[Tuple[str, Path]] = []
    for tree in sorted(projects_dir.parent.iterdir()):
        if tree.is_dir() and not tree.name.startswith(".") and tree.resolve() != projects_dir.resolve():
            found.extend((tree.name, p) for p in discover_projects(tree))
    return found


def variant_design(project: str, tree: str) -> str:
    """The design a modified copy registers as, beside its original's."""
    return f"{project}_{tree}"


def find_project_configs(project_dir: Path) -> Dict[str, Path]:
    """
    The configs the project's own flow feeds syn and par, by role (tech, syn, par).

    The Makefile is the source of truth: the EECS151 skeleton names them in
    TECH_CONF, DESIGN_SYN_CONF and DESIGN_PAR_CONF under $(vlsi_dir), the
    project directory. A role it leaves unset falls back to the skeleton's
    file name. syn is required, since the top module and RTL live there.
    """
    found: Dict[str, Path] = {}
    makefile = project_dir / "Makefile"
    if makefile.is_file():
        for line in makefile.read_text(errors="replace").splitlines():
            m = _MAKE_ASSIGNMENT.match(line.split("#", 1)[0])
            role = _MAKEFILE_CONFIG_VARS.get(m.group(1)) if m else None
            if role is None or role in found:
                continue
            value = re.sub(r"\$[({](?:vlsi_dir|CURDIR)[)}]|\$\(abspath \.\)", ".", m.group(2))
            if "$" in value:
                raise RegistrationError(
                    f"the Makefile sets {m.group(1)} to {m.group(2)!r}, which uses make variables "
                    f"registration cannot resolve.")
            found[role] = project_dir / value
    for role, name in _SKELETON_CONFIGS.items():
        if role not in found and (project_dir / name).is_file():
            found[role] = project_dir / name
    if "syn" not in found:
        raise RegistrationError(
            f"no synthesis config: the Makefile sets no DESIGN_SYN_CONF and there is no "
            f"{_SKELETON_CONFIGS['syn']}.")
    for role, path in found.items():
        if not path.is_file():
            raise RegistrationError(f"the {role} config {path.name} the Makefile names does not exist.")
        if not _inside(path, project_dir):
            raise RegistrationError(f"the {role} config {path} is outside the project.")
    return {role: found[role] for role in _CONFIG_ROLES if role in found}


def load_project_settings(configs: Dict[str, Path]) -> Dict[str, Tuple[Any, str]]:
    """
    Every setting in the project's configs, flattened as Hammer reads them and
    later roles winning: key -> (value, role it came from). A <key>_meta is a
    key of its own.
    """
    from hammer.config import load_config_from_string
    from hammer.config.config_src import _CONFIG_PATH_KEY

    settings: Dict[str, Tuple[Any, str]] = {}
    for role, path in configs.items():
        try:
            loaded = load_config_from_string(path.read_text(), is_yaml=True, path=str(path.parent))
        except Exception as e:
            raise RegistrationError(f"{path.name} does not load as a Hammer config: {e}") from None
        for key, value in loaded.items():
            if key != _CONFIG_PATH_KEY:
                settings[key] = (value, role)
    return settings


def _meta_base(settings: Dict[str, Tuple[Any, str]], key: str, configs: Dict[str, Path],
               project_dir: Path) -> Path:
    """
    What a relative path in this setting is relative to. The skeleton runs
    Hammer from the project directory, so a bare relative path is relative to
    it; under prependlocal it is relative to its own config file.
    """
    meta = settings.get(f"{key}_meta", (None, ""))[0]
    metas = [meta] if isinstance(meta, str) else list(meta or [])
    if not metas:
        return project_dir
    if metas == ["prependlocal"]:
        return configs[settings[key][1]].parent
    raise RegistrationError(
        f"{key} in {configs[settings[key][1]].name} uses the meta directive(s) {metas}, which "
        f"registration cannot translate yet.")


def _project_paths(settings: Dict[str, Tuple[Any, str]], key: str, configs: Dict[str, Path],
                   project_dir: Path) -> List[Path]:
    """
    The paths a setting lists, resolved as the project's flow resolves them.
    Each must exist inside the project; a path anywhere else only exists on
    the machine the project was written on.
    """
    if key not in settings:
        return []
    value, role = settings[key]
    source = configs[role].name
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(v, str) for v in items):
        raise RegistrationError(f"{key} in {source} must be a list of paths, not {value!r}.")
    base = _meta_base(settings, key, configs, project_dir)
    resolved: List[Path] = []
    missing: List[str] = []
    outside: List[str] = []
    for item in items:
        path = Path(os.path.normpath(base / item))
        if not path.exists():
            missing.append(item)
        elif not _inside(path, project_dir):
            outside.append(item)
        else:
            resolved.append(path)
    if missing:
        raise RegistrationError(f"{key} in {source} lists paths that do not exist: {', '.join(missing)}")
    if outside:
        raise RegistrationError(
            f"{key} in {source} lists paths outside the project, which exist only on the machine it was "
            f"written on: {', '.join(outside)}")
    twice = sorted({os.path.relpath(p, project_dir) for p in resolved if resolved.count(p) > 1})
    if twice:
        raise RegistrationError(f"{key} in {source} lists the same path more than once: {', '.join(twice)}")
    return resolved


def _is_site_path(value: Any) -> bool:
    """A path on some machine: absolute, under a home directory, or through an environment variable."""
    if isinstance(value, str):
        return value.startswith(("/", "~", "$"))
    return isinstance(value, list) and bool(value) and all(_is_site_path(v) for v in value)


def _absolute_paths(value: Any) -> List[str]:
    """Every absolute or home-relative path anywhere inside value."""
    if isinstance(value, str):
        return [value] if value.startswith(("/", "~")) else []
    if isinstance(value, dict):
        return [p for v in value.values() for p in _absolute_paths(v)]
    if isinstance(value, list):
        return [p for v in value for p in _absolute_paths(v)]
    return []


def _site_specific(key: str, value: Any) -> Optional[str]:
    """Why SledgeHammer's configs, rather than the project's, own this setting; None if it carries over."""
    namespace = key.split(".", 1)[0]
    if namespace in _SITE_NAMESPACES:
        return "licenses and tool installs come from configs-env"
    if namespace not in _HAMMER_NAMESPACES:
        return "not a Hammer setting"
    if key.startswith("vlsi.core.") and key not in _KEPT_CORE_KEYS:
        return "tools, technology and build system come from configs-pdk, configs-tool and the DAG"
    if _TOOL_INSTALL_KEY.match(key):
        return "tool versions and binaries come from configs-env"
    if namespace == "technology" and _is_site_path(value):
        return "PDK install paths come from configs-env"
    return None


def _translated_paths(value: Any, base: Path, project_dir: Path, design_dir: Path) -> Any:
    """
    value rewritten relative to design_dir if it is a path, or a list of
    paths, to files inside the project; None if it is not.
    """
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items or not all(isinstance(v, str) for v in items):
        return None
    if not all(("/" in v or v.endswith(_PATH_SUFFIXES)) and not v.startswith(("/", "~", "$")) for v in items):
        return None
    paths = [Path(os.path.normpath(base / v)) for v in items]
    if not all(p.exists() and _inside(p, project_dir) for p in paths):
        return None
    rel = [os.path.relpath(p, design_dir) for p in paths]
    return rel[0] if isinstance(value, str) else rel


def _translate_settings(settings: Dict[str, Tuple[Any, str]], configs: Dict[str, Path], project_dir: Path,
                        design_dir: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    """The project's settings that carry over, by the role they came from, and those dropped with why."""
    carried: Dict[str, Dict[str, Any]] = {role: {} for role in _CONFIG_ROLES}
    dropped: Dict[str, str] = {}
    for key, (value, role) in settings.items():
        if key == "vlsi.inputs.sram_parameters":
            dropped[key] = "replaced by the SRAMs the RTL instantiates"
        if key.endswith("_meta") or key in _REWRITTEN_KEYS:
            continue
        why = _site_specific(key, value)
        if why:
            dropped[key] = why
            continue
        base = _meta_base(settings, key, configs, project_dir)
        paths = _translated_paths(value, base, project_dir, design_dir)
        if paths is not None:
            carried[role][key] = paths
            carried[role][f"{key}_meta"] = "prependlocal"
            continue
        if f"{key}_meta" in settings:
            raise RegistrationError(
                f"{key} in {configs[role].name} uses prependlocal on something that is not a path in the "
                f"project: {value!r}.")
        absolute = _absolute_paths(value)
        if absolute:
            raise RegistrationError(
                f"{key} in {configs[role].name} names {absolute[0]}, a path on the machine the project was "
                f"written on. Put the file in the project and name it by a relative path.")
        carried[role][key] = value
    for key in settings:
        if key.endswith("_meta") and key[:-len("_meta")] in dropped:
            dropped[key] = dropped[key[:-len("_meta")]]
    return carried, dropped


def _slang_ast(files: List[Path], include_dirs: List[Path], defines: List[str], top: Optional[str],
               parsed: Optional[List[str]] = None) -> Dict[str, Any]:
    from hammer.vlsi import rtl_check
    try:
        return rtl_check._run_slang([str(f) for f in files], [str(d) for d in include_dirs],
                                    [str(d) for d in defines], top, parsed=parsed)
    except rtl_check.SlangNotFound as e:
        raise RegistrationError(f"slang is needed to elaborate the RTL. {e}") from None
    except rtl_check.RtlParseError as e:
        raise RegistrationError(f"slang could not elaborate the RTL:\n{e.report.rstrip()}") from None


def _root_modules(files: List[Path], include_dirs: List[Path], defines: List[str]) -> List[str]:
    """The modules nothing instantiates, which slang elaborates as tops when given none."""
    ast = _slang_ast(files, include_dirs, defines, None)
    return sorted(m["name"] for m in ast["design"].get("members", []) if m.get("kind") == "Instance")


def _collect_undefined(members: List[Any], path: List[str], in_generate: bool, index: Dict[str, Any],
                       out: Dict[str, List[Tuple[str, List[str], bool]]]) -> None:
    """Walk an elaborated scope, recording every instance of a module the RTL does not define."""
    for node in members:
        if not isinstance(node, dict):
            continue
        kind = node.get("kind")
        if kind == "Instance":
            body = node.get("body", {})
            if isinstance(body, str):
                # slang writes a body shared by several instances once, then "<addr> <module>"
                body = index.get(body.split(" ", 1)[0], {})
            _collect_undefined(body.get("members", []), path + [node.get("name") or "?"], in_generate, index,
                               out)
        elif kind == "UninstantiatedDef":
            ports = [p.get("name", "") for p in node.get("ports", []) if isinstance(p, dict)]
            out.setdefault(node["definitionName"], []).append(
                ("/".join(path + [node.get("name", "")]), ports, in_generate))
        elif kind in ("GenerateBlock", "GenerateBlockArray", "InstanceArray") and not node.get("isUninstantiated"):
            # The netlist names these scopes its own way (genblk1, banks_0_, ...), so
            # paths through them are approximate.
            scope = path + [node["name"]] if node.get("name") else path
            _collect_undefined(node.get("members", []), scope, True, index, out)


def _collect_packages(node: Any, out: Dict[str, str]) -> None:
    if isinstance(node, dict):
        if node.get("kind") == "Package" and node.get("name") and node.get("source_file"):
            out.setdefault(node["name"], node["source_file"])
        for value in node.values():
            _collect_packages(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_packages(value, out)


def elaborate_rtl(files: List[Path], include_dirs: List[Path], defines: List[str], top: str) -> ElaboratedRtl:
    """Elaborate top from the input files, in order, as one compilation unit, the way Genus reads them."""
    from hammer.vlsi import rtl_check
    parsed: List[str] = []
    ast = _slang_ast(files, include_dirs, defines, top, parsed)
    tops = [m for m in ast["design"].get("members", []) if m.get("kind") == "Instance"]
    if [m.get("name") for m in tops] != [top]:
        raise RegistrationError(
            f"slang elaborated {[m.get('name') for m in tops]} rather than the top module {top}.")
    index: Dict[str, Any] = {}
    rtl_check._index_by_addr(ast, index)
    undefined: Dict[str, List[Tuple[str, List[str], bool]]] = {}
    _collect_undefined(tops[0].get("body", {}).get("members", []), [top], False, index, undefined)
    packages: Dict[str, str] = {}
    _collect_packages(ast, packages)
    contributing: Set[str] = set()
    rtl_check._contributing_files(ast["design"], contributing)
    inputs = {os.path.realpath(f) for f in files}
    return ElaboratedRtl(
        undefined=undefined,
        packages=packages,
        includes=[p for p in parsed if p not in inputs],
        unused_files=[str(f) for f in files if os.path.realpath(f) not in contributing],
    )


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", text), flags=re.DOTALL)


def _check_package_order(files: List[Path], packages: Dict[str, str], project_dir: Path) -> None:
    """Genus reads input_files in order, so a package has to come before the first file that uses it."""
    position = {os.path.realpath(f): i for i, f in enumerate(files)}
    late: List[str] = []
    for i, f in enumerate(files):
        used = set(re.findall(r"\b(\w+)\s*::", _strip_comments(f.read_text(errors="replace"))))
        for pkg in sorted(used & set(packages)):
            j = position.get(os.path.realpath(packages[pkg]))
            if j is not None and j > i:
                late.append(f"{os.path.relpath(f, project_dir)} uses {pkg}, which "
                            f"{os.path.relpath(packages[pkg], project_dir)} declares later")
    if late:
        raise RegistrationError(
            "synthesis.inputs.input_files lists a package after a file that uses it, and Genus reads them "
            "in order:\n  " + "\n  ".join(late))


def sram_catalog(pdk: str) -> Dict[str, dict]:
    """Hammer's catalog of a technology's SRAMs (hammer/technology/<pdk>/sram-cache.json), by name."""
    path = Path(__file__).resolve().parents[1] / "technology" / pdk / "sram-cache.json"
    if not path.is_file():
        return {}
    return {entry["name"]: entry for entry in json.loads(path.read_text())}


def _check_macro_placements(settings: Dict[str, Tuple[Any, str]],
                            srams: Dict[str, List[Tuple[str, List[str], bool]]]) -> List[str]:
    """
    Match the project's hardmacro placement constraints against the SRAM
    instances in the elaborated design. A constraint naming no SRAM would stop
    Innovus at place_inst, so it is an error when the paths can be compared
    exactly; an SRAM without one is left for par to place, with a warning.
    """
    constraints = settings.get("vlsi.inputs.placement_constraints", ([], ""))[0] or []
    placed = {str(c.get("path")) for c in constraints
              if isinstance(c, dict) and str(c.get("type")) == "hardmacro"}
    instances = {path: in_generate for found in srams.values() for path, _, in_generate in found}
    warnings: List[str] = []
    stray = sorted(placed - set(instances))
    if stray:
        message = (f"hardmacro placement constraints name instances that are not SRAMs in the elaborated "
                   f"design: {', '.join(stray)}")
        if not any(instances.values()):
            raise RegistrationError(message + ". Innovus would stop at place_inst; the project's par config "
                                              "needs fixing.")
        warnings.append(message + " (some SRAMs sit under generate blocks, whose netlist names this check "
                                  "cannot predict)")
    unplaced = sorted(set(instances) - placed)
    if unplaced:
        warnings.append(f"{len(unplaced)} SRAM instance(s) have no hardmacro placement constraint, so par "
                        f"has to place them itself: {', '.join(unplaced)}")
    return warnings


def _sram_install(env_conf: Path, names: List[str]) -> Tuple[Path, bool]:
    """
    The sram22 install configs-env points at, and whether the design needs it
    mirrored: Hammer's sram_generator names <macro>.gds, and par merges every
    macro GDS into its output (Hammer checks each one exists), but some
    installs ship the GDS only gzipped.
    """
    from hammer.config import load_config_from_string
    key = "technology.sky130.sram22_sky130_macros"
    root = load_config_from_string(env_conf.read_text(), is_yaml=True, path=str(env_conf.parent)).get(key)
    if not root:
        raise RegistrationError(
            f"the RTL instantiates SRAMs ({', '.join(names)}), but {env_conf.name} sets no {key}.")
    site = Path(root)
    mirror = False
    for name in names:
        macro = site / name
        if not macro.is_dir():
            raise RegistrationError(f"SRAM {name} is in Hammer's catalog but not installed under {site}.")
        if not (macro / f"{name}.gds").is_file():
            if not (macro / f"{name}.gds.gz").is_file():
                raise RegistrationError(f"SRAM {name} has no GDS under {macro}.")
            mirror = True
    return site, mirror


def _link(target: Path, link: Path) -> None:
    if link.is_symlink() and os.readlink(link) == str(target):
        return
    tmp = link.with_name(f".{link.name}.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


def _mirror_srams(site: Path, mirror: Path, names: List[str]) -> None:
    """
    Make mirror look like the sram22 install for these macros: a link to each
    file, except a GDS shipped only gzipped, which is decompressed. Shared by
    every registered design, so it only ever gains macros.
    """
    mirror.mkdir(parents=True, exist_ok=True)
    if (site / "sram22.spice").is_file():
        # the technology's LVS netlist for every sram22 macro
        _link(site / "sram22.spice", mirror / "sram22.spice")
    for name in names:
        out = mirror / name
        out.mkdir(exist_ok=True)
        for src in sorted((site / name).iterdir()):
            plain = src.name[:-len(".gz")]
            if src.name.endswith(".gds.gz") and not (site / name / plain).exists():
                dst = out / plain
                if not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime:
                    tmp = out / f".{plain}.tmp"
                    with gzip.open(src, "rb") as fin, open(tmp, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    os.replace(tmp, dst)
            else:
                _link(src, out / src.name)


def _module_ports(verilog: Path, module: str) -> Optional[Set[str]]:
    """The port names in the header of module in a Verilog file, or None if the file does not declare it."""
    text = _strip_comments(verilog.read_text(errors="replace"))
    m = re.search(rf"\bmodule\s+{re.escape(module)}\s*(?:#\s*\(.*?\)\s*)?\((.*?)\)\s*;", text, re.DOTALL)
    if not m:
        return None
    header = "\n".join(line for line in m.group(1).splitlines() if not line.strip().startswith("`"))
    names: Set[str] = set()
    for item in header.split(","):
        words = re.findall(r"[A-Za-z_]\w*", re.sub(r"\[[^\]]*\]", "", item))
        if words:
            names.add(words[-1])
    return names


def _check_sram_libraries(libraries: List[dict], srams: Dict[str, List[Tuple[str, List[str], bool]]]) -> None:
    """Every view the generator bound must exist, and every SRAM instance may connect only the macro's ports."""
    missing: List[str] = []
    bound: Dict[str, dict] = {}
    for entry in libraries:
        library = entry.get("library", {})
        bound.setdefault(library.get("name"), library)
        for view in ("nldm_liberty_file", "lef_file", "gds_file", "verilog_sim"):
            if library.get(view) and not Path(library[view]).is_file():
                missing.append(f"{library.get('name')} {view}: {library[view]}")
    if missing:
        raise RegistrationError("Hammer's sram_generator bound SRAM views that do not exist:\n  " +
                                "\n  ".join(missing))
    unbound = sorted(set(srams) - set(bound))
    if unbound:
        raise RegistrationError(f"Hammer's sram_generator bound no library for {', '.join(unbound)}.")
    for name, found in srams.items():
        model = bound[name].get("verilog_sim")
        ports = _module_ports(Path(model), name) if model else None
        if ports is None:
            continue
        for path, connected, _ in found:
            wrong = sorted(p for p in connected if p and p not in ports)
            if wrong:
                raise RegistrationError(
                    f"{path} connects {', '.join(wrong)}, which SRAM {name} does not have (its ports: "
                    f"{', '.join(sorted(ports))}, from {model}).")


def _check_with_hammer(config_dir: Path, files: List[Path], e2e_dir: Path, env: str, pdk: str, tools: str,
                       obj_dir: Path, srams: bool) -> List[dict]:
    """
    Load the generated configs as the flow will (env, pdk and tool configs,
    then the design's) and, if the design has SRAMs, run Hammer's
    sram_generator on them, as `make srams` does. Returns the libraries it
    bound (vlsi.technology.extra_libraries).
    """
    from hammer.logging import HammerVLSILogging, Level
    from hammer.vlsi import HammerDriver, HammerDriverOptions
    from hammer.vlsi.constraints import PlacementConstraint

    env_conf = e2e_dir / "configs-env" / f"{env}-env.yml"
    pdk_conf = e2e_dir / "configs-pdk" / f"{pdk}.yml"
    tool_conf = e2e_dir / "configs-tool" / f"{tools}.yml"
    for conf in (env_conf, pdk_conf, tool_conf):
        if not conf.is_file():
            raise RegistrationError(f"{conf} does not exist.")
    obj_dir.mkdir(parents=True, exist_ok=True)
    log_file = obj_dir / "register-projects.log"

    errors: List[str] = []

    def keep_errors(message: Any) -> None:
        if message.level in (Level.ERROR, Level.FATAL):
            errors.append(message.message)

    def fail(what: str) -> RegistrationError:
        return RegistrationError(f"{what}:\n  " + "\n  ".join(errors or ["(no error logged)"]) +
                                 f"\nFull log: {log_file}")

    # Hammer logs to the console through class-wide callbacks, and each driver
    # adds its file log to them. Keep only what this check needs, and put the
    # caller's back afterwards.
    saved = HammerVLSILogging.callbacks
    HammerVLSILogging.callbacks = [keep_errors]
    try:
        driver = HammerDriver(HammerDriverOptions(
            environment_configs=[str(env_conf)],
            project_configs=[str(pdk_conf), str(tool_conf)] + [str(p) for p in sorted(config_dir.glob("*.yml"))],
            log_file=str(log_file),
            obj_dir=str(obj_dir)))
        if errors:
            raise fail("Hammer could not load the generated configs")
        got = {os.path.realpath(p) for p in driver.database.get_setting("synthesis.inputs.input_files")}
        if got != {os.path.realpath(f) for f in files}:
            raise RegistrationError("the generated configs do not resolve synthesis.inputs.input_files to the "
                                    "project's files.")
        for constraint in driver.database.get_setting("vlsi.inputs.placement_constraints") or []:
            # hardmacro sizes come from the macro's LEF at par time
            if str(constraint.get("type")) != "hardmacro":
                try:
                    PlacementConstraint.from_dict(constraint)
                except (ValueError, KeyError, TypeError) as e:
                    raise RegistrationError(
                        f"placement constraint for {constraint.get('path')} is invalid: {e}") from None
        if not srams:
            return []
        if not driver.load_sram_generator_tool(str(obj_dir / "sram_generator-rundir")) or errors:
            raise fail("Hammer could not load the SRAM generator")
        ok, output = driver.run_sram_generator()
        if not ok or errors:
            raise fail("Hammer's sram_generator failed")
        return [_tidy_library(entry) for entry in output.get("vlsi.technology.extra_libraries", [])]
    except RegistrationError:
        raise
    except Exception as e:
        raise RegistrationError(f"Hammer could not load the generated configs: {type(e).__name__}: {e}\n"
                                f"Full log: {log_file}") from None
    finally:
        HammerVLSILogging.callbacks = saved


def _tidy_library(value: Any) -> Any:
    """
    A generated library as it should be written: without the unset fields the
    generator dumps, and with paths that came through the staging directory
    (a sibling of the design's) normalized to what they mean from the design's.
    """
    if isinstance(value, dict):
        return {k: _tidy_library(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_tidy_library(v) for v in value]
    if isinstance(value, str) and value.startswith("/"):
        return os.path.normpath(value)
    return value


def _yaml_doc(header: str, settings: Dict[str, Any]) -> str:
    import yaml
    comment = "".join(f"# {line}".rstrip() + "\n" for line in header.splitlines())
    return comment + "\n" + (yaml.safe_dump(settings, sort_keys=False, width=1000) if settings else "")


def _check_eco_yml(path: Path) -> List[str]:
    """Warnings about a user's eco.yml; one that does not load as a Hammer config is an error."""
    from hammer.config import load_config_from_string
    try:
        settings = load_config_from_string(path.read_text(), is_yaml=True, path=str(path.parent))
    except Exception as e:
        raise RegistrationError(f"{path} does not load as a Hammer config: {e}") from None
    if not settings.get("formal.inputs.eco_reference_files"):
        return [f"{path.name} sets no formal.inputs.eco_reference_files, so the eco stage will stop at "
                f"par_to_eco for lack of a reference."]
    return []


def _previous_registration(design_dir: Path, source: str, pdk: str) -> Dict[str, Any]:
    """
    The record of an earlier registration of this design, or {} for a new
    one. A design registration did not write (it has its own common.yml or
    <pdk>.yml), or wrote from another project, is left alone; a directory
    holding only files of the user's, such as an eco.yml written ahead of
    registering, is registered into.
    """
    if not design_dir.exists():
        return {}
    record = design_dir / REGISTRATION_FILE
    if not record.is_file():
        theirs = [name for name in ("common.yml", f"{pdk}.yml", f"{pdk}-extras.yml") if (design_dir / name).exists()]
        if theirs:
            raise RegistrationError(
                f"{design_dir} already holds {', '.join(theirs)} that register-projects did not write, so it "
                f"is left alone. Rename the project, or move that design out of the way.")
        return {}
    try:
        previous = json.loads(record.read_text())
    except ValueError as e:
        raise RegistrationError(f"{record} is unreadable ({e}); remove it to register again.") from None
    if previous.get("source") != source:
        raise RegistrationError(f"{design_dir} was registered from {previous.get('source')}, not {source}.")
    return previous


def _eco_doc(reference: Path, top: str, e2e_dir: Path, design_dir: Path, pdk: str, tools: str) -> str:
    """
    eco.yml for a modified copy: the eco stage's reference netlist is what syn
    writes for the original's design (<top>.mapped.v in its syn-rundir), so
    that design's syn has to have run before the copy's eco stage can.
    """
    reference = reference.absolute()
    configs = find_project_configs(reference)
    original_top = load_project_settings(configs).get("synthesis.inputs.top_module", (None, ""))[0]
    if original_top != top:
        raise RegistrationError(f"the copy's top module is {top}, but its original "
                                f"({reference}) has {original_top}; the ECO compares the two by top module.")
    netlist = e2e_dir / f"build-{pdk}-{tools}" / reference.name / "syn-rundir" / f"{top}.mapped.v"
    source = os.path.relpath(reference, e2e_dir.parent)
    return _yaml_doc(
        f"Created once by `studio register-projects` (`make designs`); edit freely. Registration never\n"
        f"overwrites this file; delete it to get this default back.\n"
        f"The eco stage patches this design's par netlist toward the netlist syn makes from {source}\n"
        f"(the '{reference.name}' design), so run that design's syn first.\n"
        f"formal.conformal.eco_method: hierarchical (patch each non-equivalent module from its own\n"
        f"compare), flat_localized (one flat compare, patches split into the modules they touch) or\n"
        f"flat_global (one patch at the top of the flattened netlist).",
        {
            "formal.inputs.eco_reference_files": [os.path.relpath(netlist, design_dir)],
            "formal.inputs.eco_reference_files_meta": "prependlocal",
            "formal.conformal.eco_method": "hierarchical",
            # 212, the default, segfaults on the RHEL 9 BWRC hosts (readme_documents/ECO.md)
            "formal.conformal.version": "261",
        })


def register_project(
    project_dir: Path,
    e2e_dir: Path = REPO_ROOT / "e2e",
    env: str = "bwrc",
    pdk: str = "sky130",
    tools: str = "cm",
    design: Optional[str] = None,
    reference: Optional[Path] = None,
) -> ProjectRegistration:
    """
    Write e2e/configs-design/<design>/ for one project: common.yml (the
    design), <pdk>.yml (its technology settings and SRAMs), <pdk>-extras.yml
    (the SRAM libraries, for make-dag; the Makefile flow binds its own with
    `make srams`) and registration.json.

    Raises RegistrationError, saying what to fix, when the project cannot
    become a valid design. The design directory is then left as it was.
    design defaults to the project's directory name.

    reference is set for a modified copy of a project (src/<tree>/<project>,
    registered under a name of its own): the original it should behave like.
    The copy then also gets eco.yml, which points the eco stage at the
    netlist syn makes from the original, so the ECO patches the copy's
    placed-and-routed netlist toward the original's behavior.
    """
    from hammer.vlsi.rtl_check import write_if_changed

    project_dir = project_dir.absolute()
    design = design or project_dir.name
    if pdk != "sky130":
        raise RegistrationError(f"PDK '{pdk}' is not supported yet; only sky130.")
    if not _DESIGN_NAME.match(design):
        raise RegistrationError(f"'{design}' cannot name a design, since it becomes the DAG id: use letters, "
                                f"digits, '_', '.' and '-' only.")
    env_conf = e2e_dir / "configs-env" / f"{env}-env.yml"
    if not env_conf.is_file():
        raise RegistrationError(f"{env_conf} does not exist.")
    source = os.path.relpath(project_dir, e2e_dir.parent)
    configs_design = e2e_dir / "configs-design"
    design_dir = configs_design / design
    previous = _previous_registration(design_dir, source, pdk)

    configs = find_project_configs(project_dir)
    settings = load_project_settings(configs)
    technology = settings.get("vlsi.core.technology", (None, ""))[0]
    if technology not in (None, f"hammer.technology.{pdk}"):
        raise RegistrationError(f"the project targets {technology}; only hammer.technology.{pdk} can be "
                                f"registered.")

    files = _project_paths(settings, "synthesis.inputs.input_files", configs, project_dir)
    if not files:
        raise RegistrationError(f"synthesis.inputs.input_files is not set in {configs['syn'].name}.")
    include_dirs = _project_paths(settings, "synthesis.inputs.include_dirs", configs, project_dir)
    defines = settings.get("synthesis.inputs.defines", ([], ""))[0] or []
    top = settings.get("synthesis.inputs.top_module", (None, ""))[0]
    if not isinstance(top, str) or not top:
        roots = _root_modules(files, include_dirs, defines)
        raise RegistrationError(
            f"synthesis.inputs.top_module is not set in {configs['syn'].name}, and registration does not "
            f"guess it. The modules nothing instantiates are: {', '.join(roots) or 'none'}.")

    rtl = elaborate_rtl(files, include_dirs, defines, top)
    _check_package_order(files, rtl.packages, project_dir)
    for include in rtl.includes:
        if not _inside(Path(include), project_dir):
            raise RegistrationError(f"the RTL includes {include}, which is outside the project.")
        # Genus searches init_hdl_search_path, not necessarily the including file's
        # directory. slang reports real paths; name the directory through the project.
        directory = project_dir / Path(include).parent.relative_to(project_dir.resolve())
        if directory not in include_dirs:
            include_dirs.append(directory)

    catalog = sram_catalog(pdk)
    unknown = {name: found for name, found in rtl.undefined.items() if name not in catalog}
    if unknown:
        raise RegistrationError(
            f"{top} instantiates modules that no synthesis input defines and that are not SRAMs in Hammer's "
            f"{pdk} catalog (hammer/technology/{pdk}/sram-cache.json):\n  " +
            "\n  ".join(f"{name}: {', '.join(path for path, _, _ in found)}" for name, found in sorted(unknown.items())) +
            "\nAdd the files that define them to synthesis.inputs.input_files.")
    srams = {name: rtl.undefined[name] for name in sorted(rtl.undefined)}
    warnings = _check_macro_placements(settings, srams)
    notes: List[str] = []
    clocks = settings.get("vlsi.inputs.clocks", ([], ""))[0] or []
    if not clocks:
        warnings.append("the project sets no vlsi.inputs.clocks, so syn and par run without a clock constraint.")

    carried, dropped = _translate_settings(settings, configs, project_dir, design_dir)

    def rel(paths: List[Path]) -> List[str]:
        return [os.path.relpath(p, design_dir) for p in paths]

    common: Dict[str, Any] = {
        "synthesis.inputs.top_module": top,
        "synthesis.inputs.input_files": rel(files),
        "synthesis.inputs.input_files_meta": "prependlocal",
    }
    if include_dirs:
        common["synthesis.inputs.include_dirs"] = rel(include_dirs)
        common["synthesis.inputs.include_dirs_meta"] = "prependlocal"
    for role in ("syn", "par"):
        for key, value in carried[role].items():
            common.setdefault(key, value)
    tech = dict(carried["tech"])
    if srams:
        tech["vlsi.inputs.sram_parameters"] = [catalog[name] for name in srams]
        site, needs_mirror = _sram_install(env_conf, list(srams))
        if needs_mirror:
            _mirror_srams(site, configs_design / SRAM_MIRROR_DIR, list(srams))
            tech["technology.sky130.sram22_sky130_macros"] = os.path.relpath(configs_design / SRAM_MIRROR_DIR,
                                                                             design_dir)
            tech["technology.sky130.sram22_sky130_macros_meta"] = "prependlocal"
            notes.append(f"{site} ships some SRAM GDS only gzipped; the design reads the macros through "
                         f"configs-design/{SRAM_MIRROR_DIR}, which links the install and holds the GDS "
                         f"decompressed.")

    names = {role: os.path.relpath(path, project_dir) for role, path in configs.items()}
    from_files = ", ".join(names[r] for r in ("syn", "par") if r in names)
    docs: Dict[str, str] = {
        "common.yml": _yaml_doc(
            f"Generated by `studio register-projects` (`make designs` in e2e/) from {source}: {from_files}.\n"
            f"Registering again rewrites this file; other .yml files here (eco.yml, for one) are left alone.",
            common),
        f"{pdk}.yml": _yaml_doc(
            f"Generated by `studio register-projects` from {source}: {names.get('tech', 'no tech config')},\n"
            f"less the settings registration.json lists as dropped, plus the SRAMs the RTL instantiates.\n"
            f"Registering again rewrites this file.",
            tech),
    }

    obj_dir = e2e_dir / f"build-{pdk}-{tools}" / design
    configs_design.mkdir(parents=True, exist_ok=True)
    # Checked from a sibling of the design directory, so the relative paths
    # resolve the same and a failed check leaves the design as it was.
    staging = Path(tempfile.mkdtemp(prefix=f".{design}.", dir=configs_design))
    try:
        for name, text in docs.items():
            (staging / name).write_text(text)
        libraries = _check_with_hammer(staging, files, e2e_dir, env, pdk, tools, obj_dir, bool(srams))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if srams:
        _check_sram_libraries(libraries, srams)
        docs[f"{pdk}-extras.yml"] = _yaml_doc(
            f"Generated by `studio register-projects`: Hammer's sram_generator output for the SRAMs in\n"
            f"{pdk}.yml, for the DAG. The e2e Makefile flow generates its own with `make srams`.",
            {"vlsi.technology.extra_libraries": libraries, "vlsi.technology.extra_libraries_meta": "append"})

    # eco.yml is the user's: a copy gets a default once, and an existing one, written by hand or
    # created by an earlier registration, is used as it is and never rewritten.
    eco_yml = design_dir / ECO_FILE
    eco_default = None
    if eco_yml.exists():
        warnings.extend(_check_eco_yml(eco_yml))
        notes.append(f"{ECO_FILE}: kept (yours; registration never rewrites it)")
    elif reference is not None:
        eco_default = _eco_doc(reference, top, e2e_dir, design_dir, pdk, tools)

    generated = sorted(docs)
    docs[REGISTRATION_FILE] = json.dumps({
        "source": source,
        **({"reference": os.path.relpath(reference.absolute(), e2e_dir.parent)} if reference else {}),
        "configs": names,
        "top_module": top,
        "input_files": [os.path.relpath(f, project_dir) for f in files],
        "include_dirs": [os.path.relpath(d, project_dir) for d in include_dirs],
        "srams": {name: sorted(path for path, _, _ in found) for name, found in srams.items()},
        "dropped_settings": dict(sorted(dropped.items())),
        "files": generated,
    }, indent=2) + "\n"
    design_dir.mkdir(parents=True, exist_ok=True)
    written = [name for name, text in docs.items() if write_if_changed(str(design_dir / name), text)]
    # write_if_changed writes through mkstemp (mode 0600); a DAG task may run as another user
    umask = os.umask(0)
    os.umask(umask)
    for name in docs:
        os.chmod(design_dir / name, 0o666 & ~umask)
    if eco_default is not None:
        with open(eco_yml, "x") as f:
            f.write(eco_default)
        written.append(f"{ECO_FILE} (created; edit freely)")
    # Registrations before eco.yml became the user's listed it among their files; it is never removed.
    for stale in sorted(set(previous.get("files", [])) - set(generated) - {ECO_FILE}):
        (design_dir / stale).unlink(missing_ok=True)
        written.append(f"{stale} (removed)")

    return ProjectRegistration(
        project_dir=project_dir, design=design, design_dir=design_dir, configs=configs, top_module=top,
        input_files=files, srams={name: [path for path, _, _ in found] for name, found in srams.items()},
        clocks=clocks, dropped=dropped, unused_files=rtl.unused_files, written=written, notes=notes,
        warnings=warnings)
