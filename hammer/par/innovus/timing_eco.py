"""
Timing ECO (par -> timing_eco), CLIDriver's timing-eco action: restore a par run's database with its constraints and,
only if setup or hold fails, repair it with at most MAX_ATTEMPTS opt_design passes plus one opt_design -drv pass if
those made max_transition / max_capacitance worse (timing_eco.tcl). The result is checked against the restored
database (timing, electrical, Innovus DRC / connectivity, optional Conformal equivalence), reported in
timing_eco_report.txt and written like a par run's to its own run directory. Signoff DRC / LVS is not part of it.
Drivers add hooks to its steps through get_extra_timing_eco_hooks. The setup branch is UNVALIDATED on a naturally
setup-violating design.
"""
import gzip
import os
import re
import time
from typing import Dict, List, Optional

from hammer.par.innovus import Innovus
from hammer.vlsi import CLIDriver, HammerDriver, HammerToolStep
from hammer.vlsi.cli_driver import dump_config_to_json_file

MAX_ATTEMPTS = 2
SLACK_TOLERANCE = 0.01  # ns a max_transition / max_capacitance pin may lose against the restored database
TCL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timing_eco.tcl")


class TimingEcoInnovus(Innovus):

    @property
    def steps(self) -> List[HammerToolStep]:
        return self.make_steps_from_methods([self.timing_eco_restore, self.timing_eco_opt, self.timing_eco_check,
                                             self.write_regs, self.write_design])

    @property
    def par_db(self) -> str:
        return os.path.join(os.path.dirname(self.get_setting("par.outputs.output_netlist")), self.output_innovus_lib_name)

    # steps that follow one leaving the design unchanged: their checkpoint links to that one's
    UNCHANGED_AFTER = {"timing_eco_opt": "timing_eco_restore", "write_regs": "timing_eco_check", "write_design": "write_regs"}

    def do_between_steps(self, prev: HammerToolStep, next: HammerToolStep) -> bool:
        if self.UNCHANGED_AFTER.get(next.name) != prev.name:
            return super().do_between_steps(prev, next)
        assert super(Innovus, self).do_between_steps(prev, next)
        self.verbose_append(f"ln -sfn {self.par_db if prev.name == 'timing_eco_restore' else f'pre_{prev.name}'} pre_{next.name}")
        self.verbose_append(f"ln -sfn pre_{next.name} latest")
        self._step_transitions = self._step_transitions + [(prev.name, next.name)]
        return True

    def tcl(self, *calls: str) -> bool:
        """ Source timing_eco.tcl (again: any step can start a resumed run) and call its procs """
        self.append(f"source {TCL}")
        for call in calls:
            self.append(f"::hammer_timing_eco::{call}")
        return True

    def timing_eco_restore(self) -> bool:
        try:
            db = self.par_db
        except KeyError:
            db = "(no par.outputs.output_netlist)"
        problem = ("it runs Innovus in Tcl: set par.innovus.use_python to false" if self.use_python else
                   f"no par database {db}" if not os.path.isdir(db) else
                   f"it would overwrite the par database: use a run directory other than {self.run_dir}"
                   if os.path.realpath(self.run_dir) == os.path.realpath(os.path.dirname(db)) else None)
        if problem:
            self.logger.error(f"Timing ECO: {problem}")
            return False
        # on an error innovus drops to an interactive prompt instead of exiting
        self.append(f"if {{[catch {{read_db {db}}} err]}} {{puts $err; puts {{ERROR: failed to read {db}}}; exit 2}}")
        return self.tcl("sta before", "physical before")

    def timing_eco_opt(self) -> bool:
        return self.tcl(f"repair {MAX_ATTEMPTS} {SLACK_TOLERANCE}")

    def timing_eco_check(self) -> bool:
        return self.tcl(f"verify {SLACK_TOLERANCE}", "physical after")

    def timing_eco_report(self, lec: str) -> bool:
        """ timing_eco_report.txt: FAIL if a check failed, else NOOP / PASS, -UNVERIFIED if equivalence did not run """
        def read(name: str) -> str:
            with open(os.path.join(self.run_dir, name), errors="replace") as f:
                return f.read()

        def values(name: str) -> Dict[str, float]:
            return {k: float(v) for k, v in (line.split() for line in read(name).splitlines() if line.strip())}

        def measure(tag: str) -> Dict[str, float]:
            m = {}  # type: Dict[str, float]
            for kind, summary in (("setup", "setup.summary.gz"), ("hold", "hold_hold.summary.gz")):
                with gzip.open(os.path.join(self.run_dir, f"timing_eco_{tag}", summary), "rt") as f:
                    t = f.read()
                for key in ("WNS (ns)", "TNS (ns)", "Violating Paths"):
                    m[f"{kind} {key}"] = float(re.search(re.escape(key) + r":\|\s*(-?[0-9.]+)", t).group(1))  # type: ignore
            for kind, name in (("maxtran", "max_transition"), ("maxcap", "max_capacitance")):
                rows = [line.split() for line in read(f"timing_eco_{tag}/{kind}.rpt").splitlines()]
                m[f"{name} pins"] = len({w[0] for w in rows if len(w) == 5 and re.fullmatch(r"-?[0-9.]+", w[3])})
            drc = re.search(r"Total Violations\s*:\s*(\d+)", read(f"timing_eco_drc_{tag}.rpt"))
            m["Innovus DRC markers"] = int(drc.group(1)) if drc else 0
            m["connectivity problems"] = sum(map(int, re.findall(r"(\d+) Problem\(s\)", read(f"timing_eco_conn_{tag}.rpt"))))
            return {**m, **values(f"timing_eco_ppa_{tag}.txt")}

        script = os.path.join(self.run_dir, "par.tcl")
        last = os.path.join(self.run_dir, "timing_eco_ppa_after.txt")
        if not os.path.isfile(last) or os.path.getmtime(last) < os.path.getmtime(script):
            self.logger.error(f"The timing ECO stopped before its final check; see {self.run_dir}/innovus.log")
            return False
        before, after = measure("before"), measure("after")
        changes = {k: int(v) for k, v in values("timing_eco_changes.txt").items()}
        attempts = changes["attempts"]
        worse = [line for line in read("timing_eco_new_violations.txt").splitlines() if line.strip()]
        failing = [f"{kind} WNS {after[f'{kind} WNS (ns)']} ns" for kind in ("setup", "hold") if after[f"{kind} WNS (ns)"] < 0]
        regressions = [f"{key} {before[key]:g} -> {after[key]:g}" for key in ("Innovus DRC markers", "connectivity problems")
                       if after[key] > before[key]]
        verdicts = {
            "timing": ("repair failed: " if attempts else "fails: ") + ", ".join(failing) if failing else
                      "repaired" if attempts else "met, no repair needed",
            "electrical": f"{len(worse)} max_transition / max_capacitance violations introduced or worsened "
                          "(timing_eco_new_violations.txt)" if worse else "no max_transition / max_capacitance regression",
            "physical (Innovus)": "failed: " + ", ".join(regressions) if regressions else "clean (DRC, connectivity)",
            "logical equivalence": lec,
        }
        if failing or worse or regressions or lec.startswith("failed"):
            status = "FAIL"
        else:
            status = ("PASS" if attempts else "NOOP") + ("" if lec == "verified" else "-UNVERIFIED")
        done = (f"{attempts} timing passes and {changes['drv_pass']} DRV pass"
                + (" (the design before it: timing_eco_pre_drv)" if changes["drv_pass"] else "")
                + f" added {changes['added']}, resized {changes['resized']} and removed {changes['removed']} cells on {changes['nets']} nets")
        lines = [f"timing ECO {status}"] + [f"{k}: {v}" for k, v in verdicts.items()] + [
            "signoff DRC / LVS: NOT RUN (the timing ECO runs Innovus checks only; run drc / lvs on its par-output-full.json)"]
        if before["setup WNS (ns)"] < 0:
            lines.append("note: the setup repair branch is unvalidated (no real setup-violating design tested yet)")
        lines += [f"changes: {done}", f"restored database: {self.par_db}", "", f"{'':28}{'before':>16}{'after':>16}"]
        lines += [f"{key:28}{before[key]:>16g}{after[key]:>16g}" for key in before]
        lines += ["", f"Innovus run time: {time.time() - os.path.getmtime(script):.0f} s"]
        with open(os.path.join(self.run_dir, "timing_eco_report.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        log = self.logger.error if status == "FAIL" else self.logger.info if lec == "verified" else self.logger.warning
        log("; ".join(lines[:5]) + f" ({self.run_dir}/timing_eco_report.txt)")
        return status != "FAIL"


def timing_eco_action(cli: CLIDriver, driver: HammerDriver) -> Optional[dict]:
    """ No technology hooks (they target par's full-flow steps), dependency check, PD cache or resume """
    tool = TimingEcoInnovus()
    tool.package = "hammer.par.innovus"  # Innovus's settings and defaults
    if not driver.set_up_par_tool(tool, "innovus", cli.par_rundir or os.path.join(driver.obj_dir, "timing-eco-rundir")):
        return None
    success, output = driver.run_par(tool.get_tool_hooks() + cli.get_extra_timing_eco_hooks())
    if not success or (tool.ran_write_design and not tool.timing_eco_report(timing_eco_lec(driver, tool, output))):
        return None
    dump_config_to_json_file(os.path.join(tool.run_dir, "par-output.json"), output)
    dump_config_to_json_file(os.path.join(tool.run_dir, "par-output-full.json"), cli.get_full_config(driver, output))
    return output


def timing_eco_lec(driver: HammerDriver, tool: TimingEcoInnovus, output: dict) -> str:
    """
    Conformal equivalence of the repaired netlist with the par run's, in <run dir>/lec: "verified", "failed (...)" or
    "not run (...)", with the Conformal plugin's netlist-to-netlist compare (formal.inputs.check eco_verify).
    """
    if driver.database.get_setting("vlsi.core.formal_tool") != "hammer.formal.conformal":
        return "not run (vlsi.core.formal_tool is not hammer.formal.conformal)"
    for key, value in (("formal.inputs.check", "eco_verify"),
                       ("formal.inputs.input_files", [output["par.outputs.output_sim_netlist"]]),
                       ("formal.inputs.reference_files", [driver.database.get_setting("par.outputs.output_sim_netlist")]),
                       ("formal.inputs.top_module", tool.top_module)):
        driver.database.set_setting(key, value)
    lec_dir = os.path.join(tool.run_dir, "lec")
    if not driver.load_formal_tool(lec_dir):
        return "not run (Conformal did not load)"
    # force_override: --from_step / --to_step name Innovus steps, which Conformal does not have
    if driver.run_formal(driver.formal_tool.get_tool_hooks() +  # type: ignore
                         driver.tech.get_tech_formal_hooks(driver.formal_tool.name), force_override=True)[0]:  # type: ignore
        return "verified"
    if not os.path.isfile(os.path.join(lec_dir, "eco_verify_verification.rpt")):
        return f"not run (Conformal gave no result; see {lec_dir})"
    return f"failed (Conformal did not prove the netlists equivalent; see {lec_dir})"
