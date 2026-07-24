# ADD APN - same-scope conflict candidate

适用范围：CloudCore / V100R023C10 / PGW-C
来源依据：cloud_core_coldstart_md/02_commands/05_add_apn.md

## 参数说明

| 参数标识 | 参数名称 | 参数说明 |
|---|---|---|
| APNNAME | APN 名称 | 必选参数。指定 APN 标识。 |
| POOLNAME | 地址池名称 | 必选参数。所有 APN 都必须绑定本地地址池。 |

## 维护备注

该片段故意与 V100R023C10 baseline 中的 POOLNAME 可选说明冲突，用于验证 conflict_candidate 不会被当作普通答案材料。

