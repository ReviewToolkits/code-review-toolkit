# Quayside completes its first security audit

Our first external security audit was performed by an independent firm. Full report:
https://example.invalid/reviews/2026-quayside-securityreview.pdf

| ID | Title | Severity | Difficulty | Status |
| :-- | :-- | :-- | :-- | :-- |
| QS-1 | Re-authentication form verifies the password of the submitted username instead of the session user | Low | High | [Remediated](https://example.invalid/pull/101) |
| QS-2 | Unauthenticated ReDoS in the search endpoint blocks all workers | Medium | Low | [Remediated](https://example.invalid/pull/102) |
| QS-3 | Team members can grant the owner role without the manage permission | High | Medium | [Remediated](https://example.invalid/pull/103) |
| QS-4 | Reset-password responses reveal which accounts are staff | Low | High | [Remediated](https://example.invalid/pull/104) |
| QS-5 | Project token deletions missing from the security log | Low | Low | [Remediated](https://example.invalid/pull/105) |
| QS-6 | Address blocklist not enforced for API token requests | Informational | High | Accepted |
| QS-7 | Denial of service risk on compressed archive uploads | Informational | Medium | Accepted |
| QS-8 | Staff role changes are not written to the security log | Informational | High | [Remediated](https://example.invalid/pull/108) |

QS-6 and QS-7 were accepted: blocked addresses cannot log in, and archive uploads need an
authenticated, rate-limited account.
