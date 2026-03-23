//go:build !windows
// +build !windows

package core

import (
	"github.com/flipped-aurora/gin-vue-admin/server/initialize"
	"github.com/fvbock/endless"
	"github.com/gin-gonic/gin"
	"syscall"
	"time"
)

func initServer(address string, router *gin.Engine) server {
	s := endless.NewServer(address, router)
	s.ReadHeaderTimeout = 10 * time.Minute
	s.WriteTimeout = 10 * time.Minute
	s.MaxHeaderBytes = 1 << 20
	// 优雅关闭：在收到 SIGTERM/SIGINT 时先停止工作池，再关闭 HTTP 服务，避免 goroutine 与连接未释放
	stopPool := func() { initialize.StopWorkerPool() }
	s.SignalHooks[endless.PRE_SIGNAL][syscall.SIGTERM] = append(s.SignalHooks[endless.PRE_SIGNAL][syscall.SIGTERM], stopPool)
	s.SignalHooks[endless.PRE_SIGNAL][syscall.SIGINT] = append(s.SignalHooks[endless.PRE_SIGNAL][syscall.SIGINT], stopPool)
	return s
}
