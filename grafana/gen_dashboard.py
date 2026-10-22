#!/usr/bin/env python3
"""Generate the 'Hypervisor & VM Overview' Grafana dashboard.

python3 grafana/gen_dashboard.py > grafana/dashboards/hypervisor-vm-overview.json
"""
import json
import sys

from dashlib import (DS, PCT_THRESH, gauge, hypervisor_access, matrix_table, neg_y, nid, override, pct_gauge_col,
                     stat, targets,
                     timeseries)

HOST = '{hostname == "$host"}'


panels = []

# ---------------------------------------------------------------- Fleet ----
panels.append({"id": nid(), "type": "row", "title": "Fleet overview", "collapsed": False,
               "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}, "panels": []})
panels.append(matrix_table(
    "Hypervisors", "Host", "hostname",
    targets(("ops.host.cpu.util", ""), ("ops.host.mem.util", ""), ("ops.host.vms.running", ""),
            ("ops.host.vms.stopped", ""), ("kernel.all.load", ""), ("hinv.ncpu", ""), ("mem.physmem", ""),
            ("kernel.all.uptime", "")),
    {"ops.host.cpu.util": "CPU %", "ops.host.mem.util": "Memory %", "ops.host.vms.running": "Running VMs",
     "ops.host.vms.stopped": "Stopped VMs", "kernel.all.load[1 minute]": "Load 1m",
     "kernel.all.load[15 minute]": "Load 15m", "hinv.ncpu": "CPUs", "mem.physmem": "Memory",
     "kernel.all.uptime": "Uptime"},
    [pct_gauge_col("CPU %"), pct_gauge_col("Memory %"),
     override("Running VMs", decimals=0), override("Stopped VMs", decimals=0), override("CPUs", decimals=0),
     override("Load 1m", decimals=2), override("Load 15m", decimals=2),
     override("Memory", unit="kbytes"), override("Uptime", unit="dtdurations")],
    {"x": 0, "y": 1, "w": 17, "h": 5},
    exclude=("kernel.all.load[5 minute]",),
    sort_by=[{"displayName": "Host", "desc": False}],
    links=[{"title": "Drill into ${__value.text}", "url": "/d/hv-vm-overview/hypervisor-and-vm-overview?var-host=${__value.raw}&${__url_time_range}"}],
    desc="Latest values for every hypervisor reporting into the collector. Click a host name to focus the dashboard on it."))

panels.append(hypervisor_access("Cockpit", {"x": 17, "y": 1, "w": 7, "h": 5}, filled=False, value_size=22,
                                name_size=14))

# ------------------------------------------------------- Per-host (repeat) --
y = 7
panels.append({"id": nid(), "type": "row", "title": "Host: $host", "collapsed": False, "repeat": "host",
               "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
y += 1
h = 4
panels += [
    gauge("CPU", targets(("ops.host.cpu.util" + HOST, "CPU")), {"x": 0, "y": y, "w": 3, "h": h},
          desc="Non-idle time across all host CPUs."),
    gauge("Memory", targets(("ops.host.mem.util" + HOST, "Memory")), {"x": 3, "y": y, "w": 3, "h": h},
          desc="Physical memory in use, excluding reclaimable cache."),
    stat("Running VMs", targets(("ops.host.vms.running" + HOST, "Running")), {"x": 6, "y": y, "w": 3, "h": h},
         decimals=0, color_mode="value",
         thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None}]}),
    stat("Stopped VMs", targets(("ops.host.vms.stopped" + HOST, "Stopped")), {"x": 9, "y": y, "w": 3, "h": h},
         decimals=0,
         thresholds={"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": "#EAB839", "value": 1}]},
         desc="Defined but not running."),
    stat("vCPUs allocated", targets(("libvirt.dominfo.vcpu.current" + HOST, "vCPUs")), {"x": 12, "y": y, "w": 3, "h": h},
         decimals=0, sum_series=True, desc="Sum of vCPUs across running VMs."),
    stat("Host CPUs", targets(("hinv.ncpu" + HOST, "CPUs")), {"x": 15, "y": y, "w": 3, "h": h}, decimals=0),
    stat("VM memory allocated", targets(("libvirt.dominfo.memory.current" + HOST, "Memory")),
         {"x": 18, "y": y, "w": 3, "h": h}, unit="kbytes", sum_series=True, desc="Sum of memory assigned to running VMs."),
    stat("Uptime", targets(("kernel.all.uptime" + HOST, "Uptime")), {"x": 21, "y": y, "w": 3, "h": h}, unit="dtdurations"),
]
y += h
h = 7
panels += [
    timeseries("Host CPU", targets(("kernel.cpu.util.user" + HOST, "user"), ("kernel.cpu.util.sys" + HOST, "sys"),
                                   ("kernel.cpu.util.wait" + HOST, "iowait"), ("kernel.cpu.util.steal" + HOST, "steal")),
               {"x": 0, "y": y, "w": 6, "h": h}, "percent", stack=True,
               desc="User time includes time spent running guest vCPUs."),
    timeseries("Host memory used", targets(("ops.host.mem.util" + HOST, "used")),
               {"x": 6, "y": y, "w": 6, "h": h}, "percent", max_=100),
    timeseries("Host network (physical)", targets(("network.interface.in.bytes" + HOST, "$instance rx"),
                                                  ("network.interface.out.bytes" + HOST, "$instance tx")),
               {"x": 12, "y": y, "w": 6, "h": h}, "Bps", overrides=[neg_y("/ tx$/")],
               transformations=[{"id": "filterFieldsByName",
                                 "options": {"include": {"pattern": "^([Tt]ime|(en|eth|bond)[^. ]* (rx|tx))$"}}}],
               desc="Physical NICs and bonds (VLAN sub-interfaces, bridges and vnets hidden). Transmit is drawn below zero. "
                    "SR-IOV VF traffic bypasses the host stack and is NOT included."),
    timeseries("Host disk", targets(("disk.all.read_bytes" + HOST, "read"), ("disk.all.write_bytes" + HOST, "write")),
               {"x": 18, "y": y, "w": 6, "h": h}, "KBs", overrides=[neg_y("/^write$/")],
               desc="Write is drawn below zero."),
]
y += h
h = 8
# Live table from the host's own pmproxy (PCP Vector, uid "vector-<hostname>"): metrics_table gives one row per
# instance (VM) and one column per query, which a pivot of PCP Redis series can't do cleanly.
VEC = {"type": "pcp-vector-datasource", "uid": "vector-$host"}
vm_cols = [("libvirt.dominfo.name", "VM"), ("ops.vm.cpu.util", "CPU %"), ("ops.vm.mem.util", "Memory %"),
           ("libvirt.dominfo.vcpu.current", "vCPUs"), ("libvirt.dominfo.memory.current", "Memory"),
           ("libvirt.domstats.block.all.rd.bytes", "Disk read"), ("libvirt.domstats.block.all.wr.bytes", "Disk write"),
           ("libvirt.domstats.net.all.rx.bytes", "Net rx"), ("libvirt.domstats.net.all.tx.bytes", "Net tx"),
           ("libvirt.domstats.net.all.rx.drop", "Net drops rx"), ("libvirt.domstats.net.all.tx.drop", "Net drops tx")]
panels.append({
    "id": nid(), "type": "table", "title": "Virtual machines on $host (live)", "datasource": VEC,
    "gridPos": {"x": 0, "y": y, "w": 24, "h": h},
    "description": "Running VMs on this host, live from its pmproxy. CPU % is relative to the VM's own vCPUs; "
                   "Memory % is the qemu process RSS on the host relative to the VM's assigned memory. Network covers "
                   "virtio/tap interfaces only (SR-IOV VFs are not visible to libvirt).",
    "targets": [{"refId": chr(ord("A") + i), "datasource": VEC, "expr": m, "format": "metrics_table",
                 "legendFormat": name} for i, (m, name) in enumerate(vm_cols)],
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False},
                "sortBy": [{"displayName": "CPU %", "desc": True}]},
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
                                 "decimals": 1, "thresholds": PCT_THRESH, "noValue": "-"},
                    "overrides": [
                        override("VM", custom__width=200),
                        pct_gauge_col("CPU %"), pct_gauge_col("Memory %"), override("vCPUs", decimals=0),
                        override("Memory", unit="kbytes"),
                        *[override(n, unit="Bps") for n in ("Disk read", "Disk write", "Net rx", "Net tx")],
                        *[override(n, unit="pps", decimals=0, custom__cellOptions={"type": "color-text"},
                                   thresholds={"mode": "absolute", "steps": [{"color": "text", "value": None},
                                                                             {"color": "red", "value": 1}]})
                          for n in ("Net drops rx", "Net drops tx")]]},
    "transformations": [{"id": "organize", "options": {"excludeByName": {"instance": True}, "indexByName": {},
                                                       "renameByName": {}}}],
})
y += h
h = 8
panels += [
    timeseries("VM CPU", targets(("ops.vm.cpu.util" + HOST, "$guest_name")),
               {"x": 0, "y": y, "w": 8, "h": h}, "percent", desc="Percent of each VM's allocated vCPUs."),
    timeseries("VM disk throughput", targets(("libvirt.domstats.block.all.rd.bytes" + HOST, "$guest_name read"),
                                             ("libvirt.domstats.block.all.wr.bytes" + HOST, "$guest_name write")),
               {"x": 8, "y": y, "w": 8, "h": h}, "Bps", overrides=[neg_y("/ write$/")], desc="Write is drawn below zero."),
    timeseries("VM network throughput", targets(("libvirt.domstats.net.all.rx.bytes" + HOST, "$guest_name rx"),
                                                ("libvirt.domstats.net.all.tx.bytes" + HOST, "$guest_name tx")),
               {"x": 16, "y": y, "w": 8, "h": h}, "Bps", overrides=[neg_y("/ tx$/")],
               desc="Transmit is drawn below zero. virtio/tap interfaces only."),
]

dashboard = {
    "uid": "hv-vm-overview",
    "title": "Hypervisor & VM Overview",
    "description": "Ops overview of KVM hypervisors and their VMs (PCP + pmdalibvirt, historical via PCP Redis/Valkey).",
    "tags": ["pcp", "libvirt", "ops"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "10s",
    "time": {"from": "now-1h", "to": "now"},
    "schemaVersion": 38,
    "version": 1,
    "templating": {"list": [
        {"name": "datasource", "label": "Datasource", "type": "datasource", "query": "pcp-redis-datasource",
         "current": {"text": "PCP Redis", "value": "PCP Redis"}, "hide": 2, "refresh": 1, "regex": "", "options": []},
        {"name": "host", "label": "Host", "type": "query", "datasource": DS, "query": "label_values(hostname)",
         "definition": "label_values(hostname)", "refresh": 1, "sort": 1, "multi": True, "includeAll": True,
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "options": [], "regex": ""},
    ]},
    "annotations": {"list": []},
    "links": [{"title": "PCP dashboards", "type": "dashboards", "tags": ["pcp"], "asDropdown": True}],
    "panels": panels,
}

json.dump(dashboard, sys.stdout, indent=2)
print()
