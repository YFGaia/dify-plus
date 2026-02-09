package utils

import "strings"

// InferPlatformArch 根据安装包文件名推断 platform 与 arch
// 返回 platform (darwin|win32|linux), arch (x64|arm64)
// Windows 安装包通常为一个 exe 同时支持 arm/amd64，统一存为 win32+x64，客户端请求 win32 时返回该包
func InferPlatformArch(filename string) (platform, arch string) {
	name := strings.ToLower(filename)
	// .dmg → macOS
	if strings.HasSuffix(name, ".dmg") {
		platform = "darwin"
		if strings.Contains(name, "arm64") || strings.Contains(name, "aarch64") {
			arch = "arm64"
		} else if strings.Contains(name, "x64") || strings.Contains(name, "amd64") || strings.Contains(name, "x86_64") {
			arch = "x64"
		} else {
			arch = "arm64" // 默认 Apple Silicon
		}
		return
	}
	// .exe → Windows，不区分架构，统一 x64
	if strings.HasSuffix(name, ".exe") {
		platform = "win32"
		arch = "x64"
		return
	}
	// .deb → Linux
	if strings.HasSuffix(name, ".deb") {
		platform = "linux"
		if strings.Contains(name, "arm64") || strings.Contains(name, "aarch64") {
			arch = "arm64"
		} else {
			arch = "x64" // amd64
		}
		return
	}
	// .AppImage → Linux，一般为 x64（已转小写故 .appimage）
	if strings.HasSuffix(name, ".appimage") {
		platform = "linux"
		arch = "x64"
		return
	}
	return "", ""
}
