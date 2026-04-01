import type { NextConfig } from '@/next'
import createMDX from '@next/mdx'
import { codeInspectorPlugin } from 'code-inspector-plugin'
import { env } from './env'

const isDev = process.env.NODE_ENV === 'development'
const withMDX = createMDX()

const nextConfig: NextConfig = {
  basePath: env.NEXT_PUBLIC_BASE_PATH,
  transpilePackages: ['@t3-oss/env-core', '@t3-oss/env-nextjs', 'echarts', 'zrender'],
  turbopack: {
    root: process.cwd(),
    rules: codeInspectorPlugin({
      bundler: 'turbopack',
    }),
  },
  productionBrowserSourceMaps: false, // enable browser source map generation during the production build
  // Configure pageExtensions to include md and mdx
  pageExtensions: ['ts', 'tsx', 'js', 'jsx', 'md', 'mdx'],
  typescript: {
    // https://nextjs.org/docs/api-reference/next.config.js/ignoring-typescript-errors
    ignoreBuildErrors: true,
  },
  async redirects() {
    return [
      {
        source: '/',
        destination: '/apps',
        permanent: false,
      },
    ]
  },
  // dev 时把 /console/api 和 /api 代理到 5001，/admin 代理到 8888
  ...(isDev && {
    async rewrites() {
      return [
        { source: '/console/api/:path*', destination: 'http://localhost:5001/console/api/:path*' },
        { source: '/api/:path*', destination: 'http://localhost:5001/api/:path*' },
        { source: '/admin', destination: 'http://localhost:8888/' },
        { source: '/admin/:path*', destination: 'http://localhost:8888/:path*' },
      ]
    },
  }),
  output: 'standalone',
  compiler: {
    removeConsole: isDev ? false : { exclude: ['warn', 'error'] },
  },
  experimental: {
    turbopackFileSystemCacheForDev: false,
  },
}

export default withMDX(nextConfig)
