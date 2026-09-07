// Committed sample snapshot, so frontend/dashboard.html renders by double-click
// with no core run and no server. Regenerate with:
//   cd core && uv run python scripts/make_demo_snapshot.py
// The live file (public_snapshot.js) is loaded after this one and overwrites it
// whenever tevnnis-core has published a real snapshot into this directory.
window.__TEVNNIS_SNAPSHOT__ = {
  "schema_version": 1,
  "generated_at": "2026-09-02T15:00:00Z",
  "status": "running",
  "demo": true,
  "performance": {
    "index_current": 101.42,
    "index_start": 100.0,
    "cumulative_pct": 1.42,
    "today_pct": 0.37,
    "series": {
      "1D": [
        100.0,
        100.37
      ],
      "1W": [
        100.0,
        100.03,
        100.05,
        100.08,
        100.1,
        100.13,
        100.15,
        100.19,
        100.56
      ],
      "1M": [
        100.0,
        100.03,
        100.05,
        100.08,
        100.1,
        100.13,
        100.16,
        100.18,
        100.21,
        100.23,
        100.26,
        100.29,
        100.31,
        100.34,
        100.36,
        100.39,
        100.41,
        100.44,
        100.47,
        100.49,
        100.52,
        100.54,
        100.57,
        100.6,
        100.62,
        100.65,
        100.67,
        100.7,
        100.73,
        100.75,
        100.79,
        101.16
      ],
      "ALL": [
        100.0,
        100.03,
        100.05,
        100.08,
        100.1,
        100.13,
        100.16,
        100.18,
        100.21,
        100.23,
        100.26,
        100.29,
        100.31,
        100.34,
        100.36,
        100.39,
        100.42,
        100.44,
        100.47,
        100.49,
        100.52,
        100.55,
        100.57,
        100.6,
        100.62,
        100.65,
        100.68,
        100.7,
        100.73,
        100.75,
        100.78,
        100.81,
        100.83,
        100.86,
        100.88,
        100.91,
        100.94,
        100.96,
        100.99,
        101.01,
        101.05,
        101.42
      ]
    }
  },
  "positions": [
    {
      "symbol": "GOOGL.US",
      "weight_pct": 11.6,
      "last": 201.44,
      "today_pct": -0.4,
      "trend": []
    },
    {
      "symbol": "NVDA.US",
      "weight_pct": 69.4,
      "last": 231.09,
      "today_pct": 0.9,
      "trend": [
        229.0,
        229.8,
        230.1,
        229.6,
        230.9,
        231.1,
        231.09
      ]
    },
    {
      "symbol": "SPY.US",
      "weight_pct": 19.1,
      "last": 773.62,
      "today_pct": 1.1,
      "trend": []
    }
  ],
  "telemetry": {
    "events_seen": 4,
    "events_acted": 2,
    "decisions_total": 3,
    "decisions_hold": 1,
    "decisions_act": 2,
    "risk_checks": 2,
    "risk_cleared": 1,
    "risk_blocked": 1,
    "positions_open": 3,
    "win_rate": null
  },
  "news": [
    {
      "time": "11:00",
      "symbol": "NVDA.US",
      "headline": "Analyst lifts price target on data-center demand",
      "url": "https://www.reuters.com/business/nvda-pt"
    },
    {
      "time": "10:59",
      "symbol": "SPY.US",
      "headline": "US equities extend gains into the afternoon session",
      "url": "https://longbridge.com/news/n-002"
    },
    {
      "time": "10:58",
      "symbol": "AMD.US",
      "headline": "New accelerator lineup detailed at industry conference",
      "url": null
    }
  ],
  "reasoning": {
    "latest": {
      "ts": "2026-09-02T14:55:00Z",
      "action": "HOLD",
      "symbol": null,
      "model": "claude-opus-4-8",
      "risk": "none",
      "thesis": "highest priority in batch is MEDIUM"
    },
    "recent": [
      {
        "time": "10:55",
        "action": "HOLD",
        "thesis": "highest priority in batch is MEDIUM",
        "risk": "none"
      },
      {
        "time": "10:45",
        "action": "SELL",
        "thesis": "Trim into strength.",
        "risk": "blocked"
      },
      {
        "time": "10:30",
        "action": "BUY",
        "thesis": "Portfolio equity is [redacted] with [redacted] in cash; sold [redacted] of NVDA because [redacted] was close. Account [redacted] looks fine.",
        "risk": "cleared"
      }
    ]
  },
  "footer": {
    "universe": [
      "NVDA.US",
      "AMD.US",
      "XOM.US",
      "GOOGL.US",
      "META.US",
      "SPY.US",
      "QQQ.US",
      "VOO.US"
    ],
    "held": [
      "GOOGL.US",
      "NVDA.US",
      "SPY.US"
    ],
    "persona": [
      "moderate",
      "long term"
    ],
    "model": "claude-opus-4-8",
    "ai_cost_today": 0.09
  }
};
