#!/usr/bin/env python3
"""Discover SONiC-VS devices on a site management network via fping.

Reads all configuration from the NetworkInventoryData SoT:
  - intent/sites/<site>/site.yml       → site_block
  - intent/global/ip_plan.yml          → mgmt prefix, role offsets
  - intent/global/network_defaults.yml → device profiles, credentials

The management CIDR is derived: first /<mgmt.prefix> of the site_block.
Hostnames are derived: {site}-a-p{pod}-{role}-{device_id:02d}

Outputs:
  devices.json         — structured device list
  inventory.yml        — Ansible-compatible dynamic inventory
  discovery_report.txt — human-readable summary
"""

import argparse
import ipaddress
import json
import os
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("ERROR: pyyaml is required.  pip install pyyaml")


# ── SoT loaders ─────────────────────────────────────────────────────

def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def discover_sites(inventory_dir):
    """Return dict of {site_slug: site.yml data} by scanning intent/sites/."""
    sites_dir = os.path.join(inventory_dir, 'intent', 'sites')
    sites = {}
    if not os.path.isdir(sites_dir):
        return sites
    for name in sorted(os.listdir(sites_dir)):
        site_file = os.path.join(sites_dir, name, 'site.yml')
        if os.path.isfile(site_file):
            sites[name] = load_yaml(site_file)
    return sites


def derive_mgmt_cidr(site_block, mgmt_prefix):
    """First /<mgmt_prefix> of the site block."""
    net = ipaddress.ip_network(site_block, strict=False)
    return str(ipaddress.ip_network(
        f"{net.network_address}/{mgmt_prefix}", strict=False))


# ── Discovery ────────────────────────────────────────────────────────

def run_fping(cidr):
    result = subprocess.run(
        ['fping', '-a', '-g', cidr],
        capture_output=True, text=True,
    )
    return [ip.strip() for ip in result.stdout.strip().splitlines() if ip.strip()]


def classify_ip(site_slug, ip, role_offsets, gateway_offset, border_dci=None):
    octets = ip.split('.')
    pod = int(octets[2])
    host = int(octets[3])

    if host == gateway_offset:
        return None

    # Check DCI border range first (higher offset takes precedence)
    if border_dci and host >= border_dci['offset']:
        role = 'brl'
        device_id = (host - border_dci['offset']) + border_dci['id_start']
        return {
            'hostname':  f"{site_slug}-a-p{pod}-{role}-{device_id:02d}",
            'role':      role,
            'pod':       pod,
            'device_id': device_id,
            'mgmt_ip':   ip,
        }

    # Match host octet against role offset ranges
    sorted_roles = sorted(role_offsets.items(), key=lambda kv: kv[1])
    role = None
    base = None
    for i, (r, off) in enumerate(sorted_roles):
        ceiling = sorted_roles[i + 1][1] if i + 1 < len(sorted_roles) else 256
        if off <= host < ceiling:
            role = r
            base = off
            break

    if role is None:
        return None

    device_id = host - base
    if device_id < 1:
        return None

    return {
        'hostname':  f"{site_slug}-a-p{pod}-{role}-{device_id:02d}",
        'role':      role,
        'pod':       pod,
        'device_id': device_id,
        'mgmt_ip':   ip,
    }


def enrich_device(dev, profiles, border_overrides, border_pod_id):
    """Add device_type and active_ports from device profiles."""
    role = dev['role']
    pod = dev['pod']

    # Check for border pod override
    if pod == border_pod_id and role in border_overrides:
        profile = border_overrides[role]
    elif role in profiles:
        profile = profiles[role]
    else:
        profile = {}

    dev['device_type'] = profile.get('device_type', 'LeafRouter')
    dev['active_ports'] = profile.get('active_ports', [])
    return dev


# ── Output builders ────────────────────────────────────────��─────────

def build_inventory(devices, site_slug, sonic_defaults):
    hosts = {}
    for d in devices:
        hosts[d['hostname']] = {
            'ansible_host': d['mgmt_ip'],
            'role':         d['role'],
            'pod':          d['pod'],
            'device_id':    d['device_id'],
            'device_type':  d['device_type'],
            'active_ports': d['active_ports'],
        }
    return {
        'all': {
            'vars': {
                'ansible_user':            sonic_defaults.get('ansible_user', 'admin'),
                'ansible_password':        sonic_defaults.get('ansible_password', 'YourPaSsWoRd'),
                'ansible_connection':      sonic_defaults.get('ansible_connection', 'ssh'),
                'ansible_become':          sonic_defaults.get('ansible_become', True),
                'ansible_become_method':   sonic_defaults.get('ansible_become_method', 'sudo'),
                'ansible_become_password': sonic_defaults.get('ansible_password', 'YourPaSsWoRd'),
                'ansible_ssh_common_args': '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null',
            },
            'children': {
                site_slug: {'hosts': hosts},
            },
        },
    }


def build_report(devices, alive_ips, site_slug, cidr):
    hdr = (
        f"Discovery Report: {site_slug} ({cidr})\n"
        f"{'=' * 70}\n"
        f"IPs responding : {len(alive_ips)}\n"
        f"Devices mapped : {len(devices)}\n"
        f"Skipped        : {len(alive_ips) - len(devices)}\n\n"
        f"{'Hostname':<30} {'Role':<6} {'Pod':<5} "
        f"{'Mgmt IP':<18} {'Active Ports'}\n"
        f"{'-'*30} {'-'*6} {'-'*5} {'-'*18} {'-'*30}\n"
    )
    rows = []
    for d in sorted(devices, key=lambda x: (x['pod'], x['role'], x['device_id'])):
        ports = ', '.join(d['active_ports'])
        rows.append(
            f"{d['hostname']:<30} {d['role']:<6} p{d['pod']:<4} "
            f"{d['mgmt_ip']:<18} {ports}"
        )
    return hdr + '\n'.join(rows) + '\n'


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Discover SONiC-VS devices on a site management network')
    parser.add_argument('site', help='Target site slug (e.g. syd1, mel1)')
    parser.add_argument('--inventory-dir', required=True,
                        help='Path to NetworkInventoryData repo root')
    parser.add_argument('--output-dir', default='/tmp/day1',
                        help='Directory for output files')
    args = parser.parse_args()

    inv = args.inventory_dir

    # ── Load SoT ──
    sites = discover_sites(inv)
    if args.site not in sites:
        available = ', '.join(sites.keys()) or '(none)'
        sys.exit(f"ERROR: Site '{args.site}' not found in SoT. Available: {available}")

    site_data = sites[args.site]
    ip_plan = load_yaml(os.path.join(inv, 'intent', 'global', 'ip_plan.yml'))
    defaults = load_yaml(os.path.join(inv, 'intent', 'global', 'network_defaults.yml'))

    site_block = site_data['site_block']
    mgmt_prefix = ip_plan['mgmt']['prefix']
    gateway_offset = ip_plan['mgmt']['gateway_offset']
    role_offsets = ip_plan['role_offsets']
    border_pod_id = ip_plan['border_pod_id']
    border_dci = ip_plan.get('border_dci')
    profiles = defaults.get('device_profiles', {})
    border_overrides = defaults.get('border_pod_overrides', {})
    sonic_defaults = defaults.get('sonic_defaults', {})

    cidr = derive_mgmt_cidr(site_block, mgmt_prefix)

    # ── Discover ──
    os.makedirs(args.output_dir, exist_ok=True)
    print(f"[discover] fping {cidr} ...")
    alive_ips = run_fping(cidr)
    if not alive_ips:
        sys.exit("ERROR: No IPs responded. Check network connectivity.")

    devices = []
    for ip in alive_ips:
        dev = classify_ip(args.site, ip, role_offsets, gateway_offset, border_dci)
        if dev:
            enrich_device(dev, profiles, border_overrides, border_pod_id)
            devices.append(dev)

    if not devices:
        sys.exit("ERROR: Could not classify any responding IPs.")

    # ── Write outputs ──
    with open(os.path.join(args.output_dir, 'devices.json'), 'w') as f:
        json.dump(devices, f, indent=2)

    with open(os.path.join(args.output_dir, 'inventory.yml'), 'w') as f:
        yaml.dump(build_inventory(devices, args.site, sonic_defaults), f,
                  default_flow_style=False)

    report = build_report(devices, alive_ips, args.site, cidr)
    with open(os.path.join(args.output_dir, 'discovery_report.txt'), 'w') as f:
        f.write(report)

    print(report)


if __name__ == '__main__':
    main()
