# 5GC 本地分流解决方案概述

来源：

- SRC-3GPP-5GS-OVERVIEW: https://www.3gpp.org/technologies/5g-system-overview
- SRC-FREE5GC-UPF-DESIGN: https://free5gc.org/doc/Gtp5g/design/
- SRC-FREE5GC-CONFIG: https://free5gc.org/guide/Configuration/

## 方案概述

本地分流是将特定 DNN、切片或业务流量从靠近用户或园区的 UPF 出口转发到本地数据网络的方案。它通常用于园区专网、边缘计算和低时延业务。

## 业务场景

- 企业园区希望终端直接访问本地应用。
- 低时延业务希望减少回传到中心云的路径。
- 不同切片或 DNN 需要使用不同的 UPF 和出口策略。

## 技术原理

### 架构设计

SMF 负责基于 DNN、S-NSSAI、签约和策略选择合适的 UPF。UPF 负责 N3 接入侧流量、N6 数据网络出口和必要的 N9 转发。N4 接口承载 SMF 对 UPF 的会话和转发规则控制。

### 业务流程

1. UE 发起 PDU 会话建立，请求特定 DNN 或切片。
2. AMF 将会话管理相关信息转交给 SMF。
3. SMF 根据 DNN、S-NSSAI 和本地策略选择本地 UPF。
4. SMF 通过 N4 向 UPF 下发 PDR、FAR、QER 等用户面控制规则。
5. UPF 将匹配流量转发到本地 DN。

## 方案对比

| 对比项 | 本地分流 | 中心云回传 |
|---|---|---|
| 时延 | 较低，路径更短 | 较高，需回传中心 |
| 出口位置 | 本地 UPF/N6 | 中心 UPF/N6 |
| 运维重点 | 本地路由、N4、UPF 选择 | 中心出口容量、骨干链路 |
| 适用场景 | 园区、边缘、行业专网 | 通用公网访问 |

## 部署配置

- 规划 DNN、S-NSSAI 和 UPF 拓扑。
- 配置 SMF 支持的切片和 DNN。
- 配置 UE routing path 或等价用户面路径策略。
- 确认本地 DN 路由、安全策略和回程路径。

## 约束与影响

- 需要保证本地 DN 和 UPF 之间的路由正确。
- 切片、DNN 和 UPF 选择策略不一致时，可能导致会话建立失败或流量走错出口。
- 本地分流变更应结合业务窗口和回退方案执行。

