# rhpds.cohesity_data_protection

Ansible collection for Cohesity Data Protection automation.

## Installation

Add to your `requirements.yml`:

```yaml
collections:
  - name: rhpds.cohesity_data_protection
    source: https://github.com/rhpds/cohesity-data-protection-automation.git
    type: git
```

Then install:

```bash
ansible-galaxy collection install -r requirements.yml
```

## Roles

### bootstrap_cluster

Bootstraps an OpenShift cluster with Cohesity Data Protection, including:

- OpenShift Virtualization operator installation
- Cohesity VE appliance VM deployment
- Cohesity cluster initialization
- Backup integration configuration

```yaml
- hosts: localhost
  roles:
    - role: rhpds.cohesity_data_protection.bootstrap_cluster
```

#### Variables

| Variable | Default | Description |
|---|---|---|
| `cohesity_deploy_golden_ns` | `cohesity-golden` | Namespace for the golden Cohesity VE image |
| `cohesity_deploy_golden_dv` | `cohesity-golden` | DataVolume name for the golden image |
| `cohesity_deploy_lab_ns` | `cohesity-lab-vm` | Namespace for the attendee lab VMs |
