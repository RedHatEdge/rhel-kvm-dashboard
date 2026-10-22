#!/usr/bin/env python3
"""Generate the 'Fleet Health' Grafana dashboard: fleet-wide view that bubbles up outlying VMs.

python3 grafana/gen_fleet_dashboard.py > grafana/dashboards/fleet-health.json

Everything comes from PCP Redis (all hosts at once). Panels that list outliers
are single-metric: Reduce(labelsToFields) gives one row per series with its
labels (hostname, guest_name, ...) as columns, and filterByValue keeps only the
rows over threshold. Thresholds are fixed in this file (Grafana 10.2 can't
template transformation values) -- change them here and regenerate.
"""
import json
import sys

from dashlib import DS, PCT_THRESH, nid, override, pct_gauge_col, targets, timeseries

# ---- outlier thresholds ----------------------------------------------------
VM_CPU_PCT = 85        # sustained: 15-minute average, % of the VM's own vCPUs
VM_MEM_PCT = 90        # current qemu RSS, % of the VM's assigned memory
HOST_PCT = 80          # host CPU or memory, current
WINDOW = "15m"         # window for "sustained" / "top consumers"
STATE_WINDOW = "2m"    # window for VM state (sampled every 10s)

PER_HOST = "/d/hv-vm-overview/hypervisor-and-vm-overview?var-host=${__data.fields.Host}&${__url_time_range}"
HOST_LINK = [{"title": "Open ${__data.fields.Host} in Hypervisor & VM Overview", "url": PER_HOST}]

# libvirt domain state codes (openmetrics.libvirt_vms.state)
STATE_MAP = [{"type": "value", "options": {
    "0": {"text": "unknown", "color": "purple", "index": 0},
    "1": {"text": "running", "color": "green", "index": 1},
    "2": {"text": "blocked", "color": "orange", "index": 2},
    "3": {"text": "paused", "color": "#EAB839", "index": 3},
    "4": {"text": "shutting down", "color": "orange", "index": 4},
    "5": {"text": "shut off", "color": "red", "index": 5},
    "6": {"text": "crashed", "color": "dark-red", "index": 6},
    "7": {"text": "suspended", "color": "#EAB839", "index": 7}}}]


# same colours as STATE_MAP, as threshold steps (used by the state timeline, see there)
STATE_THRESH = {"mode": "absolute", "steps": [
    {"color": "purple", "value": None}, {"color": "green", "value": 1}, {"color": "orange", "value": 2},
    {"color": "#EAB839", "value": 3}, {"color": "orange", "value": 4}, {"color": "red", "value": 5},
    {"color": "dark-red", "value": 6}, {"color": "#EAB839", "value": 7}]}


def flt(field, matcher, value):
    return {"fieldName": field, "config": {"id": matcher, "options": {"value": value}}}


def filter_rows(*filters, match="all"):
    return {"id": "filterByValue", "options": {"filters": list(filters), "type": "include", "match": match}}


def reduce_rows(reducers, labels=True):
    return {"id": "reduce", "options": {"reducers": reducers, "mode": "seriesToRows", "includeTimeField": False,
                                        "labelsToFields": labels}}


def keep_columns(rename):
    """Keep only the columns in <rename> (raw name -> display name), in that order."""
    names = list(rename)
    return [{"id": "filterFieldsByName", "options": {"include": {"names": names}}},
            {"id": "organize", "options": {"excludeByName": {}, "indexByName": {n: i for i, n in enumerate(names)},
                                           "renameByName": rename}}]


def count_stat(title, tgts, transformations, gridPos, time_from, bad_from=1, bad_color="red", desc=""):
    """Number of rows left after <transformations>."""
    return {"id": nid(), "type": "stat", "title": title, "description": desc, "datasource": DS, "gridPos": gridPos,
            "targets": tgts, "timeFrom": time_from, "hideTimeOverride": True, "transformations": transformations,
            "options": {"reduceOptions": {"calcs": ["count"], "fields": "/^(Last \\*|Mean|Max)$/", "values": False},
                        "colorMode": "background", "graphMode": "none", "justifyMode": "center", "textMode": "value"},
            "fieldConfig": {"defaults": {"noValue": "0", "decimals": 0, "color": {"mode": "thresholds"},
                                         "thresholds": {"mode": "absolute", "steps": [
                                             {"color": "green", "value": None}, {"color": bad_color, "value": bad_from}]}},
                            "overrides": []}}


def outlier_table(title, tgts, transformations, gridPos, time_from, overrides, empty, sort_by=None, desc=""):
    return {"id": nid(), "type": "table", "title": title, "description": desc, "datasource": DS, "gridPos": gridPos,
            "targets": tgts, "timeFrom": time_from, "hideTimeOverride": True, "transformations": transformations,
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}, "sortBy": sort_by or []},
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
                                         "decimals": 1, "thresholds": PCT_THRESH, "noValue": empty},
                            "overrides": [override("Host", links=HOST_LINK, custom__width=110),
                                          override("VM", custom__width=190), *overrides]}}


def top_bars(title, tgts, reducer, unit, gridPos, max_=None, desc=""):
    """Top 10 series fleet-wide by <reducer> over WINDOW (series named by legend)."""
    calc = {"mean": "Mean", "lastNotNull": "Last *", "max": "Max"}[reducer]
    p = {"id": nid(), "type": "bargauge", "title": title, "description": desc, "datasource": DS, "gridPos": gridPos,
         "targets": tgts, "timeFrom": WINDOW, "hideTimeOverride": True,
         "transformations": [reduce_rows([reducer], labels=False),
                             {"id": "sortBy", "options": {"sort": [{"field": calc, "desc": True}]}},
                             {"id": "limit", "options": {"limitField": 10}}],
         "options": {"reduceOptions": {"calcs": [], "fields": "/^" + calc.replace("*", "\\*") + "$/", "values": True},
                     "orientation": "horizontal", "displayMode": "gradient", "valueMode": "color",
                     "showUnfilled": True, "namePlacement": "left", "sizing": "manual", "minVizHeight": 16,
                     "maxVizHeight": 22},
         "fieldConfig": {"defaults": {"unit": unit, "decimals": 1, "min": 0, "color": {"mode": "thresholds"},
                                      "thresholds": PCT_THRESH if unit == "percent" else
                                      {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}},
                         "overrides": []}}
    if max_ is not None:
        p["fieldConfig"]["defaults"]["max"] = max_
    return p


panels = []
y = 0


def row(title):
    global y
    panels.append({"id": nid(), "type": "row", "title": title, "collapsed": False,
                   "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
    y += 1


# openmetrics instance labels don't reach pmseries, but the instance name carries them
# ("<id> autostart:yes guest_name:<vm>"); the legend adds the host and extractFields(kvp)
# splits it into host / autostart / guest_name columns.
STATE = targets(("openmetrics.libvirt_vms.state", "host:$hostname $instance"))
STATE_ROWS = [{"id": "reduce", "options": {"reducers": ["lastNotNull"], "mode": "seriesToRows",
                                           "includeTimeField": False, "labelsToFields": False}},
              {"id": "extractFields", "options": {"source": "Field", "format": "kvp", "replace": False}}]
VMCPU = targets(("ops.vm.cpu.util", ""))
VMMEM = targets(("ops.vm.mem.util", ""))
DROPS = targets(("libvirt.domstats.net.all.rx.drop", "$guest_name · $hostname · rx"),
                ("libvirt.domstats.net.all.tx.drop", "$guest_name · $hostname · tx"))
HOSTP = targets(("ops.host.cpu.util", "$hostname · CPU"), ("ops.host.mem.util", "$hostname · memory"))

# ---------------------------------------------------------------- tiles ----
row("Fleet at a glance")
w, h = 3, 4
tiles = [
    count_stat("Hypervisors reporting", targets(("hinv.ncpu", "")), [reduce_rows(["lastNotNull"])],
               None, STATE_WINDOW, bad_from=1e9, desc="Hosts with data in the last 2 minutes."),
    count_stat("VMs running", STATE, [*STATE_ROWS, filter_rows(flt("Last *", "equal", 1))],
               None, STATE_WINDOW, bad_from=1e9),
    count_stat("VMs not running", STATE, [*STATE_ROWS, filter_rows(flt("Last *", "notEqual", 1))],
               None, STATE_WINDOW, bad_color="#EAB839", desc="Defined VMs in any state other than running."),
    count_stat("Should be running", STATE,
               [*STATE_ROWS, filter_rows(flt("Last *", "notEqual", 1), flt("autostart", "equal", "yes"))],
               None, STATE_WINDOW, desc="Not running although marked autostart -- most likely an outage."),
    count_stat(f"VMs CPU ≥ {VM_CPU_PCT}% ({WINDOW} avg)", VMCPU,
               [reduce_rows(["mean"]), filter_rows(flt("Mean", "greaterOrEqual", VM_CPU_PCT))],
               None, WINDOW, bad_color="orange"),
    count_stat(f"VMs memory ≥ {VM_MEM_PCT}%", VMMEM,
               [reduce_rows(["lastNotNull"]), filter_rows(flt("Last *", "greaterOrEqual", VM_MEM_PCT))],
               None, STATE_WINDOW, bad_color="orange"),
    count_stat("VM NICs dropping packets", DROPS,
               [reduce_rows(["max"], labels=False), filter_rows(flt("Max", "greater", 0))],
               None, WINDOW, desc=f"virtio/tap interfaces with any rx/tx drops in the last {WINDOW}."),
    count_stat(f"Hosts ≥ {HOST_PCT}% CPU/mem", HOSTP,
               [reduce_rows(["lastNotNull"], labels=False), filter_rows(flt("Last *", "greaterOrEqual", HOST_PCT))],
               None, STATE_WINDOW, bad_color="orange"),
]
for i, t in enumerate(tiles):
    t["gridPos"] = {"x": i * w, "y": y, "w": w, "h": h}
panels += tiles
y += h

# ------------------------------------------------------- needs attention ---
row("Needs attention")
h = 8
panels.append(outlier_table(
    "VMs not running", STATE,
    [*STATE_ROWS, filter_rows(flt("Last *", "notEqual", 1)),
     *keep_columns({"host": "Host", "guest_name": "VM", "Last *": "State", "autostart": "Autostart"})],
    {"x": 0, "y": y, "w": 12, "h": h}, STATE_WINDOW,
    [override("State", mappings=STATE_MAP, custom__cellOptions={"type": "color-text"}, decimals=0),
     override("Autostart", custom__cellOptions={"type": "color-background", "mode": "basic"},
              mappings=[{"type": "value", "options": {
                  "yes": {"text": "yes – should be running", "color": "red", "index": 0},
                  "no": {"text": "no", "color": "transparent", "index": 1}}}])],
    "✓ All VMs running", sort_by=[{"displayName": "Autostart", "desc": True}],
    desc="Every defined VM that isn't running, from the libvirt_vms OpenMetrics source. Autostart VMs are expected "
         "to be up, so they're flagged red."))
panels.append(outlier_table(
    f"Sustained high CPU (≥ {VM_CPU_PCT}% avg over {WINDOW})", VMCPU,
    [reduce_rows(["mean", "max", "lastNotNull"]), filter_rows(flt("Mean", "greaterOrEqual", VM_CPU_PCT)),
     *keep_columns({"hostname": "Host", "guest_name": "VM", "Mean": f"Avg {WINDOW}", "Max": "Peak",
                    "Last *": "Now"})],
    {"x": 12, "y": y, "w": 12, "h": h}, WINDOW,
    [pct_gauge_col(f"Avg {WINDOW}"), override("Peak", unit="percent"), override("Now", unit="percent")],
    "✓ No VMs running hot", sort_by=[{"displayName": f"Avg {WINDOW}", "desc": True}],
    desc="CPU as a percentage of the VM's own vCPUs. A VM pinned here may need more vCPUs or has a runaway process."))
y += h
h = 7
panels.append(outlier_table(
    f"Memory pressure (≥ {VM_MEM_PCT}% of assigned)", VMMEM,
    [reduce_rows(["lastNotNull", "max"]), filter_rows(flt("Last *", "greaterOrEqual", VM_MEM_PCT)),
     *keep_columns({"hostname": "Host", "guest_name": "VM", "Last *": "Now", "Max": "Peak"})],
    {"x": 0, "y": y, "w": 8, "h": h}, WINDOW,
    [pct_gauge_col("Now"), override("Peak", unit="percent")],
    "✓ No memory pressure", sort_by=[{"displayName": "Now", "desc": True}],
    desc="qemu process RSS on the host as a percentage of the VM's assigned memory."))
panels.append(outlier_table(
    f"Packet drops (last {WINDOW})", DROPS,
    [reduce_rows(["max", "lastNotNull"], labels=False), filter_rows(flt("Max", "greater", 0)),
     *keep_columns({"Field": "VM · host · direction", "Max": "Peak", "Last *": "Now"})],
    {"x": 8, "y": y, "w": 8, "h": h}, WINDOW,
    [override("Peak", unit="pps", decimals=1), override("Now", unit="pps", decimals=1)],
    "✓ No drops", sort_by=[{"displayName": "Peak", "desc": True}],
    desc="virtio/tap interfaces only; SR-IOV VFs are not visible to libvirt."))
panels.append(outlier_table(
    f"Hosts under pressure (≥ {HOST_PCT}%)", HOSTP,
    [reduce_rows(["lastNotNull", "max"], labels=False), filter_rows(flt("Last *", "greaterOrEqual", HOST_PCT)),
     *keep_columns({"Field": "Host · resource", "Last *": "Now", "Max": "Peak"})],
    {"x": 16, "y": y, "w": 8, "h": h}, WINDOW,
    [pct_gauge_col("Now"), override("Peak", unit="percent")],
    "✓ All hosts healthy", sort_by=[{"displayName": "Now", "desc": True}]))
y += h

# ------------------------------------------------------- top consumers -----
row(f"Top consumers (fleet-wide, last {WINDOW})")
h = 9
panels += [
    top_bars("CPU (avg % of vCPUs)", targets(("ops.vm.cpu.util", "$guest_name · $hostname")), "mean", "percent",
             {"x": 0, "y": y, "w": 6, "h": h}, max_=100),
    top_bars("Memory (% of assigned)", targets(("ops.vm.mem.util", "$guest_name · $hostname")), "lastNotNull",
             "percent", {"x": 6, "y": y, "w": 6, "h": h}, max_=100),
    top_bars("Disk throughput (avg)", targets(("libvirt.domstats.block.all.rd.bytes", "read · $guest_name · $hostname"),
                                             ("libvirt.domstats.block.all.wr.bytes", "write · $guest_name · $hostname")),
             "mean", "Bps", {"x": 12, "y": y, "w": 6, "h": h}),
    top_bars("Network throughput (avg)", targets(("libvirt.domstats.net.all.rx.bytes", "rx · $guest_name · $hostname"),
                                                ("libvirt.domstats.net.all.tx.bytes", "tx · $guest_name · $hostname")),
             "mean", "Bps", {"x": 18, "y": y, "w": 6, "h": h}, desc="virtio/tap interfaces only."),
]
y += h

# ------------------------------------------------------- trends ------------
row("Trends")
h = 8
panels.append({
    "id": nid(), "type": "state-timeline", "title": "VM state", "datasource": DS,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": h},
    "description": "State of every defined VM over time. Green running; yellow paused/suspended; orange "
                   "blocked/shutting down; red shut off; dark red crashed; purple unknown. Gaps mean no data "
                   "from that host.",
    "targets": STATE,
    "transformations": [{"id": "renameByRegex", "options": {
        "regex": "^host:(\\S+) \\d+ autostart:\\S+ guest_name:(\\S+)$", "renamePattern": "$2 · $1"}}],
    "options": {"showValue": "never", "mergeValues": True, "alignValue": "left", "rowHeight": 0.8,
                "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                "tooltip": {"mode": "single"}},
    # Colour comes from STATE_THRESH: value-mapping colours aren't applied to PCP Redis series in
    # state-timeline (Grafana 10.2), thresholds are. Mappings are kept for the hover text.
    "fieldConfig": {"defaults": {"color": {"mode": "thresholds"}, "mappings": STATE_MAP,
                                 "thresholds": STATE_THRESH,
                                 "custom": {"fillOpacity": 80, "lineWidth": 0}},
                    "overrides": []},
})
y += h
panels += [
    timeseries("Host CPU", targets(("ops.host.cpu.util", "$hostname")), {"x": 0, "y": y, "w": 8, "h": h}, "percent"),
    timeseries("Host memory used", targets(("ops.host.mem.util", "$hostname")), {"x": 8, "y": y, "w": 8, "h": h},
               "percent"),
    timeseries("Running VMs per host", targets(("ops.host.vms.running", "$hostname")),
               {"x": 16, "y": y, "w": 8, "h": h}, "none"),
]

dashboard = {
    "uid": "fleet-health",
    "title": "Fleet Health",
    "description": "Fleet-wide view of all hypervisors and VMs, surfacing VMs that are down or outliers.",
    "tags": ["pcp", "libvirt", "ops"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "schemaVersion": 38,
    "version": 1,
    "templating": {"list": [
        {"name": "datasource", "label": "Datasource", "type": "datasource", "query": "pcp-redis-datasource",
         "current": {"text": "PCP Redis", "value": "PCP Redis"}, "hide": 2, "refresh": 1, "regex": "", "options": []},
    ]},
    "annotations": {"list": []},
    "links": [{"title": "Hypervisor & VM Overview", "type": "link", "icon": "dashboard",
               "url": "/d/hv-vm-overview/hypervisor-and-vm-overview"},
              {"title": "PCP dashboards", "type": "dashboards", "tags": ["pcp"], "asDropdown": True}],
    "panels": panels,
}

json.dump(dashboard, sys.stdout, indent=2)
print()
