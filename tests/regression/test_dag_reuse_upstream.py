import json
import os
import types

import pytest

from tests.regression.test_eco_stage_runs_after_par import _FailException, dag  # noqa: F401 (fixture)


def _committed(obj: str, stage: str, needs_rerun: bool = False) -> str:
    """A completed run of syn or par in obj as hammer leaves it: outputs, their full config, the master flag."""
    rundir = os.path.join(obj, f"{stage}-rundir")
    os.makedirs(rundir, exist_ok=True)
    source = os.path.join(obj, f"{stage}-source.v")
    open(source, "w").close()
    os.utime(source, (1, 1))
    netlist = os.path.join(rundir, "dummy.v")
    open(netlist, "w").close()
    keys = {"syn": {"synthesis.outputs.output_files": [netlist], "synthesis.inputs.input_files": [source]},
            "par": {"par.outputs.output_netlist": netlist, "par.outputs.output_sim_netlist": netlist,
                    "par.inputs.input_files": [source]}}[stage]
    out = os.path.join(rundir, f"{stage}-output-full.json")
    with open(out, "w") as f:
        json.dump(keys, f)
    master = os.path.join(obj, "master_database.json")
    flags = json.load(open(master)) if os.path.exists(master) else {}
    flags[f"{stage}.needsToRerun"] = needs_rerun
    with open(master, "w") as f:
        json.dump(flags, f)
    return out


class TestReuseUpstream:
    """An ECO on an existing layout: reuse_upstream keeps syn and par from running, but only on outputs that check out."""

    ECO_ONLY = {"eco_par": True, "reuse_upstream": True}
    ECO_PAR_TASK = types.SimpleNamespace(task_id="module_dummy.eco_par", downstream_list=[])

    def _stage(self, dag, stage: str, conf: dict, out: str):
        args = {"syn": ("", ["/o/proj.yml"], out), "par": ("", "/o/par-input.json", out)}[stage]
        return dag(stage, conf, *args, downstream=[self.ECO_PAR_TASK])

    def test_the_form_has_the_option(self, dag) -> None:
        assert "'reuse_upstream': Param(default=False, type='boolean', title='Reuse existing syn/par outputs'" in dag.text

    def test_completed_syn_and_par_are_reused_without_running(self, dag, capsys) -> None:
        obj = os.path.realpath(dag.obj)
        syn_out, par_out = _committed(obj, "syn"), _committed(obj, "par")
        assert self._stage(dag, "syn", self.ECO_ONLY, syn_out) is None
        assert self._stage(dag, "par", self.ECO_ONLY, par_out) is None
        # par is reused, so syn-to-par leaves its input alone
        assert dag("syn_to_par", self.ECO_ONLY, syn_out, "/o/par-input.json",
                   downstream=[self.ECO_PAR_TASK]) is None
        assert "dependency check (fingerprints included) was not consulted" in capsys.readouterr().out

    def test_without_the_option_syn_and_par_run_as_before(self, dag) -> None:
        obj = os.path.realpath(dag.obj)
        assert self._stage(dag, "par", {"eco_par": True}, _committed(obj, "par"))[-1] == "par"

    def test_a_ticked_stage_runs_even_with_the_option(self, dag) -> None:
        obj = os.path.realpath(dag.obj)
        assert self._stage(dag, "syn", dict(self.ECO_ONLY, syn=True), _committed(obj, "syn"))[-1] == "syn"

    def test_missing_outputs_fail_instead_of_running(self, dag) -> None:
        missing = os.path.join(os.path.realpath(dag.obj), "par-rundir", "par-output-full.json")
        with pytest.raises(_FailException, match="no par outputs to reuse"):
            self._stage(dag, "par", self.ECO_ONLY, missing)

    def test_a_netlist_gone_from_the_outputs_fails(self, dag) -> None:
        out = _committed(os.path.realpath(dag.obj), "par")
        os.remove(json.load(open(out))["par.outputs.output_netlist"])
        with pytest.raises(_FailException, match="par.outputs.output_netlist .* does not exist"):
            self._stage(dag, "par", self.ECO_ONLY, out)

    def test_an_input_newer_than_the_outputs_is_stale(self, dag) -> None:
        out = _committed(os.path.realpath(dag.obj), "syn")
        os.utime(json.load(open(out))["synthesis.inputs.input_files"][0], None)
        os.utime(out, (2, 2))
        with pytest.raises(_FailException, match="is newer than"):
            self._stage(dag, "syn", self.ECO_ONLY, out)

    def test_a_recorded_input_that_is_gone_fails(self, dag) -> None:
        out = _committed(os.path.realpath(dag.obj), "par")
        source = json.load(open(out))["par.inputs.input_files"][0]
        os.remove(source)
        with pytest.raises(_FailException, match=f"input {source} recorded in .* no longer exists"):
            self._stage(dag, "par", self.ECO_ONLY, out)

    def test_a_run_hammer_did_not_complete_is_not_reused(self, dag) -> None:
        out = _committed(os.path.realpath(dag.obj), "par", needs_rerun=True)
        with pytest.raises(_FailException, match="marks par as needing a rerun"):
            self._stage(dag, "par", self.ECO_ONLY, out)
