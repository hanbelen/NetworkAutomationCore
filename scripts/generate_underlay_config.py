#!/usr/bin/env python3
"""Generate eBGP unnumbered underlay config from the SoT.

Reads directly from NetworkInventoryData:
  - intent/sites/<site>/devices.yml  → device list
  - intent/sites/<site>/site.yml     → site_block, region
  - intent/global/asn_plan.yml       → ASN generation
  - intent/global/ip_plan.yml        → loopback + mgmt addressing
  - intent/global/network_defaults.yml → device profiles, credentials

Produces per-device:
  <hostname>.json    — partial config_db.json (merge via sonic-cfggen -j --write-to-db)
  <hostname>.vtysh   — vtysh commands for BGP unnumbered peering

Also produces:
  inventory.yml      — Ansible inventory with mgmt IPs and host vars
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


# ── Address derivation ───────────────────────────────────────────────

def derive_mgmt_ip(device, site_block, ip_plan):
    """Derive management IP from the IP plan.

    mgmt_ip = site_base + pod (3rd octet) + role_offset + device_id (4th octet)
    """
    net = ipaddress.ip_network(site_block, strict=False)
    octets = list(net.network_address.packed)

    role_offset = ip_plan['role_offsets'].get(device['role'], 0)
    octets[2] = device['pod']
    octets[3] = role_offset + device['id']

    return str(ipaddress.ip_address(bytes(octets)))


def derive_loopback0(device, site_block, ip_plan):
    """Derive Loopback0 IP from the IP plan.

    lo0 = site_base + lo0_offset (2nd octet) + pod (3rd octet) + role_offset + device_id (4th octet)
    """
    net = ipaddress.ip_network(site_block, strict=False)
    octets = list(net.network_address.packed)

    lo0_offset = ip_plan['functions']['loopback_lo0']['offset']
    role_offset = ip_plan['role_offsets'].get(device['role'], 0)

    octets[2] = lo0_offset + device['pod']
    octets[3] = role_offset + device['id']

    return str(ipaddress.ip_address(bytes(octets)))


def generate_asn(device, asn_plan, region, site_code):
    """Derive 32-bit ASN.  Format: 42 R SS P PR II"""
    mappings = asn_plan['asn']['mappings']

    region_code = mappings['regions'].get(region, 0)
    s_code = mappings['sites'].get(site_code, '00')
    plane_code = mappings['planes'].get('a', 1)  # plane A for now
    role_code = mappings['roles'].get(device['role'], 0)
    pod_role = str(device['pod']) + str(role_code)

    asn_str = (
        "42"
        + str(region_code)
        + str(s_code).zfill(2)
        + str(plane_code)
        + str(pod_role).zfill(2)
        + str(device['id']).zfill(2)
    )
    return int(asn_str)


# ── Active ports ─────────────────────────────────────────────────────

def get_active_ports(device, profiles, border_overrides, border_pod_id):
    """Return active port list for a device based on its role and pod."""
    role = device['role']
    pod = device['pod']

    if pod == border_pod_id and role in border_overrides:
        return border_overrides[role].get('active_ports', [])
    return profiles.get(role, {}).get('active_ports', [])


# ── Config generators ────────────────────────────────────────────────

def generate_partial_config(device, asn, loopback0_ip, active_ports):
    """Partial config_db.json for sonic-cfggen merge."""
    cfg = {
        'DEVICE_METADATA': {
            'localhost': {
                'bgp_asn': str(asn)
            }
        },
        'LOOPBACK_INTERFACE': {
            'Loopback0': {},
            f'Loopback0|{loopback0_ip}/32': {}
        },
        'INTERFACE': {}
    }

    for port in active_ports:
        cfg['INTERFACE'][port] = {
            'ipv6_use_link_local_only': 'enable'
        }

    return cfg


def generate_vtysh_commands(device, asn, loopback0_ip, active_ports):
    """vtysh commands for BGP unnumbered.

    Removes any existing BGP instance first (factory ASN may differ),
    then configures the new one. No leading spaces — vtysh rejects them.
    """
    lines = [
        'configure terminal',
        'no router bgp',
        f'router bgp {asn}',
        f'bgp router-id {loopback0_ip}',
        'bgp log-neighbor-changes',
        'no bgp default ipv4-unicast',
        'no bgp ebgp-requires-policy',
        'bgp bestpath as-path multipath-relax',
    ]

    for port in active_ports:
        lines.append(f'neighbor {port} interface remote-as external')

    lines.append('address-family ipv4 unicast')
    lines.append(f'network {loopback0_ip}/32')
    lines.append('redistribute connected')
    for port in active_ports:
        lines.append(f'neighbor {port} activate')
    lines.append('exit-address-family')

    lines.append('exit')
    lines.append('end')
    lines.append('write memory')

    return '\n'.join(lines) + '\n'


# ── Inventory builder ────────────────────────────────────────────────

def build_inventory(devices_enriched, site_slug, sonic_defaults):
    """Build Ansible inventory from enriched device list."""
    hosts = {}
    for d in devices_enriched:
        hosts[d['name']] = {
            'ansible_host': d['mgmt_ip'],
            'role':         d['role'],
            'pod':          d['pod'],
            'device_id':    d['id'],
            'active_ports': d['active_ports'],
        }

    return {
        'all': {
            'vars': {
                'ansible_user':            sonic_defaults.get('ansible_user', 'admin'),
                'ansible_password':        sonic_defaults.get('ansible_password', 'admin'),
                'ansible_connection':      sonic_defaults.get('ansible_connection', 'ssh'),
                'ansible_become':          sonic_defaults.get('ansible_become', True),
                'ansible_become_method':   sonic_defaults.get('ansible_become_method', 'sudo'),
                'ansible_become_password': sonic_defaults.get('ansible_password', 'admin'),
                'ansible_ssh_common_args': '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null',
                'ansible_python_interpreter': '/usr/bin/python3',
            },
            'children': {
                site_slug: {'hosts': hosts},
            },
        },
    }


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Generate eBGP unnumbered underlay config from SoT')
    parser.add_argument('--inventory-dir', required=True,
                        help='Path to NetworkInventoryData repo root')
    parser.add_argument('--site', required=True,
                        help='Site slug (e.g. syd1)')
    parser.add_argument('--output-dir', default='/tmp/day1/underlay',
                        help='Directory for per-device configs')
    args = parser.parse_args()

    inv = args.inventory_dir

    # Load SoT
    site_data = load_yaml(os.path.join(inv, 'intent', 'sites', args.site, 'site.yml'))
    devices_raw = load_yaml(os.path.join(inv, 'intent', 'sites', args.site, 'devices.yml'))
    asn_plan = load_yaml(os.path.join(inv, 'intent', 'global', 'asn_plan.yml'))
    ip_plan = load_yaml(os.path.join(inv, 'intent', 'global', 'ip_plan.yml'))
    defaults = load_yaml(os.path.join(inv, 'intent', 'global', 'network_defaults.yml'))

    site_block = site_data['site_block']
    region = site_data.get('region', 'APAC')
    site_code = ''.join(c for c in args.site if c.isalpha())

    profiles = defaults.get('device_profiles', {})
    border_overrides = defaults.get('border_pod_overrides', {})
    border_pod_id = ip_plan.get('border_pod_id', 0)
    sonic_defaults = defaults.get('sonic_defaults', {})

    devices = devices_raw['devices']

    os.makedirs(args.output_dir, exist_ok=True)

    # Enrich devices with derived values
    for dev in devices:
        dev['mgmt_ip'] = derive_mgmt_ip(dev, site_block, ip_plan)
        dev['active_ports'] = get_active_ports(dev, profiles, border_overrides, border_pod_id)

        asn = generate_asn(dev, asn_plan, region, site_code)
        lo0_ip = derive_loopback0(dev, site_block, ip_plan)

        # Partial config_db.json
        cfg = generate_partial_config(dev, asn, lo0_ip, dev['active_ports'])
        cfg_path = os.path.join(args.output_dir, f"{dev['name']}.json")
        with open(cfg_path, 'w') as f:
            json.dump(cfg, f, indent=4, sort_keys=True)

        # vtysh commands
        vtysh_path = os.path.join(args.output_dir, f"{dev['name']}.vtysh")
        with open(vtysh_path, 'w') as f:
            f.write(generate_vtysh_commands(dev, asn, lo0_ip, dev['active_ports']))

        print(f"  {dev['name']:<30} ASN={asn}  lo0={lo0_ip}  mgmt={dev['mgmt_ip']}")

    # Write Ansible inventory
    inv_path = os.path.join(args.output_dir, 'inventory.yml')
    with open(inv_path, 'w') as f:
        yaml.dump(build_inventory(devices, args.site, sonic_defaults), f,
                  default_flow_style=False)

    print(f"\nGenerated {len(devices)} underlay configs + inventory in {args.output_dir}")


if __name__ == '__main__':
    main()
