# ADD APN - duplicated source excerpt

适用范围：CloudCore / V100R023C10 / PGW-C
来源依据：cloud_core_coldstart_md/02_commands/05_add_apn.md

## 命令功能

ADD APN 用于创建 APN 对象，并定义 APN 名称、地址类型、关联地址池和缺省策略。

## 参数说明

| 参数标识 | 参数名称 | 参数说明 |
|---|---|---|
| APNNAME | APN 名称 | 必选参数。指定 APN 标识。 |
| PDNTYPE | PDN 地址类型 | 必选参数。可选 IPV4、IPV6、IPV4V6。 |
| POOLNAME | 地址池名称 | 可选参数。用于绑定本地地址池。 |

## 使用实例

```mml
ADD APN:APNNAME="internet",PDNTYPE=IPV4,POOLNAME="pool_ipv4",DNSPROFILE="dns_public";
```

