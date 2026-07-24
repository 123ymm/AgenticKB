# 5GC 核心网功能术语

来源：

- SRC-3GPP-5GS-OVERVIEW: https://www.3gpp.org/technologies/5g-system-overview
- SRC-FREE5GC-UPF-DESIGN: https://free5gc.org/doc/Gtp5g/design/
- SRC-FREE5GC-CONFIG: https://free5gc.org/guide/Configuration/

## SMF

### 缩略语

Session Management Function

### 定义

SMF 是 5G 核心网中的会话管理功能，负责 PDU 会话建立、修改、释放和用户面路径控制。SMF 与 AMF、PCF、UPF 等网元协同，基于签约、策略、DNN 和切片信息选择用户面资源，并通过 N4 控制 UPF。

## UPF

### 缩略语

User Plane Function

### 定义

UPF 是 5G 核心网中的用户面功能，负责数据包路由转发、用户面锚点、QoS 处理和与数据网络的连接。UPF 通常涉及 N3、N4、N6 和 N9 等接口，其中 N4 用于 SMF 对 UPF 的控制。

## PCF

### 缩略语

Policy Control Function

### 定义

PCF 是 5G 核心网中的策略控制功能，负责向会话管理和接入控制流程提供策略规则。PCF 与 SMF 协作，使 PDU 会话能够应用合适的 QoS、计费和业务控制策略。

## NRF

### 缩略语

Network Repository Function

### 定义

NRF 是 5G 服务化架构中的网络功能注册与发现功能。各网络功能通过 NRF 注册服务实例并发现其他网络功能服务，支撑 SBA 架构下的服务调用。

