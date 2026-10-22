#!/bin/bash
# Synthetic load for the PCP/Grafana VM dashboard demo.
# Profile comes from /etc/demo-profile: cpu | disk | net | idle
P=$(cat /etc/demo-profile)
GW=$(ip route | awk '/^default/ {print $3; exit}')
r() { echo $(( RANDOM % $1 + ${2:-1} )); }                 # r <range> [min]
cpu()  { for _ in $(seq 1 "$1"); do timeout "$2" sha256sum /dev/zero & done; wait; }
disk() { dd if=/dev/urandom of=/var/tmp/demo.bin bs=1M count="$1" oflag=direct status=none
         dd if=/var/tmp/demo.bin of=/dev/null bs=1M iflag=direct status=none
         rm -f /var/tmp/demo.bin; }
net()  { timeout "$1" ping -q -s 1400 -i 0.002 "$GW" >/dev/null 2>&1; }
while true; do
  case "$P" in
    cpu)  cpu 2 "$(r 90 30)"; net 5; sleep "$(r 45)" ;;
    disk) disk "$(r 768 256)"; cpu 1 5; sleep "$(r 20)" ;;
    net)  net "$(r 60 30)"; cpu 1 "$(r 10)"; disk 64; sleep "$(r 10)" ;;
    *)    sleep 300 ;;
  esac
done
