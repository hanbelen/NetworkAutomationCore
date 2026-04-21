# Network Automation Core

Automation engine for SONiC-VS fabric provisioning and management.

## Contents

```
scripts/
  discover_site.py         # fping discovery + hostname mapping from SoT
  generate_day0_config.py  # Minimal config_db.json generator per device
  validate_intent.py       # Intent YAML validation (ASN/VNI/loopback uniqueness)
  netbox_sync.py           # Sync intent data to NetBox

playbooks/
  day0_provision.yml       # Push day0 config + set admin password
  verify_interfaces.yml    # Check cabled port status
  deploy_underlay.yml      # Deploy eBGP underlay config
  bootstrap_fabric.yml     # Kernel modules, FRR install, sysctl

templates/
  frr.conf.j2             # FRR configuration template (legacy)

references/
  config_db.json           # Reference SONiC-VS config (Force10-S6000 hwsku)

Jenkinsfile.day1           # Thin pipeline entry point → calls sre-lib shared library
```

## Day1 Pipeline

The `Jenkinsfile.day1` calls `day1Provision()` from the Jenkins shared library. Stages:

1. **Discover** — fping site mgmt range, map IPs to hostnames via ip_plan
2. **Generate** — strip reference config_db.json to minimal day0 (only cabled ports)
3. **Apply** — push config + reload (or dry-run)
4. **Verify** — report interface up/down status

## Dependencies

- Python: `pyyaml`
- System: `fping`, `ansible-core`
