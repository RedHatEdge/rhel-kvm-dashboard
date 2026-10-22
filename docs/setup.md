# Host Performance Monitoring with PCP + Grafana

Based on the Red Hat blog series
[Visualizing system performance with RHEL 8 using PCP and Grafana](https://www.redhat.com/en/blog/visualizing-system-performance-rhel-8-using-performance-co-pilot-pcp-and-grafana-part-1),
adapted for bootc-based (image mode) RHEL 9 KVM hypervisors. Developed and
tested on RHEL 9.8 with PCP 6.3.7, Grafana 10.2.6 and grafana-pcp 5.1.1.

Hostnames, addresses and the registry in this document are examples:
`hypervisor-01` (`192.0.2.10`) is the collector, `hypervisor-02`
(`192.0.2.11`) an agent, and the image is `registry.example.com/rhel-kvm-image`.

Part 1 of the blog only monitors the local host. Since every hypervisor should
report in to one place, this setup also uses the Part 2 approach: a central
`pmlogger` farm and `pmproxy` backed by Valkey for time-series storage.

On top of that, `pmdalibvirt` and a small VM-state collector add per-VM
metrics. Three custom dashboards build on them: **Ops Status** (manager / wall-screen
board with site maps), **Fleet Health** (fleet-wide, brings down or outlying VMs
to the top) and **Hypervisor & VM Overview** (per-host drill-down) (see [VM monitoring and the ops dashboards](#vm-monitoring-and-the-ops-dashboards)).

Two ways to get there are documented:

- **[Approach A](#approach-a-live-package-layering-interim)**: layer the
  packages live with `rpm-ostree` on existing hosts. Quickest; no image
  rebuild.
- **[Approach B](#approach-b-bake-into-the-bootc-image-target-state)**: bake
  the packages into the hypervisor image. This is the target state.

## Topology

| Host | IP | Role | Services |
|---|---|---|---|
| hypervisor-01 | 192.0.2.10 | Collector + visualization | `pmcd` (+ `pmdalibvirt`), `pmlogger` (local + farm), `pmproxy`, `valkey`, `grafana-server` |
| hypervisor-02 | 192.0.2.11 | Agent | `pmcd` (+ `pmdalibvirt`), `pmlogger` (local), `pmproxy` |

```
 hypervisor-02 (agent)                  hypervisor-01 (collector)
 ┌──────────────┐   tcp/44321 (pmcd)    ┌─────────────────────────────────────┐
 │ pmcd         │◄──────────────────────┤ pmlogger farm ──► /var/log/pcp/...   │
 │ pmproxy      │◄─── tcp/44322 ───┐    │ pmproxy ──► valkey (localhost:6379) │
 └──────────────┘   (live/Vector)  └────┤ grafana-server (tcp/3000)           │
                                        └─────────────────────────────────────┘
```

- **Historical data:** `pmlogger` on hypervisor-01 records its own `pmcd` and,
  through the farm, hypervisor-02's `pmcd`, both every 10s. `pmproxy` on hypervisor-01
  loads both archive sets into Valkey. Grafana queries them through the
  **PCP Redis** datasource. It works with Valkey despite the name; the plugin
  hasn't been renamed in this version.
- **Live data:** Grafana's **PCP Vector** datasources query `pmproxy` on
  each host directly, on port 44322.

**Grafana:** <http://192.0.2.10:3000>. The default login is `admin` /
`admin`, and Grafana prompts for a new password on first login. Change it.

## Packages

All packages are in the RHEL 9 AppStream repo, which the hosts already
have through their existing RHSM registration.

| Package | Tested version | Needed on | Purpose |
|---|---|---|---|
| `pcp`, `pcp-zeroconf` | 6.3.7 | both | Collection (`pmcd`), logging (`pmlogger`), REST API (`pmproxy`) |
| `pcp-pmda-libvirt` | 6.3.7 | both | Per-VM metrics from libvirt (`libvirt.*`) |
| `pcp-pmda-openmetrics` | 6.3.7 | both | Runs the VM-state script (`openmetrics.libvirt_vms.*`). Pulled in by `pcp-zeroconf`. |
| `grafana` | 10.2.6 | collector | Dashboards |
| `grafana-pcp` | 5.1.1 | collector | PCP app plugin, plus the PCP Redis/Vector/bpftrace datasources |
| `valkey` | 8.0.10 | collector | Time-series store for `pmproxy` / `pmseries` |

Install the same package set on **both** hosts, so a single image works for
either role. The role is decided by which services are enabled and by a few
`/etc` files.

## Ports

| Port | Service | Needed on | Used by |
|---|---|---|---|
| 44321/tcp | `pmcd` | agents | collector's `pmlogger` farm |
| 44322/tcp | `pmproxy` | all hosts | Grafana PCP Vector datasources |
| 3000/tcp | `grafana-server` | collector | browsers |

If firewalld is running on your hypervisors, open them:

```bash
firewall-cmd --permanent --add-service=pmcd --add-service=pmproxy   # all hosts
firewall-cmd --permanent --add-service=grafana                       # collector
firewall-cmd --reload
```

## Image-mode gotchas (read first)

These apply to both approaches, and are why the steps below differ from
the blog post:

1. **Services are not enabled by the packages.** On a package-mode system,
   `pcp-zeroconf`'s scriptlets enable `pmcd`, `pmlogger` and friends. Under
   rpm-ostree and bootc builds they don't, so every service has to be
   enabled explicitly.
2. **`pmcd` listens on localhost only by default.** `/etc/sysconfig/pmcd`
   ships with `PMCD_LOCAL=1`. Agents need `PMCD_LOCAL=0`, or the collector's
   farm can't connect.
3. **`/var/lib/pcp` is fine.** PCP keeps PMDAs, PMNS and config under `/var`,
   which images normally don't populate. The RHEL packages ship
   `tmpfiles.d` rules (`/usr/lib/tmpfiles.d/pcp*.conf`) that create this
   content at boot, so no extra work is needed.
4. **Live layering doesn't load new SELinux policy** (Approach A only).
   `grafana-selinux` and `pcp-selinux` add new types. `rpm-ostree
   --apply-live` rebuilds the policy on disk but doesn't load it, so the
   `systemd-tmpfiles` step fails with exit status 73 (`Failed to set SELinux
   security context ... grafana_var_lib_t`). Fix it with `load_policy`, then
   rerun `systemd-tmpfiles --create`. A reboot fixes it too.
5. **Grafana dashboard provisioning needs a `uid`.** The Host Overview
   dashboards shipped in `grafana-pcp` have no `uid`. Provisioning them
   unmodified works, but Grafana logs `Unique identifier needed` every 10
   seconds. We provision copies with a fixed `uid` added. If a dashboard is
   ever provisioned without a uid and you add one later, Grafana loops on
   `could not resolve dashboards:uid:...`. To fix it, move the file out of
   the provisioning directory for about 20 seconds (Grafana deletes the
   stale copy), then move it back.
6. **pmproxy 6.3.7 segfaults on pmseries arithmetic.** Any query that does
   math between series (`a / b`, `metric / 2`) crashes `pmproxy` with
   SIGSEGV. systemd restarts it, but every Grafana panel errors in the
   meantime. Functions such as `rescale()` and `max_inst()` are fine. **Never
   put arithmetic in PCP Redis queries.** Do the math at collection time
   with derived metrics (`/etc/pcp/derived/ops-overview.conf`), or in
   Grafana.
7. **A failed `--apply-live` blocks every later one** (Approach A only).
   After the tmpfiles failure in #4, rpm-ostree records an
   `InterruptedLiveCommit`, and the next `rpm-ostree install --apply-live`
   fails with `Previously interrupted while targeting commit <X>`. Finish
   the old apply first, then apply the new pending deployment:
   `rpm-ostree apply-live --target <X> && rpm-ostree apply-live`.
8. **pmlogconf reads groups through `/var/lib/pcp/config/pmlogconf`.** That
   tree is per-file symlinks (created by tmpfiles) into `/etc/pcp/pmlogconf`.
   A new group file in `/etc` needs a matching symlink (see
   `pcp/tmpfiles.d/`), and the same goes for derived-metric files
   under `/var/lib/pcp/config/derived`. Run `pmlogconf` non-interactively
   with `-c -q` and stdin from `/dev/null`, otherwise it waits for answers.
9. **`pmdalibvirt`'s labels on `libvirt.hv.*` vary over time**
   (`metric_type` flips between `host` and `info`). Every distinct label set
   becomes a separate pmseries series, so charts grow duplicate lines. Chart
   the `ops.host.vms.*` derived aliases instead; they only carry the host's
   labels.
10. **The OpenMetrics PMDA ships three sample sources** (`grafana.url`,
    `kepler.url`, `vllm.url`), and once the agent is registered the stock
    `openmetrics/summary` pmlogconf group logs **all** of `openmetrics.*`.
    On the collector that pulled Grafana's own `/metrics` (hundreds of
    series) into the archives and Valkey. Setting the group to `n` doesn't
    stick, because `pmlogger_check` re-enables it. So we drop the sample
    sources with a same-named override in `/etc/tmpfiles.d`
    (`pcp/tmpfiles.d/pcp-pmda-openmetrics.conf`). Re-check that
    file against `/usr/lib/tmpfiles.d/pcp-pmda-openmetrics.conf` after PCP
    upgrades.
11. **OpenMetrics instance labels don't reach pmseries.** `pminfo -l` shows
    `guest_name`/`autostart`, but the series in Valkey don't have them. The
    instance *name* does, though (`3 autostart:yes guest_name:<vm>`). The
    Fleet Health dashboard legends these series as `host:$hostname
    $instance`, then splits that string with Grafana's key-value field
    extractor (or a rename regex).
12. **Grafana: PCP series ignore panel-default value mappings.** The PCP
    datasource sends its own field config, which beats panel *defaults*.
    Put mappings in an *override* (works in tables). In the state timeline
    not even that applied, so colours come from threshold steps there.

The PCP Vector **live VM table** needs a couple of polls before rate
columns fill in. After a fresh page load, or a pmproxy restart, expect
`-` values (and no CPU % column) for roughly 30–60 seconds.

---

## Approach A: Live package layering (interim)

Use this to try the setup on existing hosts, or as an interim step before
an image rebuild. It works immediately and needs no image rebuild. The layered packages are tracked by rpm-ostree on each host and
are reapplied automatically when the base image is upgraded. Plain
`bootc upgrade`/`bootc switch` refuses to run while local layers exist.
See [Moving from A to B](#moving-from-approach-a-to-approach-b).

All commands run as root (`sudo`).

### A1. Prerequisite: root access (both hosts)

Every step needs root. If you drive the steps over SSH as an automation user
(here `automation`), give it passwordless sudo:

```bash
echo 'automation ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/automation
sudo chmod 0440 /etc/sudoers.d/automation
sudo visudo -cf /etc/sudoers.d/automation
```

### A2. Layer the packages (both hosts)

```bash
rpm-ostree install --idempotent --apply-live \
  pcp pcp-zeroconf grafana grafana-pcp valkey

# --apply-live doesn't load the new SELinux modules (gotcha #4):
load_policy
systemd-tmpfiles --create   # "already exists and is not a directory" lines for /home, /srv, /root are normal
```

No reboot is needed. `rpm-ostree status` now shows the packages under
`LayeredPackages` and the booted deployment as `Unlocked: transient`.

### A3. Agent: hypervisor-02 (192.0.2.11)

```bash
# Accept remote pmcd connections (gotcha #2)
sed -i 's/^PMCD_LOCAL=1/PMCD_LOCAL=0/' /etc/sysconfig/pmcd

# No valkey on agents: stop pmproxy trying to reach localhost:6379
sed -i '/^\[keys\]/,/^\[/ s/^enabled = true/enabled = false/' /etc/pcp/pmproxy/pmproxy.conf

systemctl enable --now pmcd pmlogger pmlogger_check.timer pmlogger_daily.timer pmproxy
```

### A4. Collector: hypervisor-01 (192.0.2.10)

**PCP and Valkey:**

```bash
# Sanity check: can we reach the agent's pmcd?
pminfo -h 192.0.2.11 -f kernel.all.load

systemctl enable --now valkey
systemctl enable --now pmcd pmlogger pmlogger_check.timer pmlogger_daily.timer pmproxy
```

The stock `/etc/pcp/pmproxy/pmproxy.conf` already points `[keys]` at
`localhost:6379` with `enabled = true`, so the collector needs no pmproxy
changes.

**pmlogger farm.** Add one line per remote host:

```bash
cat > /etc/pcp/pmlogger/control.d/remote <<'EOF'
$version=1.1
# Remote hosts logged by this collector (pmlogger farm).
# Host        P?  S?  directory                       args
192.0.2.11   n   n   PCP_ARCHIVE_DIR/hypervisor-02       -r -T24h10m -t 10s -c config.hypervisor-02 -v 100Mb
EOF

systemctl enable --now pmlogger_farm pmlogger_farm_check.timer
```

- `-t 10s` matches the 10-second interval zeroconf uses for the local host.
  Without it, remote hosts are logged every 60 seconds.
- `config.hypervisor-02` doesn't need to exist ahead of time. `pmlogger_check`
  generates it in `/var/lib/pcp/config/pmlogger/` with `pmlogconf` on first
  run.
- Archives land in `/var/log/pcp/pmlogger/hypervisor-02/`. `pmproxy` discovers
  them automatically and loads them into Valkey.

**Grafana.** Everything is provisioned from files, which replaces the
blog's manual UI steps (enabling the plugin, adding datasources, importing
dashboards):

```bash
# Plugin, dashboard provider, and datasources (edit the datasource example:
# one PCP Vector datasource per hypervisor, uid vector-<hostname>)
install -m 0640 -g grafana grafana/provisioning/plugins/pcp.yaml     /etc/grafana/provisioning/plugins/pcp.yaml
install -m 0640 -g grafana grafana/provisioning/dashboards/pcp.yaml  /etc/grafana/provisioning/dashboards/pcp.yaml
install -m 0640 -g grafana grafana/provisioning/datasources/pcp.yaml.example \
                           /etc/grafana/provisioning/datasources/pcp.yaml
vi /etc/grafana/provisioning/datasources/pcp.yaml

# The PCP Host Overview dashboards, copied with a fixed uid (gotcha #5)
mkdir -p /etc/grafana/dashboards/pcp
for ds in vector redis; do
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["uid"]=sys.argv[2]; json.dump(d,sys.stdout,indent=2)' \
    /usr/share/performancecopilot-pcp-app/datasources/$ds/dashboards/pcp-$ds-host-overview.json \
    pcp-$ds-host-overview > /etc/grafana/dashboards/pcp/pcp-$ds-host-overview.json
done

systemctl enable --now grafana-server
```

The datasource file contains one **PCP Redis** datasource (history for all
hosts, through this collector's pmproxy and Valkey) and one **PCP Vector**
datasource per hypervisor (live data from that host's pmproxy).

Notes:

- The `grafana-pcp` RPM already adds its plugin IDs to
  `allow_loading_unsigned_plugins` in `/etc/grafana/grafana.ini`, so no
  ini changes are needed.
- The names `PCP Vector` and `PCP Redis` matter. The shipped dashboards
  default their datasource dropdown to those names.
- Enabling the app also auto-imports the **PCP Vector Checklist** and
  **PCP Redis: Metric Preview** dashboards. Grafana 10 logs harmless
  `Could not make user admin ... identifier is not initialized` errors when
  it does this at startup.
- To add another agent: run A2, A3 and A5 on it, add a line to
  `control.d/remote`, and add a `PCP Vector <host>` datasource with
  `uid: vector-<hostname>`. Then regenerate its farm config (A5 step 4) and
  run `systemctl restart pmlogger_farm grafana-server`.

### A5. VM monitoring (pmdalibvirt, VM state, dashboards)

Run these from a checkout of this repo on each host (or copy the files
over). First create your site inventory from the example: hostnames as in
the pmlogger archive directories, the management address dashboards should
link to, and map coordinates:

```bash
cp pcp/openmetrics/sites.example.json pcp/openmetrics/sites.json
vi pcp/openmetrics/sites.json
```

**1. Both hosts: layer and register the libvirt PMDA.**

```bash
rpm-ostree install --idempotent --apply-live pcp-pmda-libvirt
# If this fails with "Previously interrupted while targeting commit <X>" (gotcha #7):
#   rpm-ostree apply-live --target <X> && rpm-ostree apply-live
cd /var/lib/pcp/pmdas/libvirt && ./Install </dev/null
```

`Install` adds a `libvirt` line to `/etc/pcp/pmcd/pmcd.conf`, rebuilds the
PMNS and restarts the agent. Expect `Check libvirt metrics have appeared ...
137 metrics`. VM instances are named by domain UUID, but every value carries
a `guest_name` label, which the dashboards use. `pmdalibvirt` only reports
**running** domains; step 3 covers the rest.

**2. Both hosts: derived metrics, pmlogconf group, VM-state script and tmpfiles links.**

```bash
install -m 0644 pcp/derived/ops-overview.conf   /etc/pcp/derived/ops-overview.conf
install -D -m 0644 pcp/pmlogconf/ops/overview   /etc/pcp/pmlogconf/ops/overview
install -m 0755 pcp/openmetrics/libvirt_vms.python /etc/pcp/openmetrics/libvirt_vms.python
install -m 0755 pcp/openmetrics/pcp_hosts.python   /etc/pcp/openmetrics/pcp_hosts.python
install -m 0644 pcp/openmetrics/sites.json         /etc/pcp/openmetrics/sites.json
install -m 0644 pcp/tmpfiles.d/pcp-ops-overview.conf   /etc/tmpfiles.d/
install -m 0644 pcp/tmpfiles.d/pcp-pmda-openmetrics.conf /etc/tmpfiles.d/
rm -f /var/lib/pcp/pmdas/openmetrics/config.d/{grafana,kepler,vllm}.url
systemd-tmpfiles --create pcp-ops-overview.conf pcp-pmda-openmetrics.conf

# pmproxy evaluates derived metrics for live (Vector) queries; reload it
systemctl restart pmproxy
```

- `ops-overview.conf` defines `ops.host.cpu.util`, `ops.host.mem.util`,
  `ops.vm.cpu.util` (percent of the VM's own vCPUs), `ops.vm.mem.util`
  (qemu RSS as a percent of assigned memory), and `ops.host.vms.running` /
  `ops.host.vms.stopped`. `pmlogger` computes these at collection time,
  because pmproxy can't do the math (gotcha #6). The VM counts are plain
  aliases of `libvirt.hv.domains.*`, for a stable series identity
  (gotcha #9).
- The `ops/overview` pmlogconf group logs those, plus the per-VM `.all`
  totals (disk/net bytes, drops) and `openmetrics.libvirt_vms.state`.
- `pcp-pmda-openmetrics.conf` overrides the package's tmpfiles rule so the
  OpenMetrics agent doesn't scrape its sample Grafana/Kepler/vLLM URLs
  (gotcha #10).
- `pcp_hosts.python` and `sites.json` only do anything on the collector (the
  script prints nothing on hosts without pmlogger farm entries); installing
  them everywhere keeps one config set for all hypervisors.

**3. Both hosts: register the OpenMetrics PMDA (VM state for every domain).**

```bash
cd /var/lib/pcp/pmdas/openmetrics && ./Install </dev/null
pminfo -f openmetrics.libvirt_vms.state      # one instance per defined VM
```

`libvirt_vms.python` lists **all** domains, including shut-off ones, through
the read-only libvirt socket. It exports the libvirt state code (1 running,
3 paused, 5 shut off, 6 crashed, ...) with the VM name and autostart flag.
It's Python rather than a `virsh` script because SELinux blocks `pmcd`'s
domain from executing `virsh` (silently: `Permission denied` only shows up in
`/var/log/pcp/pmcd/openmetrics.log`). It also exports per-host totals
(`vms_defined`, `vms_running`, `vms_down_autostart`, `vms_stopped`).

On the collector, `pcp_hosts.python` adds `openmetrics.pcp_hosts.*`:

- reachability of every hypervisor in the pmlogger farm (TCP to `pmcd`), so an
  offline host is a real signal, not just missing data;
- `status` per hypervisor (0 healthy, 2 autostart VMs down, 3 offline) with
  the site/latitude/longitude from `sites.json`, for the Ops Status maps;
- fleet totals (`hosts_up`, `hosts_down`, `fleet_vms_running`,
  `fleet_vms_down`), so the dashboard doesn't have to add up series that are
  sampled at different moments.

To add a hypervisor to the maps, add it to `sites.json`, using the name from
its pmlogger archive directory. Its `address` is the management address that
the dashboards' **Cockpit links** use (`https://<address>:9090/`). It
overrides the pmlogger control address, which is `localhost` for the
collector itself. Sites are free text (the example uses central Houston);
the close-up map on Ops Status shows the first site in the file. The script
opens a PMAPI connection to each reachable host on every fetch: fine for
tens of hosts, not thousands.

**4. Collector: regenerate both logger configs and restart.**

```bash
cd /var/lib/pcp/config/pmlogger
pmlogconf -c -q config.default </dev/null
pmlogconf -c -q -h 192.0.2.11 config.hypervisor-02 </dev/null
grep '^#+ ops/overview:y' config.default config.hypervisor-02     # both must match
chown pcp:pcp config.default config.hypervisor-02
systemctl restart pmlogger pmlogger_farm
```

Don't add `-r` (reprobe) once the setup is established. It re-decides every
group and can switch groups back on that you turned off.

**5. Collector: dashboards.** Use the datasource file from A4, with the
`vector-<hostname>` uids. Build the dashboards for your inventory (host
order, per-host colours and the close-up map come from `sites.json`), then
install them:

```bash
make dashboards          # writes build/*.json
install -m 0644 build/hypervisor-vm-overview.json build/fleet-health.json \
                build/ops-status.json /etc/grafana/dashboards/pcp/
```

The existing `pcp-host-overview` file provider picks them up within about 10
seconds; no restart is needed.

---

## Approach B: Bake into the bootc image (target state)

The goal: one image for every hypervisor, with packages, generic config and
service enablement baked in. Only the collector-specific, site-specific
config is applied at deploy time: the farm host list, the datasource URLs,
and enabling Valkey and Grafana.

### B1. Containerfile additions

Add this to your bootc image's `Containerfile`. Copy this repo (with your
`sites.json`, and dashboards built by `make dashboards`) into the build
context first:

```dockerfile
# =============================================================================
# Performance monitoring (PCP + Grafana)
#     Same packages on every hypervisor; the collector role is enabled post-deploy.
# =============================================================================
RUN dnf install -y \
      pcp pcp-zeroconf pcp-pmda-libvirt \
      grafana grafana-pcp \
      valkey && \
    dnf clean all && rm -rf /var/cache/* /var/lib/{dnf,rhsm}

# pmcd: accept remote connections (the collector's pmlogger farm pulls from it)
# pmproxy: agents have no local valkey; the collector re-enables [keys]
RUN sed -i 's/^PMCD_LOCAL=1/PMCD_LOCAL=0/' /etc/sysconfig/pmcd && \
    sed -i '/^\[keys\]/,/^\[/ s/^enabled = true/enabled = false/' /etc/pcp/pmproxy/pmproxy.conf

# Grafana: enable the PCP app and provision the Host Overview dashboards
# (copied with a fixed uid; the shipped JSON has none)
RUN printf '%s\n' 'apiVersion: 1' 'apps:' \
      '  - type: performancecopilot-pcp-app' '    disabled: false' \
      > /etc/grafana/provisioning/plugins/pcp.yaml && \
    printf '%s\n' 'apiVersion: 1' 'providers:' \
      '  - name: pcp-host-overview' '    folder: PCP' '    type: file' \
      '    allowUiUpdates: false' '    options:' '      path: /etc/grafana/dashboards/pcp' \
      > /etc/grafana/provisioning/dashboards/pcp.yaml && \
    chgrp grafana /etc/grafana/provisioning/{plugins,dashboards}/pcp.yaml && \
    mkdir -p /etc/grafana/dashboards/pcp && \
    for ds in vector redis; do \
      python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["uid"]=sys.argv[2]; json.dump(d,sys.stdout,indent=2)' \
        /usr/share/performancecopilot-pcp-app/datasources/$ds/dashboards/pcp-$ds-host-overview.json \
        pcp-$ds-host-overview > /etc/grafana/dashboards/pcp/pcp-$ds-host-overview.json; \
    done

# VM monitoring: derived metrics, pmlogconf group, VM-state script, their
# /var/lib/pcp links, and the dashboards. The openmetrics override must live
# in /etc/tmpfiles.d to shadow the package's same-named file in /usr/lib.
COPY pcp/derived/ops-overview.conf          /etc/pcp/derived/ops-overview.conf
COPY pcp/pmlogconf/ops/overview             /etc/pcp/pmlogconf/ops/overview
COPY pcp/openmetrics/libvirt_vms.python     /etc/pcp/openmetrics/libvirt_vms.python
COPY pcp/openmetrics/pcp_hosts.python       /etc/pcp/openmetrics/pcp_hosts.python
COPY pcp/openmetrics/sites.json             /etc/pcp/openmetrics/sites.json
COPY pcp/tmpfiles.d/pcp-ops-overview.conf   /usr/lib/tmpfiles.d/pcp-ops-overview.conf
COPY pcp/tmpfiles.d/pcp-pmda-openmetrics.conf /etc/tmpfiles.d/pcp-pmda-openmetrics.conf
COPY build/hypervisor-vm-overview.json build/fleet-health.json build/ops-status.json \
     /etc/grafana/dashboards/pcp/
RUN chmod 0755 /etc/pcp/openmetrics/libvirt_vms.python /etc/pcp/openmetrics/pcp_hosts.python

# PMDA Install scripts need a running pmcd, so they can't run during the
# build; register libvirt + openmetrics once on first boot instead
# (their pmns files are the markers; "|" makes the conditions OR)
RUN printf '%s\n' \
      '[Unit]' \
      'Description=Register the PCP libvirt and openmetrics PMDAs with pmcd' \
      'After=pmcd.service libvirtd.service' \
      'Requires=pmcd.service' \
      'ConditionPathExists=|!/var/lib/pcp/pmns/libvirt' \
      'ConditionPathExists=|!/var/lib/pcp/pmns/openmetrics' \
      '[Service]' \
      'Type=oneshot' \
      'ExecStart=/bin/sh -c "for p in libvirt openmetrics; do [ -f /var/lib/pcp/pmns/$p ] || (cd /var/lib/pcp/pmdas/$p && ./Install </dev/null); done"' \
      'ExecStartPost=/usr/bin/systemctl try-restart pmproxy.service' \
      '[Install]' \
      'WantedBy=multi-user.target' \
      > /usr/lib/systemd/system/pcp-pmda-install.service
```

Then enable the PCP services that run everywhere, with both a preset and
explicit symlinks. **Don't** enable `valkey`, `grafana-server` or
`pmlogger_farm` in the image:

```dockerfile
RUN printf '%s\n' \
      'enable pmcd.service' \
      'enable pmlogger.service' \
      'enable pmlogger_check.timer' \
      'enable pmlogger_daily.timer' \
      'enable pmproxy.service' \
      'enable pcp-pmda-install.service' \
      > /usr/lib/systemd/system-preset/80-pcp-monitoring.preset

RUN ln -sf /usr/lib/systemd/system/pmcd.service            /etc/systemd/system/multi-user.target.wants/pmcd.service && \
    ln -sf /usr/lib/systemd/system/pmlogger.service        /etc/systemd/system/multi-user.target.wants/pmlogger.service && \
    ln -sf /usr/lib/systemd/system/pmproxy.service         /etc/systemd/system/multi-user.target.wants/pmproxy.service && \
    ln -sf /usr/lib/systemd/system/pmlogger_check.timer    /etc/systemd/system/timers.target.wants/pmlogger_check.timer && \
    ln -sf /usr/lib/systemd/system/pmlogger_daily.timer    /etc/systemd/system/timers.target.wants/pmlogger_daily.timer && \
    ln -sf /usr/lib/systemd/system/pcp-pmda-install.service /etc/systemd/system/multi-user.target.wants/pcp-pmda-install.service
```

> The VM-monitoring additions are untested as an image build. The live
> equivalent (A5) is what's verified. In particular, check after the first
> boot that `pminfo libvirt | wc -l` returns 137 and that
> `pminfo -f openmetrics.libvirt_vms.state` lists every VM.

The explicit symlinks matter, not just the presets. Presets only apply on
first boot of a fresh install. Symlinks in the image's `/etc` are carried
into existing systems by bootc's `/etc` 3-way merge on upgrade.

Optionally, also bake the automation user's sudoers rule from A1 into the
image:

```dockerfile
RUN echo 'automation ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/automation && \
    chmod 0440 /etc/sudoers.d/automation
```

Build and push as usual (bump the tag, e.g. `1.1.0`; add whatever build
arguments your image needs, such as subscription credentials):

```bash
podman build -t registry.example.com/rhel-kvm-image:1.1.0 .
podman push registry.example.com/rhel-kvm-image:1.1.0
```

Because the packages are installed at build time, gotchas #1 (enablement)
and #4 (SELinux policy load) don't apply on hosts running this image.

### B2. Collector post-deploy config (hypervisor-01 only)

The image already contains everything generic. On the collector:

```bash
# Turn pmproxy's valkey support back on
sed -i '/^\[keys\]/,/^\[/ s/^enabled = false/enabled = true/' /etc/pcp/pmproxy/pmproxy.conf
```

Then follow [A4](#a4-collector-hypervisor-01-19202010) with these
changes:

- **Skip** the plugin and dashboard provisioning files, which are already in
  the image.
- **Do** write `control.d/remote` and `provisioning/datasources/pcp.yaml`
  (with the `vector-<hostname>` uids).
- **Do** run A5 step 4 (regenerate the logger configs) so the `ops/overview`
  group is logged.
- `systemctl enable --now valkey pmlogger_farm pmlogger_farm_check.timer grafana-server`,
  then `systemctl restart pmproxy`.

These are all plain `/etc` changes, so they persist across `bootc upgrade`.
They're a good fit for configuration management (for example an Ansible
role applied to a `monitoring_collector` group).

Agents need **no** post-deploy steps.

### Moving from Approach A to Approach B

`bootc switch` refuses to run while rpm-ostree has local layered
packages. Drop the layers and rebase in one transaction, so the services
never lose their binaries:

```bash
rpm-ostree rebase \
  --uninstall=pcp --uninstall=pcp-zeroconf --uninstall=pcp-pmda-libvirt \
  --uninstall=grafana --uninstall=grafana-pcp --uninstall=valkey \
  ostree-unverified-registry:registry.example.com/rhel-kvm-image:1.1.0
systemctl reboot
```

After the reboot, `rpm-ostree status` should show no `LayeredPackages`, and
`bootc status` / `bootc upgrade` work normally again. All the `/etc`
changes from Approach A (sysconfig, pmproxy.conf, control.d, Grafana
provisioning, service enablement) are preserved by the `/etc` merge. Data in
`/var` (archives, Valkey, `grafana.db`) is untouched.

---

## VM monitoring and the ops dashboards

### What PCP can see about VMs

| Source | Metrics | Notes |
|---|---|---|
| `pmdalibvirt` (`pcp-pmda-libvirt`) | `libvirt.dominfo.*`, `libvirt.domstats.*` (cpu, vcpu, balloon/RSS, block, net), `libvirt.hv.*` | Per VM, labelled `guest_name`. **Running domains only.** The dashboards' main source. |
| `libvirt_vms.python` via `pmdaopenmetrics` (ours) | `openmetrics.libvirt_vms.*` | State code of **every** defined VM (incl. shut off / crashed), with name and autostart flag, plus per-host totals. Powers "down VM" detection. |
| `pcp_hosts.python` via `pmdaopenmetrics` (ours, collector) | `openmetrics.pcp_hosts.*` | Hypervisor reachability, per-host status with map coordinates, fleet totals. |
| cgroups (stock `pmdalinux`/`proc`) | `cgroup.*` for `/machine.slice/machine-qemu-N-<name>.scope` | Per-VM CPU and memory with no extra PMDA. Already logged by zeroconf. |
| `pmdakvm` (stock) | `kvm.*` | Hypervisor-wide exits, halts and so on. Not per VM. Some need root (debugfs). |

**SR-IOV blind spot.** VNF data-plane traffic on SR-IOV VFs bypasses the
host network stack. Neither `libvirt.domstats.net.*` nor
`network.interface.*` sees it. The dashboard's VM network columns cover
virtio/tap interfaces only. For VF throughput, use per-VF counters from the
physical port (`ip -s link show <pf>`; would need a small custom PMDA), or
in-guest stats (for example `pcp-pmda-snmp` polling the VNF).

### Hypervisor & VM Overview dashboard

URL: <http://192.0.2.10:3000/d/hv-vm-overview>, in the **PCP** folder.

| Section | Contents | Source |
|---|---|---|
| **Fleet overview** | One row per hypervisor: CPU %, memory %, running/stopped VMs, load 1m/15m, CPUs, RAM, uptime. Click a host name to drill in. Beside it, a **Cockpit** panel: click a hypervisor to open its Cockpit. | PCP Redis (history) |
| **Host: `$host`** (repeats per selected host) | Gauges and stats: CPU, memory, running/stopped VMs, vCPUs allocated vs host CPUs, VM memory allocated, uptime. Charts: host CPU breakdown, memory, physical NIC throughput, disk. | PCP Redis |
| **Virtual machines on `$host` (live)** | One row per running VM: CPU % (of its vCPUs), memory % (host RSS of assigned), vCPUs, RAM, disk read/write, net rx/tx, drops (red when non-zero). Sorted by CPU. | PCP Vector, uid `vector-$host` |
| VM charts | Per-VM CPU %, disk throughput, network throughput (write/tx drawn below zero). | PCP Redis |

The `Host` dropdown is multi-select. With **All**, the host section repeats
for every host reporting to Valkey.

Design notes, for anyone editing it:

- The JSON is generated by `grafana/gen_dashboard.py`:
  `python3 grafana/gen_dashboard.py > grafana/dashboards/hypervisor-vm-overview.json`.
- **No arithmetic in queries** (gotcha #6). Percentages come from the
  `ops.*` derived metrics.
- **The VM table uses PCP Vector's `metrics_table` format**, which gives one
  row per instance and one column per query. A PCP Redis query can't produce
  this cleanly: the backend only returns `time_series`, and Grafana's
  Reduce→"labels to fields" keys on the raw field name
  (`metric[<domain-uuid>]`), not the legend. The fleet table can pivot
  Redis series because host metrics have no instances.
- Grafana `byRegexp` override matchers wrap patterns as `^…$` unless they're
  written `/…/`. Use `/ tx$/`, not `" tx$"`.
- Each host needs a PCP Vector datasource with `uid: vector-<hostname>`
  (hostname as reported by PCP), otherwise its live VM table shows "No data".

### Ops Status dashboard

URL: <http://192.0.2.10:3000/d/ops-status> (add `?kiosk` for a wall screen).
This is the board for a manager or a TV: no time picker, and readable from
across the room. Its layout is modelled on a homelab-style board: filled
tiles, two maps, UP tiles.

| Area | Contents |
|---|---|
| **Fleet status** (hero) | ✓ All systems normal / ⚠ Attention needed / ✕ Service impacted, as one filled tile. |
| **KPI tiles** | Hypervisors online, VMs running (with 24 h sparklines), VMs down (✓ 0 / ✕ n), warnings (✓ 0 / ⚠ n). |
| **Sites map** (US) and a **site close-up** (first site in `sites.json`) | One marker per site with a halo, coloured by its worst hypervisor: green healthy, red when a hypervisor is offline or has autostart VMs down. Hover for details. |
| **Hypervisor row** | Uptime, UP/DOWN, and running VMs per hypervisor. **Click an UP/DOWN tile to open that hypervisor's Cockpit.** |
| **Charts** | Hypervisor CPU over the last 6 h, a VM-state donut (running / down / stopped), and the busiest VMs by CPU. |
| **Issue lists** | *Service-impacting issues* and *Warnings*, in plain words (e.g. "⚠ VM CPU high (15m avg, %) · pcp-demo-batch-02 · hypervisor-02"), or ✓ None. |
| **Throughput tiles** | Average hypervisor CPU and memory (amber/red past 80% / 90%), plus total network in and out, each with a 6 h sparkline. |
| **Throughput charts** | Hypervisor network over the last 6 h (received above the line, sent below), and the top VM talkers. |

How the hero is decided (same thresholds as Fleet Health):

| Status | When |
|---|---|
| ✕ Service impacted | a hypervisor is unreachable, or an autostart VM isn't running |
| ⚠ Attention needed | a VM's CPU averages ≥ 85% over 15 min, VM memory ≥ 90%, any packet drops, or hypervisor CPU/memory ≥ 80% |
| ✓ All systems normal | none of the above |

Design notes:

- Regenerate with
  `python3 grafana/gen_status_dashboard.py > grafana/dashboards/ops-status.json`.
- The hero and the issue lists use Grafana **server-side expressions**: per
  rule, a hidden PCP query, a *Reduce* (last / mean / max) and a *Math*
  threshold. That's the only way to give each query its own threshold.
  Expressions need one series per label set, which is why the
  collector scripts export per-host and fleet totals as plain metrics.
- The "VMs down", "Warnings" and issue panels reuse one panel's results
  through the `-- Dashboard --` datasource. It renames frames to its own
  refId, so rules are picked by the expression's field name, not by refId.
- The issue lists are stat panels rather than a table, because an empty
  table shows "No data", while a stat shows its *no value* text ("✓ None").
- Fleet tiles (average CPU/memory, total network) first **resample** each
  host's series onto a shared 2-minute grid with a server-side expression,
  then average or sum per timestamp. Hosts are sampled a few seconds apart,
  so without resampling a per-timestamp sum only ever includes one host.
- The board is taller than one 1080p screen; on a wall display, use a
  portrait or 4K screen, or let it scroll.
- **Map tiles:** Esri *World Dark Gray* base and reference layers, loaded by
  the viewer's browser from `services.arcgisonline.com`. CARTO, Grafana's
  default basemap, now returns "API KEY REQUIRED". A screen without internet
  access shows the markers on a blank background.
- Colour follows the status palette (green `#0ca30c`, amber `#fab219`, red
  `#d03b3b`), always paired with ✓ / ⚠ / ✕ and words. The neutral tiles use
  categorical blue and violet, and the hypervisor lines blue/orange. Those
  were checked with the dataviz palette validator against Grafana's dark
  surface.

### Cockpit click-through

If your hypervisors run Cockpit (`cockpit.socket`, port 9090), the per-hypervisor **UP/DOWN** tiles on Ops Status, and the
**Cockpit** panel on Hypervisor & VM Overview, link to
`https://<address>:9090/` in a new tab. The address comes from `sites.json`,
via `openmetrics.pcp_hosts.up`. Cockpit uses its own self-signed
certificate, so browsers warn on first visit until a trusted certificate is
installed (`/etc/cockpit/ws-certs.d/`). Users log in with their own accounts
on that host.

### Fleet Health dashboard

URL: <http://192.0.2.10:3000/d/fleet-health>, in the **PCP** folder. It
shows every host and VM at once (PCP Redis only, no host selection), and
brings the outliers to the top. Click a host name in any table to open it
in Hypervisor & VM Overview.

| Section | Contents |
|---|---|
| **Fleet at a glance** | Count tiles that turn colour when non-zero: hypervisors reporting, VMs running, VMs not running, **should be running** (down and autostart=yes, red), VMs with sustained high CPU, VMs at high memory, VM NICs dropping packets, hosts under pressure. |
| **Needs attention** | Tables that list only the offenders ("✓ ..." when clear): **VMs not running** (state, autostart; autostart ones in red), **sustained high CPU** (15-min avg / peak / now), **memory pressure**, **packet drops**, **hosts under pressure**. |
| **Top consumers** | Fleet-wide top 10 by CPU (15-min avg), memory, disk throughput and network throughput, as bar gauges. |
| **Trends** | VM state timeline for every VM (green running, red shut off, ...), plus host CPU, host memory and running VMs per host. |

Outlier thresholds (constants at the top of
`grafana/gen_fleet_dashboard.py`; edit and regenerate):

| Rule | Threshold |
|---|---|
| VM CPU | 15-minute average ≥ 85% of its vCPUs |
| VM memory | qemu RSS ≥ 90% of assigned memory |
| Packet drops | any rx/tx drop in the last 15 minutes |
| Host pressure | CPU or memory ≥ 80% |
| VM down | state ≠ running in the last 2 minutes; *should be running* if autostart=yes |

Design notes:

- Regenerate with
  `python3 grafana/gen_fleet_dashboard.py > grafana/dashboards/fleet-health.json`.
  Both generators share panel helpers in `grafana/dashlib.py`.
- Outlier tables are **single-metric** on purpose: Reduce (with labels to
  fields) gives one row per series with `hostname`/`guest_name` columns, and
  *Filter data by values* keeps only rows over the threshold. Multi-metric
  lists (drops rx+tx, host CPU+memory) put the identity in the legend
  instead.
- Thresholds are hard-coded because Grafana 10.2 can't use dashboard
  variables inside transformations.
- VM-state rows are parsed from the series name (gotcha #11).

### Demo VMs

To generate data for testing, `demo/` creates small Fedora Cloud VMs
(2 vCPU / 2 GB, autostart) with a synthetic workload. During development,
seven were spread over two hypervisors:

| Host | VMs (profile) |
|---|---|
| hypervisor-01 | `pcp-demo-web-01` (net), `pcp-demo-db-01` (disk), `pcp-demo-batch-01` (cpu) |
| hypervisor-02 | `pcp-demo-web-02` (net), `pcp-demo-db-02` (disk), `pcp-demo-batch-02` (cpu), `pcp-demo-idle-01` (idle) |

- `demo-load.sh` runs in each guest as `demo-load.service`, delivered by
  cloud-init. `cpu` runs bursts of `sha256sum /dev/zero` on both vCPUs.
  `disk` runs O_DIRECT `dd` write/read cycles of 256 MB–1 GB. `net` floods
  1400-byte pings at the libvirt NAT gateway, so the traffic stays on the
  host.
- `mkvm.sh <name> <profile> <vcpus> <ram_mb>` (as root on a hypervisor)
  creates one VM. It uses a qcow2 overlay on a Fedora Cloud image in
  `/var/lib/libvirt/images/pcp-demo/base/`. Put the image there (verify it
  against Fedora's published CHECKSUM), along with `demo-load.sh`,
  `mkvm.sh`, and the SSH public key to inject for the `fedora` user (as
  `authorized_key`).

To see the outlier views light up, create a couple of problems on purpose:

- **Down VM:** `virsh shutdown pcp-demo-idle-01`. It's autostart, so Fleet
  Health and Ops Status show it as *should be running*. Undo with
  `virsh start pcp-demo-idle-01`.
- **Hot VM:** in a `cpu` guest, swap the bursty load for a constant one:
  `systemctl stop demo-load; systemd-run --unit=cpu-hog sh -c "sha256sum /dev/zero & sha256sum /dev/zero & wait"`.
  After about 15 minutes it trips the sustained-CPU rule. Undo with
  `systemctl stop cpu-hog; systemctl start demo-load`. Reach the guest with
  `ssh -J <admin>@<hypervisor> fedora@<guest IP from virsh domifaddr>`.

Remove them when you're done:

```bash
for v in $(virsh list --all --name | grep '^pcp-demo-'); do
  virsh destroy "$v"; virsh undefine "$v"
done
rm -rf /var/lib/libvirt/images/pcp-demo
```

---

## Verification

Checks used to validate a deployment (shown for Approach A).

```bash
# Agent: pmcd and pmproxy listening on all interfaces
ss -ltnp | grep -E '4432[12]'

# Collector: remote pmcd reachable, and the farm is writing archives
pminfo -h 192.0.2.11 -f kernel.all.load
ls /var/log/pcp/pmlogger/          # hypervisor-01/  hypervisor-02/

# Collector: both hosts present in valkey, with ~10s samples
curl -s 'localhost:44322/series/labels?names=hostname'
#   {"hostname":["hypervisor-02","hypervisor-01"]}
pmseries 'kernel.all.load{hostname=="hypervisor-02"}[samples:3]'

# Collector: live fetch from the agent's pmproxy
curl -s 'http://192.0.2.11:44322/pmapi/fetch?names=kernel.all.load'

# VM monitoring: PMDA active, derived metrics resolve, per-VM history in valkey
pminfo libvirt | wc -l                                   # 137
pmrep -t 2 -s 2 ops.host.cpu.util ops.host.mem.util
pmseries 'ops.vm.cpu.util{hostname=="hypervisor-02"}[samples:1]'

# VM state for all domains (incl. shut off): script works as pcp, PMDA exports it, valkey has it
sudo -u pcp /etc/pcp/openmetrics/libvirt_vms.python
pminfo -f openmetrics.libvirt_vms.state openmetrics.control.status
pmseries 'openmetrics.libvirt_vms.state{hostname=="hypervisor-02"}[samples:1]'
pminfo openmetrics | wc -l                               # 7 on agents (libvirt_vms + control), more on the collector

# Collector: hypervisor reachability, map status, fleet totals
sudo -u pcp /etc/pcp/openmetrics/pcp_hosts.python
pminfo -f openmetrics.pcp_hosts.status openmetrics.pcp_hosts.fleet_vms_running

# Grafana: datasources answer through the Grafana proxy
curl -s -u admin:<password> localhost:3000/api/datasources | python3 -m json.tool | grep -E '"(uid|name)"'
curl -s -u admin:<password> 'localhost:3000/api/datasources/proxy/uid/<uid>/pmapi/fetch?names=kernel.all.load'
```

`/api/datasources/uid/<uid>/health` returns `Plugin unavailable` for the
**PCP Vector** datasources. That's expected: Vector is a frontend-only
plugin with no backend health check. Use the proxy fetch above, or
**Save & Test** in the UI, instead.

**Dashboards** (folder **PCP**):

- **Ops Status**: manager / wall-screen board with site maps.
- **Fleet Health**: fleet-wide outliers (down VMs, hot VMs, top consumers).
- **Hypervisor & VM Overview**: per-host drill-down.
- **PCP Redis: Host Overview**: historical data. Use the `host` dropdown
  to switch between hypervisor-01 and hypervisor-02.
- **PCP Vector: Host Overview**: live data. Use the `datasource` dropdown
  to pick `PCP Vector` (hypervisor-01) or `PCP Vector hypervisor-02`.
