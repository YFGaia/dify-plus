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
	// 注入 Bedrock 必需的 anthropic_version
	if _, ok := bodyObj["anthropic_version"]; !ok {
		bodyObj["anthropic_version"] = "bedrock-2023-05-31"
	}
	rewritten, err := json.Marshal(bodyObj)
	if err != nil {
		return fmt.Errorf("重写 Bedrock 请求 body 失败：%w", err)
	}

	// 3) 构建 Bedrock URL
	host := fmt.Sprintf("bedrock-runtime.%s.amazonaws.com", region)
	op := "invoke"
	if streaming {
		op = "invoke-with-response-stream"
	}
	requestURL := fmt.Sprintf("https://%s/model/%s/%s", host, modelID, op)

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
