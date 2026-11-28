# GitHub self-hosted runner on Azure

Thin wrapper around [`KangarooKube/terraform-infrastructure-modules//modules/github-runner/azure-vmss`](https://github.com/KangarooKube/terraform-infrastructure-modules/tree/main/modules/github-runner/azure-vmss).

```bash
src/.scripts/deploy-gh-runner.sh apply     # ~15 min end-to-end
src/.scripts/deploy-gh-runner.sh destroy
src/.scripts/deploy-gh-runner.sh plan
```

Plan and apply use the authenticated `gh` CLI to resolve the current Linux x64 runner
release for the target repository. That version is embedded in cloud-init for new
and reimaged instances; apply also mints a fresh registration token.
Use the canonical `GH_REPO=https://github.com/openivm/ivm-bench` in `.env`. The GitHub API redirects the old repository name, but runner registration does not.

Every `apply` reimages all VMSS instances so they re-run cloud-init and
re-register with the freshly minted token. The instances reboot simultaneously,
so expect a brief runner outage mid-apply.

The wrapper installs a Terraform override into the pinned module after downloading
it. This keeps the Bastion public IP's `FirstPartyUsage=/NonProd` tag and leaves
existing subnet NSG attachment and NSG rules under Azure network management.
Run plans through the wrapper so they use the same network configuration as apply.

## Override VMSS sizing

Set in `.env`:

```bash
TF_VAR_location=eastus2
TF_VAR_instance_sku=Standard_E16as_v4
TF_VAR_instance_count=4
```

## SSH

```bash
RG=$(grep ^TF_RESOURCE_GROUP .env | cut -d= -f2)
INSTANCE_ID=$(az vmss list-instances -g "$RG" -n ivm-bench-vmss-runner --query '[0].id' -o tsv)
az network bastion ssh -g "$RG" -n ivm-bench-bas \
  --target-resource-id "$INSTANCE_ID" \
  --auth-type ssh-key --username azureuser --ssh-key ~/.ssh/id_ed25519
```

Or read `ssh_via_bastion_hint` / `tunnel_via_bastion_hint` from `terraform apply` output.

## Bump pinned module

Edit `?ref=<SHA>` in `main.tf`, then `deploy-gh-runner.sh plan`.
