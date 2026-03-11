package gaia

import (
	"testing"

	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia/request"
)

// TestBuildURL_NewFormat 测试新格式 Params 自动拼接 URL
func TestBuildURL_NewFormat(t *testing.T) {
	config := request.EmailApiConfig{
		URL:    "https://api.example.com/user",
		Params: []request.RequestParam{},
	}

	// 无参数
	got := buildURL("https://api.example.com/user", config, "USER123")
	if got != "https://api.example.com/user" {
		t.Errorf("无参数时 URL 应保持不变，got: %s", got)
	}

	// 单个 string 类型参数
	config.Params = []request.RequestParam{
		{Key: "appKey", ValueType: "string", Value: "mykey"},
	}
	got = buildURL("https://api.example.com/user", config, "USER123")
	if got != "https://api.example.com/user?appKey=mykey" {
		t.Errorf("单参数拼接错误，got: %s", got)
	}

	// URL 已有 ? 时使用 &
	got = buildURL("https://api.example.com/user?type=admin", config, "USER123")
	if got != "https://api.example.com/user?type=admin&appKey=mykey" {
		t.Errorf("已有参数时应使用 & 拼接，got: %s", got)
	}
}

// TestBuildURL_DingIDParam 测试钉钉 ID 类型参数自动替换
func TestBuildURL_DingIDParam(t *testing.T) {
	config := request.EmailApiConfig{
		URL: "https://api.example.com/user",
		Params: []request.RequestParam{
			{Key: "userId", ValueType: "ding_id"},
		},
	}

	got := buildURL("https://api.example.com/user", config, "USER123")
	if got != "https://api.example.com/user?userId=USER123" {
		t.Errorf("钉钉 ID 类型参数应自动替换，got: %s", got)
	}
}

// TestBuildURL_OldFormat 测试旧格式 RequestParamField 兼容
func TestBuildURL_OldFormat(t *testing.T) {
	config := request.EmailApiConfig{
		URL:               "https://api.example.com/user",
		RequestParamField: "userId",
		Params:            nil, // 旧格式：Params 为 nil
	}

	got := buildURL("https://api.example.com/user", config, "USER123")
	if got != "https://api.example.com/user?userId=USER123" {
		t.Errorf("旧格式应使用 RequestParamField，got: %s", got)
	}
}

// TestResolveParamValue 测试参数值解析
func TestResolveParamValue(t *testing.T) {
	tests := []struct {
		vt     string
		value  string
		dingId string
		want   string
	}{
		{"ding_id", "", "USER123", "USER123"},
		{"string", "myvalue", "USER123", "myvalue"},
		{"string", "prefix_{{ding_id}}_suffix", "USER123", "prefix_USER123_suffix"},
		{"string", "prefix_$<{[ding_id]}>_suffix", "USER123", "prefix_USER123_suffix"},
		{"int", "42", "USER123", "42"},
		{"bool", "true", "USER123", "true"},
	}

	for _, tt := range tests {
		got := resolveParamValue(tt.vt, tt.value, tt.dingId)
		if got != tt.want {
			t.Errorf("resolveParamValue(%q, %q, %q) = %q, want %q", tt.vt, tt.value, tt.dingId, got, tt.want)
		}
	}
}

// TestExtractJSONPathAdvanced 测试 JSON 路径提取
func TestExtractJSONPathAdvanced(t *testing.T) {
	data := map[string]interface{}{
		"code": float64(0),
		"data": []interface{}{
			map[string]interface{}{
				"userName": "test@example.com",
				"userId":   "USER123",
			},
		},
		"nested": map[string]interface{}{
			"email": "nested@example.com",
		},
	}

	tests := []struct {
		path string
		want string
	}{
		{"code", "0"},
		{"data[0].userName", "test@example.com"},
		{"data[0].userId", "USER123"},
		{"nested.email", "nested@example.com"},
		{"notexist", ""},
		{"data[1].userName", ""},
	}

	for _, tt := range tests {
		got := extractJSONPathAdvanced(data, tt.path)
		if got != tt.want {
			t.Errorf("extractJSONPathAdvanced(data, %q) = %q, want %q", tt.path, got, tt.want)
		}
	}
}

// TestParseEmailApiConfigFromJSON_NewFormat 测试新格式解析
func TestParseEmailApiConfigFromJSON_NewFormat(t *testing.T) {
	jsonStr := `{
		"enabled": true,
		"url": "https://api.example.com",
		"method": "GET",
		"params": [
			{"key": "userId", "value_type": "ding_id", "value": ""},
			{"key": "appKey", "value_type": "string", "value": "mykey"}
		],
		"response_email_field": "data[0].email"
	}`

	cfg, err := parseEmailApiConfigFromJSON([]byte(jsonStr))
	if err != nil {
		t.Fatalf("解析新格式配置失败: %v", err)
	}
	if !isNewEmailApiConfig(cfg) {
		t.Error("应检测为新格式")
	}
	if len(cfg.Params) != 2 {
		t.Errorf("Params 数量错误，got: %d", len(cfg.Params))
	}
}

// TestParseEmailApiConfigFromJSON_OldFormat 测试旧格式兼容解析
func TestParseEmailApiConfigFromJSON_OldFormat(t *testing.T) {
	jsonStr := `{
		"enabled": true,
		"url": "https://api.example.com",
		"method": "GET",
		"request_param_field": "userId",
		"response_email_field": "data[0].email",
		"body_data": {
			"form_data": [{"userId": ""}],
			"urlencoded": []
		}
	}`

	cfg, err := parseEmailApiConfigFromJSON([]byte(jsonStr))
	if err != nil {
		t.Fatalf("解析旧格式配置失败: %v", err)
	}
	if isNewEmailApiConfig(cfg) {
		t.Error("旧格式配置不应检测为新格式")
	}
	if cfg.RequestParamField != "userId" {
		t.Errorf("RequestParamField 应为 userId，got: %s", cfg.RequestParamField)
	}
}

// TestValidateEmailApiConfigFields_NewFormat 测试新格式配置验证
func TestValidateEmailApiConfigFields_NewFormat(t *testing.T) {
	cfg := request.EmailApiConfig{
		Enabled:            true,
		URL:                "https://api.example.com",
		Method:             "GET",
		Params:             []request.RequestParam{},
		ResponseEmailField: "data[0].email",
	}

	if err := validateEmailApiConfigFields(cfg); err != nil {
		t.Errorf("有效的新格式配置不应报错: %v", err)
	}
}

// TestValidateEmailApiConfigFields_InvalidParamType 测试 Params 不支持 int 类型
func TestValidateEmailApiConfigFields_InvalidParamType(t *testing.T) {
	cfg := request.EmailApiConfig{
		Enabled: true,
		URL:     "https://api.example.com",
		Method:  "GET",
		Params: []request.RequestParam{
			{Key: "count", ValueType: "int", Value: "10"}, // Params 不支持 int
		},
		ResponseEmailField: "data[0].email",
	}

	if err := validateEmailApiConfigFields(cfg); err == nil {
		t.Error("Params 不应支持 int 类型，应报错")
	}
}

// TestValidateEmailApiConfigFields_InvalidBodyType 测试 Body 不支持未知类型
func TestValidateEmailApiConfigFields_InvalidBodyType(t *testing.T) {
	cfg := request.EmailApiConfig{
		Enabled: true,
		URL:     "https://api.example.com",
		Method:  "POST",
		Params:  []request.RequestParam{},
		BodyData: request.BodyData{
			FormData: []request.BodyField{
				{Key: "field1", ValueType: "invalid_type", Value: "val"},
			},
		},
		ResponseEmailField: "data[0].email",
	}

	if err := validateEmailApiConfigFields(cfg); err == nil {
		t.Error("Body 不应支持未知类型，应报错")
	}
}

// TestBuildBodyFields 测试 Body 字段类型转换
func TestBuildBodyFields(t *testing.T) {
	fields := []request.BodyField{
		{Key: "userId", ValueType: "ding_id", Value: ""},
		{Key: "appKey", ValueType: "string", Value: "mykey"},
		{Key: "count", ValueType: "int", Value: "10"},
		{Key: "enabled", ValueType: "bool", Value: "true"},
	}

	form := buildBodyFields(fields, "USER123")

	if form.Get("userId") != "USER123" {
		t.Errorf("ding_id 类型应替换为实际钉钉 ID，got: %s", form.Get("userId"))
	}
	if form.Get("appKey") != "mykey" {
		t.Errorf("string 类型应直接使用，got: %s", form.Get("appKey"))
	}
	if form.Get("count") != "10" {
		t.Errorf("int 类型值不正确，got: %s", form.Get("count"))
	}
}

// TestDingIDMarkerReplacement 测试 Raw 模式钉钉 ID 标记替换
func TestDingIDMarkerReplacement(t *testing.T) {
	raw := `{"userId": "$<{[ding_id]}>", "other": "value"}`
	dingId := "USER789"

	// 使用 resolveParamValue 测试标记替换
	replaced := resolveParamValue("string", raw, dingId)
	expected := `{"userId": "USER789", "other": "value"}`

	if replaced != expected {
		t.Errorf("钉钉 ID 标记替换错误\ngot:  %s\nwant: %s", replaced, expected)
	}
}

// TestDingIDMarkerOldFormat 测试旧格式 {{ding_id}} 占位符替换
func TestDingIDMarkerOldFormat(t *testing.T) {
	raw := `{"userId": "{{ding_id}}", "other": "value"}`
	replaced := resolveParamValue("string", raw, "USER789")
	expected := `{"userId": "USER789", "other": "value"}`
	if replaced != expected {
		t.Errorf("旧格式占位符替换错误\ngot:  %s\nwant: %s", replaced, expected)
	}
}
