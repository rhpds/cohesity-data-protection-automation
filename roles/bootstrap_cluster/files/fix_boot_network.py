#!/usr/bin/env python3
"""Make the Cohesity VE guest network usable on the KubeVirt masquerade pod
network, persistently (survives a guest reboot).

Why this exists
---------------
The Cohesity VE appliance image bonds its primary NIC (``enp1s0``) into
``bond0`` (active-backup) at MTU 1500. On a KubeVirt *masquerade* pod network
the guest NIC's maximum MTU is the (smaller) pod-network MTU (1300), so the
kernel refuses to enslave it::

    enp1s0: mtu greater than device maximum
    bond0: (slave enp1s0): Error -22 calling dev_set_mtu

The bond never gets carrier, NetworkManager sits forever in "connecting
(getting IP configuration)", the guest never obtains its DHCP lease (10.0.2.2),
and the VM is unreachable on the pod network -- so every later SSH/HTTPS task
fails ("no route to host").

The fix
-------
Lower the MTU to 1300 in the NetworkManager *keyfiles* for the bond master
(``bond0``) and its port (``enp1s0``). With the master at 1300 the bond can
enslave the 1300-max pod NIC, carrier comes up, DHCP returns 10.0.2.2 and the
default route. Crucially this keeps the bond topology intact (the appliance
expects ``bond0``), unlike a runtime teardown of the bond.

Because the edit lands in ``/etc/NetworkManager/system-connections/*.nmconnection``
on the appliance's writable ext4 root, it **persists across a guest reboot**:
on next boot NetworkManager reads the keyfile, brings ``bond0`` up at MTU 1300
and DHCPs without any console intervention. This matters because once
``iris_cli`` creates the cluster the console/SSH credentials rotate and the web
UI route is the only remaining way in -- the network must come back by itself.

We cannot modify the locked appliance image and it has no cloud-init, so the
only way into a guest with no IP is the serial console. This script connects
over ``virtctl console`` and, idempotently:

  1. sets ``802-3-ethernet.mtu 1300`` on the ``bond0`` and ``enp1s0`` keyfiles,
  2. reloads NetworkManager so the keyfiles take effect,
  3. bounces ``bond0`` (down/up) to re-enslave the port at the new MTU,
  4. verifies ``bond0`` obtained an IPv4 lease and a default route.

Every step is safe to re-run, so an ArgoCD/Job retry is harmless. Re-running on
an already-fixed guest is a no-op (the keyfiles already carry mtu=1300).

NOTE on the appliance's "do not modify /etc" guidance: this is a disposable lab
appliance and reboot-survival of the pod-network path is a hard requirement, so
persisting the MTU in the NM keyfile is intentional and accepted here.

Configuration (all via environment, with sane defaults):
  VIRTCTL              path to the virtctl binary (default: "virtctl")
  COHESITY_VM          VM name (default: "cohesity-ve1")
  COHESITY_NAMESPACE   VM namespace (default: "cohesity-ve")
  COHESITY_USER        console login user (default: "cohesity")
  COHESITY_PASSWORD    console login password (default: "Cohe$1ty")
  COHESITY_BOND        bond master connection/device (default: "bond0")
  COHESITY_NIC         bonded port connection/device (default: "enp1s0")
  COHESITY_MTU         MTU to set (default: "1300", the masquerade pod-NIC max)
"""

import os
import sys
import time

import pexpect

VIRTCTL = os.environ.get("VIRTCTL", "virtctl")
VM = os.environ.get("COHESITY_VM", "cohesity-ve1")
NS = os.environ.get("COHESITY_NAMESPACE", "cohesity-ve")
USER = os.environ.get("COHESITY_USER", "cohesity")
PASSWORD = os.environ.get("COHESITY_PASSWORD", "Cohe$1ty")
BOND = os.environ.get("COHESITY_BOND", "bond0")
NIC = os.environ.get("COHESITY_NIC", "enp1s0")
MTU = os.environ.get("COHESITY_MTU", "1300")

PROMPT = r"\[%s@[^\]]*\][\$#]" % USER


def spawn_console():
    child = pexpect.spawn(
        f"{VIRTCTL} console {VM} -n {NS}", timeout=30, encoding="utf-8"
    )
    child.logfile = sys.stdout
    return child


def login(child, attempts=40):
    """Reach a usable shell prompt, logging in if needed.

    "VM Running" only means the VMI is scheduled, not that the guest has
    reached a login prompt, so nudge with a newline and retry until the guest
    has finished booting.
    """
    for _ in range(attempts):
        child.sendline("")
        idx = child.expect(
            [r"login:", PROMPT, r"[Pp]assword:", pexpect.TIMEOUT], timeout=10
        )
        if idx == 0:  # login prompt
            child.sendline(USER)
            if child.expect([r"[Pp]assword:", pexpect.TIMEOUT], timeout=10) == 0:
                child.sendline(PASSWORD)
                if child.expect([PROMPT, pexpect.TIMEOUT], timeout=15) == 0:
                    return True
        elif idx == 1:  # already at a shell
            return True
        elif idx == 2:  # mid-login password prompt
            child.sendline(PASSWORD)
            if child.expect([PROMPT, pexpect.TIMEOUT], timeout=15) == 0:
                return True
        # TIMEOUT -> still booting; loop and nudge again
    return False


def run(child, cmd, settle=2, timeout=60):
    """Send a command and wait for the prompt to return."""
    child.sendline(cmd)
    child.expect([PROMPT, pexpect.TIMEOUT], timeout=timeout)
    time.sleep(settle)


def main():
    child = spawn_console()
    if not login(child):
        print("ERROR: could not reach a shell prompt on the VM console", file=sys.stderr)
        sys.exit(1)

    # Stop the shell echoing our command lines so the verify token below appears
    # in the *output* only and we never match the echoed command by mistake.
    run(child, "stty -echo", settle=1, timeout=15)

    # Persist MTU in the NM keyfiles for the bond master and its port. With the
    # master at 1300 the bond can enslave the 1300-max pod NIC. Survives reboot.
    run(child, f"sudo nmcli con mod {BOND} 802-3-ethernet.mtu {MTU}")
    run(child, f"sudo nmcli con mod {NIC} 802-3-ethernet.mtu {MTU}")
    # Apply the keyfiles and re-enslave the port at the new MTU.
    run(child, "sudo nmcli con reload")
    run(child, f"sudo nmcli con down {BOND} || true")
    time.sleep(2)
    run(child, f"sudo timeout 60 nmcli con up {BOND} || true", settle=8, timeout=90)

    # Verify: bond has an IPv4 lease, a default route over it, and the keyfile
    # carries the persistent MTU. The tokens in the *output* differ from the
    # echoed command, so we never match the echo by mistake.
    child.sendline(
        "L=$(ip -4 -o addr show %s | grep -c 'inet '); "
        "R=$(ip -4 route | grep -c \"^default .* dev %s \"); "
        "M=$(sudo grep -A2 '\\[ethernet\\]' "
        "/etc/NetworkManager/system-connections/%s.nmconnection "
        "| grep -m1 mtu | cut -d= -f2); "
        "echo NETFIX LEASE=$L ROUTE=$R KEYMTU=$M" % (BOND, BOND, BOND)
    )
    idx = child.expect(
        [r"NETFIX LEASE=([0-9]+) ROUTE=([0-9]+) KEYMTU=([0-9]+)",
         pexpect.TIMEOUT],
        timeout=30,
    )

    ok = False
    if idx == 0:
        lease = child.match.group(1)
        route = child.match.group(2)
        keymtu = child.match.group(3)
        ok = lease != "0" and route != "0" and keymtu == MTU
        result = f"LEASE={lease} ROUTE={route} KEYMTU={keymtu}"
    else:
        result = "no NETFIX line (timeout)"

    child.send("\x1d")  # Ctrl-] to exit the console
    time.sleep(1)
    child.close(force=True)

    if ok:
        print(f"\n[fix_boot_network: {BOND} up at MTU {MTU} with lease+route+persisted keyfile -- {result}]")
        sys.exit(0)
    print(f"ERROR: {BOND} not fully fixed -- {result}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
