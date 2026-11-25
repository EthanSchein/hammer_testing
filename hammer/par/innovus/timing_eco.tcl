# Timing ECO procs (hammer.par.innovus.timing_eco): STA, bounded setup / hold repair, conditional DRV pass and final
# checks of a restored par database. Independent of the functional ECO; files are written in the run directory.

namespace eval ::hammer_timing_eco {}

proc ::hammer_timing_eco::log {msg} { puts "hammer-timing-eco: [clock format [clock seconds] -format %T] $msg" }

proc ::hammer_timing_eco::write_list {name items} { set f [open $name w]; puts $f [join $items "\n"]; close $f }

# setup / hold {WNS TNS} ("all" column) of the time_design summaries in timing_eco_<tag>/
proc ::hammer_timing_eco::read_sta {tag} {
  set d [dict create]
  foreach {kind file} {setup setup.summary.gz hold hold_hold.summary.gz} {
    set t [exec zcat timing_eco_$tag/$file]
    set w 1e9; set n 0.0
    regexp {WNS \(ns\):\|\s*(-?[0-9.]+)} $t - w
    regexp {TNS \(ns\):\|\s*(-?[0-9.]+)} $t - n
    dict set d $kind [list $w $n]
  }
  return $d
}

# time_design setup and hold in timing_eco_<tag>/, with the max_transition / max_capacitance listings taken in setup mode
proc ::hammer_timing_eco::sta {tag} {
  time_design -post_route -expanded_views -report_dir timing_eco_$tag -report_prefix setup
  report_constraint -drv_violation_type max_transition -all_violators > timing_eco_$tag/maxtran.rpt
  report_constraint -drv_violation_type max_capacitance -all_violators > timing_eco_$tag/maxcap.rpt
  time_design -post_route -hold -expanded_views -report_dir timing_eco_$tag -report_prefix hold
  set d [read_sta $tag]
  log "$tag: setup WNS TNS [dict get $d setup] ns, hold WNS TNS [dict get $d hold] ns"
  return $d
}

# "kind pin" -> worst slack over views, from the report_constraint rows (pin required actual slack view) of <tag>
proc ::hammer_timing_eco::drv_pins {tag} {
  set d [dict create]
  foreach kind {maxtran maxcap} {
    set f [open timing_eco_$tag/$kind.rpt]
    foreach line [split [read $f] "\n"] {
      set w [regexp -all -inline {\S+} $line]
      if {[llength $w] == 5 && [string is double -strict [lindex $w 3]]} {
        set k "$kind [lindex $w 0]"
        if {![dict exists $d $k] || [lindex $w 3] < [dict get $d $k]} { dict set d $k [lindex $w 3] }
      }
    }
    close $f
  }
  return $d
}

# max_transition / max_capacitance violations of <tag> new against the restored database, or worse by more than tol ns
proc ::hammer_timing_eco::regressions {tag tol} {
  set base [drv_pins before]
  set bad {}
  dict for {k s} [drv_pins $tag] { if {![dict exists $base $k] || $s < [dict get $base $k] - $tol} { lappend bad "$k $s" } }
  return $bad
}

# the kinds (setup, hold) whose WNS is negative
proc ::hammer_timing_eco::failing {sta} {
  set kinds {}
  dict for {kind v} $sta { if {[lindex $v 0] < 0} { lappend kinds $kind } }
  return $kinds
}

# logical (non-physical) cell -> base cell
proc ::hammer_timing_eco::snapshot {} {
  set cells [get_db insts -if {!.is_physical}]
  set d [dict create]
  foreach n [get_db $cells .name] c [get_db $cells .base_cell.name] { dict set d $n $c }
  return $d
}

# timing_eco_changes.txt: the passes run, and the logical cells added / resized / removed since snapshot $before with
# the signal nets on the added or resized ones
proc ::hammer_timing_eco::write_changes {attempts drv before} {
  set pg [dict create]
  foreach n [get_db [get_db nets -if {.is_power || .is_ground}] .name] { dict set pg $n 1 }
  set added 0; set resized 0; set nets {}
  dict for {n c} [snapshot] {
    if {![dict exists $before $n]} {
      incr added
    } else {
      set old [dict get $before $n]
      dict unset before $n
      if {$old eq $c} continue
      incr resized
    }
    foreach net [get_db [get_db insts $n] .pins.net.name] { if {$net ne "" && ![dict exists $pg $net]} { lappend nets $net } }
  }
  set nets [lsort -unique $nets]
  write_list timing_eco_changes.txt [list "attempts $attempts" "drv_pass $drv" "added $added" "resized $resized" \
    "removed [dict size $before]" "nets [llength $nets]"]
  log "$attempts timing passes and $drv DRV pass added $added, resized $resized and removed [dict size $before] cells on [llength $nets] nets"
}

# Nothing when the restored design meets setup and hold. Otherwise at most max_attempts opt_design -post_route passes on
# the failing kinds, stopping once none fails or a pass improves no failing WNS / TNS, with area reclaim and power
# optimization off. The fillers stay (opt_design's transparent filler flow) but count in its density, so its maximum
# density, a placement limit and not a timing constraint, goes to 1. Then one opt_design -drv pass if the timing passes
# made max_transition / max_capacitance worse by more than tol ns, keeping the design before it in timing_eco_pre_drv.
# The setup branch is UNVALIDATED on a naturally setup-violating design.
proc ::hammer_timing_eco::repair {max_attempts tol} {
  set cur [read_sta before]
  set before [snapshot]
  set attempts 0
  set drv 0
  if {![llength [failing $cur]]} {
    log "the restored design meets setup and hold: no optimization"
  } else {
    set_db opt_post_route_area_reclaim none
    set_db opt_power_effort none
    set_db opt_max_density 1.0
    while {$attempts < $max_attempts && [llength [set kinds [failing $cur]]]} {
      incr attempts
      log "attempt $attempts: opt_design -post_route -[join $kinds { -}]"
      opt_design -post_route {*}[lmap k $kinds {string cat - $k}] -expanded_views -report_dir timing_eco_opt$attempts -report_prefix opt
      set new [sta opt$attempts]
      set better 0
      foreach k $kinds {
        lassign [dict get $cur $k] w0 t0
        lassign [dict get $new $k] w1 t1
        if {$w1 > $w0 + 0.001 || $t1 > $t0 + 0.001} { set better 1 }
      }
      set cur $new
      if {!$better} { log "attempt $attempts improved no failing WNS or TNS: stopping"; break }
    }
    if {[llength [regressions opt$attempts $tol]]} {
      log "the timing passes made max_transition / max_capacitance worse: one opt_design -drv pass (before it: timing_eco_pre_drv)"
      write_db timing_eco_pre_drv
      opt_design -post_route -drv -expanded_views -report_dir timing_eco_opt_drv -report_prefix drv
      sta drv
      set drv 1
    }
  }
  write_changes $attempts $drv $before
}

# final check against the restored database (timing_eco_after/, timing_eco_new_violations.txt)
proc ::hammer_timing_eco::verify {tol} {
  sta after
  set bad [regressions after $tol]
  write_list timing_eco_new_violations.txt $bad
  log "final check: [llength $bad] max_transition / max_capacitance violations new or worse"
}

# Innovus DRC and connectivity of the whole design (timing_eco_{drc,conn}_<tag>.rpt), and PPA (timing_eco_ppa_<tag>.txt)
proc ::hammer_timing_eco::physical {tag} {
  catch {delete_markers -all}
  check_drc -limit 100000 -out_file timing_eco_drc_$tag.rpt
  check_connectivity -type regular -error 100000 -out_file timing_eco_conn_$tag.rpt
  set cells [get_db insts -if {!.is_physical}]
  set area 0.0
  foreach a [get_db $cells .area] { set area [expr {$area + $a}] }
  set wl 0.0
  foreach l [get_db [get_db nets -if {!.is_power && !.is_ground}] .wires.length] { set wl [expr {$wl + $l}] }
  write_list timing_eco_ppa_$tag.txt [list "cells [llength $cells]" "area $area" "wirelength $wl" \
    "buffers [llength [get_db $cells -if {.base_cell.is_buffer}]]" "inverters [llength [get_db $cells -if {.base_cell.is_inverter}]]"]
}
