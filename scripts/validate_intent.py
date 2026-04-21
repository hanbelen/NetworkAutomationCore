import yaml
import sys
import ipaddress
from pathlib import Path

def validate_yaml(file_path):
    try:
        with open(file_path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Error: Failed to parse {file_path}: {e}")
        sys.exit(1)

def validate_intent(site_dir):
    site_path = Path(site_dir)
    devices = validate_yaml(site_path / 'devices.yml')['devices']
    tenants = validate_yaml(site_path / 'tenants.yml')['tenants']
    
    # 1. Validate Unique ASNs for Leafs (EBGP to the host requirement)
    leaf_asns = [d['asn'] for d in devices if d['role'] == 'leaf']
    if len(leaf_asns) != len(set(leaf_asns)):
        print("Error: Duplicate ASNs found across Leaf switches.")
        sys.exit(1)

    # 2. Validate VNI Uniqueness
    l3_vnis = [t['l3vni'] for t in tenants]
    l2_vnis = [v['vni'] for t in tenants for v in t['vlans']]
    all_vnis = l3_vnis + l2_vnis
    if len(all_vnis) != len(set(all_vnis)):
        print("Error: Duplicate VNIs detected in tenants.yml.")
        sys.exit(1)

    # 3. Validate IPv4 Uniqueness for Loopbacks
    loopbacks = [d['loopback0'].split('/')[0] for d in devices]
    if len(loopbacks) != len(set(loopbacks)):
        print("Error: Duplicate Loopback0 IP addresses found.")
        sys.exit(1)

    print("SUCCESS: Intent validation passed.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python validate_intent.py <site_directory>")
        sys.exit(1)
    validate_intent(sys.argv[1])
