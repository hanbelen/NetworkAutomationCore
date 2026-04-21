import pynetbox
import yaml
import os
import glob

# Load NetBox connection info from environment (Workload Identity)
nb = pynetbox.api(
    url=os.environ.get('NETBOX_URL', 'http://localhost:8000'),
    token=os.environ.get('NETBOX_TOKEN')
)

def sync_all_sites():
    # Get the directory of the current script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Assuming the repo root is one level up from 'scripts/'
    repo_root = os.path.abspath(os.path.join(script_dir, "../../"))
    
    # Path to the data repo (NetworkInventoryData)
    # Note: We assume NetworkInventoryData is a sibling to NetworkAutomationCore or in the same root.
    site_files = glob.glob(os.path.join(repo_root, 'NetworkInventoryData/intent/sites/*/site.yml'))
    
    for sf in site_files:
        with open(sf, 'r') as f:
            site_data = yaml.safe_load(f)
            
        print(f"Syncing Site: {site_data['site_name']}")
        # 1. Sync Site Object
        nb_site = nb.dcim.sites.get(slug=site_data['site_slug'])
        if not nb_site:
            nb_site = nb.dcim.sites.create(
                name=site_data['site_name'], 
                slug=site_data['site_slug']
            )

        # 2. Sync Devices for this site
        device_file = sf.replace('site.yml', 'devices.yml')
        with open(device_file, 'r') as f:
            dev_data = yaml.safe_load(f)
            
        for dev in dev_data['devices']:
            nb_dev = nb.dcim.devices.get(name=dev['name'])
            
            # Ensure Role exists
            nb_role = nb.dcim.device_roles.get(slug=dev['role'])
            if not nb_role:
                nb_role = nb.dcim.device_roles.create(name=dev['role'].capitalize(), slug=dev['role'])

            # Ensure Device Type exists (Placeholder)
            nb_type = nb.dcim.device_types.get(slug='generic-cisco')
            if not nb_type:
                # Assuming Manufacturer 'Cisco' exists
                nb_man = nb.dcim.manufacturers.get(slug='cisco')
                if not nb_man:
                    nb_man = nb.dcim.manufacturers.create(name='Cisco', slug='cisco')
                nb_type = nb.dcim.device_types.create(manufacturer=nb_man.id, model='Generic Cisco', slug='generic-cisco')

            if not nb_dev:
                print(f"  Adding device: {dev['name']}")
                nb_dev = nb.dcim.devices.create(
                    name=dev['name'],
                    site=nb_site.id,
                    device_role=nb_role.id,
                    device_type=nb_type.id,
                    status='active'
                )
            
            # 3. Sync Management IP
            if 'mgmt_ip' in dev:
                # Check if IP already exists
                nb_ip = nb.ipam.ip_addresses.get(address=dev['mgmt_ip'])
                if not nb_ip:
                    print(f"  Adding IP: {dev['mgmt_ip']}")
                    nb_ip = nb.ipam.ip_addresses.create(address=dev['mgmt_ip'], status='active')
                
                # Assign IP to device (Management Interface)
                # Note: In a real scenario, we'd ensure the interface exists first.
                # For this MVP, we'll assume a 'Management0' interface exists or skip direct assignment
                # and just ensure the IP is tracked.
                
if __name__ == "__main__":
    sync_all_sites()
