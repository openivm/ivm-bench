resource "azurerm_public_ip" "bastion" {
  ip_tags = { FirstPartyUsage = "/NonProd" }
  tags    = var.tags
}

# Azure network management owns the effective rule set and subnet attachment.
# Keep the module's defaults for first creation, but never revert those owners.
resource "azurerm_network_security_group" "runners" {
  lifecycle {
    ignore_changes = [security_rule]
  }
}

resource "azurerm_subnet_network_security_group_association" "runners" {
  lifecycle {
    ignore_changes = [network_security_group_id]
  }
}
