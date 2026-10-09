# Swing Model B adjustment study

Paper only. This does not change the Monday paper defaults.

Fixed before the run. Costs stay on (fees, 25 bp main / 30 bp xyz stop slip, funding). Flow gates stay off because the venue has no historical aggressor tape. Halves are split at the midpoint of each frame's candle span, and a trade belongs to the half it opened in. Levels still use only bars that had closed by the decision. Improvement requires at least 20 trades, higher expectancy than that frame's reference in both halves, and higher expectancy on at least 4 of 6 coins. A half with fewer than 8 trades is thin and cannot pass. Profitable requires expectancy above zero in both halves on top of that. US hours are 13:30–20:00 UTC. A partial banks half at 1R or 2R and moves the stop to entry; the same bar trading back to entry stops the remainder. A stop on the same bar as the target still wins. Time-none is a 30-day cap so the sample can end the trade. Worst coins dropped: {'best-15m': 'BTC', 'struct-1h': 'xyz:XYZ100', 'daily-1h': 'xyz:SP500'}.

## Diagnosis

Primary label, in this order: macro result opposes the trade, stop was a wick (close within 0.25R of the stop) and the original target traded within 5 days, bad level (under 2 touches, or the next three 1h closes stayed through the level and the target did not come back), entry too early (favorable excursion under 0.25R and the target did not come back), time stop, stop narrower than the 25/30 bp slip allowance, otherwise other. Flags can overlap. The primary column is one label.

### Best book: 15m confirm, 1h+4h ADX, 4h leads (14 trades, 52d)

Loss labels: bad_level 4, stop_too_tight 4

| when | coin | side | exit | R | stop bp | touches | MFE R | target R | target later | primary | macro |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| 2026-08-19 08:15 | BTC | short | stop | -2.50 | 21 | 10 | 0.53 | 4.36 | False | bad_level | 1h=down(adx=28.5) 4h=up(adx=23.0) macro=range |
| 2026-08-18 17:15 | xyz:GOLD | short | stop | -2.56 | 26 | 6 | 3.53 | 4.99 | False | bad_level | 1h=down(adx=23.5) 4h=down(adx=21.3) macro=down |
| 2026-09-14 19:30 | xyz:GOLD | long | stop | -2.01 | 43 | 7 | 1.03 | 3.02 | True | stop_too_tight | 1h=down(adx=37.7) 4h=range(adx=17.4) macro=range |
| 2026-09-15 22:45 | xyz:GOLD | long | stop | -2.61 | 26 | 7 | 0.08 | 4.98 | True | stop_too_tight | 1h=up(adx=22.0) 4h=range(adx=17.1) macro=range |
| 2026-09-16 19:00 | xyz:GOLD | long | stop | -1.42 | 99 | 7 | 0.22 | 1.32 | True | stop_too_tight | 1h=up(adx=28.4) 4h=range(adx=17.9) macro=range |
| 2026-09-17 08:30 | xyz:XYZ100 | short | stop | -2.54 | 27 | 10 | 0.19 | 4.26 | False | bad_level | 1h=range(adx=15.7) 4h=down(adx=23.1) macro=down |
| 2026-09-17 13:00 | xyz:GOLD | short | stop | -1.77 | 52 | 7 | 1.34 | 3.00 | True | stop_too_tight | 1h=up(adx=21.4) 4h=range(adx=16.0) macro=range |
| 2026-10-08 18:30 | xyz:SP500 | short | stop | -2.44 | 29 | 15 | 0.54 | 2.48 | False | bad_level | 1h=down(adx=24.7) 4h=down(adx=27.3) macro=down |

| when | coin | side | exit | target R | MFE R | 5d after R | hold h |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| 2026-09-01 13:00 | xyz:GOLD | short | tp | 4.82 | 5.20 | 7.36 | 12.2 |
| 2026-09-11 19:00 | ETH | short | max_hold | 4.94 | 2.25 | 4.93 | 72.0 |
| 2026-09-16 15:00 | xyz:GOLD | short | tp | 4.19 | 6.83 | 8.63 | 3.8 |
| 2026-09-16 16:00 | xyz:XYZ100 | short | tp | 3.60 | 3.60 | 8.09 | 2.8 |
| 2026-09-17 02:15 | xyz:GOLD | long | tp | 2.37 | 2.87 | 3.49 | 10.2 |
| 2026-09-16 19:00 | ETH | long | tp | 2.81 | 2.91 | 8.79 | 42.8 |

### Baseline book: 15m confirm, 4h+daily ADX, both (16 trades, 52d)

Loss labels: bad_level 4, other 1, stop_too_tight 5

| when | coin | side | exit | R | stop bp | touches | MFE R | target R | target later | primary | macro |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| 2026-08-19 04:30 | BTC | short | stop | -2.26 | 24 | 10 | 1.05 | 3.69 | False | bad_level | 4h=up(adx=24.0) 1d=range(adx=13.2) macro=range |
| 2026-08-19 04:45 | xyz:XYZ100 | long | stop | -2.35 | 31 | 7 | 2.71 | 4.85 | False | other | 4h=down(adx=33.1) 1d=up(adx=21.0) macro=range |
| 2026-09-02 17:00 | xyz:GOLD | short | stop | -2.31 | 32 | 6 | 0.06 | 3.98 | False | bad_level | 4h=down(adx=57.3) 1d=down(adx=31.8) macro=down |
| 2026-09-14 19:30 | xyz:GOLD | long | stop | -2.01 | 43 | 7 | 1.03 | 3.02 | True | stop_too_tight | 4h=range(adx=17.4) 1d=down(adx=23.2) macro=range |
| 2026-09-15 22:45 | xyz:GOLD | long | stop | -2.61 | 26 | 7 | 0.08 | 4.98 | True | stop_too_tight | 4h=range(adx=17.1) 1d=down(adx=24.4) macro=range |
| 2026-09-16 19:00 | xyz:GOLD | long | stop | -1.42 | 99 | 7 | 0.22 | 1.32 | True | stop_too_tight | 4h=range(adx=17.9) 1d=down(adx=25.6) macro=range |
| 2026-09-17 08:30 | xyz:XYZ100 | short | stop | -2.54 | 27 | 10 | 0.19 | 4.26 | False | bad_level | 4h=down(adx=23.1) 1d=down(adx=22.8) macro=down |
| 2026-09-17 13:00 | xyz:GOLD | short | stop | -1.77 | 52 | 7 | 1.34 | 3.00 | True | stop_too_tight | 4h=range(adx=16.0) 1d=down(adx=25.6) macro=range |
| 2026-09-18 14:00 | xyz:GOLD | short | stop | -1.84 | 50 | 10 | 0.45 | 2.60 | True | stop_too_tight | 4h=up(adx=21.0) 1d=down(adx=25.2) macro=range |
| 2026-10-08 18:30 | xyz:SP500 | short | stop | -2.44 | 29 | 15 | 0.54 | 2.48 | False | bad_level | 4h=down(adx=27.3) 1d=range(adx=18.3) macro=range |

| when | coin | side | exit | target R | MFE R | 5d after R | hold h |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| 2026-09-01 13:00 | xyz:GOLD | short | tp | 4.82 | 5.20 | 7.36 | 12.2 |
| 2026-09-11 19:00 | ETH | short | max_hold | 4.94 | 2.25 | 4.93 | 72.0 |
| 2026-09-16 15:00 | xyz:GOLD | short | tp | 4.19 | 6.83 | 8.63 | 3.8 |
| 2026-09-16 16:00 | xyz:XYZ100 | short | tp | 3.60 | 3.60 | 8.09 | 2.8 |
| 2026-09-17 02:15 | xyz:GOLD | long | tp | 2.37 | 2.87 | 3.49 | 10.2 |
| 2026-09-16 19:00 | ETH | long | tp | 2.81 | 2.91 | 8.79 | 42.8 |

## Grid

Half columns are trades and expectancy for entries before and after the midpoint of that frame. A coin counts as better only when the variant traded it and its expectancy beat the reference.

### best-15m

15m book, 1h+4h ADX with 4h leading. About 52 days.

| config | trades | win | avg win R | avg loss R | E | net | max DD | H1 n/E | H2 n/E | coins better | thin | improves | profitable |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| room-2.0 | 2 | 100.0% | 1.42 | 0.00 | 1.419 | 2.44% | 1.97% | 1/0.066 | 1/2.772 | 0 | True | False | False |
| minr-3.0 | 12 | 50.0% | 2.95 | -2.27 | 0.336 | 2.72% | 4.88% | 4/-0.067 | 8/0.537 | 2 | True | False | False |
| session-us | 10 | 50.0% | 2.76 | -2.09 | 0.331 | 1.74% | 3.91% | 3/0.386 | 7/0.308 | 2 | True | False | False |
| drop-BTC | 13 | 46.2% | 2.85 | -2.19 | 0.136 | 1.29% | 4.88% | 3/0.745 | 10/-0.047 | 2 | True | False | False |
| room-1.0 | 11 | 45.5% | 2.77 | -2.15 | 0.083 | 1.30% | 4.33% | 3/0.745 | 8/-0.165 | 0 | True | False | False |
| levels-daily | 12 | 41.7% | 3.38 | -2.30 | 0.067 | 0.54% | 3.63% | 3/1.908 | 9/-0.547 | 2 | True | False | False |
| short-only | 9 | 44.4% | 3.02 | -2.36 | 0.030 | -0.43% | 3.41% | 4/-0.067 | 5/0.108 | 3 | True | False | False |
| time-48h | 14 | 42.9% | 2.98 | -2.23 | 0.001 | 0.89% | 4.29% | 4/0.120 | 10/-0.047 | 4 | True | False | False |
| ref | 14 | 42.9% | 2.85 | -2.23 | -0.053 | 0.26% | 4.88% | 4/-0.067 | 10/-0.047 | 0 | True | False | False |
| touches-2 | 14 | 42.9% | 2.85 | -2.23 | -0.053 | 0.26% | 4.88% | 4/-0.067 | 10/-0.047 | 0 | True | False | False |
| touches-3 | 14 | 42.9% | 2.85 | -2.23 | -0.053 | 0.26% | 4.88% | 4/-0.067 | 10/-0.047 | 0 | True | False | False |
| touches-4 | 14 | 42.9% | 2.85 | -2.23 | -0.053 | 0.26% | 4.88% | 4/-0.067 | 10/-0.047 | 0 | True | False | False |
| minr-1.5 | 14 | 42.9% | 2.85 | -2.23 | -0.053 | 0.26% | 4.88% | 4/-0.067 | 10/-0.047 | 0 | True | False | False |
| minr-2.0 | 14 | 42.9% | 2.85 | -2.23 | -0.053 | 0.26% | 4.88% | 4/-0.067 | 10/-0.047 | 0 | True | False | False |
| room-0.5 | 14 | 42.9% | 2.85 | -2.23 | -0.053 | 0.26% | 4.88% | 4/-0.067 | 10/-0.047 | 0 | True | False | False |
| time-none | 14 | 35.7% | 3.41 | -2.11 | -0.140 | -0.76% | 5.85% | 4/-0.373 | 10/-0.047 | 1 | True | False | False |
| time-24h | 14 | 42.9% | 2.60 | -2.23 | -0.162 | -1.02% | 3.79% | 4/0.036 | 10/-0.241 | 2 | True | False | False |
| atr-1.5 | 25 | 40.0% | 2.19 | -1.73 | -0.162 | -1.45% | 6.05% | 12/-0.011 | 13/-0.302 | 3 | False | False | False |
| long-only | 5 | 40.0% | 2.52 | -2.01 | -0.201 | 0.69% | 3.47% | 0/0.000 | 5/-0.201 | 1 | True | False | False |
| time-12h | 16 | 43.8% | 2.02 | -2.01 | -0.246 | -1.55% | 3.90% | 5/-0.212 | 11/-0.262 | 2 | True | False | False |
| atr-2.0 | 25 | 40.0% | 1.81 | -1.64 | -0.263 | -3.40% | 7.81% | 14/-0.301 | 11/-0.214 | 3 | False | False | False |
| levels-session | 74 | 33.8% | 2.86 | -1.97 | -0.337 | -10.23% | 17.38% | 28/0.277 | 46/-0.711 | 3 | False | False | False |
| partial-2r | 16 | 50.0% | 1.53 | -2.23 | -0.350 | -1.28% | 4.12% | 6/-0.160 | 10/-0.465 | 2 | True | False | False |
| levels-4h | 19 | 31.6% | 3.08 | -2.27 | -0.579 | -4.14% | 6.42% | 8/-0.503 | 11/-0.634 | 1 | False | False | False |
| partial-1r | 17 | 29.4% | 1.27 | -1.35 | -0.579 | -2.70% | 4.10% | 6/-0.493 | 11/-0.626 | 1 | True | False | False |
| atr-1.0 | 20 | 30.0% | 1.92 | -1.97 | -0.800 | -6.95% | 8.12% | 7/-1.101 | 13/-0.638 | 1 | True | False | False |
| confirm-1h | 85 | 22.4% | 2.30 | -2.08 | -1.101 | -35.18% | 37.40% | 76/-1.174 | 9/-0.480 | 2 | False | False | False |
| confirm-5m | 2 | 0.0% | 0.00 | -2.31 | -2.315 | -1.99% | 1.99% | 0/0.000 | 2/-2.315 | 0 | True | False | False |

### struct-1h

Same macro, entries on the 1h close. About 130 days.

| config | trades | win | avg win R | avg loss R | E | net | max DD | H1 n/E | H2 n/E | coins better | thin | improves | profitable |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| confirm-15m | 14 | 42.9% | 2.85 | -2.23 | -0.053 | 0.26% | 4.88% | 0/0.000 | 14/-0.053 | 3 | True | False | False |
| levels-daily | 22 | 36.4% | 2.70 | -1.79 | -0.161 | -2.77% | 6.42% | 7/-1.339 | 15/0.389 | 2 | True | False | False |
| atr-2.0 | 104 | 36.5% | 1.76 | -1.36 | -0.219 | -15.22% | 22.09% | 74/-0.386 | 30/0.191 | 6 | False | True | False |
| combo-2 | 26 | 38.5% | 1.37 | -1.44 | -0.360 | -5.23% | 7.17% | 9/-0.223 | 17/-0.433 | 5 | False | True | False |
| atr-1.5 | 105 | 32.4% | 1.86 | -1.49 | -0.406 | -23.82% | 27.71% | 79/-0.580 | 26/0.121 | 6 | False | True | False |
| levels-session | 152 | 29.6% | 2.48 | -2.01 | -0.682 | -33.74% | 36.36% | 74/-0.996 | 78/-0.384 | 5 | False | True | False |
| levels-4h | 90 | 26.7% | 2.83 | -1.98 | -0.696 | -26.39% | 28.11% | 66/-0.722 | 24/-0.624 | 4 | False | True | False |
| room-2.0 | 19 | 15.8% | 2.98 | -1.41 | -0.719 | -8.89% | 11.63% | 15/-0.994 | 4/0.310 | 3 | True | False | False |
| atr-1.0 | 96 | 26.0% | 1.94 | -1.70 | -0.751 | -33.97% | 36.19% | 73/-0.848 | 23/-0.442 | 5 | False | True | False |
| partial-1r | 104 | 31.7% | 0.71 | -1.54 | -0.824 | -31.96% | 32.60% | 80/-0.897 | 24/-0.580 | 5 | False | True | False |
| session-us | 45 | 24.4% | 2.37 | -1.94 | -0.889 | -18.74% | 19.21% | 35/-0.967 | 10/-0.615 | 2 | False | False | False |
| touches-4 | 62 | 29.0% | 2.49 | -2.33 | -0.929 | -17.57% | 21.14% | 42/-0.999 | 20/-0.780 | 5 | False | False | False |
| drop-xyz:XYZ100 | 70 | 21.4% | 2.46 | -1.94 | -0.994 | -29.43% | 31.85% | 54/-1.165 | 16/-0.420 | 0 | False | False | False |
| partial-2r | 89 | 28.1% | 1.64 | -2.02 | -0.996 | -33.11% | 35.16% | 68/-1.092 | 21/-0.682 | 3 | False | False | False |
| time-12h | 89 | 29.2% | 1.35 | -2.00 | -1.021 | -34.37% | 36.14% | 69/-1.116 | 20/-0.695 | 4 | False | True | False |
| room-1.0 | 48 | 14.6% | 3.06 | -1.73 | -1.036 | -24.49% | 28.52% | 35/-1.314 | 13/-0.289 | 2 | False | False | False |
| touches-3 | 74 | 25.7% | 2.26 | -2.19 | -1.047 | -27.36% | 29.85% | 55/-1.141 | 19/-0.776 | 5 | False | True | False |
| minr-2.0 | 76 | 22.4% | 2.46 | -2.08 | -1.069 | -31.33% | 33.69% | 57/-1.167 | 19/-0.776 | 3 | False | False | False |
| time-48h | 85 | 23.5% | 2.19 | -2.08 | -1.078 | -34.43% | 36.68% | 66/-1.169 | 19/-0.764 | 3 | False | False | False |
| short-only | 55 | 23.6% | 2.54 | -2.22 | -1.095 | -23.00% | 23.00% | 43/-1.199 | 12/-0.721 | 3 | False | False | False |
| touches-2 | 84 | 22.6% | 2.30 | -2.09 | -1.099 | -34.53% | 36.96% | 65/-1.193 | 19/-0.776 | 1 | False | False | False |
| time-none | 84 | 21.4% | 2.47 | -2.07 | -1.099 | -35.11% | 37.33% | 66/-1.167 | 18/-0.853 | 1 | False | False | False |
| ref | 85 | 22.4% | 2.30 | -2.08 | -1.101 | -35.18% | 37.40% | 66/-1.194 | 19/-0.776 | 0 | False | False | False |
| minr-1.5 | 80 | 21.2% | 2.46 | -2.06 | -1.102 | -34.05% | 36.31% | 61/-1.204 | 19/-0.776 | 2 | False | False | False |
| long-only | 30 | 20.0% | 1.79 | -1.84 | -1.111 | -15.84% | 20.49% | 23/-1.184 | 7/-0.871 | 3 | True | False | False |
| time-24h | 85 | 23.5% | 1.91 | -2.05 | -1.119 | -36.13% | 37.19% | 66/-1.172 | 19/-0.935 | 2 | False | False | False |
| room-0.5 | 69 | 17.4% | 2.58 | -1.90 | -1.124 | -32.56% | 34.87% | 51/-1.280 | 18/-0.681 | 1 | False | False | False |
| minr-3.0 | 58 | 19.0% | 2.66 | -2.16 | -1.244 | -27.17% | 30.15% | 42/-1.268 | 16/-1.180 | 3 | False | False | False |
| confirm-5m | 2 | 0.0% | 0.00 | -2.31 | -2.315 | -1.99% | 1.99% | 0/0.000 | 2/-2.315 | 0 | True | False | False |

### daily-1h

4h and daily levels, ADX on 4h and daily, entries on the 1h close. About 130 days.

| config | trades | win | avg win R | avg loss R | E | net | max DD | H1 n/E | H2 n/E | coins better | thin | improves | profitable |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| levels-daily | 22 | 36.4% | 2.66 | -1.76 | -0.155 | -3.08% | 6.71% | 6/-1.339 | 16/0.289 | 3 | True | False | False |
| atr-2.0 | 91 | 36.3% | 1.81 | -1.42 | -0.249 | -13.68% | 18.97% | 54/-0.336 | 37/-0.122 | 5 | False | True | False |
| confirm-15m | 16 | 37.5% | 2.85 | -2.16 | -0.278 | -1.76% | 4.88% | 0/0.000 | 16/-0.278 | 3 | True | False | False |
| combo-2 | 30 | 36.7% | 1.47 | -1.32 | -0.296 | -6.07% | 7.87% | 11/-0.469 | 19/-0.195 | 5 | False | True | False |
| atr-1.5 | 94 | 30.9% | 2.00 | -1.49 | -0.414 | -20.41% | 21.52% | 62/-0.481 | 32/-0.282 | 5 | False | True | False |
| atr-1.0 | 82 | 28.0% | 2.46 | -1.67 | -0.510 | -20.82% | 23.30% | 55/-0.567 | 27/-0.394 | 5 | False | True | False |
| levels-session | 140 | 31.4% | 2.62 | -2.11 | -0.622 | -22.96% | 24.90% | 47/-0.521 | 93/-0.673 | 3 | False | False | False |
| room-2.0 | 18 | 22.2% | 2.09 | -1.41 | -0.631 | -6.51% | 9.98% | 14/-1.112 | 4/1.053 | 3 | True | False | False |
| drop-xyz:SP500 | 49 | 28.6% | 2.22 | -1.97 | -0.769 | -15.69% | 20.11% | 31/-1.008 | 18/-0.359 | 0 | False | False | False |
| partial-1r | 80 | 30.0% | 0.92 | -1.59 | -0.835 | -23.30% | 24.06% | 56/-1.053 | 24/-0.327 | 5 | False | True | False |
| touches-4 | 63 | 28.6% | 2.47 | -2.25 | -0.900 | -18.81% | 22.24% | 41/-1.119 | 22/-0.491 | 2 | False | False | False |
| room-1.0 | 43 | 20.9% | 2.46 | -1.79 | -0.900 | -17.76% | 21.36% | 28/-1.242 | 15/-0.262 | 2 | False | False | False |
| minr-3.0 | 54 | 24.1% | 2.90 | -2.13 | -0.918 | -18.50% | 22.99% | 38/-1.066 | 16/-0.565 | 5 | False | True | False |
| partial-2r | 69 | 29.0% | 1.69 | -2.01 | -0.937 | -23.64% | 25.90% | 48/-1.155 | 21/-0.437 | 3 | False | False | False |
| short-only | 41 | 26.8% | 2.53 | -2.32 | -1.020 | -15.82% | 16.40% | 30/-1.328 | 11/-0.181 | 4 | False | False | False |
| session-us | 41 | 24.4% | 2.21 | -2.10 | -1.047 | -17.31% | 17.66% | 29/-1.285 | 12/-0.473 | 3 | False | False | False |
| room-0.5 | 60 | 21.7% | 2.37 | -2.02 | -1.066 | -25.21% | 27.70% | 40/-1.299 | 20/-0.600 | 1 | False | False | False |
| time-48h | 68 | 25.0% | 2.15 | -2.16 | -1.081 | -26.58% | 29.02% | 48/-1.285 | 20/-0.592 | 3 | False | False | False |
| levels-4h | 77 | 20.8% | 2.44 | -2.01 | -1.083 | -32.75% | 32.75% | 47/-1.125 | 30/-1.017 | 2 | False | False | False |
| touches-3 | 61 | 26.2% | 2.15 | -2.23 | -1.084 | -23.28% | 25.83% | 41/-1.319 | 20/-0.600 | 1 | False | False | False |
| ref | 67 | 23.9% | 2.28 | -2.14 | -1.084 | -26.84% | 29.27% | 47/-1.290 | 20/-0.600 | 0 | False | False | False |
| touches-2 | 67 | 23.9% | 2.28 | -2.14 | -1.084 | -26.84% | 29.27% | 47/-1.290 | 20/-0.600 | 0 | False | False | False |
| minr-2.0 | 62 | 22.6% | 2.47 | -2.14 | -1.102 | -25.71% | 28.89% | 43/-1.348 | 19/-0.547 | 2 | False | False | False |
| time-12h | 76 | 25.0% | 1.43 | -1.95 | -1.105 | -30.79% | 32.59% | 56/-1.323 | 20/-0.496 | 3 | False | False | False |
| time-24h | 70 | 25.7% | 1.92 | -2.16 | -1.114 | -28.68% | 29.80% | 50/-1.250 | 20/-0.773 | 2 | False | False | False |
| minr-1.5 | 64 | 21.9% | 2.47 | -2.15 | -1.135 | -27.18% | 29.60% | 44/-1.378 | 20/-0.600 | 2 | False | False | False |
| time-none | 67 | 19.4% | 2.55 | -2.12 | -1.217 | -31.55% | 33.82% | 47/-1.374 | 20/-0.849 | 0 | False | False | False |
| long-only | 30 | 16.7% | 1.73 | -1.91 | -1.307 | -16.50% | 20.47% | 21/-1.391 | 9/-1.112 | 2 | False | False | False |
| confirm-5m | 2 | 0.0% | 0.00 | -2.31 | -2.315 | -1.99% | 1.99% | 0/0.000 | 2/-2.315 | 0 | True | False | False |

## Verdict

No config is profitable in both halves. These beat the reference on both halves, on at least four coins, with at least 20 trades and neither half thinner than 8: struct-1h/atr-2.0 E -0.219R, struct-1h/combo-2 E -0.360R, struct-1h/atr-1.5 E -0.406R, struct-1h/levels-session E -0.682R, struct-1h/levels-4h E -0.696R, struct-1h/atr-1.0 E -0.751R, struct-1h/partial-1r E -0.824R, struct-1h/time-12h E -1.021R, struct-1h/touches-3 E -1.047R, daily-1h/atr-2.0 E -0.249R, daily-1h/combo-2 E -0.296R, daily-1h/atr-1.5 E -0.414R, daily-1h/atr-1.0 E -0.510R, daily-1h/partial-1r E -0.835R, daily-1h/minr-3.0 E -0.918R. They are less bad, not a reason to go live. Monday paper stays on the spec defaults.
