// extend: fork-only oRPC contract base.
// 上游 1.16.0 删除了手写契约层（web/contract/ 整目录，提交 61650d34ce），契约改由生成物
// @dify/contracts/api/console/router.gen.ts 提供。fork 自有端点（login_config 双阶段、
// system-manage-extend 代码执行控制）无法进入生成物体系，因此保留本目录作为 fork-only
// 契约宿主，并通过 web/service/console-router-loader.ts 的 fork 段注册接入运行时，
// 通过 web/contract/router-extend.ts 接入 consoleClient/consoleQuery 类型。
import { oc } from '@orpc/contract'

export const base = oc.$route({ inputStructure: 'detailed' })
