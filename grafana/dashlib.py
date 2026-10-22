"""Shared panel builders for the PCP Grafana dashboard generators.

All PCP Redis queries are plain metric selectors: pmproxy 6.3.7 segfaults on
pmseries arithmetic, so percentages come from derived metrics computed by
pmlogger (ops.* in /etc/pcp/derived/ops-overview.conf) and everything else is
done with Grafana transformations.
"""

import json
import os

# ---- site inventory --------------------------------------------------------
# Hostnames, management addresses and map coordinates come from the same file the
# collector's pcp_hosts.python reads. Use your own pcp/openmetrics/sites.json
# (git-ignored); fall back to the shipped example. PCP_SITES=<file> overrides both.
_OM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pcp", "openmetrics")
SITES_FILE = os.environ.get("PCP_SITES") or next(
    p for p in (os.path.join(_OM, "sites.json"), os.path.join(_OM, "sites.example.json")) if os.path.exists(p))
SITES = json.load(open(SITES_FILE))          # {"<host>": {"site", "lat", "lon", "address"}}, in file order
KNOWN_HOSTS = list(SITES)

# categorical palette, dark-surface steps, fixed order (validated); hosts past the
# eighth get Grafana's default palette rather than a generated colour
CATEGORICAL = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
HOST_COLORS = CATEGORICAL[:len(KNOWN_HOSTS)]


DS = {"type": "pcp-redis-datasource", "uid": "${datasource}"}
_id = 0


def nid():
    global _id
    _id += 1
    return _id


def tgt(expr, legend, ref):
    return {"refId": ref, "datasource": DS, "expr": expr, "format": "time_series", "legendFormat": legend}


def targets(*pairs):
    return [tgt(e, l, chr(ord("A") + i)) for i, (e, l) in enumerate(pairs)]


PCT_THRESH = {"mode": "absolute", "steps": [
    {"color": "green", "value": None}, {"color": "#EAB839", "value": 70}, {"color": "red", "value": 90}]}


def override(name, **props):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k.replace("__", "."), "value": v} for k, v in props.items()]}


def matrix_table(title, row_label, row_field, tgts, columns, overrides, gridPos, sort_by=None, links=None, exclude=(), desc=""):
    """Multi-query -> one row per <row_field> label, one column per metric.

    Reduce(labelsToFields) keys the pivot on the raw field name (metric[instance]),
    not the legend, so <columns> maps raw field names to display names, in order.
    """
    rename = {f"{row_field}\\Field": row_label, **columns}
    index = {f"{row_field}\\Field": 0}
    index.update({raw: i + 1 for i, raw in enumerate(columns)})
    p = {
        "id": nid(), "type": "table", "title": title, "description": desc, "datasource": DS,
        "gridPos": gridPos, "targets": tgts, "timeFrom": "5m", "hideTimeOverride": True,
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False},
                    "sortBy": sort_by or []},
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
                                     "decimals": 1, "thresholds": PCT_THRESH, "noValue": "-"},
                        "overrides": overrides},
        "transformations": [
            {"id": "reduce", "options": {"reducers": ["lastNotNull"], "mode": "seriesToRows",
                                         "includeTimeField": False, "labelsToFields": True}},
            {"id": "groupingToMatrix", "options": {"columnField": "Field", "rowField": row_field,
                                                    "valueField": "Last *", "emptyValue": "null"}},
            {"id": "organize", "options": {"excludeByName": {n: True for n in exclude}, "indexByName": index,
                                           "renameByName": rename}},
        ],
    }
    if links:
        p["fieldConfig"]["overrides"].append(override(row_label, links=links))
    return p


def pct_gauge_col(name):
    return override(name, unit="percent", min=0, max=100, decimals=1,
                    custom__cellOptions={"type": "gauge", "mode": "basic", "valueDisplayMode": "text"},
                    custom__width=170)


def stat(title, tgts, gridPos, unit="none", sum_series=False, thresholds=None, decimals=None, desc="", color_mode="value"):
    p = {"id": nid(), "type": "stat", "title": title, "description": desc, "datasource": DS, "gridPos": gridPos,
         "targets": tgts,
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "colorMode": color_mode, "graphMode": "none", "justifyMode": "center", "textMode": "value"},
         "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "thresholds"},
                                      "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "text", "value": None}]}},
                         "overrides": []}}
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    if sum_series:
        p["transformations"] = [
            {"id": "joinByField", "options": {"byField": "Time", "mode": "outer"}},
            {"id": "calculateField", "options": {"mode": "reduceRow", "reduce": {"reducer": "sum"},
                                                 "replaceFields": True, "alias": title}},
        ]
    return p


def gauge(title, tgts, gridPos, desc=""):
    return {"id": nid(), "type": "gauge", "title": title, "description": desc, "datasource": DS, "gridPos": gridPos,
            "targets": tgts,
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "showThresholdMarkers": True, "showThresholdLabels": False},
            "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100, "decimals": 1,
                                         "color": {"mode": "thresholds"}, "thresholds": PCT_THRESH},
                            "overrides": []}}


def timeseries(title, tgts, gridPos, unit, stack=False, max_=None, overrides=None, transformations=None, desc=""):
    custom = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 15 if stack else 8, "showPoints": "never",
              "spanNulls": 30000, "axisSoftMin": 0, "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    p = {"id": nid(), "type": "timeseries", "title": title, "description": desc, "datasource": DS, "gridPos": gridPos,
         "targets": tgts,
         "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                     "tooltip": {"mode": "multi", "sort": "desc"}},
         "fieldConfig": {"defaults": {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}},
                         "overrides": overrides or []}}
    if max_ is not None:
        p["fieldConfig"]["defaults"]["max"] = max_
    if transformations:
        p["transformations"] = transformations
    return p


def neg_y(pattern):
    return {"matcher": {"id": "byRegexp", "options": pattern},
            "properties": [{"id": "custom.transform", "value": "negative-Y"}]}


def hypervisor_access(title, gridPos, filled=True, value_size=30, name_size=16, desc=None):
    """One UP/DOWN tile per hypervisor (from the collector's openmetrics.pcp_hosts.up), each a
    click-through to that hypervisor's Cockpit (https://<address>:9090).

    The instance name is "<id> address:<mgmt address> host:<name>" (openmetrics instance labels
    don't reach pmseries), so the key-value extractor splits it into host / address columns;
    a stat row's name is its string fields joined ("hypervisor-01 192.0.2.10").
    """
    good, crit, muted = "#0ca30c", "#d03b3b", "#898781"
    return {
        "id": nid(), "type": "stat", "title": title, "datasource": DS, "gridPos": gridPos,
        "timeFrom": "2m", "hideTimeOverride": True,
        "description": desc or "Hypervisor PCP agents as seen from the collector. Click a tile to open that "
                               "hypervisor's Cockpit for troubleshooting.",
        "targets": [{"refId": "A", "datasource": DS, "expr": "openmetrics.pcp_hosts.up", "format": "time_series",
                     "legendFormat": "$instance"}],
        "transformations": [
            {"id": "reduce", "options": {"reducers": ["lastNotNull"], "mode": "seriesToRows",
                                         "includeTimeField": False, "labelsToFields": False}},
            {"id": "extractFields", "options": {"source": "Field", "format": "kvp", "replace": False}},
            {"id": "filterFieldsByName", "options": {"include": {"names": ["host", "address", "Last *"]}}},
            {"id": "organize", "options": {"indexByName": {"host": 0, "address": 1, "Last *": 2}}},
            {"id": "sortBy", "options": {"sort": [{"field": "host"}]}}],
        "options": {"reduceOptions": {"calcs": [], "fields": "/^Last \\*$/", "values": True},
                    "colorMode": "background" if filled else "value", "graphMode": "none",
                    "justifyMode": "center", "textMode": "value_and_name", "orientation": "vertical",
                    "text": {"valueSize": value_size, "titleSize": name_size}, "wideLayout": False},
        "fieldConfig": {"defaults": {
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [{"color": muted, "value": None}]},
            "mappings": [{"type": "value", "options": {"1": {"text": "UP", "color": good, "index": 0},
                                                       "0": {"text": "DOWN", "color": crit, "index": 1}}}],
            "links": [{"title": "Open Cockpit on ${__data.fields.host}",
                       "url": "https://${__data.fields.address}:9090/", "targetBlank": True}]},
            "overrides": []}}
