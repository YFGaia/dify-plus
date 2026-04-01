# AGENTS.md

## Project Overview

`dify-plus` is a fork of [langgenius/dify](https://github.com/langgenius/dify) with an additional enterprise admin center and a set of business-oriented extensions.

- Upstream comparison baseline for this branch: `upstream-1.12.1`
- Current working branch baseline: local release tag `1.12.1`
- Main code areas:
  - `/api`: Dify backend plus forked backend extensions
  - `/web`: Dify console/web frontend plus forked UI features
  - `/admin`: standalone admin center based on `gin-vue-admin`
  - `/docker`: integrated deployment for Dify-Plus services
  - `/docs`: architecture and fork feature documentation

## Working Baseline

- When analyzing fork changes, compare against `upstream-1.12.1`.
- Keep fork-specific logic discoverable. Existing code usually uses `extend` in file names, model names, migration names, comments, or helper functions.
- Preserve the separation between:
  - Upstream Dify runtime capability
  - Forked enterprise features
  - Admin center management capability

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
- Admin center is independent from the Next.js app and lives under `/Users/liuxingwang/go/src/dify-plus/admin`.

## Deployment Workflow

- `origin` is the fork repository.
- `upstream` should point to `https://github.com/langgenius/dify`.
- The integrated compose file for the fork is `/Users/liuxingwang/go/src/dify-plus/docker/docker-compose.dify-plus.yaml`.
- Admin deployment assets live under `/Users/liuxingwang/go/src/dify-plus/admin/deploy`.

## Documentation

Start from these docs when you need the full fork context:

- **Python**: Keep type hints on functions and attributes, and implement relevant special methods (e.g., `__repr__`, `__str__`). Prefer `TypedDict` over `dict` or `Mapping` for type safety and better code documentation.
- **TypeScript**: Use the strict config, rely on ESLint (`pnpm lint:fix` preferred) plus `pnpm type-check:tsgo`, and avoid `any` types.

- `/Users/liuxingwang/go/src/dify-plus/docs/README.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/整体架构图.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/与上游差异总表.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/二开功能详解-后端与数据层.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/二开功能详解-Web与管理后台.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/二开部署配置与运维说明.md`
- `/Users/liuxingwang/go/src/dify-plus/docs/上游升级与回归检查清单.md`
