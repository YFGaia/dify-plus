# Admin (Go Backend) Agent Guide

## Rules (must follow)

### 禁止匿名 struct

- **禁止在代码中出现匿名 struct**。不得使用 `var x []struct { ... }`、`var x struct { ... }` 或字面量 `struct { A int }{1}` 等匿名结构体。
- 所有用于 GORM 查询扫描、缓存结构、API 请求/响应的结构体必须定义为**具名类型**，放在合适的 model 包（如 `model/gaia/request`、`model/gaia/response`）或当前包顶部，便于复用和规范约束。
- 示例：用 `[]response.AppQuotaRankingRow` 替代 `[]struct { AppID string; TotalCost float64; ... }`；用 `response.AppQuotaRankingCache` 替代 `struct { List ...; Total int64 }`。
