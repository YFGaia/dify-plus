'use client'

// 二开部分：AI 作图导航（预留挂载位，默认关闭）。原实现位于旧 web/app/components/header/draw-nav-extend/
// （迁移前即为注释态未挂载；上游 1.15.0 删除旧 header 后迁至 main-nav 体系，保持默认关闭）。
// 启用方式：在 main-nav/index.tsx 中取消对应挂载行的注释。
import type { MainNavItem } from '../types'
import { useTranslation } from 'react-i18next'
import MainNavLink from './nav-link'

const isDrawPath = (path: string) => path === '/draw-extend' || path.startsWith('/draw-extend/')

type DrawNavExtendProps = {
  pathname: string
}

const DrawNavExtend = ({ pathname }: DrawNavExtendProps) => {
  const { t } = useTranslation()

  const item: MainNavItem = {
    href: 'https://gaia-x.yafex.cn/draw',
    label: t(($) => $['aiDraw.title'], { ns: 'extend' }),
    active: isDrawPath,
    icon: 'i-ri-image-2-line',
    activeIcon: 'i-ri-image-2-fill',
  }

  return <MainNavLink item={item} pathname={pathname} />
}

export default DrawNavExtend
