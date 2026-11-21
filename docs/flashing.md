# 刷机指引（解锁 SSH + 本地 client）

> 目标：给音箱刷入 Open-XiaoAI patch 固件，获得 SSH(root) + 本地 client（把麦克风音频/原生 ASR 事件经 WebSocket 推给你的服务端）。
> ⚠️ 刷机有变砖风险，请完整读完本页再动手。

## 1. 支持机型与固件版本门

| 机型 | 固件 ≤1.58.6 | 固件 ≥1.62.2 |
|---|---|---|
| 小爱音箱 Pro (LX06) | 官方 open-xiaoai 预构建补丁 | 用 fork 自制补丁 |
| Xiaomi 智能音箱 Pro (OH2P) | 官方 open-xiaoai 预构建补丁 | 用 fork 自制补丁 |

**先查版本**：米家 app → 设备 → 通用设置 → 检查更新页看当前固件版本。
**版本不一致不要硬刷旧补丁**（跨版本 rootfs 有变砖风险）。

## 2. 获取补丁固件

- 固件 ≤1.58.6：官方 https://github.com/idootop/open-xiaoai 直接下载对应型号
- 固件 ≥1.62.2：fork https://github.com/stevenjoezhang/xiaoai-agent 的 client-patch（从小米 OTA 自动提取当前版本官方固件打补丁）：
  1. `deploy/client-patch/.env` 填小米账号 MI_USER / MI_PASS / MI_DID（设备名与米家 app 完全一致，含空格；`docker --env-file` 不剥引号）
  2. `docker build --platform linux/amd64 -t xiaoai-agent-client-patch .`（国内拉不动 node 镜像用 docker.m.daocloud.io）
  3. `docker run -it --rm --platform linux/amd64 --env-file .env -v $(pwd)/assets:/app/assets -v $(pwd)/patches:/app/patches xiaoai-agent-client-patch`
  4. 产物：`assets/<固件目录>/root-patched.squashfs`

小米登录被验证码风控时：若同账号在 Home Assistant 有 xiaomi_miot 集成，可从 `core.config_entries` 取 passToken 绕过。

## 3. USB 刷写（macOS）

- Type-C **传输线**直连 Mac（纯充电线不行）
- 依赖 libusb-compat：brew 装不上就从源码编译（libusb-1.0.27 + libusb-compat 0.1.8）
- **自检门**：裸跑 `./macos/update`，应打印 Amlogic 横幅且无 dyld 报错——包装脚本会吞错误假报成功，验证标准只能是重启后 22 端口开放
- **上电窗口约 1 秒**：必须原子化脚本——高频 `./macos/update identify 7` 扫描，命中后**同一 uboot 会话内**串行执行：
  ```
  bulkcmd "     setenv bootdelay 15"
  bulkcmd "     setenv boot_part boot0"
  bulkcmd "     saveenv"
  partition system0 <root-patched.squashfs>
  ```
  认准日志 `[update]mwrite success`。刷机期间局域网其他小米音箱断电。
- **安全垫**：双分区设计只动 system0，`./flash switch boot1` 可切回原厂；patch 禁用了 OTA 自动更新；原生小爱功能零修改。

## 4. 首次进入

断电 + 断数据线 → 等 10 秒 → 只插电源。用 `arp -a` 按音箱 MAC 找 IP：

```bash
ssh -o HostKeyAlgorithms=+ssh-rsa root@<音箱IP>   # 密码 open-xiaoai
```

## 5. 参考

- 官方文档：https://github.com/idootop/open-xiaoai （docs/flash.md）
- 1.62.2 补丁：https://github.com/stevenjoezhang/xiaoai-agent
