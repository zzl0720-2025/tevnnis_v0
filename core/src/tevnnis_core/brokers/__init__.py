"""Real broker adapters (§13: mock-first, then swap in).

`longbridge.LongbridgeTradeBroker` is the real TradePort. It is imported
lazily by the CLI so the default `--broker mock` path never loads the SDK.
"""
