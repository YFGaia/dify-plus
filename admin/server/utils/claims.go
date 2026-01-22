package utils

import (
	"github.com/flipped-aurora/gin-vue-admin/server/global"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/model/system"
	systemReq "github.com/flipped-aurora/gin-vue-admin/server/model/system/request"
	"github.com/gin-gonic/gin"
	"github.com/gofrs/uuid/v5"
	jwt "github.com/golang-jwt/jwt/v4"
	"net"
	"time"
)

func ClearToken(c *gin.Context) {
	// 增加cookie x-token 向来源的web添加
	host, _, err := net.SplitHostPort(c.Request.Host)
	if err != nil {
		host = c.Request.Host
	}

	if net.ParseIP(host) != nil {
		c.SetCookie("x-token", "", -1, "/", "", false, false)
	} else {
		c.SetCookie("x-token", "", -1, "/", host, false, false)
	}
}

func SetToken(c *gin.Context, token string, maxAge int) {
	// 增加cookie x-token 向来源的web添加
	host, _, err := net.SplitHostPort(c.Request.Host)
	if err != nil {
		host = c.Request.Host
	}

	if net.ParseIP(host) != nil {
		c.SetCookie("x-token", token, maxAge, "/", "", false, false)
		c.Request.Header.Set("console_token", token) // Extend: add token
	} else {
		c.SetCookie("x-token", token, maxAge, "/", host, false, false)
		c.Request.Header.Set("console_token", token) // Extend: add token
	}
}

func GetToken(c *gin.Context) string {
	// Extend Start: Admin and Gaia JWT
	token, _ := c.Cookie("x-token")
	if len(token) == 0 {
		token = c.Request.Header.Get("Authorization")
	}
	if len(token) > 7 && token[0:7] == "Bearer " {
		token = token[7:]
	}
	if token == "" {
		j := NewJWT()
		token, _ = c.Cookie("x-token")
		claims, err := j.ParseToken(token)
		if err != nil {
			global.GVA_LOG.Error("重新写入cookie token失败,未能成功解析token,请检查请求头是否存在x-token且claims是否为规定结构")
			return token
		}
		SetToken(c, token, int((claims.ExpiresAt.Unix()-time.Now().Unix())/60))
	}
	// Extend Stop: Admin and Gaia JWT
	return token
}

func GetClaims(c *gin.Context) (*systemReq.CustomClaims, error) {
	token := GetToken(c)
	j := NewJWT()
	claims, err := j.ParseToken(token)
	if err != nil {
		global.GVA_LOG.Error("从Gin的Context中获取从jwt解析信息失败, 请检查请求头是否存在x-token且claims是否为规定结构")
	}
	// 判断是否dify的token
	if claims.Username == "" {
		var user system.SysUser
		var account gaia.Account
		if err = global.GVA_DB.Where("uuid=?", claims.UserId).First(&user).Error; err == nil {
			claims.BaseClaims.ID = user.ID
			claims.Username = user.Username
			claims.AuthorityId = user.AuthorityId
		} else if err = global.GVA_DB.Where("id=?", claims.UserId).First(&account).Error; err == nil {
			if err = global.GVA_DB.Where("email=?", account.Email).First(&user).Error; err == nil {
				claims.AuthorityId = user.AuthorityId
				claims.Username = user.Username
				claims.BaseClaims.ID = user.ID
				user.UUID = account.ID
				global.GVA_DB.Save(&user)
			}
		}
	}
	return claims, err
}

// GetUserID 从Gin的Context中获取从jwt解析出来的用户ID
func GetUserID(c *gin.Context) uint {
	if claims, exists := c.Get("claims"); exists {
		waitUse := claims.(*systemReq.CustomClaims)
		if waitUse.BaseClaims.ID != 0 {
			return waitUse.BaseClaims.ID
		}
	}
	if cl, err := GetClaims(c); err != nil {
		return 0
	} else {
		return cl.BaseClaims.ID
	}
}

// GetUserUuid 从Gin的Context中获取从jwt解析出来的用户UUID
func GetUserUuid(c *gin.Context) uuid.UUID {
	if claims, exists := c.Get("claims"); !exists {
		if cl, err := GetClaims(c); err != nil {
			return uuid.UUID{}
		} else {
			return cl.UUID
		}
	} else {
		waitUse := claims.(*systemReq.CustomClaims)
		return waitUse.UUID
	}
}

// GetUserAuthorityId 从Gin的Context中获取从jwt解析出来的用户角色id
func GetUserAuthorityId(c *gin.Context) uint {
	if claims, exists := c.Get("claims"); !exists {
		if cl, err := GetClaims(c); err != nil {
			return 0
		} else {
			return cl.AuthorityId
		}
	} else {
		waitUse := claims.(*systemReq.CustomClaims)
		return waitUse.AuthorityId
	}
}

// GetUserInfo 从Gin的Context中获取从jwt解析出来的用户角色id
func GetUserInfo(c *gin.Context) *systemReq.CustomClaims {
	if claims, exists := c.Get("claims"); !exists {
		if cl, err := GetClaims(c); err != nil {
			return nil
		} else {
			return cl
		}
	} else {
		waitUse := claims.(*systemReq.CustomClaims)
		return waitUse
	}
}

// GetUserName 从Gin的Context中获取从jwt解析出来的用户名
func GetUserName(c *gin.Context) string {
	if claims, exists := c.Get("claims"); !exists {
		if cl, err := GetClaims(c); err != nil {
			return ""
		} else {
			return cl.Username
		}
	} else {
		waitUse := claims.(*systemReq.CustomClaims)
		return waitUse.Username
	}
}

func LoginToken(user system.Login) (token string, claims systemReq.CustomClaims, err error) {
	var account gaia.Account
	dr, err := ParseDuration(global.GVA_CONFIG.JWT.BufferTime)
	if err != nil {
		return token, claims, err
	}
	j := &JWT{SigningKey: []byte(global.GVA_CONFIG.JWT.SigningKey)} // 唯一签名
	if err = global.GVA_DB.Where("email=?", user.GetUserEmail()).First(&account).Error; err != nil {
		return token, claims, err
	}
	claims = j.CreateClaims(systemReq.BaseClaims{
		UUID:        user.GetUUID(),
		ID:          user.GetUserId(),
		NickName:    user.GetNickname(),
		Username:    user.GetUsername(),
		AuthorityId: user.GetAuthorityId(),
		// Extend Start: add gaia token
		UserId: account.ID.String(),
		Exp:    time.Now().Add(dr).Unix(),
		Sub:    "Console API Passport",
		Email:  account.Email,
		// Extend Start: add gaia token
	})
	token, err = j.CreateToken(claims)
	return
}

// LoginTokenWithCSRF 生成登录token和CSRF token (用于批量处理API调用)
func LoginTokenWithCSRF(user system.Login) (
	token string, csrfToken string, claims systemReq.CustomClaims, err error) {
	var account gaia.Account
	dr, err := ParseDuration(global.GVA_CONFIG.JWT.BufferTime)
	if err != nil {
		return token, csrfToken, claims, err
	}
	j := &JWT{SigningKey: []byte(global.GVA_CONFIG.JWT.SigningKey)} // 唯一签名
	if err = global.GVA_DB.Where("email=?", user.GetUserEmail()).First(&account).Error; err != nil {
		return token, csrfToken, claims, err
	}
	claims = j.CreateClaims(systemReq.BaseClaims{
		UUID:        user.GetUUID(),
		ID:          user.GetUserId(),
		NickName:    user.GetNickname(),
		Username:    user.GetUsername(),
		AuthorityId: user.GetAuthorityId(),
		// Extend Start: add gaia token
		UserId: account.ID.String(),
		Exp:    time.Now().Add(dr).Unix(),
		Sub:    "Console API Passport",
		Email:  account.Email,
		// Extend Start: add gaia token
	})
	token, err = j.CreateToken(claims)
	if err != nil {
		return token, csrfToken, claims, err
	}

	// 生成CSRF token
	csrfToken, err = GenerateCSRFToken(account.ID.String())
	return
}

// GenerateCSRFToken 生成CSRF token (与Dify API兼容)
func GenerateCSRFToken(userID string) (string, error) {
	ep, err := ParseDuration(global.GVA_CONFIG.JWT.ExpiresTime)
	if err != nil {
		return "", err
	}
	j := &JWT{SigningKey: []byte(global.GVA_CONFIG.JWT.SigningKey)}

	// CSRF token只需要exp和sub字段，使用RegisteredClaims的ExpiresAt
	return j.CreateCSRFToken(systemReq.CSRFClaims{
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(ep)),
		},
		Sub: userID,
	})
}
