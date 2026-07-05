import type { UserMoney } from '@/models/common-extend'
import { get } from '@/service/base'

export const fetchUserMoney = () => {
  return get<UserMoney>('account/money')
}
