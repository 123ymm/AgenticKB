# N4 互通前置检查清单

适用范围：CloudCore / 5GC / SMF, UPF
来源依据：cloud_core_coldstart_md/05_constraints_alarms/06_N4互通前置检查清单.md 与 cloud_core_coldstart_md/03_procedures/03_配置SMF到UPF的N4互通.md

## 检查项

- 基础网络：确认 N4 承载 IP 可达，避免本端到对端出现路由黑洞。
- 安全策略：确认 PFCP 端口已放通，ACL 和防火墙允许相关流量。
- 版本兼容：确认双方 PFCP 能力集经过互通验证。
- 配置一致性：确认 PeerIP 和 LocalIP 正确，避免地址填错或方向错误。
- 运维策略：确认心跳参数合理，避免过严参数造成震荡。

## 操作步骤

1. 规划 N4 承载地址，明确 SMF 与 UPF 之间的 PFCP 通信地址。
2. 配置对端节点，在 SMF 和 UPF 侧分别添加 N4PEER 配置。
3. 检查心跳参数，确认心跳周期和超时阈值符合网络环境。
4. 建立邻接并观察状态，检查 Peer 状态是否变为正常。
5. 联调会话建立，验证 N4 会话控制正常。

## 建议输出

检查通过后再执行 ADD N4PEER 并进行会话联调。

## 验证方法

```mml
SHOW N4PEER;
TRACE PFCP;
```

