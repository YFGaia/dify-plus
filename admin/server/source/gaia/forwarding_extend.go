package gaia

import (
	"context"
	"github.com/flipped-aurora/gin-vue-admin/server/model/gaia"
	"github.com/flipped-aurora/gin-vue-admin/server/service/system"
	"github.com/gofrs/uuid/v5"
	"github.com/pkg/errors"
	"gorm.io/gorm"
)

const initOrderForwardingExtend = system.InitOrderInternal + 1

type initForwardingExtend struct{}

// auto run
func init() {
	system.RegisterInit(initOrderForwardingExtend, &initForwardingExtend{})
}

func (i *initForwardingExtend) MigrateTable(ctx context.Context) (context.Context, error) {
	db, ok := ctx.Value("db").(*gorm.DB)
	if !ok {
		return ctx, system.ErrMissingDBContext
	}
	return ctx, db.AutoMigrate(&gaia.ForwardingExtend{})
}

func (i *initForwardingExtend) TableCreated(ctx context.Context) bool {
	db, ok := ctx.Value("db").(*gorm.DB)
	if !ok {
		return false
	}
	return db.Migrator().HasTable(&gaia.ForwardingExtend{})
}

func (i initForwardingExtend) InitializerName() string {
	return gaia.ForwardingExtend{}.TableName()
}

func (i *initForwardingExtend) InitializeData(ctx context.Context) (context.Context, error) {
	db, ok := ctx.Value("db").(*gorm.DB)
	if !ok {
		return ctx, system.ErrMissingDBContext
	}

	// 使用指定的 UUID
	id, err := uuid.FromString("dbb08cae-2118-469c-a991-0c8f3f2515da")
	if err != nil {
		return ctx, errors.Wrap(err, "解析 UUID 失败")
	}

	entities := []gaia.ForwardingExtend{
		{
			ID:          id,
			Path:        "workflow",
			Address:     "http://admin-server:8888/gaia/workflow/",
			Header:      "[]",
			Description: "",
		},
	}

	if err := db.Create(&entities).Error; err != nil {
		return ctx, errors.Wrap(err, gaia.ForwardingExtend{}.TableName()+"表数据初始化失败!")
	}

	next := context.WithValue(ctx, i.InitializerName(), entities)
	return next, nil
}

func (i *initForwardingExtend) DataInserted(ctx context.Context) bool {
	db, ok := ctx.Value("db").(*gorm.DB)
	if !ok {
		return false
	}

	// 检查是否存在指定的记录
	if errors.Is(db.Where("id = ?", "dbb08cae-2118-469c-a991-0c8f3f2515da").
		First(&gaia.ForwardingExtend{}).Error, gorm.ErrRecordNotFound) {
		return false
	}
	return true
}
