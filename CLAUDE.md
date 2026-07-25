# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A logging/management proxy for Ollama + ComfyUI, running as a systemd
service on `worker` and forwarding to a GPU host (`gpu-agent` sidecar).
See `README.md` for the full architecture.

## Deployment is owned by LabControl

Production deployment is split across two LabControl Ansible roles in the
`labcontrol` repo:
- `app_llmproxy` (`app/seed_playbooks/roles/app_llmproxy/`) — the
  `llmproxy.service` systemd unit + `llmproxy-dashboard` Docker container
  on `worker`. Both are built from a single checkout of this repo
  (`dashboard/Dockerfile` needs the full repo root as build context, not
  an isolated `dashboard/` subtree).
- `svc_gpu_agent` (`app/seed_playbooks/roles/svc_gpu_agent/`) — the
  `gpu-agent.service` sidecar on the GPU host (`dana`).

`scripts/install.sh` in this repo is the reference the roles were built
against, but it isn't what actually runs production — the Ansible roles
are. The GPU host address is `LLMPROXY_GPU_HOST`, currently
`gpuhost.internal.familie-frischkorn.de` (the plain hostname `dana` does
not resolve on `worker` — a real DNS gap, not a typo, worked around via
the FQDN).
