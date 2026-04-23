#!/usr/bin/env python3
"""Generate minimal SONiC Day0 config_db.json for each discovered device.

Takes default.json (clean baseline with PORT table and empty interfaces)
and injects per-device values:
  - DEVICE_METADATA: hostname, mac, type (LeafRouter/SpineRouter)
  - MGMT_INTERFACE:  eth0 with management IP and gateway
"""

import argparse
import copy
import json
import os
import sys


def generate_day0(default, device):
    """Return a config_db dict for *device* based on default.json."""
    cfg = copy.deepcopy(default)

    # -- DEVICE_METADATA --
    cfg['DEVICE_METADATA']['localhost']['hostname'] = device['hostname']
    cfg['DEVICE_METADATA']['localhost']['type'] = device['device_type']

    # -- MGMT_INTERFACE --
    mgmt_prefix = device.get('mgmt_prefix', 22)
    mgmt_gateway = device.get('mgmt_gateway', '')
    cfg['MGMT_INTERFACE'] = {
        f"eth0|{device['mgmt_ip']}/{mgmt_prefix}": {
            'gwaddr': mgmt_gateway,
        }
    }

    return cfg


def main():
    parser = argparse.ArgumentParser(
        description='Generate SONiC Day0 config_db.json per device')
    parser.add_argument('devices_json',
                        help='Path to devices.json from discover_site.py')
    parser.add_argument('default_config',
                        help='Path to default.json baseline')
    parser.add_argument('--output-dir', default='/tmp/day1/configs',
                        help='Directory for per-device configs')
    args = parser.parse_args()

    with open(args.devices_json) as f:
        devices = json.load(f)
    with open(args.default_config) as f:
        default = json.load(f)

    os.makedirs(args.output_dir, exist_ok=True)

    for dev in devices:
        cfg = generate_day0(default, dev)
        out_path = os.path.join(args.output_dir, f"{dev['hostname']}.json")
        with open(out_path, 'w') as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
        print(f"  {dev['hostname']:<30} → {out_path}")

    print(f"\nGenerated {len(devices)} configs in {args.output_dir}")


if __name__ == '__main__':
    main()
