# WOL 与 AMT

## 1. 需求

2026-10-30 04:38 关闭 PVE 做电池空载充电，计划早上 08:00 用网络唤醒把 PVE 和 NAS 叫起来，避免人到现场开机。

- PVE：`192.168.88.1`，MAC `00-D8-61-B2-3D-83`，Web 端口 8006。
- NAS：`192.168.88.99`，MAC `48-F3-17-06-28-58`，DSM 端口 5000。

## 2. 魔术包

标准 WOL：6 字节 `0xFF` + 目标 MAC 重复 16 次，共 102 字节，UDP 广播到 `255.255.255.255` 或子网广播 `192.168.88.255`，端口 7 或 9。

`windows/wol-ups.ps1` 的实现：

```powershell
function New-MagicPacket([string]$mac) {
    $bytes = [byte[]]($mac -split "[:-]" | ForEach-Object { [Convert]::ToByte($_, 16) })
    $packet = New-Object System.Collections.Generic.List[byte]
    for ($i = 0; $i -lt 6; $i++) { $packet.AddRange([byte[]](0xFF,0xFF,0xFF,0xFF,0xFF,0xFF)) }
    for ($i = 0; $i -lt 16; $i++) { $packet.AddRange($bytes) }
    return $packet.ToArray()
}
```

每个目标 × 两个广播地址 × 两个端口 = 每次 4 包。

## 3. 踩过的坑：AMT 会在关机时代答 ping

08:00 的计划任务第一轮探测报告"PVE already up"，于是**没有发魔术包**。实际 PVE 是关着的。

原因：这块 PVE 主板启用了 Intel AMT/vPro。系统处于 S5 时，ME 仍然持有网卡并应答：

- ICMP：有回应，TTL 255（正常 Linux 是 64/128）。
- TCP 16992：开放，HTTP 返回 303。

所以 `Test-Connection` 完全不能用来判断"操作系统是否在线"。

修复：`Test-Up` 对带端口的目标改用 TCP connect，1.2 s 超时：

```powershell
$client = New-Object System.Net.Sockets.TcpClient
$iar = $client.BeginConnect($t.Ip, $t.Port, $null, $null)
if (-not $iar.AsyncWaitHandle.WaitOne(1200)) { return $false }
$client.EndConnect($iar)
return $true
```

PVE 探 8006，NAS 探 5000。只有真正能建立 TCP 才认为系统起来了。

## 4. 脚本行为

`windows/wol-ups.ps1`：

1. 先探测两个目标；已在线的不发。
2. 未在线的发魔术包。
3. 等 45 s，最多 6 轮（约 4.5 分钟）。
4. 每轮都重新探测、对仍未醒的补发。
5. 结果写 `wol.log`（与脚本同目录）。

参数：

- `-Test`：只向假 MAC `02-00-00-00-00-01` 发包，验证脚本本身，不会唤醒任何设备。
- `-Force`：不探测，先无条件发一轮，再进入正常重试。

日志示例：

```
2026-10-30 08:08:12  wake run start
2026-10-30 08:08:12  magic packet -> PVE (00-D8-61-B2-3D-83) x4
2026-10-30 08:08:12  magic packet -> NAS (48-F3-17-06-28-58) x4
2026-10-30 08:09:37  PVE up (port 8006)
2026-10-30 08:12:43  final: NAS NOT reachable
```

## 5. 计划任务

`windows/install-wol-task.ps1` 注册 `UPS-WOL-0800`：

- 触发：当天 08:00，一次性。
- `WakeToRun`：机器睡眠也能触发。
- `StartWhenAvailable`：错过时间后补跑。
- `AllowStartIfOnBatteries`：笔记本/小主机用电池时也跑。
- 执行时限 20 分钟。
- 交互式 principal，保证能访问网络和写日志。

注册命令：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-wol-task.ps1
```

检查：

```powershell
Get-ScheduledTask UPS-WOL-0800 | Format-List *
Get-ScheduledTaskInfo UPS-WOL-0800
```

## 6. 本次结果

| 目标 | 结果 |
|---|---|
| PVE | 08:08 手动补发后 **08:09:37 上线**，8006 可访问；UPS 相关服务全部自启 |
| NAS | 6 轮 20 多个魔术包，**未唤醒**；5000/22 端口关闭，ARP 无记录 |

NAS 未唤醒的可能原因：

1. DSM 的"控制面板 → 硬件和电源 → 启用 Wake on LAN"没开，或只在某个网口开。
2. `48-F3-17-06-28-58` 是第二网口/未接线网口的 MAC，魔术包打到了错误的 NIC。
3. 关机方式不是 S5（例如 DSM 的"深度休眠"或断电），网卡没进入 WOL 待机。
4. 交换机/路由器的广播被隔离，或 NAS 与发送端不在同一 VLAN。

### 下一步

1. 手动开机一次，进 DSM 确认当前活动网口的真实 MAC。
2. 在"硬件和电源"里为**每个**网口启用 WOL。
3. 确认 NAS 关机后网口指示灯仍亮（有些主板关灯即断 WOL）。
4. 再跑一次 `wol-ups.ps1 -Force`。
5. 若仍失败，在路由器上查 ARP/交换表，确认魔术包到达 NAS 所在端口。

## 7. 注意

- WOL 只在同一二层广播域有效；跨网段需要路由器配置定向广播。
- 魔术包不带认证，能发广播的人都能唤醒；家里内网可接受，公网不要暴露。
- AMT 的 16992 端口建议不要映射到公网；如果不用 vPro，可在 BIOS 里关闭 AMT 以减少困惑。
- 判断主机在线优先用业务端口（PVE 8006、SSH 22、DSM 5000），不要用 ping。
