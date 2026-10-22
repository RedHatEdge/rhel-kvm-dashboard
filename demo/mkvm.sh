#!/bin/bash
# mkvm.sh <name> <profile> <vcpus> <ram_mb>  -- run as root on the hypervisor
set -euo pipefail
N=$1 P=$2 C=$3 M=$4
D=/var/lib/libvirt/images/pcp-demo
qemu-img create -q -f qcow2 -F qcow2 -b $D/base/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2 $D/$N.qcow2 10G
cat > $D/$N.user-data <<UD
#cloud-config
hostname: $N
ssh_authorized_keys:
  - $(cat $D/authorized_key)
write_files:
  - path: /etc/demo-profile
    content: "$P\n"
  - path: /usr/local/bin/demo-load.sh
    permissions: "0755"
    encoding: b64
    content: $(base64 -w0 $D/demo-load.sh)
  - path: /etc/systemd/system/demo-load.service
    content: |
      [Unit]
      Description=Synthetic load for PCP dashboard demo
      [Service]
      ExecStart=/usr/local/bin/demo-load.sh
      Restart=always
      [Install]
      WantedBy=multi-user.target
runcmd:
  - [systemctl, daemon-reload]
  - [systemctl, enable, --now, demo-load.service]
UD
virt-install --name $N --vcpus $C --memory $M --osinfo fedora42 --import \
  --disk path=$D/$N.qcow2,bus=virtio --network network=default,model=virtio \
  --cloud-init user-data=$D/$N.user-data --graphics none --noautoconsole
virsh autostart $N
