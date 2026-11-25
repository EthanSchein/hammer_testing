# eco_par helpers (hammer.par.innovus): baseline of the restored database, targeted post-route
# optimization, final verification. Files are written in the run directory; log lines start "hammer-eco: <time>".

namespace eval ::hammer_eco {
  variable decisions {}
}

proc ::hammer_eco::log {msg} {
  variable decisions
  puts "hammer-eco: [clock format [clock seconds] -format %T] $msg"
  lappend decisions $msg
}

# WNS (the "all" column) of a time_design summary report; 1e9 when it reports none
proc ::hammer_eco::summary_wns {file} {
  if {[catch {exec zcat $file} t]} { error "hammer-eco: cannot read $file" }
  if {[regexp {WNS \(ns\):\|\s*(-?[0-9.]+)} $t -> w]} { return $w }
  return 1e9
}

# setup (-late) or hold (-early) WNS. report_timing -collection can return no path right after opt_design
# in the same session; then the WNS comes from a time_design summary
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

# "kind pin" -> worst slack over views, from report_constraint -all_violators rows (pin required actual slack view)
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

proc ::hammer_eco::baseline {} {
  # setup-mode queries first, then hold: each switch re-times the design
  write_drv eco_before
  set s [wns -late]
  set h [wns -early]
  set f [open eco_before_timing.txt w]
  puts $f "setup $s"
  puts $f "hold $h"
  close $f
  set d [drv_pins eco_before]
  log "baseline (restored database): [dict size $d] max_transition / max_capacitance violations, [string map {\n {, }} [string trim [read_file eco_before_timing.txt]]]"
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

# nets on the patch's cells
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

# signal nets of cells added or resized since snapshot $before (fillers, decaps and taps have none)
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

# violations the ECO caused: not in the baseline, or worse than it by more than tol on an ECO-affected net
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

# up to max_passes opt_design -drv passes on the nets of ECO-caused violations; stops when none remain or
# a pass would target the same set as the last. Returns the ECO-affected nets, plus nets of cells it touched.
proc ::hammer_eco::drv_passes {base affected tol max_passes tag {listing {}}} {
  if {$listing eq ""} {
    write_drv eco_${tag}0
    set listing eco_${tag}0
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
    write_drv eco_${tag}${pass}
    set cur [drv_pins eco_${tag}${pass}]
  }
  lassign [eco_violations $base $cur $affected $tol] new worse
  log "$tag: after $max_passes passes [llength $new] new, [llength $worse] worse remain (par.innovus.eco_opt_max_drv_passes)"
  return $affected
}

# setup / hold kinds whose WNS is below min(0, baseline) - tol
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

# targeted mode: if the ECO broke setup or hold, the whole-design optimization and one DRV pass (it also
# repairs DRV, and each opt_design call has a large fixed cost); otherwise DRV passes on ECO-caused
# violations only, then setup / hold optimization and one more DRV pass if those passes broke timing.
proc ::hammer_eco::targeted {max_passes tol} {
  variable decisions
  set decisions {}
  set_db opt_post_route_area_reclaim none
  set_db opt_power_effort none
  set base [drv_pins eco_before]
  set btime [timing_of eco_before_timing.txt]
  set affected [eco_nets]
  log "ECO nets (on the patch's [llength [read_file eco_new_insts.txt]] cells): [llength $affected]"

  write_drv eco_post
  set fails [timing_fails $btime $tol]
  if {[llength $fails]} {
    log "the ECO broke [join $fails { and }]: whole-design optimization (opt_design -post_route -setup -hold)"
    set before [snapshot]
    opt_design -post_route -setup -hold -expanded_views -timing_debug_report
    lassign [touched_nets $before] tnets added resized
    set affected [lsort -unique [concat $affected $tnets]]
    log "whole-design optimization added $added and resized $resized cells"
    set affected [drv_passes $base $affected $tol 1 post_timing_drv]
  } else {
    set affected [drv_passes $base $affected $tol $max_passes drv eco_post]
    set fails [timing_fails $btime $tol]
    foreach kind $fails {
      log "the DRV passes broke $kind: $kind optimization"
      set before [snapshot]
      opt_design -post_route -$kind -expanded_views -report_dir eco_opt_$kind -report_prefix $kind
      lassign [touched_nets $before] tnets added resized
      set affected [lsort -unique [concat $affected $tnets]]
      log "$kind optimization added $added and resized $resized cells"
    }
    if {[llength $fails]} { set affected [drv_passes $base $affected $tol 1 post_timing_drv] }
  }
  write_list eco_affected_nets.txt $affected
  write_list eco_opt_decisions.txt $decisions
}

# full / none: record the nets the optimization touched, so eco_check checks them too
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
}

# final verification (eco_check, every mode)
proc ::hammer_eco::verify {tol} {
  set affected [affected]
  if {[llength $affected]} {
    check_connectivity -nets $affected -type regular -out_file eco_connectivity.rpt
  } else {
    set f [open eco_connectivity.rpt w]; puts $f {Found no problems or warnings (the patch added no cells).}; close $f
  }
  # DRV listing taken in the setup mode time_design leaves, so it does not re-time the design
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
}
