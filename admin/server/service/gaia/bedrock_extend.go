package gaia

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go/aws/credentials"
	v4 "github.com/aws/aws-sdk-go/aws/signer/v4"
	estream "github.com/aws/aws-sdk-go/private/protocol/eventstream"

	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	gaiaResponse "github.com/flipped-aurora/gin-vue-admin/server/model/gaia/response"
	"go.uber.org/zap"
)

// proxyBedrockRequest 直连 AWS Bedrock 原生 API 转发 Anthropic Messages 请求。
//
// 路径转换：v1/messages → model/{modelId}/invoke 或 model/{modelId}/invoke-with-response-stream
// 鉴权：SigV4（service=bedrock，region 来自 Dify 凭证 aws_region 字段）
// 请求体改写：去掉 model 字段（Bedrock 走 URL 路径），注入 anthropic_version=bedrock-2023-05-31
// 响应：
//   - 非流式：Bedrock 返回 Anthropic Messages JSON（含 usage.input_tokens/output_tokens），原样转发
//   - 流式：vnd.amazon.eventstream 二进制帧，每帧 payload 是 {"bytes":"<base64>"}，
//     解出后是 Anthropic SSE 事件 JSON，在此重组为标准 SSE 写回客户端
//
// 计费：成功后按 (input_tokens, output_tokens) 调 calcQuotaDelta 扣额。
func (s *ModelProviderService) proxyBedrockRequest(
	userID, _ /* path */, method string, _ /* reqHeader */ http.Header, body []byte, writer io.Writer,
	creds *gaiaResponse.ProviderCredentials,
) error {
	// 1) 校验 AWS 凭证
	if creds == nil || creds.AWSAccessKeyID == "" || creds.AWSSecretAccessKey == "" {
		return fmt.Errorf("AWS Bedrock 凭证缺失（需要 aws_access_key_id / aws_secret_access_key）")
	}
	region := creds.AWSRegion
	if region == "" {
		region = "us-east-1"
	}

	// 2) 解析 body：拿到 modelId 与 stream 标记，并改写为 Bedrock 期望的格式
	if len(body) == 0 {
		return fmt.Errorf("Bedrock 请求 body 不能为空")
	}
	var bodyObj map[string]interface{}
	if err := json.Unmarshal(body, &bodyObj); err != nil {
		return fmt.Errorf("解析 Bedrock 请求 body 失败：%w", err)
	}
	modelID, _ := bodyObj["model"].(string)
	if modelID == "" {
		return fmt.Errorf("Bedrock 请求 body 缺少 model 字段")
	}
	streaming := false
	if v, ok := bodyObj["stream"].(bool); ok {
		streaming = v
	}
	// 移除 model（Bedrock 不需要）；删除 stream（流式由 URL 决定）
	delete(bodyObj, "model")
	delete(bodyObj, "stream")
	delete(bodyObj, "stream_options")
	// OpenAI 兼容字段转换：max_completion_tokens → max_tokens（Bedrock/Anthropic 使用 max_tokens）
	if _, hasMaxTokens := bodyObj["max_tokens"]; !hasMaxTokens {
		if v, ok := bodyObj["max_completion_tokens"]; ok {
			bodyObj["max_tokens"] = v
			delete(bodyObj, "max_completion_tokens")
		}
	} else {
		delete(bodyObj, "max_completion_tokens")
	}
	// 注入 Bedrock 必需的 anthropic_version
	if _, ok := bodyObj["anthropic_version"]; !ok {
		bodyObj["anthropic_version"] = "bedrock-2023-05-31"
	}
	rewritten, err := json.Marshal(bodyObj)
	if err != nil {
		return fmt.Errorf("重写 Bedrock 请求 body 失败：%w", err)
	}

	// 3) 构建 Bedrock URL
	// 新一代 Claude 模型（3.5v2、3.7、Sonnet-4、Opus-4 等）要求通过跨区域推理配置文件调用，
	// 模型 ID 需加地理前缀（us. / eu. / ap.），否则 Bedrock 返回 "on-demand throughput isn't supported" 错误。
	// 若调用方已传入带前缀的 ID（如 us.anthropic.xxx）则直接使用，不重复添加。
	invokeModelID := bedrockResolveModelID(modelID, region)
	if invokeModelID != modelID {
		global.GVA_LOG.Info("Bedrock 模型 ID 已映射为跨区域推理配置文件",
			zap.String("original", modelID),
			zap.String("resolved", invokeModelID),
			zap.String("region", region),
		)
	}
	host := fmt.Sprintf("bedrock-runtime.%s.amazonaws.com", region)
	op := "invoke"
	if streaming {
		op = "invoke-with-response-stream"
	}
	requestURL := fmt.Sprintf("https://%s/model/%s/%s", host, url.PathEscape(invokeModelID), op)

	// 打印请求地址、参数和代理地址
	global.GVA_LOG.Info("Bedrock 请求详情",
		zap.String("request_url", requestURL),
		zap.String("method", method),
		zap.ByteString("body", rewritten),
		zap.String("proxy_url", creds.BedrockProxyURL),
	)

	httpReq, err := http.NewRequest(method, requestURL, bytes.NewReader(rewritten))
	if err != nil {
		return fmt.Errorf("构建 Bedrock 请求失败：%w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if streaming {
		httpReq.Header.Set("Accept", "application/vnd.amazon.eventstream")
		httpReq.Header.Set("X-Amzn-Bedrock-Accept", "application/json")
	} else {
		httpReq.Header.Set("Accept", "application/json")
	}

	// 4) SigV4 签名（service=bedrock）
	awsCreds := credentials.NewStaticCredentials(creds.AWSAccessKeyID, creds.AWSSecretAccessKey, creds.AWSSessionToken)
	signer := v4.NewSigner(awsCreds)
	if _, err = signer.Sign(httpReq, bytes.NewReader(rewritten), "bedrock", region, time.Now()); err != nil {
		return fmt.Errorf("Bedrock SigV4 签名失败：%w", err)
	}

	// 5) 发起请求（若配置了 bedrock_proxy_url 则经 HTTP 代理转发）
	startTime := time.Now()
	transport := http.DefaultTransport
	if creds.BedrockProxyURL != "" {
		proxyAddr := creds.BedrockProxyURL
		if !strings.HasPrefix(proxyAddr, "http://") && !strings.HasPrefix(proxyAddr, "https://") {
			proxyAddr = "http://" + proxyAddr
		}
		if proxyURL, parseErr := url.Parse(proxyAddr); parseErr == nil {
			transport = &http.Transport{Proxy: http.ProxyURL(proxyURL)}
		}
	}
	client := &http.Client{Timeout: 5 * time.Minute, Transport: transport}
	resp, err := client.Do(httpReq)
	if err != nil {
		s.logBedrock(userID, modelID, "error", err.Error(), startTime, 0, 0)
		return err
	}
	defer func() { _ = resp.Body.Close() }()

	// 6) 写回响应头/状态码（流式改写 Content-Type 为 SSE）
	if w, ok := writer.(http.ResponseWriter); ok {
		for k, v := range resp.Header {
			lower := strings.ToLower(k)
			if streaming && (lower == "content-type" || lower == "content-length") {
				continue
			}
			for _, vv := range v {
				w.Header().Add(k, vv)
			}
		}
		if streaming {
			w.Header().Set("Content-Type", "text/event-stream")
			w.Header().Set("Cache-Control", "no-cache")
			w.Header().Set("Connection", "keep-alive")
		}
		w.WriteHeader(resp.StatusCode)
	}

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		raw, _ := io.ReadAll(resp.Body)
		_, _ = writer.Write(raw)
		s.logBedrock(userID, modelID, "error",
			fmt.Sprintf("bedrock %d: %s", resp.StatusCode, string(raw)), startTime, 0, 0)
		return nil
	}

	// 7) 处理响应体
	var inputTokens, outputTokens int
	if streaming {
		inputTokens, outputTokens, err = s.streamBedrockEventStream(resp.Body, writer)
		if err != nil {
			s.logBedrock(userID, modelID, "error", err.Error(), startTime, inputTokens, outputTokens)
			return err
		}
	} else {
		var buf bytes.Buffer
		tee := io.TeeReader(resp.Body, &buf)
		if _, err = io.Copy(writer, tee); err != nil {
			s.logBedrock(userID, modelID, "error", err.Error(), startTime, 0, 0)
			return err
		}
		inputTokens, outputTokens = parseAnthropicUsage(buf.Bytes())
	}

	// 8) 记录日志 + 计费扣款
	s.logBedrock(userID, modelID, "success", "", startTime, inputTokens, outputTokens)
	if inputTokens > 0 || outputTokens > 0 {
		pricing, _ := s.fetchModelPricingFromDify(modelID)
		delta := calcQuotaDelta(pricing, modelID, inputTokens, outputTokens)
		deductAccountQuota(userID, delta)
	}
	return nil
}

// streamBedrockEventStream 解析 Bedrock 的 vnd.amazon.eventstream 二进制流，
// 把每个事件还原为 Anthropic SSE（event: <type>\ndata: <json>\n\n）写给客户端。
// 返回累计的 input/output token 数（用于计费）。
func (s *ModelProviderService) streamBedrockEventStream(r io.Reader, w io.Writer) (int, int, error) {
	flusher, _ := w.(http.Flusher)
	dec := estream.NewDecoder(r)
	payloadBuf := make([]byte, 0, 32*1024)

	var inputTokens, outputTokens int
	for {
		msg, err := dec.Decode(payloadBuf)
		if err != nil {
			if err == io.EOF {
				return inputTokens, outputTokens, nil
			}
			return inputTokens, outputTokens, fmt.Errorf("eventstream decode 失败：%w", err)
		}

		// Bedrock 的事件 payload 形如 {"bytes":"<base64-encoded inner JSON>"}
		var wrap struct {
			Bytes string `json:"bytes"`
		}
		var inner []byte
		if e := json.Unmarshal(msg.Payload, &wrap); e == nil && wrap.Bytes != "" {
			if decoded, e2 := base64.StdEncoding.DecodeString(wrap.Bytes); e2 == nil {
				inner = decoded
			}
		}
		if len(inner) == 0 {
			// 非包装格式（如错误/ping），直接用原 payload
			inner = msg.Payload
		}

		// 解析事件类型和 usage（Anthropic 在 message_start.message.usage 给 input_tokens，
		// message_delta.usage 给 output_tokens）
		var ev struct {
			Type    string `json:"type"`
			Message struct {
				Usage struct {
					InputTokens  int `json:"input_tokens"`
					OutputTokens int `json:"output_tokens"`
				} `json:"usage"`
			} `json:"message"`
			Usage struct {
				InputTokens  int `json:"input_tokens"`
				OutputTokens int `json:"output_tokens"`
			} `json:"usage"`
		}
		_ = json.Unmarshal(inner, &ev)
		if ev.Message.Usage.InputTokens > 0 {
			inputTokens = ev.Message.Usage.InputTokens
		}
		if ev.Message.Usage.OutputTokens > 0 {
			outputTokens = ev.Message.Usage.OutputTokens
		}
		if ev.Usage.InputTokens > 0 {
			inputTokens = ev.Usage.InputTokens
		}
		if ev.Usage.OutputTokens > 0 {
			outputTokens = ev.Usage.OutputTokens
		}

		// 重组为 Anthropic SSE 写回
		eventName := ev.Type
		if eventName == "" {
			eventName = "message"
		}
		sse := "event: " + eventName + "\ndata: " + string(inner) + "\n\n"
		if _, err = w.Write([]byte(sse)); err != nil {
			return inputTokens, outputTokens, err
		}
		if flusher != nil {
			flusher.Flush()
		}
	}
}

// parseAnthropicUsage 从非流式 Anthropic Messages 响应 JSON 中提取 usage 字段。
func parseAnthropicUsage(data []byte) (input, output int) {
	var obj struct {
		Usage struct {
			InputTokens  int `json:"input_tokens"`
			OutputTokens int `json:"output_tokens"`
		} `json:"usage"`
	}
	if json.Unmarshal(data, &obj) == nil {
		return obj.Usage.InputTokens, obj.Usage.OutputTokens
	}
	return 0, 0
}

// bedrockCrossRegionPrefixes 是需要跨区域推理配置文件的模型 ID 前缀列表（anthropic. 开头）。
// 来源：https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html
// 规则：凡模型不在旧版 on-demand 列表中，均需加地理前缀才能调用。
var bedrockCrossRegionPrefixes = []string{
	// Claude 3.5 v2
	"anthropic.claude-3-5-sonnet-20241022-v2",
	"anthropic.claude-3-5-haiku-20241022",
	// Claude 3.7
	"anthropic.claude-3-7-sonnet",
	// Claude Sonnet 4 / Opus 4 / Haiku 4（及后续新版本）
	"anthropic.claude-sonnet-4",
	"anthropic.claude-opus-4",
	"anthropic.claude-haiku-4",
}

// bedrockResolveModelID 将用户输入的模型 ID 解析为正确的 Bedrock 调用 ID。
//
// 支持以下输入格式（会自动规范化）：
//   - 完整带前缀：us.anthropic.claude-sonnet-4-6      → 直接使用
//   - 带厂商前缀：anthropic.claude-sonnet-4-6         → 按需加 us./eu./ap.
//   - 短横线名称：claude-sonnet-4-6                   → 补 anthropic. 再按需加前缀
//   - 空格+点号： "claude sonnet 4.6"                  → 规范化后同上
func bedrockResolveModelID(modelID, region string) string {
	// Step 1: 规范化输入
	// 小写、空格→横线、版本中的点→横线（如 4.6 → 4-6，但保留 : 用于版本后缀如 v2:0）
	normalized := strings.ToLower(strings.TrimSpace(modelID))
	normalized = strings.ReplaceAll(normalized, " ", "-")
	// 仅将数字之间的 `.` 替换为 `-`（处理 "4.6" → "4-6"），保留 anthropic. 这样的厂商点
	normalized = replaceVersionDots(normalized)

	// Step 2: 补全 anthropic. 前缀（用户只填了 claude-xxx）
	if !strings.HasPrefix(normalized, "anthropic.") &&
		!strings.HasPrefix(normalized, "us.") &&
		!strings.HasPrefix(normalized, "eu.") &&
		!strings.HasPrefix(normalized, "ap.") {
		normalized = "anthropic." + normalized
	}

	// Step 3: 已经带地理前缀 → 直接使用
	if strings.HasPrefix(normalized, "us.") || strings.HasPrefix(normalized, "eu.") || strings.HasPrefix(normalized, "ap.") {
		return normalized
	}

	// Step 4: 判断是否属于需要跨区域推理配置文件的模型
	needsCrossRegion := false
	for _, prefix := range bedrockCrossRegionPrefixes {
		if strings.HasPrefix(normalized, prefix) {
			needsCrossRegion = true
			break
		}
	}
	if !needsCrossRegion {
		return normalized
	}

	// Step 5: 根据 region 推导地理前缀
	geoPrefix := "us" // 默认
	switch {
	case strings.HasPrefix(region, "us-"):
		geoPrefix = "us"
	case strings.HasPrefix(region, "eu-"):
		geoPrefix = "eu"
	case strings.HasPrefix(region, "ap-"):
		geoPrefix = "ap"
	}
	return geoPrefix + "." + normalized
}

// replaceVersionDots 将版本号中数字之间的 `.` 替换为 `-`（如 4.6 → 4-6），
// 保留厂商命名空间中的点（如 anthropic. 开头不受影响，因为点后紧跟字母）。
func replaceVersionDots(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '.' && i > 0 && i < len(s)-1 {
			prev := s[i-1]
			next := s[i+1]
			// 仅当点号两侧都是数字时才替换为 -
			if prev >= '0' && prev <= '9' && next >= '0' && next <= '9' {
				b.WriteByte('-')
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// logBedrock 记录代理日志（与 ProxyRequest 中的 ModelProxyLog 行为一致）。
func (s *ModelProviderService) logBedrock(userID, modelID, status, errMsg string, startTime time.Time, in, out int) {
	if err := global.GVA_DB.Create(&gaia.ModelProxyLog{
		UserId:         userID,
		ProviderName:   gaia.ProviderAWS,
		ModelName:      modelID,
		RequestTokens:  in,
		ResponseTokens: out,
		Status:         status,
		ErrorMessage:   errMsg,
		CreatedAt:      startTime,
	}).Error; err != nil {
		global.GVA_LOG.Warn("logBedrock 写日志失败", zap.Error(err))
	}
}
