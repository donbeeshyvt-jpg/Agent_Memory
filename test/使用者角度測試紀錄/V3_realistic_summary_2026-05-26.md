# V3 真實模擬壓測摘要 — 2026-05-26

對齊 goal 2026-05-26: 1 owner + 20 viewer + 注入 + 24h 直播 fast-forward.

## S1 真實聊天室 5min

- 總 turn: 150 (owner 30 / viewer 120 / injection 10)
- 注入攔截: 10 / 10
- 主動觸發 turn: 0
- Flow mode 切換次數: 1
- 異常 total: 0 (RED_LINE 0)
- 通過: ✅ PASS

### Final state (top users by interaction)

- `owner_main` intim=0.80 (親密), interactions=30, dominant=joy
- `v07_injector` intim=0.04 (初識), interactions=13, dominant=joy
- `v06_hostile` intim=0.03 (初識), interactions=9, dominant=joy
- `v01_loyal_fan` intim=0.02 (初識), interactions=6, dominant=joy
- `v02_quiet_regular` intim=0.02 (初識), interactions=6, dominant=joy
- `v03_curious_new` intim=0.02 (初識), interactions=6, dominant=joy

## S2 24h 直播 fast-forward

- 總 turn: 400 (owner 110 / viewer 290 / injection 8)
- 注入攔截: 8 / 8
- 主動觸發 turn: 19
- Flow mode 切換次數: 8
- 異常 total: 0 (RED_LINE 0)
- 通過: ✅ PASS

### Final state (top users by interaction)

- `owner_main` intim=0.80 (親密), interactions=140, dominant=joy
- `v07_injector` intim=0.10 (初識), interactions=34, dominant=joy
- `v06_hostile` intim=0.09 (初識), interactions=29, dominant=joy
- `v01_loyal_fan` intim=0.07 (初識), interactions=25, dominant=joy
- `v05_jokester` intim=0.07 (初識), interactions=25, dominant=joy
- `v16_normal_f` intim=0.07 (初識), interactions=25, dominant=joy

