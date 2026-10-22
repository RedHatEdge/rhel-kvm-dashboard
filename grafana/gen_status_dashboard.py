#!/usr/bin/env python3
"""Generate the 'Ops Status' Grafana dashboard: an at-a-glance board for managers / wall screens.

python3 grafana/gen_status_dashboard.py > grafana/dashboards/ops-status.json

Layout follows a homelab-style board: filled stat tiles with sparklines, a site map
and a close-up map, per-hypervisor uptime / UP tiles, gradient area charts and a donut.

- The overall status and the issue lists use Grafana server-side expressions
  (Reduce + Math per rule), because each rule has its own threshold.
- The maps plot openmetrics.pcp_hosts.status (collector-side script), whose
  instance names carry host / site / lat / lon from the collector's sites.json;
  Grafana splits them with the key-value extractor.
- Value mappings live on fields created by transformations or expressions:
  PCP's own field config overrides panel defaults on raw PCP fields.

Colour: status colours (good / warning / critical) only where they mean status,
always with an icon and words; neutral tiles use categorical blue and violet;
one fixed categorical colour per hypervisor in the trends.
"""
import json
import sys

from dashlib import CATEGORICAL, DS, HOST_COLORS, KNOWN_HOSTS, SITES, hypervisor_access, nid

EX = {"type": "__expr__", "uid": "__expr__"}
DASH_DS = {"type": "datasource", "uid": "-- Dashboard --"}

# status palette (fixed) and chart colours, validated on Grafana's dark surface
GOOD, WARN, CRIT, MUTED = "#0ca30c", "#fab219", "#d03b3b", "#898781"
BLUE, VIOLET = "#2a78d6", "#6250d6"     # neutral tile fills (categorical blue / violet)

WINDOW = "15m"
VM_CPU_PCT, VM_MEM_PCT, HOST_PCT = 85, 90, 80
# close-up map: the first site in the inventory
CLOSEUP_SITE = next(iter(SITES.values()))
CLOSEUP = (CLOSEUP_SITE["lat"], CLOSEUP_SITE["lon"])
CLOSEUP_HOSTS = [h for h, v in SITES.items() if v["site"] == CLOSEUP_SITE["site"]]
ESRI = "https://services.arcgisonline.com/arcgis/rest/services/Canvas/World_Dark_Gray_{layer}/MapServer/tile/{{z}}/{{y}}/{{x}}"
ESRI_ATTRIBUTION = "Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap contributors"

# Rules: (refId letter, metric, reducer, threshold expression on $R, severity, issue text)
# severity 2 = critical (service impacted), 1 = warning (attention)
RULES = [
    ("A", "openmetrics.pcp_hosts.hosts_down", "last", "$R > 0", 2, "✕  Hypervisor offline (count)"),
    ("B", "openmetrics.libvirt_vms.vms_down_autostart", "last", "$R > 0", 2, "✕  VMs down that should be running (count)"),
    ("C", "ops.vm.cpu.util", "mean", f"$R >= {VM_CPU_PCT}", 1, f"⚠  VM CPU high ({WINDOW} avg, %)"),
    ("D", "ops.vm.mem.util", "last", f"$R >= {VM_MEM_PCT}", 1, "⚠  VM memory high (% of assigned)"),
    ("E", "libvirt.domstats.net.all.rx.drop", "max", "$R > 0", 1, "⚠  VM dropping packets in (pkts/s)"),
    ("F", "libvirt.domstats.net.all.tx.drop", "max", "$R > 0", 1, "⚠  VM dropping packets out (pkts/s)"),
    ("G", "ops.host.cpu.util", "last", f"$R >= {HOST_PCT}", 1, "⚠  Hypervisor CPU high (%)"),
    ("H", "ops.host.mem.util", "last", f"$R >= {HOST_PCT}", 1, "⚠  Hypervisor memory high (%)"),
]
CRIT_LETTERS = [r[0] for r in RULES if r[4] == 2]
WARN_LETTERS = [r[0] for r in RULES if r[4] == 1]
ISSUES_ID = 900   # fixed id: other panels read its results through the "-- Dashboard --" datasource

HOST_STATUS_MAP = [{"type": "value", "options": {
    "0": {"text": "✓  Healthy", "color": GOOD, "index": 0},
    "2": {"text": "✕  VMs down", "color": CRIT, "index": 1},
    "3": {"text": "✕  Offline", "color": CRIT, "index": 2}}}]


# ---------------------------------------------------------------- helpers ---
def q(expr, legend="", ref="A", hide=False):
    return {"refId": ref, "datasource": DS, "expr": expr, "format": "time_series", "legendFormat": legend,
            "hide": hide}


def rule_queries(output):
    """Per rule: hidden PCP query (<L>q), hidden reduce (<L>r), visible math output (<L>).

    output="severity": rule severity when breached, else 0 (hero).
    output="value":    the reduced value when breached, else 0 (issue lists / tiles).
    """
    qs = []
    for letter, metric, reducer, cond, sev, _ in RULES:
        cond = cond.replace("$R", f"${letter}r")
        math = f"({cond}) * {sev}" if output == "severity" else f"({cond}) * ${letter}r"
        qs += [q(metric, ref=f"{letter}q", hide=True),
               {"refId": f"{letter}r", "datasource": EX, "type": "reduce", "expression": f"{letter}q",
                "reducer": reducer, "settings": {"mode": "dropNN"}, "hide": True},
               {"refId": letter, "datasource": EX, "type": "math", "expression": math}]
    return qs


def reduce_rows(reducers=("lastNotNull",), labels=False):
    return {"id": "reduce", "options": {"reducers": list(reducers), "mode": "seriesToRows",
                                        "includeTimeField": False, "labelsToFields": labels}}


def pick_rules(letters):
    """After Reduce(labelsToFields), keep breached rows of the given rules (Field = rule letter)."""
    return {"id": "filterByValue", "options": {"type": "include", "match": "all", "filters": [
        {"fieldName": "Field", "config": {"id": "regex", "options": {"value": "^(" + "|".join(letters) + ")$"}}},
        {"fieldName": "Last *", "config": {"id": "greater", "options": {"value": 0}}}]}}


def count_mappings(ok_text, bad_fmt, color, upto=50):
    m = {str(n): {"text": bad_fmt.format(n=n), "color": color, "index": n} for n in range(1, upto + 1)}
    m["0"] = {"text": ok_text, "color": GOOD, "index": 0}
    return [{"type": "value", "options": m},
            {"type": "range", "options": {"from": upto + 1, "to": None,
                                          "result": {"text": bad_fmt.format(n=f"{upto}+"), "color": color,
                                                     "index": upto + 1}}}]


def thresholds(*steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}


def tile(title, gridPos, targets, transformations=(), calc="lastNotNull", fields="", color=BLUE, mappings=None,
         thresh=None, unit="none", sparkline=False, value_size=36, no_value=None, desc="", datasource=DS,
         time_from=None, values=False, text_mode="value", orientation="auto", title_size=13, decimals=0,
         wide=True):
    """A filled stat tile (colorMode background)."""
    p = {"id": nid(), "type": "stat", "title": title, "description": desc, "datasource": datasource,
         "gridPos": gridPos, "targets": targets, "transformations": list(transformations),
         "options": {"reduceOptions": {"calcs": [calc] if not values else [], "fields": fields, "values": values},
                     "colorMode": "background", "graphMode": "area" if sparkline else "none",
                     "justifyMode": "center", "textMode": text_mode, "orientation": orientation,
                     "text": {"valueSize": value_size, "titleSize": title_size}, "wideLayout": wide},
         "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                                      "color": {"mode": "thresholds"},
                                      "thresholds": thresh or thresholds((None, color)),
                                      "mappings": mappings or []},
                         "overrides": []}}
    if time_from:
        p["timeFrom"], p["hideTimeOverride"] = time_from, True
    if no_value is not None:
        p["fieldConfig"]["defaults"]["noValue"] = no_value
    return p


panels = []

# ------------------------------------------------- left column: hero + KPIs -
panels.append(tile(
    "Fleet status", {"x": 0, "y": 0, "w": 6, "h": 6}, rule_queries("severity"), [reduce_rows()], calc="max",
    fields="/^Last \\*$/", value_size=34, no_value="No data", thresh=thresholds((None, MUTED)),
    mappings=[{"type": "value", "options": {
        "0": {"text": "✓  All systems normal", "color": GOOD, "index": 0},
        "1": {"text": "⚠  Attention needed", "color": WARN, "index": 1},
        "2": {"text": "✕  Service impacted", "color": CRIT, "index": 2}}}],
    time_from=WINDOW,
    desc="Worst current condition across all hypervisors and VMs. Service impacted: a hypervisor is offline or an "
         "autostart VM is down. Attention: a VM or hypervisor is over a CPU/memory threshold or dropping packets."))

panels.append(tile("Hypervisors online", {"x": 0, "y": 6, "w": 3, "h": 3},
                   [q("openmetrics.pcp_hosts.hosts_up")], sparkline=True, time_from="24h",
                   desc="Hypervisors reachable from the collector."))
panels.append(tile("VMs running", {"x": 3, "y": 6, "w": 3, "h": 3},
                   [q("openmetrics.pcp_hosts.fleet_vms_running")], color=VIOLET,
                   sparkline=True, time_from="24h", desc="Running VMs across all hypervisors."))
panels.append(tile("VMs down", {"x": 0, "y": 9, "w": 3, "h": 3},
                   [q("openmetrics.pcp_hosts.fleet_vms_down")],
                   mappings=count_mappings("✓  0", "✕  {n}", CRIT), thresh=thresholds((None, GOOD), (1, CRIT)),
                   sparkline=True, time_from="24h", desc="Autostart VMs that are not running."))
panels.append(tile("Warnings", {"x": 3, "y": 9, "w": 3, "h": 3},
                   [{"refId": "A", "datasource": DASH_DS, "panelId": ISSUES_ID, "withTransforms": False}],
                   [reduce_rows(labels=True), pick_rules(WARN_LETTERS)], calc="count", fields="/^Last \\*$/",
                   mappings=count_mappings("✓  0", "⚠  {n}", WARN), thresh=thresholds((None, GOOD), (1, WARN)),
                   no_value="✓  0", datasource=DASH_DS,
                   desc="VMs/hypervisors over CPU or memory thresholds, or dropping packets (see Warnings below)."))


# ------------------------------------------------------------------ maps ----
def site_map(title, gridPos, view, size, desc):
    """Sites from openmetrics.pcp_hosts.status, one marker per site coloured by its worst host."""
    marker_style = {
        "color": {"field": "status", "fixed": "dark-green"}, "opacity": 0.95,
        "size": {"fixed": size, "min": 2, "max": 30},
        "symbol": {"fixed": "img/icons/marker/circle.svg", "mode": "fixed"},
        "symbolAlign": {"horizontal": "center", "vertical": "center"},
        # no marker text: the basemap labels the city, and the tooltip names the site and status
        "text": {"fixed": "", "mode": "fixed"},
        "rotation": {"fixed": 0, "max": 360, "min": -360, "mode": "mod"}}
    halo_style = dict(marker_style, opacity=0.25, size={"fixed": size * 2.6, "min": 2, "max": 80},
                      text={"fixed": "", "mode": "fixed"})
    location = {"mode": "coords", "latitude": "lat", "longitude": "lon"}
    return {
        "id": nid(), "type": "geomap", "title": title, "description": desc, "datasource": DS, "gridPos": gridPos,
        "timeFrom": "2m", "hideTimeOverride": True,
        "targets": [q("openmetrics.pcp_hosts.status", "$instance")],
        "transformations": [
            reduce_rows(),
            {"id": "extractFields", "options": {"source": "Field", "format": "kvp", "replace": False}},
            {"id": "convertFieldType", "options": {"fields": {}, "conversions": [
                {"targetField": "lat", "destinationType": "number"},
                {"targetField": "lon", "destinationType": "number"}]}},
            {"id": "groupBy", "options": {"fields": {
                "site": {"aggregations": [], "operation": "groupby"},
                "lat": {"aggregations": ["first"], "operation": "aggregate"},
                "lon": {"aggregations": ["first"], "operation": "aggregate"},
                "Last *": {"aggregations": ["max"], "operation": "aggregate"},
                "host": {"aggregations": ["count"], "operation": "aggregate"}}}},
            {"id": "organize", "options": {"renameByName": {
                "lat (first)": "lat", "lon (first)": "lon", "Last * (max)": "status", "host (count)": "hypervisors"}}}],
        "options": {
            "view": view, "controls": {"showZoom": False, "mouseWheelZoom": False, "showAttribution": True,
                                       "showScale": False, "showMeasure": False, "showDebug": False},
            # Esri Dark Gray Canvas (CARTO's basemap now needs an API key); labels as their own layer
            "basemap": {"type": "xyz", "name": "Basemap", "config": {"url": ESRI.format(layer="Base"),
                                                                     "attribution": ESRI_ATTRIBUTION}},
            "layers": [
                {"type": "xyz", "name": "Labels", "config": {"url": ESRI.format(layer="Reference"),
                                                             "attribution": ESRI_ATTRIBUTION}},
                {"type": "markers", "name": "Halo", "config": {"showLegend": False, "style": halo_style},
                 "location": location, "tooltip": False},
                {"type": "markers", "name": "Sites", "config": {"showLegend": False, "style": marker_style},
                 "location": location, "tooltip": True}],
            "tooltip": {"mode": "details"}},
        "fieldConfig": {"defaults": {"color": {"mode": "thresholds"},
                                     "thresholds": thresholds((None, GOOD), (2, CRIT)), "decimals": 0},
                        "overrides": [{"matcher": {"id": "byName", "options": "status"},
                                       "properties": [{"id": "mappings", "value": HOST_STATUS_MAP}]}]}}


panels.append(site_map("Sites", {"x": 6, "y": 0, "w": 11, "h": 12},
                       {"id": "coords", "lat": 38.5, "lon": -96.5, "zoom": 3.55}, 9,
                       "Hypervisor sites, coloured by the worst hypervisor at each site: green healthy, red when a "
                       "hypervisor is offline or has autostart VMs down. Locations come from sites.json on the collector."))
panels.append(site_map(CLOSEUP_SITE["site"], {"x": 17, "y": 0, "w": 7, "h": 12},
                       {"id": "coords", "lat": CLOSEUP[0], "lon": CLOSEUP[1], "zoom": 10.5}, 16,
                       f"Close-up of the {CLOSEUP_SITE['site']} site ({', '.join(CLOSEUP_HOSTS)})."))

# ---------------------------------------------------- hypervisor tile row ---
HOST_ROWS = [reduce_rows(), {"id": "sortBy", "options": {"sort": [{"field": "Field"}]}}]   # one tile per host, by name
y = 12
panels.append(tile("Hypervisor uptime", {"x": 0, "y": y, "w": 8, "h": 4},
                   [q("kernel.all.uptime", "$hostname")], HOST_ROWS, unit="dtdurations", fields="/^Last \\*$/",
                   values=True,
                   text_mode="value_and_name", value_size=28, title_size=16, time_from="2m", decimals=1,
                   wide=False))
panels.append(hypervisor_access("Hypervisor status  ·  click for Cockpit", {"x": 8, "y": y, "w": 8, "h": 4}))
panels.append(tile("VMs per hypervisor", {"x": 16, "y": y, "w": 8, "h": 4},
                   [q("ops.host.vms.running", "$hostname")], HOST_ROWS, color=VIOLET, fields="/^Last \\*$/",
                   values=True,
                   text_mode="value_and_name", value_size=30, title_size=16, time_from="24h", wide=False))

# ---------------------------------------------------------- charts row ------
y, h = 16, 7
panels.append({
    "id": nid(), "type": "timeseries", "title": "Hypervisor CPU – last 6 hours", "datasource": DS,
    "gridPos": {"x": 0, "y": y, "w": 9, "h": h}, "timeFrom": "6h", "hideTimeOverride": True,
    "targets": [q("ops.host.cpu.util", "$hostname")],
    "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                "tooltip": {"mode": "multi", "sort": "desc"}},
    "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "decimals": 0, "color": {"mode": "palette-classic"},
                                 "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 30,
                                            "gradientMode": "opacity", "showPoints": "never", "spanNulls": 60000,
                                            "axisSoftMin": 0, "axisSoftMax": 25, "lineInterpolation": "smooth",
                                            "axisBorderShow": False}},
                    "overrides": [{"matcher": {"id": "byName", "options": host},
                                   "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                                  for host, c in zip(KNOWN_HOSTS, HOST_COLORS)]}})

STATE_SLICES = [("Running", "openmetrics.libvirt_vms.vms_running", GOOD),
                ("Down (should be running)", "openmetrics.libvirt_vms.vms_down_autostart", CRIT),
                ("Stopped", "openmetrics.libvirt_vms.vms_stopped", MUTED)]
panels.append({
    "id": nid(), "type": "piechart", "title": "VM states", "datasource": DS,
    "gridPos": {"x": 9, "y": y, "w": 6, "h": h}, "timeFrom": "2m", "hideTimeOverride": True,
    "description": "All defined VMs across the fleet. Stopped = not running and not set to autostart.",
    "targets": [q(metric, name, chr(ord("A") + i)) for i, (name, metric, _) in enumerate(STATE_SLICES)],
    # sum each state across hypervisors: rows per (state, host) -> group by state -> one field per state
    "transformations": [reduce_rows(),
                        {"id": "groupBy", "options": {"fields": {
                            "Field": {"aggregations": [], "operation": "groupby"},
                            "Last *": {"aggregations": ["sum"], "operation": "aggregate"}}}},
                        {"id": "rowsToFields", "options": {"mappings": [
                            {"fieldName": "Field", "handlerKey": "field.name"},
                            {"fieldName": "Last * (sum)", "handlerKey": "field.value"}]}}],
    "options": {"pieType": "donut", "displayLabels": ["value"],
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "legend": {"displayMode": "table", "placement": "right", "showLegend": True, "values": ["value"]},
                "tooltip": {"mode": "single"}},
    "fieldConfig": {"defaults": {"decimals": 0, "color": {"mode": "palette-classic"}},
                    "overrides": [{"matcher": {"id": "byName", "options": name},
                                   "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                                  for name, _, c in STATE_SLICES]}})

panels.append({
    "id": nid(), "type": "bargauge", "title": f"Busiest VMs (CPU, {WINDOW} avg)", "datasource": DS,
    "gridPos": {"x": 15, "y": y, "w": 9, "h": h}, "timeFrom": WINDOW, "hideTimeOverride": True,
    "description": f"Top VMs fleet-wide by CPU as a % of their own vCPUs. Amber from {VM_CPU_PCT}%.",
    "targets": [q("ops.vm.cpu.util", "$guest_name · $hostname")],
    "transformations": [reduce_rows(("mean",)),
                        {"id": "sortBy", "options": {"sort": [{"field": "Mean", "desc": True}]}},
                        {"id": "limit", "options": {"limitField": 6}}],
    "options": {"reduceOptions": {"calcs": [], "fields": "/^Mean$/", "values": True},
                "orientation": "horizontal", "displayMode": "gradient", "valueMode": "color",
                "showUnfilled": True, "namePlacement": "left", "sizing": "auto"},
    "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100, "decimals": 0,
                                 "color": {"mode": "thresholds"},
                                 "thresholds": thresholds((None, BLUE), (VM_CPU_PCT, WARN))}, "overrides": []}})

# ---------------------------------------------------------- issues row ------
# Stat lists rather than tables: a stat shows its "no value" text ("✓ None") when
# nothing is left after filtering, a table just says "No data". A stat row's name is
# its string fields joined, using their mapped text: rule letter -> issue text,
# then "· <vm>", "· <host>".
y, h = 23, 6
NAME_MAPS = [
    {"matcher": {"id": "byName", "options": "Field"}, "properties": [{"id": "mappings", "value": [
        {"type": "value", "options": {letter: {"text": text, "index": i}
                                      for i, (letter, _, _, _, _, text) in enumerate(RULES)}}]}]},
    # host-level rows have no guest_name: show nothing (a zero-width space, so the panel's
    # "no value" text isn't used for the empty cell)
    *[{"matcher": {"id": "byName", "options": label}, "properties": [
        {"id": "mappings", "value": [
            {"type": "regex", "options": {"pattern": "(.+)", "result": {"text": "·  $1", "index": 0}}}]},
        {"id": "noValue", "value": "\u200b"}]}
      for label in ("guest_name", "hostname")],
]


def issue_list(title, panel_id, letters, color, x, desc, own_queries=False):
    keep = {"id": "filterFieldsByName", "options": {"include": {"names": ["Field", "guest_name", "hostname", "Last *"]}}}
    p = {"id": panel_id, "type": "stat", "title": title, "description": desc,
         "gridPos": {"x": x, "y": y, "w": 12, "h": h},
         "transformations": [reduce_rows(labels=True), pick_rules(letters), keep,
                             {"id": "organize", "options": {"indexByName": {"Field": 0, "guest_name": 1, "hostname": 2}}}],
         "options": {"reduceOptions": {"calcs": [], "fields": "/^Last \\*$/", "values": True},
                     "colorMode": "value", "graphMode": "none", "justifyMode": "auto", "textMode": "value_and_name",
                     "orientation": "horizontal", "text": {"valueSize": 20, "titleSize": 16}, "wideLayout": True},
         # value > 0 is an issue (status colour); "✓ None" (no rows) gets the base colour
         "fieldConfig": {"defaults": {"decimals": 0, "noValue": "✓  None", "color": {"mode": "thresholds"},
                                      "thresholds": thresholds((None, GOOD), (1e-9, color))},
                         "overrides": NAME_MAPS},
         "links": [{"title": "Fleet Health", "url": "/d/fleet-health/fleet-health"}]}
    if own_queries:
        p.update({"datasource": DS, "targets": rule_queries("value"), "timeFrom": WINDOW, "hideTimeOverride": True})
    else:
        p.update({"datasource": DASH_DS,
                  "targets": [{"refId": "A", "datasource": DASH_DS, "panelId": ISSUES_ID, "withTransforms": False}]})
    return p


panels.append(issue_list("Service-impacting issues", ISSUES_ID, CRIT_LETTERS, CRIT, 0,
                         "Hypervisors offline and autostart VMs that are down. The value is the count.",
                         own_queries=True))
panels.append(issue_list("Warnings", nid(), WARN_LETTERS, WARN, 12,
                         f"VMs or hypervisors over threshold (VM CPU ≥ {VM_CPU_PCT}% over {WINDOW}, VM memory ≥ "
                         f"{VM_MEM_PCT}%, hypervisor CPU/memory ≥ {HOST_PCT}%) or dropping packets. The value is the "
                         "measured figure (% or packets/s)."))

# ------------------------------------------------------ throughput row ------
# Fleet tiles: each host's series is resampled (server-side expression) onto a shared
# 2-minute grid, so they can be averaged / summed per timestamp. Without that, hosts
# sampled a few seconds apart never line up and a per-row sum only sees one host.
AQUA, MAGENTA = CATEGORICAL[2], CATEGORICAL[4]   # categorical slots 3 and 5 (dark steps)


def fleet_tile(title, metric, agg, unit, color, x, desc, thresh=None):
    return tile(title, {"x": x, "y": 29, "w": 6, "h": 5},
                [q(metric, ref="A", hide=True),
                 {"refId": "B", "datasource": EX, "type": "resample", "expression": "A", "window": "2m",
                  "downsampler": "mean", "upsampler": "pad"}],
                [{"id": "joinByField", "options": {"mode": "outer"}},
                 {"id": "calculateField", "options": {"mode": "reduceRow", "reduce": {"reducer": agg},
                                                      "replaceFields": True, "alias": title}}],
                unit=unit, color=color, thresh=thresh, sparkline=True, time_from="6h", value_size=40,
                decimals=None if unit != "percent" else 0, desc=desc)


panels += [
    fleet_tile("Hypervisor CPU (avg)", "ops.host.cpu.util", "mean", "percent", BLUE, 0,
               f"Average non-idle CPU across hypervisors, last 6 hours. Amber from {HOST_PCT}%, red from 90%.",
               thresh=thresholds((None, BLUE), (HOST_PCT, WARN), (90, CRIT))),
    fleet_tile("Hypervisor memory (avg)", "ops.host.mem.util", "mean", "percent", AQUA, 6,
               f"Average memory in use across hypervisors (excluding reclaimable cache). Amber from {HOST_PCT}%, red from 90%.",
               thresh=thresholds((None, AQUA), (HOST_PCT, WARN), (90, CRIT))),
    fleet_tile("Network in", "network.all.in.bytes", "sum", "Bps", MAGENTA, 12,
               "Total received across hypervisors (physical NICs and the libvirt NAT bridge; SR-IOV VF traffic "
               "bypasses the host and isn't included)."),
    fleet_tile("Network out", "network.all.out.bytes", "sum", "Bps", VIOLET, 18,
               "Total sent across hypervisors (physical NICs and the libvirt NAT bridge; SR-IOV VF traffic "
               "bypasses the host and isn't included)."),
]

y, h = 34, 8
panels.append({
    "id": nid(), "type": "timeseries", "title": "Hypervisor network – last 6 hours", "datasource": DS,
    "gridPos": {"x": 0, "y": y, "w": 12, "h": h}, "timeFrom": "6h", "hideTimeOverride": True,
    "description": "Received above the line, sent below, per hypervisor.",
    "targets": [q("network.all.in.bytes", "$hostname in", "A"), q("network.all.out.bytes", "$hostname out", "B")],
    "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                "tooltip": {"mode": "multi", "sort": "none"}},
    "fieldConfig": {"defaults": {"unit": "Bps", "decimals": 1, "color": {"mode": "palette-classic"},
                                 "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 30,
                                            "gradientMode": "opacity", "showPoints": "never", "spanNulls": 60000,
                                            "lineInterpolation": "smooth", "axisBorderShow": False,
                                            "axisCenteredZero": True}},
                    "overrides": [
                        *[{"matcher": {"id": "byRegexp", "options": f"/^{host} /"},
                           "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                          for host, c in zip(KNOWN_HOSTS, HOST_COLORS)],
                        {"matcher": {"id": "byRegexp", "options": "/ out$/"},
                         "properties": [{"id": "custom.transform", "value": "negative-Y"},
                                        {"id": "custom.fillOpacity", "value": 15}]}]}})
panels.append({
    "id": nid(), "type": "bargauge", "title": f"Top VM talkers (network, {WINDOW} avg)", "datasource": DS,
    "gridPos": {"x": 12, "y": y, "w": 12, "h": h}, "timeFrom": WINDOW, "hideTimeOverride": True,
    "description": "VMs moving the most traffic (virtio/tap interfaces), received and sent shown separately.",
    "targets": [q("libvirt.domstats.net.all.rx.bytes", "in · $guest_name · $hostname", "A"),
                q("libvirt.domstats.net.all.tx.bytes", "out · $guest_name · $hostname", "B")],
    "transformations": [reduce_rows(("mean",)),
                        {"id": "sortBy", "options": {"sort": [{"field": "Mean", "desc": True}]}},
                        {"id": "limit", "options": {"limitField": 6}}],
    "options": {"reduceOptions": {"calcs": [], "fields": "/^Mean$/", "values": True},
                "orientation": "horizontal", "displayMode": "gradient", "valueMode": "color",
                "showUnfilled": True, "namePlacement": "left", "sizing": "auto"},
    "fieldConfig": {"defaults": {"unit": "Bps", "min": 0, "decimals": 1,
                                 "color": {"mode": "continuous-BlPu"}}, "overrides": []}})

dashboard = {
    "uid": "ops-status",
    "title": "Ops Status",
    "description": "At-a-glance status of the hypervisor fleet, for managers and wall screens.",
    "tags": ["pcp", "ops", "status"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "30s",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {"hidden": True},
    "schemaVersion": 38,
    "version": 1,
    "templating": {"list": [
        {"name": "datasource", "label": "Datasource", "type": "datasource", "query": "pcp-redis-datasource",
         "current": {"text": "PCP Redis", "value": "PCP Redis"}, "hide": 2, "refresh": 1, "regex": "", "options": []},
    ]},
    "annotations": {"list": []},
    "links": [{"title": "Fleet Health", "type": "link", "icon": "dashboard", "url": "/d/fleet-health/fleet-health"},
              {"title": "Hypervisor & VM Overview", "type": "link", "icon": "dashboard",
               "url": "/d/hv-vm-overview/hypervisor-and-vm-overview"}],
    "panels": panels,
}

json.dump(dashboard, sys.stdout, indent=2, ensure_ascii=False)
print()
