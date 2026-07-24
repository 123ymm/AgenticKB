# free5GC SMF S-NSSAI 配置指南

来源：

- SRC-FREE5GC-CONFIG: https://free5gc.org/guide/Configuration/

## 概述

在 free5GC 环境中，SMF 配置需要同时考虑 SBI 地址、NRF 注册信息、S-NSSAI、DNN 和用户面路径。该指南用于说明配置思路，不替代具体版本的配置文件说明。

## 前提条件

- NRF、AMF、SMF、UPF 等网络功能已部署。
- SMF 能通过 SBI 地址注册到 NRF。
- 规划好 S-NSSAI、DNN、UPF 节点和 UE 路由路径。
- 确认 SMF 与 UPF 之间的 N4 地址可达。

## 配置流程

### 步骤1: 配置 SBI 地址

确认 `registerIPv4` 和 `bindingIPv4`。在容器或云平台场景中，注册到 NRF 的地址和进程实际绑定的地址可能不同；单机实验环境可以先配置为相同地址。

### 步骤2: 配置 SMF 支持的 S-NSSAI

在 SMF 配置中声明支持的切片信息，使 SMF 能基于会话请求和签约信息处理对应切片的 PDU 会话。

### 步骤3: 配置 DNN 与用户面路径

将 DNN、S-NSSAI 和 UPF 数据路径关联起来。存在 ULCL 或多 UPF 场景时，需要额外配置 UE routing path。

### 步骤4: 验证 N4 可达

确认 SMF 与 UPF 的 N4 地址、端口和路由可达，避免会话建立阶段 PFCP 失败。

## 配置参数

| 参数名 | 参数说明 | 取值示例 |
|---|---|---|
| registerIPv4 | 注册到 NRF 的服务地址 | 127.0.0.18 |
| bindingIPv4 | NF 进程监听地址 | 127.0.0.18 |
| S-NSSAI | 切片选择信息 | SST=1, SD=010203 |
| DNN | 数据网络名称 | internet |
| UPF | 用户面节点 | UPF1 |

## 验证方法

1. 检查 SMF 是否成功注册到 NRF。
2. 发起 UE PDU 会话建立。
3. 查看 SMF 和 UPF 日志，确认 PFCP Association 和 Session Establishment 正常。
4. 验证 UE 是否获得地址并访问目标数据网络。

## 常见问题

- 如果 NRF 中看到的 SMF 地址不可达，优先检查 `registerIPv4`。
- 如果进程启动失败，优先检查 `bindingIPv4` 和端口占用。
- 如果会话建立失败，优先检查 S-NSSAI、DNN、UPF 路径和 N4 连通性。

