package request

import (
	"github.com/gofrs/uuid/v5"
	jwt "github.com/golang-jwt/jwt/v4"
)

// Custom claims structure
type CustomClaims struct {
	BaseClaims
	BufferTime int64
	jwt.RegisteredClaims
	// Extend Start: add gaia token
	UserId string `json:"user_id"`
	Exp    int64  `json:"exp"`
	Sub    string `json:"sub"`
	Email  string `json:"email,omitempty"`
	// Extend Start: add gaia token
}

type BaseClaims struct {
	UUID        uuid.UUID
	ID          uint
	Username    string
	NickName    string
	AuthorityId uint
	// Extend Start: add gaia token
	UserId string `json:"user_id,omitempty"`
	Exp    int64  `json:"exp,omitempty"`
	Email  string `json:"email,omitempty"`
	Sub    string `json:"sub,omitempty"`
	// Extend Start: add gaia token
}

// CSRFClaims CSRF token claims (与Dify API兼容)
type CSRFClaims struct {
	jwt.RegisteredClaims
	Sub string `json:"sub"`
}
