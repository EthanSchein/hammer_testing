#  hammer-vlsi plugin for Cadence Conformal.
#
#  See LICENSE for licence details.

from typing import List, Dict, Tuple, Any, Optional

import os
import re
import errno

from hammer.vlsi import HammerFormalTool, HammerToolStep
from hammer.vlsi.constraints import MMMCCornerType
from hammer.logging import HammerVLSILogging
import hammer.tech as hammer_tech
from hammer.common.cadence import CadenceTool

# Notes: Tcl mode is enabled for harmonization with other Cadence tools and additional Tcl functionality.
# There is a minor performance hit with database operations vs. native language.

class Conformal(HammerFormalTool, CadenceTool):

    def tool_config_prefix(self) -> str:
        return "formal.conformal"

    @property
    def env_vars(self) -> Dict[str, str]:
        v = dict(super().env_vars)
        if self.check in ["constraint", "cdc"]:
            v["CONFORMAL_BIN"] = self.get_setting("formal.conformal.conformal_ccd_bin")
        else:
            v["CONFORMAL_BIN"] = self.get_setting("formal.conformal.conformal_lec_bin")
        return v

    @property
    def start_cmd(self) -> List[str]:
        """ Generate required startup command based on the requested check and license level """
        lec_bin = self.get_setting("formal.conformal.conformal_lec_bin")
        ccd_bin = self.get_setting("formal.conformal.conformal_ccd_bin")
        license = self.get_setting("formal.conformal.license")
        cmd = ["", ""]
        if not license in ["L", "XL", "GXL"]:
            self.logger.error("License must be L, XL, or GXL. For CCD, -MCC is equivalent to GXL here.")

        if self.check in ("lec", "eco_verify"):
            cmd = [lec_bin, f"-{license}"]
        elif self.check == "power":
            if license == "L":
                self.logger.error("power not supported with L license")
            else:
                cmd = [lec_bin, f"-LP{license}"]
        elif self.check == "eco":
            if license == "L":
                self.logger.error("eco not supported with L license")
            elif license == "XL":
                cmd = [lec_bin, "-ECO"]
            else:
                cmd = [lec_bin, "-ECOGXL"]
        elif self.check == "property":
            return [lec_bin, "-VERIFY"]
        elif self.check in ["constraint", "cdc"]:
            if license == "GXL":
                cmd = [ccd_bin, "-MCC"]
            else:
                cmd = [ccd_bin, f"-{license}"]
        else:
            self.logger.error("Unsupported check type")

        return cmd

    def check_reference_files(self, extensions: List[str]) -> bool:
        """
        Verify that reference files exist and have the specified extensions.
        Analogous to check_input_files in HammerTool.

        :param extensions: List of extensions e.g. [".v", ".sv"]
        :return: True if all files exist and have the specified extensions.
        """
        refs = self.reference_files
        error = False
        for r in refs:
            if not r.endswith(tuple(extensions)):
                self.logger.error(f"Input of unsupported type {r} detected!")
                error = True
            if not os.path.isfile(r):
                self.logger.error(f"Input file {r} does not exist!")
                error = True
        return not error

    @property
    def restart_checkpoint(self) -> str:
        """ Name of checkpoint to be restarted from (set by do_pre_steps) """
        return self.attr_getter("_restart_checkpoint", "")

    @restart_checkpoint.setter
    def restart_checkpoint(self, val: str) -> None:
        self.attr_setter("_restart_checkpoint", val)

    @property
    def _step_transitions(self) -> List[Tuple[str, str]]:
        """
        Private helper property to keep track of which steps we ran so that we
        can create symlinks.
        This is a list of (pre, post) steps
        """
        return self.attr_getter("__step_transitions", [])

    @_step_transitions.setter
    def _step_transitions(self, value: List[Tuple[str, str]]) -> None:
        self.attr_setter("__step_transitions", value)


    def do_pre_steps(self, first_step: HammerToolStep) -> bool:
        assert super().do_pre_steps(first_step)
        # Restart from the last checkpoint if we're not starting over.
        # Not in the dofile, must be a command-line option
        if first_step != self.first_step:
            self.restart_checkpoint = f"pre_{first_step.name}"
        return True

    def do_between_steps(self, prev: HammerToolStep, next: HammerToolStep) -> bool:
        assert super().do_between_steps(prev, next)
        # Write a checkpoint to disk.
        self.append(f"checkpoint pre_{next.name} -replace")
        # Symlink the checkpoint to latest for open_checkpoint script later.
        self.append(f"ln -sfn pre_{next.name} latest")
        self._step_transitions = self._step_transitions + [(prev.name, next.name)]
        return True

    def do_post_steps(self) -> bool:
        assert super().do_post_steps()
        # Create symlinks for post_<step> to pre_<step+1> to improve usability.
        try:
            for prev, next in self._step_transitions:
                os.symlink(
                    os.path.join(self.run_dir, f"pre_{next}"), # src
                    os.path.join(self.run_dir, f"post_{prev}") # dst
                )
        except OSError as e:
            if e.errno != errno.EEXIST:
                self.logger.warning("Failed to create post_* symlinks: " + str(e))

        # Create checkpoint post_<last step>
        # TODO: this doesn't work if you're only running the very last step
        if len(self._step_transitions) > 0:
            last = f"post_{self._step_transitions[-1][1]}"
            self.append(f"checkpoint {last} -replace")
            # Symlink the database to latest for open_checkpoint script later.
            self.append(f"ln -sfn {last} latest")

        return self.generate_open_checkpoint() and self.run_conformal()

    @property
    def steps(self) -> List[HammerToolStep]:
        if self.check == "eco":
            return self.make_steps_from_methods([
                self.setup_eco_designs,
                self.patch_eco_designs
            ])
        if self.check == "eco_verify":
            return self.make_steps_from_methods([self.verify_eco_netlist])
        if self.check != "lec":
            self.logger.error("Check type {self.check} not yet supported!")
        steps = [
            self.setup_designs,
            self.compare_designs
        ]
        return self.make_steps_from_methods(steps)

    def setup_designs(self) -> bool:
        """ Setup the designs """
        append = self.append

        # Exit on dofile error
        append("set_dofile_abort exit")

        # Multithreading (max 16 allowed by tool)
        max_threads = min(self.get_setting("vlsi.core.max_threads"), 16)
        append(f"set_parallel_option -threads 1,{max_threads}")

        # Read libraries (macros, stdcells)
        # TODO: support VHDL + Liberty. For now, -sva = SystemVerilog w/ assertion support.
        lib_v_files = self.technology.read_libs(
                [hammer_tech.filters.verilog_synth_filter],
                hammer_tech.HammerTechnologyUtils.to_plain_item)
        lib_v_files.extend(self.technology.read_libs(
                [hammer_tech.filters.verilog_sim_filter],
                hammer_tech.HammerTechnologyUtils.to_plain_item))
        append(f"read_library {' '.join(lib_v_files)} -sva -bboxsolver -both")

        # Read designs
        valid_exts = [".v", ".v.gz", ".sv", ".sv.gz"]
        if not self.check_input_files(valid_exts) or not self.check_reference_files(valid_exts):
            return False
        golden_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.reference_files))
        append(f"read_design {' '.join(golden_files)} -sva -golden")
        revised_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))
        append(f"read_design {' '.join(revised_files)} -sva -revised")

        # Set top module
        append(f"set_root_module {self.top_module} -both")

        # Auto setup analysis optimizations
        if self.get_setting("formal.conformal.license") != "L":
            append("set_analyze_option -auto")

        # Setup reports
        append("report_design_data")

        return True

    def compare_designs(self) -> bool:
        """ Depending on license, performs flat or hierarchical comparison """
        append = self.append

        if self.get_setting("formal.conformal.license") == "L":
            append("report_black_box")
            append("set_system_mode lec")
            append("add_compare_point -all")
            append("compare")
            append("report_compare_data")
        else:
            # TODO: need resource file for DC-mapped netlists
            append('write_hier_compare_dofile hier_compare.tcl -replace '\
                   '-prepend_string "analyze_datapath -module; analyze_datapath"')
            append("run_hier_compare hier_compare.tcl")
            append("set_system_mode lec")

        append("report_statistics")

        return True

    # Functional ECO (formal.inputs.check: eco), from the EECS 144 ECO project's
    # conformal_eco.do, in Tcl mode. In Conformal's ECO commands the golden
    # design is the one that gets patched (ANALYZE ECO "analyzes the ECO change
    # in the Revised root module when compared to the Golden root module"), so
    # the implemented design (input_files, the post-par netlist) is read as
    # golden and the reference (reference_files, synthesized from the corrected
    # RTL) as revised. formal.conformal.eco_method picks how the patch is found:
    #
    #   hierarchical    compare module by module; patch each non-equivalent
    #                   module from its own compare
    #   flat_localized  one flat compare; the patch is split into the modules it
    #                   touches (Cadence's "flattened ECO flow")
    #   flat_global     one flat compare of the flattened netlist; one patch at
    #                   the top, so the patched netlist comes out flat
    #
    # Every method ends the same way: the patched netlist is written out, read
    # back, and compared flat against the reference from scratch.

    ECO_METHODS = ("hierarchical", "flat_localized", "flat_global")

    @property
    def eco_method(self) -> str:
        return str(self.get_setting("formal.conformal.eco_method"))

    def setup_eco_designs(self) -> bool:
        """ Read the Liberty and both netlists, and set them up for formal.conformal.eco_method """
        append = self.append
        method = self.eco_method
        if method not in self.ECO_METHODS:
            self.logger.error(f"formal.conformal.eco_method is {method!r}; use one of {', '.join(self.ECO_METHODS)}")
            return False

        # Exit on dofile error
        append("set_dofile_abort exit")

        # Multithreading (max 16 allowed by tool)
        max_threads = min(self.get_setting("vlsi.core.max_threads"), 16)
        append(f"set_parallel_option -threads 1,{max_threads}")

        append("set_log_file eco.log -replace")
        append("usage -auto -elapse")

        # The patch is built from library cells, so read their Liberty
        liberty_files = self.eco_liberty_files()
        if len(liberty_files) == 0:
            self.logger.error("No Liberty files to build the ECO patch from")
            return False
        append(f"read_library {' '.join(liberty_files)} -liberty -both -replace")

        valid_exts = [".v", ".v.gz"]
        if not self.check_input_files(valid_exts) or not self.check_reference_files(valid_exts):
            return False
        implemented_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))
        append(f"read_design {' '.join(implemented_files)} -golden -replace")
        append(f"read_design {' '.join(self.eco_reference_paths)} -revised -replace")
        append(f"set_root_module {self.top_module} -both")

        if method == "flat_localized":
            append("set_eco_option -flat")
            append("uniquify -all -nolibrary -revised")
            append("flatten -matchhierarchy -nolibrary -revised")
        elif method == "hierarchical":
            append("set_flatten_model -eco")
            append("uniquify -all -nolibrary -revised")
        else:
            # Conformal applies a patch only to the golden it read in (ECO1.12 otherwise),
            # so a top-level patch needs the implemented design read in flat.
            append("flatten -nolibrary -golden")
            append("write_design golden_flat.v -golden -replace")
            append("read_design golden_flat.v -golden -replace")
            append(f"set_root_module {self.top_module} -golden")
            # and the reference flat too, or analyze_eco finds no golden nets for its gates
            append("flatten -nolibrary -revised")
            append("set_flatten_model -eco")
        return True

    def patch_eco_designs(self) -> bool:
        """
        Compare, patch what differs, write the results, and verify them. One
        step: each checkpoint between steps costs about as much time as the
        whole ECO on a small design.
        """
        append = self.append
        method = self.eco_method
        if method == "hierarchical":
            self.patch_eco_hierarchical()
        else:
            # One flat compare before patching; the designs are expected to differ
            append("set_system_mode lec")
            if method == "flat_localized":
                append("analyze_hier_compare -dofile hier.eco.do -replace -eco_aware -verbose")
            append("add_compared_points -all")
            append("compare")
            append("redirect 01_pre_eco_verification.rpt {report_verification}")
            append("redirect 02_pre_eco_nonequivalent.rpt {report_compare_data -class nonequivalent}")
            if method == "flat_localized":
                # Find the modules that differ, write a patch for each, add their ECO pins
                append("compare_eco_hierarchy")
                append("redirect 03_eco_check.rpt {report_eco_check -verbose}")
                append("redirect 04_eco_hierarchy_noneq.rpt {report_eco_hierarchy -noneq -verbose}")
                append(f"analyze_eco {self.eco_patch_path} -hierarchical -ecopin_dofile ecopin.do -replace")
                append("set_system_mode setup")
                append("dofile ecopin.do")
                append("apply_patch -auto -golden -keephierarchy")
            else:
                append(f"analyze_eco {self.eco_patch_path} -replace")
                append("set_system_mode setup")
                append("apply_patch -auto -golden -keephierarchy")

        # analyze_eco writes the patch in generic gates; Genus maps it onto library
        # cells, which is what place-and-route can implement
        avoid = " ".join(self.get_dont_use_list())
        append(f"optimize_patch -workdir {self.eco_genus_dir} "
               f"-library {{{' '.join(self.eco_liberty_files())}}} -synexec {self.eco_genus_bin} -relative2cwd "
               "-instancenaming ECO_inst_%d -netnaming ECO_net_%d -sequentialnaming ECO_reg_%s"
               + (f" -avoid {avoid}" if avoid else ""))

        # The patched netlist, and the patch as Innovus netlist edits (the Stylus
        # script is what the eco_par stage applies to the par database)
        append(f"report_eco_changes -innovus -file {self.eco_innovus_script_path} -replace")
        append(f"report_eco_changes -stylus -file {self.eco_stylus_script_path} -replace")
        append(f"write_eco_design -newfile {self.eco_netlist_path} -replace -report 07_ECOprelogics.rpt")

        # Verify what was written: read it back and compare it flat against the reference
        # from scratch. An in-session compare after a hierarchical compare still sees the
        # submodules as black boxes and checks only the top level.
        append("set_system_mode setup")
        append(f"read_design {self.eco_netlist_path} -golden -replace")
        append(f"read_design {' '.join(self.eco_reference_paths)} -revised -replace")
        append(f"set_root_module {self.top_module} -both")
        append("set_system_mode lec")
        append("add_compared_points -all")
        append("compare")
        append("redirect 05_post_eco_verification.rpt {report_verification}")
        append("redirect 06_post_eco_compare_data.rpt {report_compare_data}")

        self.ran_patch_eco_designs = True
        return True

    def patch_eco_hierarchical(self) -> None:
        """
        Compare module by module (Cadence's "ECO for hierarchical files"), then
        for each non-equivalent module compare it alone and write its patch, and
        apply every patch. The module list comes from the hierarchical compare,
        read back in Tcl. All patches are also collected into patch.v.
        """
        append = self.append
        append("write_hier_compare_dofile hier.eco.do -replace -constraints -noexact_pin_match "
               "-balanced_extractions -function_pin_mapping -input_output_pin_equivalence -eco_aware "
               "-threshold 0 -ecopin_dofile ecopin.do")
        append("dofile ecopin.do")
        append("run_hier_compare hier.eco.do -nodynamic")
        append("redirect 01_pre_eco_verification.rpt {report_hier_compare_result}")
        append("redirect -variable eco_noneq {report_hier_compare_result -noneq}")
        append("redirect 02_pre_eco_nonequivalent.rpt {puts $eco_noneq}")
        append("set eco_modules {}")
        append('foreach line [split $eco_noneq "\\n"] {')
        append("    if {[regexp {^Non-equivalent:\\s+(\\S+)\\s+(\\S+)} $line -> golden revised]} {")
        append("        lappend eco_modules [list $golden $revised]")
        append("    }")
        append("}")
        append('puts "ECO modules: $eco_modules"')
        append("foreach pair $eco_modules {")
        append("    lassign $pair golden revised")
        append("    set_system_mode setup")
        append("    set_root_module $golden -golden")
        append("    set_root_module $revised -revised")
        append("    set_system_mode lec")
        append("    add_compared_points -all")
        append("    compare")
        append("    analyze_eco patch_$golden.v -replace")
        append("}")
        append("set_system_mode setup")
        append(f"set_root_module {self.top_module} -both")
        # the hierarchical compare's black boxes would hide the patches from optimize_patch
        append("delete_black_box -all -hier -both")
        append(f"set eco_patch [open {self.eco_patch_path} w]")
        append("foreach pair $eco_modules {")
        append("    set golden [lindex $pair 0]")
        append("    read_design patch_$golden.v -golden -append -lastmod -norulesummary")
        append("    apply_patch $golden ${golden}_eco -golden -keephierarchy")
        append("    set f [open patch_$golden.v r]")
        append("    puts $eco_patch [read $f]")
        append("    close $f")
        append("}")
        append("close $eco_patch")

    # Final check of an ECO (formal.inputs.check: eco_verify, run by eco-par once
    # place and route has implemented the patch): the netlist par wrote
    # (input_files) against the corrected reference (reference_files), read and
    # compared flat with the eco stage's libraries, as its own final compare is.

    ECO_VERIFY_REPORT = "eco_verify_verification.rpt"
    ECO_VERIFY_SUMMARY = "eco_verify_compare_summary.rpt"

    def verify_eco_netlist(self) -> bool:
        """ Compare the post-ECO netlist with the reference """
        append = self.append
        # a report left by an earlier run must not pass for this one
        for report in (self.ECO_VERIFY_REPORT, self.ECO_VERIFY_SUMMARY):
            if os.path.exists(os.path.join(self.run_dir, report)):
                os.remove(os.path.join(self.run_dir, report))

        append("set_dofile_abort exit")
        max_threads = min(self.get_setting("vlsi.core.max_threads"), 16)
        append(f"set_parallel_option -threads 1,{max_threads}")
        append("set_log_file eco_verify.log -replace")
        append("usage -auto -elapse")

        liberty_files = self.eco_liberty_files()
        if len(liberty_files) == 0:
            self.logger.error("No Liberty files to read the post-ECO netlist with")
            return False
        append(f"read_library {' '.join(liberty_files)} -liberty -both -replace")

        valid_exts = [".v", ".v.gz"]
        if not self.check_input_files(valid_exts) or not self.check_reference_files(valid_exts):
            return False
        implemented_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))
        append(f"read_design {' '.join(self.eco_reference_paths)} -golden -replace")
        append(f"read_design {' '.join(implemented_files)} -revised -replace")
        append(f"set_root_module {self.top_module} -both")
        append("set_system_mode lec")
        append("add_compared_points -all")
        append("compare")
        append(f"redirect {self.ECO_VERIFY_REPORT} {{report_verification}}")
        append(f"redirect {self.ECO_VERIFY_SUMMARY} {{report_compare_data -summary}}")
        self.ran_verify_eco_netlist = True
        return True

    def eco_verify_problems(self) -> List[str]:
        """
        Why the post-ECO compare is not a pass: anything but a PASS verdict, any incomplete
        verification, no compared points, or a compared point that is not (inverted-)equivalent.
        """
        problems = []
        result = self.eco_compare_result(self.ECO_VERIFY_REPORT)
        if result != "PASS":
            problems.append(f"the compare reported {result or 'no result'}")
        try:
            with open(os.path.join(self.run_dir, self.ECO_VERIFY_REPORT)) as f:
                incomplete = re.search(r"Incomplete verification:\s*(\d+)", f.read())
            if incomplete is None or int(incomplete.group(1)) != 0:
                problems.append("the verification is incomplete")
            with open(os.path.join(self.run_dir, self.ECO_VERIFY_SUMMARY)) as f:
                rows = {m.group(1): int(m.group(2)) for m in
                        re.finditer(r"^([A-Za-z][\w-]*(?: [A-Za-z][\w-]*)*)\s+[\d\s]*?(\d+)\s*$", f.read(), re.M)}
        except OSError as e:
            problems.append(f"no report ({e})")
            return problems
        if rows.get("Equivalent", 0) == 0:
            problems.append("no points were compared equivalent")
        for name, count in rows.items():
            if name not in ("Equivalent", "Inverted-equivalent") and count:
                problems.append(f"{count} {name} point(s)")
        return problems

    @property
    def ran_verify_eco_netlist(self) -> bool:
        """ The verify_eco_netlist step sets this to True if it was run """
        return self.attr_getter("_ran_verify_eco_netlist", False)

    @ran_verify_eco_netlist.setter
    def ran_verify_eco_netlist(self, val: bool) -> None:
        self.attr_setter("_ran_verify_eco_netlist", val)

    @property
    def eco_reference_paths(self) -> List[str]:
        return list(map(lambda name: os.path.join(os.getcwd(), name), self.reference_files))

    def eco_liberty_files(self) -> List[str]:
        """
        Liberty of the setup corner (every corner when there are no MMMC corners):
        one corner so the patch maps onto a single characterization of each cell,
        and every library at it, pads and macros included, since Conformal stops
        on a netlist cell it has no model for.
        """
        setup = [c for c in self.get_mmmc_corners() if c.type is MMMCCornerType.Setup]
        return self.get_timing_libs(setup[0] if setup else None).split()

    def eco_compare_result(self, report: str) -> Optional[str]:
        """ The Compare Results line of a report_verification report in the rundir, e.g. PASS or FAIL:NONEQ """
        try:
            with open(os.path.join(self.run_dir, report)) as f:
                text = f.read()
        except OSError:
            return None
        found = re.search(r"Compare Results:\s*(\S+)", text)
        if found:
            return found.group(1)
        # report_hier_compare_result, the hierarchical method's pre-ECO report
        found = re.search(r"Hierarchical compare\s*:\s*(\S+)", text)
        if found:
            return {"Equivalent": "PASS", "Non-equivalent": "FAIL:NONEQ"}.get(found.group(1), found.group(1))
        return None

    def fill_outputs(self) -> bool:
        if self.check == "eco_verify" and self.ran_verify_eco_netlist:
            problems = self.eco_verify_problems()
            if problems:
                self.logger.error("The post-ECO netlist is not proven equivalent to the reference: " + "; ".join(problems)
                                  + f". See {os.path.join(self.run_dir, self.ECO_VERIFY_REPORT)}.")
                return False
            self.logger.info("The post-ECO netlist is equivalent to the reference (Conformal PASS).")
            return super().fill_outputs()
        if self.check != "eco":
            return super().fill_outputs()
        if not self.ran_patch_eco_designs:
            self.logger.info("Did not run patch_eco_designs")
            return super().fill_outputs()
        # lec's exit status flags the non-equivalent points of the first
        # compare, which every ECO has, so the post-ECO report decides.
        report = os.path.join(self.run_dir, "05_post_eco_verification.rpt")
        result = self.eco_compare_result("05_post_eco_verification.rpt")
        if result != "PASS":
            self.logger.error(f"The patched design does not match the reference: the post-ECO compare "
                              f"reported {result or 'no result'}. See {report}.")
            return False
        if self.eco_compare_result("01_pre_eco_verification.rpt") == "PASS":
            self.logger.warning("The implemented design already matched the reference, so the ECO patch is empty.")
        for path in (self.eco_patch_path, self.eco_netlist_path, self.eco_innovus_script_path,
                     self.eco_stylus_script_path):
            if not os.path.isfile(path):
                self.logger.error(f"ECO output {path} not found")
                return False
        return super().fill_outputs()

    def export_config_outputs(self) -> Dict[str, Any]:
        outputs = dict(super().export_config_outputs())
        if self.check == "eco" and self.ran_patch_eco_designs:
            outputs["formal.outputs.eco_netlist"] = self.eco_netlist_path
            outputs["formal.outputs.eco_patch"] = self.eco_patch_path
            outputs["formal.outputs.eco_innovus_script"] = self.eco_innovus_script_path
            outputs["formal.outputs.eco_stylus_script"] = self.eco_stylus_script_path
        return outputs

    @property
    def eco_patch_path(self) -> str:
        return os.path.join(self.run_dir, "patch.v")

    @property
    def eco_netlist_path(self) -> str:
        return os.path.join(self.run_dir, f"{self.top_module}.eco.v")

    @property
    def eco_innovus_script_path(self) -> str:
        return os.path.join(self.run_dir, "eco.invs")

    @property
    def eco_stylus_script_path(self) -> str:
        return os.path.join(self.run_dir, "eco_stylus.tcl")

    @property
    def eco_genus_dir(self) -> str:
        return os.path.join(self.run_dir, "eco_genus")

    @property
    def eco_genus_bin(self) -> str:
        """ formal.conformal.eco_genus_bin, else the DDI Genus of synthesis.genus.version, else genus on the PATH """
        configured = self.get_setting("formal.conformal.eco_genus_bin", nullvalue="")
        if configured:
            return str(configured)
        try:
            version = self.get_setting("synthesis.genus.version", nullvalue="")
        except KeyError:
            version = ""
        if not version:
            return "genus"
        return f"{self.get_setting('cadence.cadence_home')}/DDI/DDI{version}/bin/genus"

    @property
    def ran_patch_eco_designs(self) -> bool:
        """ The patch_eco_designs step sets this to True if it was run """
        return self.attr_getter("_ran_patch_eco_designs", False)

    @ran_patch_eco_designs.setter
    def ran_patch_eco_designs(self, val: bool) -> None:
        self.attr_setter("_ran_patch_eco_designs", val)

    def generate_open_checkpoint(self) -> bool:
        # Make sure that generated-scripts exists.
        generated_scripts_dir = os.path.join(self.run_dir, "generated-scripts")
        os.makedirs(generated_scripts_dir, exist_ok=True)

        # Script to open results checkpoint
        self.create_enter_script()
        open_checkpoint_tcl = os.path.join(generated_scripts_dir, "open_checkpoint.tcl")
        with open(open_checkpoint_tcl, "w") as f:
            f.write("set_gui -mapping")
        open_checkpoint_script = os.path.join(generated_scripts_dir, "open_checkpoint")
        with open(open_checkpoint_script, "w") as f:
            assert super().do_pre_steps(self.first_step)
            args = self.start_cmd
            args.extend(["-gui", "-restart_checkpoint", "latest"])
            f.write("#!/bin/bash\n")
            f.write(f"cd {self.run_dir}\n")
            f.write("source enter\n")
            f.write(f"$CONFORMAL_BIN -restart_checkpoint latest -dofile {open_checkpoint_tcl}")
        os.chmod(open_checkpoint_script, 0o755)

        return True

    def run_conformal(self) -> bool:
        # Quit
        self.append("exit")

        # Write main dofile
        dofile = os.path.join(self.run_dir, f"{self.check}.tcl")
        self.write_contents_to_path("\n".join(self.output), dofile)

        # Build args
        args = self.start_cmd
        args.extend([
            "-nogui",
            "-color",
            "-tclmode",
            "-dofile", dofile
        ])
        if self.restart_checkpoint != "":
            args.extend([
                "-restart_checkpoint",
                self.restart_checkpoint
            ])

        # Temporarily disable colours/tag to make run output more readable.
        # TODO: think of a more elegant way to do this?
        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False
        self.run_executable(args, cwd=self.run_dir)
        # TODO: check for errors and deal with them
        # According to user guide:
        # Bit   Condition
        # 0     Internal error
        # 1     Exit status before comparison
        # 2     Command error
        # 3     Unmapped points
        # 4     Non-equivalent points
        # 5     Abort or uncompared points exist during any comparison
        # 6     Abort or uncompared points exist during last comparison
        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True

        # TODO: check that formal run was successful

        return True

tool = Conformal
