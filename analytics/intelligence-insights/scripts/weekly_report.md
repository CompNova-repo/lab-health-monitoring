# mock-network-target-03 - Weekly Health Report
**Period:** 2026-07-09 to 2026-07-15

## Executive Summary
Machine mock-network-target-03 is generally healthy with low resource utilization. However, it is experiencing persistent network instability, characterized by significant packet loss and high latency spikes.

## Core Metrics
| Metric | Avg | Max |
| :--- | :--- | :--- |
| CPU % | 8.02% | 12.0% |
| RAM % | 29.53% | 31.0% |
| Disk % | 20.0% | 20.0% |
| Net Rx | 45,000 B/s | 45,000 B/s |
| Net Tx | 40,000 B/s | 40,000 B/s |

## Correlation Insights
- 🔗 **Correlation Detected:** `Packet Loss ↔ Network Latency` has a Pearson coefficient of `0.9327`. High `packet_loss_pct` strongly correlates with `net_latency_ms`, suggesting congestion leading to dropped packets (usually a network-layer issue).

## Fleet Context
- **open_files** averages 0.0 vs. fleet average of 1222.4 (z-score: -85.42).
- **established_connections** averages 0.0 vs. fleet average of 14.57 (z-score: -9.65).

## Efficiency
💰 **Efficiency Insight:** This machine is over-provisioned. Peak usage over 7 days never exceeded `12.0%` CPU / `31.0%` RAM. Downsizing the instance class by one tier would save resources without risking stability.

## Health Verdict
🟡 **STABLE (NETWORK DEGRADED)** - Compute and memory are healthy and underutilized, but network reliability is poor.
