#!/usr/bin/env python3
"""Generate minimal SONiC Day0 config_db.json for each discovered device.

Takes the reference config_db.json (with full PORT table / hwsku defaults)
and strips it down to a clean baseline:

  KEPT    : DEVICE_METADATA, MGMT_INTERFACE, LOOPBACK_INTERFACE, PORT, FEATURE, VERSIONS
  UPDATED : hostname, device type, mgmt IP, port admin_status per cabling plan
  STRIPPED: BGP_NEIGHBOR, INTERFACE IPs, CRM, AUTO_TECHSUPPORT, LOGGER,
            BANNER_MESSAGE, KDUMP, PASSW_HARDENING, SYSLOG_*, SNMP, etc.
"""

import argparse
import copy
import json
import os
import sys


def generate_day0(reference, device):
    """Return a minimal config_db dict for *device*."""
    ref = reference
    cfg = {}

    # -- DEVICE_METADATA --
    ref_meta = ref['DEVICE_METADATA']['localhost']
    cfg['DEVICE_METADATA'] = {
        'localhost': {
            'hostname':     device['hostname'],
            'hwsku':        ref_meta['hwsku'],
            'platform':     ref_meta['platform'],
            'type':         device['device_type'],
            'buffer_model': ref_meta.get('buffer_model', 'traditional'),
        }
    }

    # -- MGMT_INTERFACE --
    cfg['MGMT_INTERFACE'] = {
        f"eth0|{device['mgmt_ip']}/22": {}
    }

    # -- LOOPBACK_INTERFACE  (placeholder, no IP yet) --
    cfg['LOOPBACK_INTERFACE'] = {
        'Loopback0': {}
    }

    # -- PORT  (from reference hwsku, toggle admin_status) --
    active = set(device['active_ports'])
    cfg['PORT'] = {}
    for port_name in sorted(ref.get('PORT', {}),
                            key=lambda p: int(p.replace('Ethernet', ''))):
        entry = copy.deepcopy(ref['PORT'][port_name])
        entry['admin_status'] = 'up' if port_name in active else 'down'
        entry['mtu'] = '9100'
        # drop dhcp_rate_limit — not needed day0
        entry.pop('dhcp_rate_limit', None)
        cfg['PORT'][port_name] = entry

    # -- FEATURE  (keep reference states) --
    if 'FEATURE' in ref:
        cfg['FEATURE'] = copy.deepcopy(ref['FEATURE'])

    # -- VERSIONS  (SONiC may expect this) --
    if 'VERSIONS' in ref:
        cfg['VERSIONS'] = copy.deepcopy(ref['VERSIONS'])

    return cfg


def main():
    parser = argparse.ArgumentParser(
        description='Generate SONiC Day0 config_db.json per device')
    parser.add_argument('devices_json',
                        help='Path to devices.json from discover_site.py')
    parser.add_argument('reference_config',
                        help='Path to reference config_db.json')
    parser.add_argument('--output-dir', default='/tmp/day1/configs',
                        help='Directory for per-device configs')
    args = parser.parse_args()

    with open(args.devices_json) as f:
        devices = json.load(f)
    with open(args.reference_config) as f:
        reference = json.load(f)

    if 'PORT' not in reference:
        sys.exit("ERROR: Reference config_db.json has no PORT table.")

    os.makedirs(args.output_dir, exist_ok=True)

    for dev in devices:
        cfg = generate_day0(reference, dev)
        out_path = os.path.join(args.output_dir, f"{dev['hostname']}.json")
        with open(out_path, 'w') as f:
            json.dump(cfg, f, indent=4, sort_keys=True)
        active_count = len(dev['active_ports'])
        total_ports = len(cfg['PORT'])
        print(f"  {dev['hostname']:<30} "
              f"{active_count}/{total_ports} ports up  →  {out_path}")

    print(f"\nGenerated {len(devices)} configs in {args.output_dir}")


if __name__ == '__main__':
    main()
