package utils

// InArray @author: [Fantasia](https://www.npc0.com)
// @function: InArray
// @description: 判断是否在数组中
// @return: err error, conf config.Server
func InArray(value interface{}, array []interface{}) (isIn bool) {
	// 判断array是否数组
	for _, item := range array {
		if value == item {
			isIn = true
			return
		}
	}
	return false
}

// InUintArray @author: [Fantasia](https://www.npc0.com)
// @function: InUintArray
// @description: 判断是否在uint数组中
// @return: err error, conf config.Server
func InUintArray(value uint, array []uint) (isIn bool) {
	// 判断array是否数组
	for _, item := range array {
		if value == item {
			isIn = true
			return
		}
	}
	return false
}

// InStringArray @author: [Fantasia](https://www.npc0.com)
// @function: InStringArray
// @description: 判断是否在字符串数组中
// @return: err error, conf config.Server
func InStringArray(value string, array []string) (isIn bool) {
	// 判断array是否数组
	for _, item := range array {
		if value == item {
			isIn = true
			return
		}
	}
	return false
}

// AddAsteriskToString @author: [Fantasia](https://www.npc0.com)
// @function: AddAsteriskToString
// @description: 字符串加星号
// @return: err error, conf config.Server
func AddAsteriskToString(s string) string {
	// 处理空字符串或长度不足的情况
	if len(s) == 0 {
		return ""
	}
	if len(s) == 1 {
		return "*"
	}
	if len(s) == 2 {
		return s[:1] + "*"
	}
	if len(s) <= 4 {
		return s[:1] + "***" + s[len(s)-1:]
	}
	if len(s) <= 8 {
		return s[:2] + "***" + s[len(s)-2:]
	}

	// 保留前6个字符和后6个字符，中间用星号替换
	prefix := s[:6]
	suffix := s[len(s)-6:]
	middle := "********"

	return prefix + middle + suffix
}
