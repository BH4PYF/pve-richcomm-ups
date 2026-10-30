# 旧方案：在 Ubuntu 24.04 上运行原厂 32 位程序

这是 2026-10-29 的第一版方案：启用 i386 架构、安装 `libc6:i386`，把 2005 年的
`ups_manager` / `ups_monitor` / `ups_status` 跑起来，再用 NUT 桥接它的共享内存。

它证明了原厂程序可以运行，但没解决根本问题：

- `ups_monitor` 走 usbhid/hiddev，USB 抖动就 `ENODEV`；
- 重插后 hiddev 主次设备号漂移，进程活着但读数为零；
- 250 Hz 轮询对这条低速链路太重。

现在的主方案是 `deploy/ups-poller`（libusb 直接 claim interface 0），本目录仅作对照保留。

`install.sh` 需要 `payload/` 里的原厂二进制，仓库不包含（版权与体积原因）。
自行准备时，把原厂安装包解出的 `TimeCfg.conf`、`ups_monitor`、`ups_status`、
`ups_config`、`shutdown.sh`、`SendMSG.sh`、`pm.conf` 放到 `payload/`，再执行
`sudo ./install.sh`。
