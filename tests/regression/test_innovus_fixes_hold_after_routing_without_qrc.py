import json

import pytest

from hammer.config import HammerJSONEncoder
from hammer.par.innovus import Innovus
from hammer.vlsi import HammerDriver, HammerDriverOptions

TCL_OPT = "opt_design -post_route -setup -hold -expanded_views -timing_debug_report"
PY_OPT = "opt_design(post_route=True, setup=True, hold=True, expanded_views=True, timing_debug_report=True)"


def _route(tmp_path, version: str, use_python: bool = False) -> str:
    """The route step Innovus emits for this version, on a technology with no QRC tech files (as sky130)."""
    (tmp_path / "top.v").write_text("module top(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.par_tool": "hammer.par.innovus",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "par.inputs.input_files": [str(tmp_path / "top.v")],
        "par.inputs.top_module": "top",
        "par.innovus.version": version,
        "par.innovus.use_python": use_python,
    }, cls=HammerJSONEncoder))
    driver = HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(tmp_path / "config.json")],
                                              log_file=str(tmp_path / "log.txt"), obj_dir=str(tmp_path / "obj")))
    assert driver.load_par_tool()
    tool = driver.par_tool
    assert isinstance(tool, Innovus)
    assert tool.get_qrc_tech() == ""
    tool.route_opt_design()
    return "\n".join(tool.output)


def test_innovus_251_without_qrc_fixes_setup_and_hold_after_routing(tmp_path) -> None:
    out = _route(tmp_path, "251")
    assert "route_design" in out and "route_opt_design" not in out
    assert TCL_OPT in out
    assert out.index("set_db extract_rc_engine post_route") < out.index(TCL_OPT)


def test_innovus_251_without_qrc_fixes_setup_and_hold_in_python_mode(tmp_path) -> None:
    out = _route(tmp_path, "251", use_python=True)
    assert "route_design()" in out and PY_OPT in out


@pytest.mark.parametrize("version", ["231", "241"])
def test_innovus_231_to_241_keeps_route_opt_design(tmp_path, version) -> None:
    out = _route(tmp_path, version)
    assert "route_opt_design -timing_debug_report" in out and "opt_design -post_route" not in out
