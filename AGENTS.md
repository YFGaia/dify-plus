# AGENTS.md

## Project Overview

`dify-plus` is a fork of [langgenius/dify](https://github.com/langgenius/dify) with a set of business-oriented extensions. The former standalone admin center (`/admin`, gin-vue-admin) was decommissioned on 2026-07-05 (change `p5-admin-decommission`, tag `pre-admin-removal`); its management capabilities now live in the Console under `/system-manage-extend/*`.

- Upstream comparison baseline for this branch: upstream tag `1.15.0`
- Current working branch baseline: local tag `fork-merged-1.15.0`
- Main code areas:
  - `/api`: Dify backend plus forked backend extensions
  - `/web`: Dify console/web frontend plus forked UI features (including system management pages)
  - `/docker`: integrated deployment for Dify-Plus services
  - `/docs`: architecture and fork feature documentation

## Working Baseline

- When analyzing fork changes, compare against upstream tag `1.15.0`.
- Keep fork-specific logic discoverable. Existing code usually uses `extend` in file names, model names, migration names, comments, or helper functions.
- Preserve the separation between:
  - Upstream Dify runtime capability
  - Forked enterprise features (including Console system management under `system-manage-extend`)

## Backend Workflow

- Read `/Users/liuxingwang/go/src/dify-plus/api/AGENTS.md` before editing backend code.
- Run backend commands through `uv run --project api <command>`.
- Fork-specific schema changes live in `/Users/liuxingwang/go/src/dify-plus/api/migrations_extend`.
- Fork-specific configuration is wired through `/Users/liuxingwang/go/src/dify-plus/api/configs/extend/__init__.py`.

## Frontend Workflow

- Read `/Users/liuxingwang/go/src/dify-plus/web/AGENTS.md` before editing frontend code.
- Web fork features mostly live under:
  - `web/service/*extend*`
  - `web/app/components/**/*extend*`
  - `web/app/(commonLayout)/**`
  - `web/app/signin/**`

## Deployment Workflow

- `origin` is the fork repository.
- `upstream` should point to `https://github.com/langgenius/dify`.
- The integrated compose file for the fork is `/Users/liuxingwang/go/src/dify-plus/docker/docker-compose.dify-plus.yaml`.

## Documentation

Start from these docs when you need the full fork context:

- **Python**: Keep type hints on functions and attributes, and implement relevant special methods (e.g., `__repr__`, `__str__`). Prefer `TypedDict` over `dict` or `Mapping` for type safety and better code documentation.
- **TypeScript**: Use the strict config, run `pnpm check` for formatting, Oxlint, ESLint non-code checks, and type checking, and avoid `any` types.

- `/Users/liuxingwang/go/src/dify-plus/docs/README.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/整体架构图.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/与上游差异总表.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/二开功能详解-后端与数据层.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/二开功能详解-Web与管理后台.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/二开部署配置与运维说明.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/上游升级与回归检查清单.md`
