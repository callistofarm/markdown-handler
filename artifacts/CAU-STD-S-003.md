SecDevOps IaC Reference|| Document ID | CAU-STD-S-003 | Classification | INTERNAL CONFIDENTIAL || Version | 1.0 (Baseline) | Status | APPROVED || Owner | Head of SecDevOps (OPS-03) | Department | OT SecDevOps || Scope | SecDevOps Code Repository | Effective Date | 12 December 2025 || Next Review Date | 12 December 2026 |  |  |Document ControlRevision History| Version | Date | Author | Description of Changes || 1.0 | 12 Dec 2025 | Head of SecDevOps (OPS-03) | Baseline Release. Collation of all system code snippets as the Single Source of Truth. |1. PurposeThe purpose of this document is to provide a centralised reference for all Infrastructure-as-Code (IaC), configuration scripts, and policy definitions used within the Cloud/On-Prem SCADA environment. It serves as the "Gold Image" reference for code reviewers and platform engineers to validate PRs against the approved baseline.2. Operating System & Hardening2.1 Kernel Tuning ConfigurationUse Case: Hardens the Linux kernel for OT security (network stack) and real-time performance (memory/panic handling).Source Document: [CAU-MAN-S-001], [CAU-STD-S-001]File Path: /etc/sysctl.d/99-onprem-tuning.conf# /etc/sysctl.d/99-onprem-tuning.conf
# 1. Network Hardening (ISO 27001 A.8.20)
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.tcp_syncookies = 1
net.ipv4.ip_forward = 1  # Required for Tailscale Subnet Routing

# 2. Memory & Virtualization Tuning
vm.swappiness = 10                  # Prefer RAM over Swap to reduce SSD wear
vm.overcommit_memory = 1            # Required for Redis/Database reliability
fs.inotify.max_user_watches = 524288 # Required for File Watchers (Ignition)

# 3. Reboot behavior
kernel.panic = 10          # Auto-reboot 10s after a kernel panic
2.2 Local Firewall (UFW) BaselineUse Case: Host-based firewall definition ensuring "Default Deny" ingress/egress.Source Document: [CAU-STD-S-001]File Path: Applied via Ansible/Packer Shell Provisioner# Default Deny
ufw default deny incoming
ufw default deny outgoing

# Management & Overlay (Strict)
ufw allow in on tailscale0 to any port 2299 proto tcp # SSH
ufw allow out on tailscale0 to any port 8060 proto tcp # GAN Sync
ufw allow out on tailscale0 to any port 10051 proto tcp # Zabbix Agent
ufw allow out on tailscale0 to any port 443 proto tcp # HTTPS/API

# Critical Infrastructure (Direct)
ufw allow out to any port 443 proto tcp # HTTPS (Licensing/Updates)

# Time
ufw allow out to any port 123 proto udp # NTP
3. Infrastructure Provisioning3.1 Packer Build ConfigurationUse Case: Defines the immutable OS image build process for On-Prem Gateways.Source Document: [CAU-MAN-S-001]File Path: repo/packer/ubuntu-onprem.pkr.hclpacker {
  required_plugins {
    qemu = {
      version = "~> 1.0"
      source  = "[github.com/hashicorp/qemu](https://github.com/hashicorp/qemu)"
    }
    ansible = {
      version = "~> 1.0"
      source  = "[github.com/hashicorp/ansible](https://github.com/hashicorp/ansible)"
    }
  }
}

variable "tailscale_auth_key" {
  type      = string
  sensitive = true
  default   = "${env("TAILSCALE_AUTH_KEY_PACKER")}"
}

source "qemu" "ubuntu-onprem" {
  iso_url           = "[https://releases.ubuntu.com/22.04/ubuntu-22.04.3-live-server-amd64.iso](https://releases.ubuntu.com/22.04/ubuntu-22.04.3-live-server-amd64.iso)"
  iso_checksum      = "file:[https://releases.ubuntu.com/22.04/SHA256SUMS](https://releases.ubuntu.com/22.04/SHA256SUMS)"
  output_directory  = "build/output-onprem"
  shutdown_command  = "echo 'packer' | sudo -S shutdown -P now"
  disk_size         = "20G"
  format            = "qcow2"
  accelerator       = "kvm"
  http_directory    = "http"
  ssh_username      = "ubuntu"
  ssh_password      = "packer-temp-password"
  ssh_timeout       = "20m"
  vm_name           = "onprem-node-v1.img"
  net_device        = "virtio-net"
  disk_interface    = "virtio"
  boot_wait         = "5s"
  boot_command      = [
    "<esc><wait>",
    "c<wait>",
    "linux /casper/vmlinuz --- autoinstall ds=nocloud-net;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/",
    "<enter><wait>",
    "initrd /casper/initrd",
    "<enter><wait>",
    "boot<enter>"
  ]
}

build {
  sources = ["source.qemu.ubuntu-onprem"]

  # 1. System Upgrade & Docker Install
  provisioner "shell" {
    inline = [
      "sudo apt-get update",
      "sudo apt-get upgrade -y",
      "curl -fsSL [https://get.docker.com](https://get.docker.com) | sudo sh",
      "sudo usermod -aG docker ubuntu",
      "sudo systemctl enable docker"
    ]
  }

  # 2. Install Tailscale (Binary Only - No Auth yet)
  provisioner "shell" {
    inline = [
      "curl -fsSL [https://tailscale.com/install.sh](https://tailscale.com/install.sh) | sudo sh",
      "sudo systemctl enable tailscaled"
    ]
  }

  # 3. Security Hardening (UFW & Sysctl)
  provisioner "shell" {
    inline = [
      "sudo ufw default deny incoming",
      "sudo ufw default deny outgoing",
      "sudo ufw allow in on tailscale0 to any port 2299 proto tcp", # Tailscale SSH
      "sudo ufw allow out on tailscale0 to any port 443 proto tcp",
      "sudo ufw allow out on tailscale0 to any port 8060 proto tcp",
      "sudo ufw allow out on eth0 to any port 123 proto udp", # NTP
      "sudo ufw allow out on eth0 to any port 443 proto tcp", # Licensing/Updates
      "sudo ufw enable"
    ]
  }

  # 4. Inject Cloud-Init for First Boot Registration
  provisioner "shell" {
    inline = [
      "sudo mkdir -p /etc/cloud/cloud.cfg.d/",
      "echo 'runcmd:' | sudo tee /etc/cloud/cloud.cfg.d/99-tailscale.cfg",
      "echo ' - tailscale up --authkey=${var.tailscale_auth_key} --ssh --hostname=onprem-{{v1}}' | sudo tee -a /etc/cloud/cloud.cfg.d/99-tailscale.cfg"
    ]
  }
}
3.2 Terraform Backend ConfigurationUse Case: Configures secure remote state storage in Azure.Source Document: [CAU-STD-S-002]File Path: backend.tf# backend.tf
terraform {
  backend "azurerm" {
    resource_group_name  = "rg-terraform-state"
    storage_account_name = "stotsecdevopstfstate"
    container_name       = "tfstate-prod"
    key                  = "core.terraform.tfstate"
    use_oidc             = true
    # Access Key NOT used. Authenticates via Azure AD Workload Identity.
  }
}
4. Network Policy & Segmentation4.1 Zero Trust ACLs (HuJSON)Use Case: Defines micro-segmentation rules for the Tailnet.Source Document: [CAU-MAN-S-001], [CAU-STD-S-001]File Path: repo/tailscale/policy.hujson{
  // Groups
  "groups": {
    "group:admins": ["alice@company.com", "bob@company.com"],
    "group:bots": ["ci-runner@company.com"]
  },
  // Tag Definitions
  "tagOwners": {
    "tag:ignition-onprem": ["group:admins"],
    "tag:ignition-cloud": ["group:admins"],
    "tag:provisioning": ["group:bots"], // Used by Packer/ZTP
    "tag:zabbix-proxy": ["group:admins"]
  },
  // Access Control Lists (Allow Rules)
  "acls": [
    // 1. On-Prem to Cloud (GAN Sync)
    {
      "action": "accept",
      "src": ["tag:ignition-onprem"],
      "dst": ["tag:ignition-cloud:8060"]
    },
    // 2. Monitoring (Zabbix)
    {
      "action": "accept",
      "src": ["tag:ignition-onprem"],
      "dst": ["tag:zabbix-proxy:10051"]
    }
  ],
  // SSH Access Policies (ISO 27001 A.5.15)
  "ssh": [
    {
      "action": "check", // Enforces Re-Authentication (MFA)
      "src": ["group:admins"],
      "dst": ["tag:ignition-onprem"],
      "users": ["ubuntu", "root"]
    }
  ],
  // Mandatory Tests
  "tests": [
    { "src": "tag:ignition-onprem", "accept": ["tag:ignition-cloud:8060"] },
    { "src": "tag:ignition-onprem", "deny": ["tag:ignition-onprem:*"] },
    { "src": "tag:provisioning", "deny": ["tag:ignition-cloud:*"] }
  ]
}
5. CI/CD & Automation5.1 Provisioning Key RotationUse Case: Automates rotation of the Tailscale Auth Key used by Packer.Source Document: [CAU-MAN-S-001]File Path: .github/workflows/rotate-keys.ymlname: Rotate Tailscale Provisioning Keys
on:
  schedule:
    - cron: '0 0 1 * *' # Monthly at midnight
  workflow_dispatch:
jobs:
  rotate-keys:
    runs-on: ubuntu-latest
    steps:
      - name: Generate New Key
        id: tailscale
        uses: tailscale/github-action@v2
        with:
          oauth-client-id: ${{ secrets.TS_OAUTH_CLIENT_ID }}
          oauth-secret: ${{ secrets.TS_OAUTH_SECRET }}
          tags: tag:provisioning
          expiry: 90 # Days (Overlap allowed, rotated every 30)
          ephemeral: false
          preauthorized: false # Requires Admin Approval in Portal
      - name: Update GitHub Secret (Packer)
        uses: gliech/create-github-secret-action@v1
        with:
          name: TAILSCALE_AUTH_KEY_PACKER
          value: ${{ steps.tailscale.outputs.authkey }}
          pa_token: ${{ secrets.GH_PAT_ADMIN }}
      - name: Audit Log
        run: echo "::notice::Successfully rotated Packer Auth Key. Next build will use new credential."
5.2 Container Security Scan (Trivy)Use Case: Enforces vulnerability scanning in the CI pipeline.Source Document: [CAU-STD-S-002]File Path: .github/workflows/security-scan.ymlname: Security Scan
on: [pull_request]
jobs:
  trivy-scan:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v3
      - name: Build Image
        run: docker build -t test-image:${{ github.sha }} .
      - name: Run Trivy vulnerability scanner
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: 'test-image:${{ github.sha }}'
          format: 'table'
          exit-code: '1' # Fail pipeline on detection
          ignore-unfixed: true
          vuln-type: 'os,library'
          severity: 'CRITICAL,HIGH'
6. Workload Orchestration6.1 Docker Compose (SCADA Gateway)Use Case: Defines the production container stack for On-Prem Gateways.Source Document: [CAU-MAN-S-001]File Path: repo/docker-compose.ymlversion: '3.8'
services:
  # --- SCADA Gateway ---
  scada-gateway:
    image: ghcr.io/my-org/ignition-core:${TAG:-8.3.0}
    hostname: ${GATEWAY_HOSTNAME}
    restart: unless-stopped
    ulimits:
      rtprio: 99 # Real-time priority for deterministic scanning
    mem_limit: "8g"
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
    ports:
      - 127.0.0.1:8088:8088 # Bind to localhost ONLY. Exposed via Tailscale proxy if needed.
    environment:
      IGNITION_EDITION: edge # Ignition Edge License
      GATEWAY_ADMIN_PASSWORD: ${GATEWAY_ADMIN_PASSWORD} # Injected via Portainer Secret
      TZ: Australia/Brisbane
    volumes:
      # Mutable State (Logs, Cache)
      - gateway-data:/usr/local/bin/ignition/data
      # Immutable Logic (Projects) - Read Only
      - ./projects:/usr/local/bin/ignition/data/config/resources/external/projects:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8088/StatusPing"]
      interval: 30s
      timeout: 10s
      retries: 3
  # --- Monitoring Agent ---
  zabbix-agent:
    image: zabbix/zabbix-agent2:alpine-6.4-latest
    restart: unless-stopped
    privileged: true # Required for hardware metrics
    network_mode: host # Required to see host network interfaces
    environment:
      ZBX_HOSTNAME: ${GATEWAY_HOSTNAME}
      ZBX_SERVER_HOST: 100.x.y.z # Tailscale IP of Zabbix Proxy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
volumes:
  gateway-data:
7. Emergency Scripts7.1 Break Glass ProcedureUse Case: Provides temporary egress for commissioning/emergency diagnostics.Source Document: [CAU-MAN-S-001], [CAU-SOP-S-005]File Path: /usr/local/bin/break-glass.sh#!/bin/bash
# /usr/local/bin/break-glass.sh
# Purpose: Enable temporary egress for commissioning.
# Trigger: sudo /usr/local/bin/break-glass.sh
set -e
LOG_FACILITY="local0.notice"
DURATION="30 minutes"

# 1. Audit Logging (ISO 27001 A.8.15)
logger -p $LOG_FACILITY "SECURITY_EVENT: BREAK_GLASS_ACTIVATED by user $(whoami). Egress enabled for $DURATION."

# 2. Apply Permissive Rules
ufw allow out to any port 80 proto tcp
ufw allow out to any port 443 proto tcp
ufw reload
echo "WARNING: Commissioning Mode Active. HTTP/HTTPS Egress enabled for $DURATION."

# 3. Schedule Reversion
# Requires 'at' package installed
echo "ufw delete allow out to any port 80 proto tcp; ufw delete allow out to any port 443 proto tcp; ufw reload; logger -p $LOG_FACILITY 'SECURITY_EVENT: BREAK_GLASS_ENDED. Rules reverted.'" | at now + 30 minutes
echo "Reversion scheduled."
