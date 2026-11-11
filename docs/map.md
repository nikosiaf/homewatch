# Map

The **Map** page shows how your devices are connected, drawn top down: the ISP modem and the Proxmox
host with its WAN and LAN cards, the switches with their uplink, what is wired to each switch port, and
the Wi-Fi clients of each access point. One thing opens at a time on each level. homeWatch finds it
from what the modules already read:

- **MikroTik:** each switch's MAC table; a device sits on the port with the fewest MACs (an uplink
  carries many), and a switch on the port where another switch sees its MACs.
- **UniFi:** Wi-Fi clients under their access point; clients not connected now stay in red under the
  access point they last used (UniFi's client history, 30 days).
- **Home Assistant:** device names and MACs, hubs and bridges with the devices that come through them
  (Zigbee, alarm panels), and Bluetooth devices under the Bluetooth proxy.
- **Proxmox:** the host on the switch port where its guests are seen, its network cards from its
  bridges, its VMs and containers (stopped ones in red), and the UPS when its NUT server runs there.
- **Addresses and names:** OPNsense's ARP table and DHCP leases (see the OPNsense module), UniFi, Home
  Assistant, and the addresses the modules are set up with (the PBS, the NAS…).

The map is rebuilt at every check run (Home Assistant's devices every 30 minutes, Proxmox's guests and
network cards every 6 hours); **Scan now** reads them all at once. What can't be found is added under
**Manually set dependencies** (an integration and the bridge it runs through, a device on a switch
homeWatch doesn't read).

- **Root cause:** a problem on a device behind something that has a problem, or that just restarted or
  lost its link, names it. If that parent's problem was already sent, the device's own message is
  left out and listed with the incident when it is over; "a switch or access point restarted and N
  devices behind it went with it" is one of the likely causes.
- **New on the network:** a device never seen before (a MAC on a switch port or access point, or a new
  VM) is listed on the Status page, marked "new" on the Map and sent once as a WARNING, until you mark
  it as **Known**. What was there the first time (and UniFi's client history) counts as known. Phones
  with a private Wi-Fi address show up again now and then; they are labelled so. A MAC that only a switch
  has seen (no IP address, not a Wi-Fi client) must stay for 10 minutes over 3 readings before it counts,
  so stray addresses from a garbled frame don't warn; multicast and all-zero MACs never count. A bare MAC on
  an access point's switch port (not a UniFi client, no IP address, no name) is not a device at all: it is
  left off the Map and never warns. Guests on a
  guest network count at once and are labelled "waiting at the portal" or "passed the portal".

---

[← homeWatch README](../README.md) · [All documentation](README.md)
