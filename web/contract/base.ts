// Extend: code-execution-control keeps its handwritten systemManage contract.
// Other Console endpoints, including login configuration, use generated contracts.
// service/console/contract-loader.ts and router-extend.ts register this segment.
import { oc } from '@orpc/contract'

export const base = oc.$route({ inputStructure: 'detailed' })
