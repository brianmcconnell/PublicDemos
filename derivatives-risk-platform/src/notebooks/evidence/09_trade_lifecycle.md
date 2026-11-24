## Stage: 09_trade_lifecycle

* started (UTC): `2026-11-24 15:44:50`
* finished (UTC): `2026-11-24 15:48:44`  (234.4s)

**Checks**

| check | result | detail |
| --- | --- | --- |
| no live trade past maturity | PASS | 0 rows |
| no future maturity flags | PASS | 0 rows |
| IRS current fixing = market fixing event | PASS | 0 mismatches |
* mode: `incremental`
* trading days: `1`
* live trades: `168438`
* matured trades: `604187`
* events: `{'RESET': 10314, 'MATURED': 7174, 'NEW': 820}`
* runtime_s: `234`
