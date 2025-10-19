# Dify-Plus

## 项目介绍

在原有 Dify 的基础中，该项目做了一些二开以及新增了管理中心的功能，原先这些功能只是在我们企业内部使用，对外交流后发现很多伙伴也遇到我们相同一些痛点，故将我们的二开内容进行开源，欢迎大家一起交流。

简而言之：该项目基于 [gin-vue-admin](https://github.com/flipped-aurora/gin-vue-admin) 做了 Dify 的管理中心，基于  [Dify](https://github.com/langgenius/dify) 做了一些适合企业场景的二开功能。

即 *Dify-Plus* = *管理中心* + *Dify 二开*

## 名字说明

Dify-Plus，该名字不是说比 Dify 项目牛的意思，意思是想说比 Dify 多做了一些针对企业场景多了一些二开的功能而已。

## 新增功能介绍

### 一.  Dify 二开功能
1.  新增：用户额度
    1.  对话余额限制判断
    2.  异步计算用户额度逻辑
    3.  左上角新增使用额度显示
    4.  新增个人监测页
2.  新增：密钥额度设置
    1.  新增应用 API 调用余额限制判断
3.  新增 ：Web 公开页登录鉴权
4.  新增：管理员同步应用到应用模版
5.  新增：后台创建用户，自动邀请进管理员空间
6.  新增：可以鉴权的 cookie
7.  新增：同步应用到模版中心
8.  新增：应用中心页面
9.  调整 ：默认跳转到应用中心
10.  新增：应用使用次数记录、应用中心按照使用次数排序
11.  权限调整
     1.  调整：不允许普通成员关闭模型
     2.  调整：空间普通成员不渲染“模型供应商”标签
     3.  调整：非管理员，隐藏密钥显示 
     4. 优化： csv 编码监测，修复批量请求，windows 下载后保存再上传问题
     5. 优化： markdown 图片放大问题优化
12.  [新增：钉钉登录](https://github.com/YFGaia/dify-plus/wiki/Ding%E2%80%90Talk-%E4%BD%BF%E7%94%A8%E6%96%87%E6%A1%A3)
13.  [新增：sandbox-full，以放开代码执行节点函数限制](https://github.com/YFGaia/dify-plus/wiki/Sandbox%E2%80%90full%E4%BD%BF%E7%94%A8%E6%96%87%E6%A1%A3)
## 二. 管理中心
> 代码所在目录：/admin
1.  JWT 与 Dify 打通
2.  用户同步
3. 用户额度修改
4. 费用报表

## 部分功能页面展示截图

1. 应用中心

   ![应用中心.jpg](images/dify-plus/应用中心.jpg)

1. API密钥列表

   ![API密钥列表.png](images/dify-plus/API密钥列表.png)

1. 创建API密钥

   ![创建API密钥.jpg](images/dify-plus/创建API密钥.jpg)

1. 用户额度显示

   ![用户额度显示.jpg](images/dify-plus/用户额度显示.jpg)

1. 同步至应用模版

   ![同步至应用模版.jpg](images/dify-plus/同步至应用模版.jpg)

1. API调用测试

   ![API调用测试.jpg](images/dify-plus/API调用测试.jpg)

1. 个人额度修改

   ![个人额度修改.jpg](images/dify-plus/个人额度修改.jpg)

1. 费用报表

   ![费用报表.jpg.png](images/dify-plus/费用报表.png)

1. 密钥使用分析

   ![密钥使用分析.jpg](images/dify-plus/密钥使用分析.jpg)

1. 每月密钥额度花费

   ![每月密钥额度花费.jpg](images/dify-plus/每月密钥额度花费.jpg)

1. 钉钉登录配置

   ![image](https://github.com/user-attachments/assets/c87b315b-89aa-4bed-882f-c3243c4d9440)



## 版本更新说明

1. 会持续跟随 gin-vue-admin 和 Dify 两个开源项目的版本。
2. 为了标志二开的部分，我们特意在注释、文件名、方法名、表名都加上`extend`，可通过搜索这个关键字，查看我们二开的代码

## 整体服务

![dify-plus.png](images/dify_plus.png)


## 启动方式（docker-compose）
见文档：[部署详细步骤（docker‐compose）](https://github.com/YFGaia/dify-plus/wiki/%E9%83%A8%E7%BD%B2%E8%AF%A6%E7%BB%86%E6%AD%A5%E9%AA%A4%EF%BC%88docker%E2%80%90compose%EF%BC%89)

## 启动方式（源码）
见文档：[部署详细步骤（源码）](https://github.com/YFGaia/dify-plus/wiki/%E9%83%A8%E7%BD%B2%E8%AF%A6%E7%BB%86%E6%AD%A5%E9%AA%A4%EF%BC%88%E6%BA%90%E7%A0%81%E9%83%A8%E7%BD%B2%EF%BC%89)

## 用户问的较多的问题
见文档：[Q&A](https://github.com/YFGaia/dify-plus/wiki/Q&A)


## 相关配置说明
- Dify 相关配置说明：https://docs.dify.ai/zh-hans/getting-started/install-self-hosted/environments
- 管理中心 相关配置说明：
  - 后端：https://gin-vue-admin.com/guide/server/config.html
  - 前端：https://gin-vue-admin.com/guide/web/env.html
- Dify-plus 新增环境变量说明
    ```
    待补充
    ```

## 联系我们
### email
- toxingwang@gmail.com
- 906631095@qq.com

### 微信交流
1. 微信交流群
   - Dify-plus官方交流群1（已满）
   - Dify-plus官方交流群2（已满）
   - Dify-plus官方交流群3（已满）
   - Dify-plus&Coze开源交流群4

     <img width="200" height="583" alt="image" src="https://github.com/user-attachments/assets/e467e5ec-d493-46e7-8b6d-8d984a304508" />





2. 防止广告进群，添加微信，输入以下代码执行结果
```python
encoded_str = "5Yqg5YWlZGlmeS1wbHVz5Lqk5rWB576kMgo="
decoded_bytes = base64.b64decode(encoded_str)
decoded_str = decoded_bytes.decode('utf-8')
print(decoded_str)
```

3. 微信二维码：
<img width="200" alt="image" src="https://github.com/user-attachments/assets/b5aa106e-4bd9-40d7-925f-05c3f5265ef6" />

备注：微信交流群若加不进去，可添加我们微信，我们拉你进群

### 请作者喝咖啡~
<img width="200" alt="image" src="https://github.com/user-attachments/assets/9a1ce3d4-3101-46eb-8a72-0a39db5b836b" />



### Star History

[![Star History Chart](https://api.star-history.com/svg?repos=YFGaia/dify-plus&type=Date)](https://star-history.com/#YFGaia/dify-plus&Date)


## License

版权说明：本项目在 Dify 项目基础上进行二开，需要遵守 Dify 的开源协议，如下

This repository is available under the [Dify Open Source License](LICENSE), which is essentially Apache 2.0 with a few additional restrictions.
