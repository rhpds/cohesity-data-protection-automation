#!/usr/bin/env python3
"""Drive iris_cli interactively to initialize the Cohesity cluster.

The node IP is discovered from whichever interface actually carries the
address, NOT from a hardcoded ``bond0``. The Cohesity VE golden image bonds
its primary NIC into ``bond0`` at MTU 1500, but on a KubeVirt masquerade pod
network that bond never comes up (the pod NIC's max MTU is smaller, so the
kernel refuses to enslave it), and the live address ends up on the plain
ethernet device (e.g. ``enp1s0``). Discover the interface dynamically so this
keeps working regardless of which device holds the lease.
"""

import ipaddress
import subprocess
import sys

import pexpect


def _mask_and_gw(dev, node_ip):
    """Return (netmask, gateway) for ``node_ip`` on ``dev``.

    Gateway falls back to the first host address of the subnet (the KubeVirt
    masquerade gateway is always ``<network>.1``) when no route gives one.
    """
    out = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", dev],
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        fields = line.split()
        if fields[3].split("/")[0] == node_ip:
            net = ipaddress.ip_network(fields[3], strict=False)
            return str(net.netmask), str(net.network_address + 1)
    return None, None


def discover_primary_ipv4():
    """Discover (node_ip, netmask, gateway, dev) for the VM's primary NIC.

    Prefer the interface that owns the default route (most reliable); fall back
    to the first global-scope IPv4 that is not loopback/virbr/docker/bond.
    """
    # Preferred: the device that can reach off-link (carries the default route).
    try:
        out = subprocess.run(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, check=True,
        ).stdout
        # e.g. "1.1.1.1 via 10.0.2.1 dev enp1s0 src 10.0.2.2 uid 1000"
        toks = out.split()
        dev = toks[toks.index("dev") + 1]
        node_ip = toks[toks.index("src") + 1]
        gw = toks[toks.index("via") + 1] if "via" in toks else None
        mask, calc_gw = _mask_and_gw(dev, node_ip)
        if node_ip and mask:
            return node_ip, mask, gw or calc_gw, dev
    except Exception:
        pass

    # Fallback: first usable global-scope IPv4 address.
    out = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "scope", "global"],
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        fields = line.split()
        dev, ip_cidr = fields[1], fields[3]
        if dev.startswith(("lo", "virbr", "docker")):
            continue
        net = ipaddress.ip_network(ip_cidr, strict=False)
        node_ip = ip_cidr.split("/")[0]
        return node_ip, str(net.netmask), str(net.network_address + 1), dev

    return None, None, None, None


node_ip, subnet_mask, subnet_gw, dev = discover_primary_ipv4()

if not node_ip:
    print(
        "ERROR: could not determine the VM's primary IPv4 address "
        "(checked default-route device and global-scope addresses)",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"Using {dev}: node-ips={node_ip} subnet-mask={subnet_mask} subnet-gateway={subnet_gw}")

IRIS_CMD = (
    f"cluster virtual-robo-create "
    f"node-ips={node_ip} "
    f"subnet-mask={subnet_mask} "
    f"subnet-gateway={subnet_gw} "
    "dns-server-ips=8.8.8.8,1.1.1.1 "
    "ntp-servers=pool.ntp.org "
    "apps-subnet=172.26.64.0 "
    "apps-subnet-mask=255.255.255.0 "
    # hostname must be an in-cluster-resolvable FQDN: Cohesity uses it as the
    # S3 host in the Velero BackupStorageLocation, and generates its TLS cert
    # with SAN *.<domain-names>. lab-ve-cluster.cohesity-ve.svc.cluster.local
    # resolves via the selector-based lab-ve-cluster Service (tracks the VM's
    # pod IP across restarts) and matches SAN *.cohesity-ve.svc.cluster.local
    # -- no DNS hacks or TLS-verify skip needed.
    "hostname=lab-ve-cluster.cohesity-ve.svc.cluster.local "
    "name=lab-ve-cluster "
    "domain-names=cohesity-ve.svc.cluster.local"
)

child = pexpect.spawn(f"iris_cli {IRIS_CMD}", timeout=300, encoding="utf-8")
child.logfile = sys.stdout

child.expect("Username:")
child.sendline("admin")

child.expect("Password:")
child.sendline("admin")

child.expect(pexpect.EOF, timeout=300)
child.close()

if child.signalstatus is not None:
    print(f"iris_cli killed by signal {child.signalstatus}", file=sys.stderr)
    sys.exit(1)
if child.exitstatus != 0:
    sys.exit(child.exitstatus)
