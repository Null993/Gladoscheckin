
# Glados自动签到 - 已更换域名为glados.cloud

## 食用方式：

### 注册一个GLaDOS的账号([注册地址](https://glados.space/landing/YXXW6-BQ9WG-BDNO0-AWHS4))

#### 我的邀请码：YXXW6-BQ9WG-BDNO0-AWHS4

### **Fork**本仓库

![图片加载失败](imgs/1.png)

### 添加**secret**

1. 跳转至自己的仓库的`Settings`->`Secrets and variables`->`Action`

2. 添加1个`repository secret`，命名为`GLADOS_COOKIE`，其值对应GLaDOS账号的cookie值中的有效部分（获取方式如下）

- 在GLaDOS的签到页面按`F12`

- 切换到`Network`页面下，刷新

![图片加载失败](imgs/2.png)

- 点击第一个选项卡后在`Request Headers`下找到`Cookie`，右键复制cookie的值即可

  > 参考格式：koa:sess=eyJ1c2xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxAwMH0=; koa:sess.sig=xJkOxxxxxxxxxxxxxxxtnM

![图片加载失败](imgs/3.png)

3. 企业微信机器人推送（非必须）

- 添加1个`repository secret`，命名为`WECOM_WEBHOOK`，其值对应WEBHOOK地址: [获取地址](https://open.work.weixin.qq.com/help2/pc/14931)。

### **star**自己的仓库

![图片加载失败](imgs/4.png)

## 文件结构

```shell
│  glados.py	# 签到脚本
│
├─.github
│  └─workflows
│          gladosCheck.yml	# Actions 配置文件
```



