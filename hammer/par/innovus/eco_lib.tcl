# Hammer eco_par helpers (hammer.par.innovus): electrical baseline, targeted post-route optimization,
# final verification. Sourced by the eco_par steps; every proc works in the run directory.
#
# Files (all in the run directory):
#   eco_new_insts.txt              cells the patch added (eco_apply_patch)
#   eco_before_{maxtran,maxcap}.rpt, eco_before_timing.txt
#                                  max_transition / max_capacitance violators and setup / hold WNS of the
#                                  restored database (eco_restore_design): the baseline every check is set against
#   eco_affected_nets.txt          ECO nets (on the patch's cells) plus the nets of every cell the
#                                  optimization added or resized (eco_opt_design, any mode)
#   eco_opt_decisions.txt          targeted mode: each decision and what it was based on
#   eco_after_{maxtran,maxcap}.rpt, eco_after_timing.txt, eco_final_timing/
#                                  the final verification (eco_check)
# Every line this file prints starts with "hammer-eco:".

namespace eval ::hammer_eco {
  variable t0 [clock seconds]
  variable decisions {}
}

proc ::hammer_eco::log {msg} {
  variable decisions
  puts "hammer-eco: $msg"
  lappend decisions $msg
}

proc ::hammer_eco::stamp {tag} {
  variable t0
  puts "hammer-eco-time $tag [expr {[clock seconds] - $t0}] s"
  set t0 [clock seconds]
}

# WNS (the "all" column) of a time_design summary report; 1e9 when it reports none
proc ::hammer_eco::summary_wns {file} {
  if {[catch {exec zcat $file} t]} { error "hammer-eco: cannot read $file" }
  if {[regexp {WNS \(ns\):\|\s*(-?[0-9.]+)} $t -> w]} { return $w }
  return 1e9
}

# setup (-late) or hold (-early) WNS over every active view. report_timing -collection is quick, but it can
# come back empty while paths exist (seen right after a whole-design opt_design in the same session): then the
# number comes from a time_design summary instead of being taken as "nothing constrained"
proc ::hammer_eco::wns {mode} {
  set p [report_timing -collection -max_paths 1 $mode]
  if {[sizeof_collection $p] > 0} {
    set s [get_db $p .slack]
    if {[string is double -strict $s]} { return $s }
  }
  set dir eco_wns_[string trimleft $mode -]_[clock seconds]
  log "report_timing $mode returned no path; WNS from time_design ($dir)"
  if {$mode eq "-late"} {
    time_design -post_route -expanded_views -report_dir $dir -report_prefix setup
    return [summary_wns $dir/setup.summary.gz]
  }
  time_design -post_route -hold -expanded_views -report_dir $dir -report_prefix hold
  return [summary_wns $dir/hold_hold.summary.gz]
}

proc ::hammer_eco::write_drv {prefix} {
  report_constraint -drv_violation_type max_transition -all_violators > ${prefix}_maxtran.rpt
  report_constraint -drv_violation_type max_capacitance -all_violators > ${prefix}_maxcap.rpt
}

# check pin -> worst slack, from report_constraint -all_violators listings
# (rows "pin required actual slack view"; a pin can appear once per view)
proc ::hammer_eco::drv_pins {prefix} {
  set d [dict create]
  foreach kind {maxtran maxcap} {
    set f [open ${prefix}_${kind}.rpt]
    foreach line [split [read $f] "\n"] {
      set w [regexp -all -inline {\S+} $line]
      if {[llength $w] == 5 && [string is double -strict [lindex $w 3]]} {
        set k "$kind [lindex $w 0]"
        set s [lindex $w 3]
        if {![dict exists $d $k] || $s < [dict get $d $k]} { dict set d $k $s }
      }
    }
    close $f
  }
  return $d
}

# baseline of the restored database: what the ECO is checked against
proc ::hammer_eco::baseline {} {
  # setup-mode queries together, then hold: every switch between the two re-times the design
  write_drv eco_before
  set s [wns -late]
  set h [wns -early]
  set f [open eco_before_timing.txt w]
  puts $f "setup $s"
  puts $f "hold $h"
  close $f
  set d [drv_pins eco_before]
  log "baseline (restored database): [dict size $d] max_transition / max_capacitance violations, [string map {\n {, }} [string trim [read_file eco_before_timing.txt]]]"
  stamp baseline
}

proc ::hammer_eco::read_file {name} {
  set f [open $name]; set t [read $f]; close $f; return $t
}

proc ::hammer_eco::timing_of {name} {
  set d [dict create setup 1e9 hold 1e9]
  foreach line [split [read_file $name] "\n"] {
    if {[llength $line] == 2} { dict set d [lindex $line 0] [lindex $line 1] }
  }
  return $d
}

# nets on the patch's cells, as eco_check has always derived them
proc ::hammer_eco::eco_nets {} {
  set nets {}
  foreach n [read_file eco_new_insts.txt] {
    foreach i [get_db insts $n] { lappend nets {*}[get_db $i .pins.net.name] }
  }
  return [lsort -unique [lsearch -all -inline -not -exact $nets {}]]
}

proc ::hammer_eco::snapshot {} {
  set d [dict create]
  foreach n [get_db insts .name] c [get_db insts .base_cell.name] { dict set d $n $c }
  return $d
}

# signal nets of the cells added or resized since snapshot $before; cells with no signal net (fillers,
# decaps, taps, which opt_design deletes and re-adds around what it changes) are not counted
proc ::hammer_eco::touched_nets {before} {
  set pg [dict create]
  foreach n [concat [get_db nets -if {.is_power}] [get_db nets -if {.is_ground}]] { dict set pg [get_db $n .name] 1 }
  set nets {}; set added 0; set resized 0
  foreach n [get_db insts .name] c [get_db insts .base_cell.name] {
    if {![dict exists $before $n]} { set kind added } elseif {[dict get $before $n] ne $c} { set kind resized } else { continue }
    set sig {}
    foreach net [get_db [get_db insts $n] .pins.net.name] {
      if {$net ne "" && ![dict exists $pg $net]} { lappend sig $net }
    }
    if {![llength $sig]} { continue }
    incr $kind
    lappend nets {*}$sig
  }
  return [list [lsort -unique $nets] $added $resized]
}

proc ::hammer_eco::write_list {name items} {
  set f [open $name w]; puts $f [join $items "\n"]; close $f
}

proc ::hammer_eco::affected {} {
  if {[file exists eco_affected_nets.txt]} { return [read_file eco_affected_nets.txt] }
  return [eco_nets]
}

# check pins that count against the ECO: violating now and not in the baseline, or in the baseline,
# on an ECO-affected net, and worse than the baseline by more than tol
proc ::hammer_eco::eco_violations {base cur affected tol} {
  set new {}; set worse {}
  set aff [dict create]; foreach n $affected { dict set aff $n 1 }
  dict for {k s} $cur {
    if {![dict exists $base $k]} { lappend new $k; continue }
    if {$s < [dict get $base $k] - $tol} {
      set net [get_db [get_db pins [lindex $k 1]] .net.name]
      if {[dict exists $aff $net]} { lappend worse $k }
    }
  }
  return [list $new $worse]
}

proc ::hammer_eco::nets_of {keys} {
  set nets {}
  foreach k $keys {
    set net [get_db [get_db pins [lindex $k 1]] .net.name]
    if {$net ne "" && $net ni {VDD VSS}} { lappend nets $net }
  }
  return [lsort -unique $nets]
}

# targeted post-route optimization (par.innovus.eco_opt_mode targeted)
# up to max_passes opt_design -drv passes on the nets of the violations the ECO caused (new against the
# baseline, or worse on an ECO-affected net). A pass runs only when such violations remain and they are not
# exactly the set the previous pass already targeted. Returns the ECO-affected nets, grown by every cell a
# pass added or resized.
proc ::hammer_eco::drv_passes {base affected tol max_passes tag {listing {}}} {
  if {$listing eq ""} {
    write_drv eco_${tag}0
    set listing eco_${tag}0
    stamp ${tag}_check
  }
  set cur [drv_pins $listing]
  set targeted {}
  for {set pass 1} {$pass <= $max_passes} {incr pass} {
    lassign [eco_violations $base $cur $affected $tol] new worse
    set keys [concat $new $worse]
    set nets [nets_of $keys]
    log "$tag pass $pass check: [llength $new] new max_transition / max_capacitance violations, [llength $worse] made worse on ECO-affected nets -> [llength $nets] nets"
    if {![llength $nets]} { log "$tag pass $pass: nothing the ECO caused is left, stopping"; return $affected }
    if {$pass > 1 && [lsort $keys] eq $targeted} { log "$tag pass $pass: the same violations the previous pass targeted, stopping"; return $affected }
    set targeted [lsort $keys]
    write_list eco_opt_${tag}${pass}_nets.txt $nets
    set before [snapshot]
    opt_design -post_route -drv -include_nets eco_opt_${tag}${pass}_nets.txt -expanded_views -report_dir eco_opt_${tag}${pass} -report_prefix drv
    lassign [touched_nets $before] tnets added resized
    set affected [lsort -unique [concat $affected $tnets]]
    log "$tag pass $pass: opt_design -drv added $added and resized $resized cells; ECO-affected nets now [llength $affected]"
    stamp ${tag}_pass$pass
    write_drv eco_${tag}${pass}
    set cur [drv_pins eco_${tag}${pass}]
  }
  lassign [eco_violations $base $cur $affected $tol] new worse
  log "$tag: after $max_passes passes [llength $new] new, [llength $worse] worse remain (par.innovus.eco_opt_max_drv_passes)"
  return $affected
}

# the timing checks a decision needs: {kind now limit} for setup and hold, failing when below min(0, baseline) - tol
proc ::hammer_eco::timing_fails {btime tol} {
  set fails {}
  foreach {kind mode} {setup -late hold -early} {
    set now [wns $mode]
    set limit [expr {min(0.0, [dict get $btime $kind]) - $tol}]
    log "$kind WNS $now ns (baseline [dict get $btime $kind] ns, fails below $limit ns)"
    if {$now < $limit} { lappend fails $kind }
  }
  return $fails
}

# targeted post-route optimization (par.innovus.eco_opt_mode targeted):
#   1. post-ECO STA and electrical checks against the baseline;
#   2. the ECO broke setup or hold: the whole-design optimization (the full mode's command, which also repairs
#      DRV), then one DRV pass for whatever it left or caused. A separate DRV pass first would only add an
#      opt_design call: each has a large fixed cost, and the timing optimization repairs DRV anyway;
#   3. timing holds: opt_design -drv passes on the nets of the ECO-caused max_transition / max_capacitance
#      violations only; if those passes then break timing, setup / hold optimization and one more DRV pass.
proc ::hammer_eco::targeted {max_passes tol} {
  variable decisions
  set decisions {}
  stamp start
  # whole-design work no ECO requires
  set_db opt_post_route_area_reclaim none
  set_db opt_power_effort none
  set base [drv_pins eco_before]
  set btime [timing_of eco_before_timing.txt]
  set affected [eco_nets]
  log "ECO nets (on the patch's [llength [read_file eco_new_insts.txt]] cells): [llength $affected]"

  # 1. post-ECO STA (setup-mode queries first: the DRV listing, then setup, then hold)
  write_drv eco_post
  set fails [timing_fails $btime $tol]
  stamp post_eco_sta
  if {[llength $fails]} {
    # 2. the ECO broke timing
    log "the ECO broke [join $fails { and }]: whole-design optimization (opt_design -post_route -setup -hold)"
    set before [snapshot]
    opt_design -post_route -setup -hold -expanded_views -timing_debug_report
    lassign [touched_nets $before] tnets added resized
    set affected [lsort -unique [concat $affected $tnets]]
    log "whole-design optimization added $added and resized $resized cells"
    stamp timing_opt
    set affected [drv_passes $base $affected $tol 1 post_timing_drv]
  } else {
    # 3. timing holds: DRV only
    set affected [drv_passes $base $affected $tol $max_passes drv eco_post]
    set fails [timing_fails $btime $tol]
    stamp timing_check
    foreach kind $fails {
      log "the DRV passes broke $kind: $kind optimization"
      set before [snapshot]
      opt_design -post_route -$kind -expanded_views -report_dir eco_opt_$kind -report_prefix $kind
      lassign [touched_nets $before] tnets added resized
      set affected [lsort -unique [concat $affected $tnets]]
      log "$kind optimization added $added and resized $resized cells"
      stamp ${kind}_opt
    }
    if {[llength $fails]} { set affected [drv_passes $base $affected $tol 1 post_timing_drv] }
  }
  write_list eco_affected_nets.txt $affected
  write_list eco_opt_decisions.txt $decisions
}

# full / none: record what the optimization touched (full: the cells it added or resized since snapshot
# $before), so eco_check checks those nets too
proc ::hammer_eco::record {mode {before {}}} {
  variable decisions
  set decisions {}
  set affected [eco_nets]
  if {$mode eq "full"} {
    lassign [touched_nets $before] tnets added resized
    set affected [lsort -unique [concat $affected $tnets]]
    log "full: opt_design added $added and resized $resized cells"
  } else {
    log "none: no post-route optimization"
  }
  write_list eco_affected_nets.txt $affected
  write_list eco_opt_decisions.txt $decisions
  stamp opt_$mode
}

# final verification (eco_check, every mode)
proc ::hammer_eco::verify {tol} {
  set affected [affected]
  if {[llength $affected]} {
    check_connectivity -nets $affected -type regular -out_file eco_connectivity.rpt
  } else {
    set f [open eco_connectivity.rpt w]; puts $f {Found no problems or warnings (the patch added no cells).}; close $f
  }
  check_drc -out_file eco_drc_after.rpt -limit 100000
  stamp check_connectivity_drc
  # sign-off reports (eco_final_timing/): the WNS checked is the one they report; the max_transition /
  # max_capacitance listing is taken in the setup mode time_design leaves, so it does not re-time the design
  time_design -post_route -expanded_views -report_dir eco_final_timing -report_prefix setup
  set s [summary_wns eco_final_timing/setup.summary.gz]
  write_drv eco_after
  time_design -post_route -hold -expanded_views -report_dir eco_final_timing -report_prefix hold
  set h [summary_wns eco_final_timing/hold_hold.summary.gz]
  set f [open eco_after_timing.txt w]
  puts $f "setup $s"
  puts $f "hold $h"
  close $f
  set base [drv_pins eco_before]
  set cur [drv_pins eco_after]
  lassign [eco_violations $base $cur $affected $tol] new worse
  write_list eco_new_violations.txt [concat $new $worse]
  log "final verification: [dict size $cur] max_transition / max_capacitance violations (baseline [dict size $base]); [llength $new] new, [llength $worse] worse on ECO-affected nets; [string map {\n {, }} [string trim [read_file eco_after_timing.txt]]]"
  stamp final_sta
}
