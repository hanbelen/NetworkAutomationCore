#!/usr/bin/env python3
"""Generate eBGP unnumbered underlay config for SONiC-VS devices.

For each discovered device, produces:
  <hostname>.json    — partial config_db.json (merge via sonic-cfggen -j --write-to-db)
  <hostname>.vtysh   — vtysh commands for BGP unnumbered peering

Reads from NetworkInventoryData SoT:
  - intent/global/asn_plan.yml      → ASN generation
  - intent/global/ip_plan.yml       → loopback addressing
  - intent/global/network_defaults.yml → device profiles (active ports)
  - intent/sites/<site>/site.yml    → site_block, region
"""

import argparse
import ipaddress
import json
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("ERROR: pyyaml required.  pip install pyyaml")


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def generate_asn(device, asn_plan):
    """Derive 32-bit ASN from device attributes using the ASN plan.

    Format: 42 R SS P PR II
    PR = pod digit + role digit (single digit each)
    """
    mappings = asn_plan['asn']['mappings']
    region = device.get('region', 'APAC')
    site = device.get('site_code')      # e.g. 'syd'
    plane = device.get('plane', 'a')
    role = device['role']
    pod = device['pod']
    device_id = device['device_id']

    region_code = mappings['regions'].get(region, 0)
    site_code = mappings['sites'].get(site, '00')
    plane_code = mappings['planes'].get(plane, 1)
    role_code = mappings['roles'].get(role, 0)
    pod_role = str(pod) + str(role_code)

    asn_str = (
        "42"
        + str(region_code)
        + str(site_code).zfill(2)
        + str(plane_code)
        + str(pod_role).zfill(2)
        + str(device_id).zfill(2)
    )
    return int(asn_str)


def derive_loopback0(device, site_block, ip_plan):
    """Derive Loopback0 IP from the IP plan.

    lo0 = site_base + lo0_offset (2nd octet) + pod (3rd octet) + role_offset + device_id (4th octet)
    Example: syd (10.132.0.0/14), lo0 offset=64, p1-spn-01 → 10.132.65.101
    """
    net = ipaddress.ip_network(site_block, strict=False)
    octets = list(net.network_address.packed)

    lo0_offset = ip_plan['functions']['loopback_lo0']['offset']
    role_offset = ip_plan['role_offsets'].get(device['role'], 0)

    octets[2] = lo0_offset + device['pod']
    octets[3] = role_offset + device['device_id']

    return str(ipaddress.ip_address(bytes(octets)))


def generate_partial_config(device, asn, loopback0_ip):
    """Generate partial config_db.json for sonic-cfggen merge."""
    cfg = {}

    # Update ASN in DEVICE_METADATA
    cfg['DEVICE_METADATA'] = {
        'localhost': {
            'bgp_asn': str(asn)
        }
    }

    # Loopback0 IP
    cfg['LOOPBACK_INTERFACE'] = {
        'Loopback0': {},
        f'Loopback0|{loopback0_ip}/32': {}
    }

    # Enable IPv6 link-local on fabric interfaces for BGP unnumbered
    cfg['INTERFACE'] = {}
    for port in device['active_ports']:
        cfg['INTERFACE'][port] = {
            'ipv6_use_link_local_only': 'enable'
        }

    return cfg


def generate_vtysh_commands(device, asn, loopback0_ip):
    """Generate vtysh commands for BGP unnumbered configuration."""
    lines = [
        'configure terminal',
        f'router bgp {asn}',
        f'  bgp router-id {loopback0_ip}',
        '  bgp log-neighbor-changes',
        '  no bgp default ipv4-unicast',
        '  no bgp ebgp-requires-policy',
        '  bgp bestpath as-path multipath-relax',
    ]

    # Add each fabric port as an unnumbered peer
    for port in device['active_ports']:
        lines.append(f'  neighbor {port} interface remote-as external')

    # Address family IPv4 unicast
    lines.append('  address-family ipv4 unicast')
    lines.append(f'    network {loopback0_ip}/32')
    lines.append('    redistribute connected')
    for port in device['active_ports']:
        lines.append(f'    neighbor {port} activate')
    lines.append('  exit-address-family')

    lines.append('exit')
    lines.append('end')
    lines.append('write memory')

    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(
        description='Generate eBGP unnumbered underlay config per device')
    parser.add_argument('devices_json',
                        help='Path to devices.json from discover_site.py')
    parser.add_argument('--inventory-dir', required=True,
                        help='Path to NetworkInventoryData repo root')
    parser.add_argument('--site', required=True,
                        help='Site slug (e.g. syd1)')
    parser.add_argument('--output-dir', default='/tmp/day1/underlay',
                        help='Directory for per-device configs')
    args = parser.parse_args()

    inv = args.inventory_dir

    # Load SoT
    asn_plan = load_yaml(os.path.join(inv, 'intent', 'global', 'asn_plan.yml'))
    ip_plan = load_yaml(os.path.join(inv, 'intent', 'global', 'ip_plan.yml'))
    site_data = load_yaml(os.path.join(inv, 'intent', 'sites', args.site, 'site.yml'))

    site_block = site_data['site_block']
    region = site_data.get('region', 'APAC')
    # Extract site code from slug (e.g. 'syd1' → 'syd')
    site_code = ''.join(c for c in args.site if c.isalpha())

    with open(args.devices_json) as f:
        devices = json.load(f)

    os.makedirs(args.output_dir, exist_ok=True)

    for dev in devices:
        dev['region'] = region
        dev['site_code'] = site_code
        dev['plane'] = 'a'  # derived from hostname, currently all plane A

        asn = generate_asn(dev, asn_plan)
        lo0_ip = derive_loopback0(dev, site_block, ip_plan)

        # Partial config_db.json
        cfg = generate_partial_config(dev, asn, lo0_ip)
        cfg_path = os.path.join(args.output_dir, f"{dev['hostname']}.json")
        with open(cfg_path, 'w') as f:
            json.dump(cfg, f, indent=4, sort_keys=True)

        # vtysh commands
        vtysh_path = os.path.join(args.output_dir, f"{dev['hostname']}.vtysh")
        with open(vtysh_path, 'w') as f:
            f.write(generate_vtysh_commands(dev, asn, lo0_ip))

        print(f"  {dev['hostname']:<30} ASN={asn}  lo0={lo0_ip}  ports={len(dev['active_ports'])}")

    print(f"\nGenerated {len(devices)} underlay configs in {args.output_dir}")


if __name__ == '__main__':
    main()
