'use client'

// 二开部分：广告运营导航（预留挂载位，默认关闭）。原实现位于旧 web/app/components/header/nav-extend/
// （迁移前即为注释态未挂载；上游 1.15.0 删除旧 header 后迁至 main-nav 体系，保持默认关闭）。
// 启用方式：在 main-nav/index.tsx 中取消对应挂载行的注释。
import type { MainNavItem } from '../types'
import MainNavLink from './nav-link'

const isAmazonMarketingPath = (path: string) =>
  path === '/amazon-marketing-extend' || path.startsWith('/amazon-marketing-extend/')

type AmazonMarketingNavExtendProps = {
  pathname: string
}

const AmazonMarketingNavExtend = ({ pathname }: AmazonMarketingNavExtendProps) => {
  const item: MainNavItem = {
    href: '/amazon-marketing-extend',
    label: '广告运营',
    active: isAmazonMarketingPath,
    icon: 'i-ri-amazon-line',
    activeIcon: 'i-ri-amazon-fill',
  }

  return <MainNavLink item={item} pathname={pathname} />
}

export default AmazonMarketingNavExtend
