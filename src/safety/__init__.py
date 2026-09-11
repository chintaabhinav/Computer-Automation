"""Guardrails: domain allowlist, risky-action policy, and secret redaction for logs and evidence.

Three things are enforced here. Where the automation may go (`policy.allowed_url_patterns`),
what it may do (`policy.allowed_actions` plus `risky_action_mode`), and what may be written down
(`src.evidence.redact`, reused rather than reimplemented -- one masking function, so the
recorder and the policy layer cannot disagree about what counts as sensitive).

Enforcement is structural: `GuardedSurface` implements the `Surface` ABC and wraps the real one,
so every action any caller performs passes through policy on its way to the driver. See
`guard.py` for the honest limits of that guarantee.

LIMITS -- what this layer deliberately does not do
==================================================

Each of these is a conscious omission rather than an oversight, and each would be required
before this drove a real back office.

* **No RBAC or user roles.** There are no users in this system: it is a single-operator tool
  invoked from a CLI, so there is no principal to authorize and no role to check. A real
  deployment needs an authenticated caller, per-capability authorization ("this service account
  may replay balance lookups but not open accounts"), and the operator's identity recorded in
  every evidence directory.
* **No rate limiting or circuit breakers.** A replay loop pointed at a live core banking system
  can generate load no human ever would, and a broken target plus an eager retry is how an
  incident becomes an outage. Production needs per-capability rate limits, a breaker that trips
  on a rising failure rate, and a global kill switch. The bounded recovery in the replay engine
  limits one run, not a fleet of them.
* **No secrets management beyond `.env`.** A file on disk read into the process environment is
  adequate for a single API key on a developer machine and nothing more. Real deployments need a
  secret manager with rotation, short-lived credentials, and audited access -- and the target
  application's own credentials, which this project never handles because the mock app has no
  login, are a harder problem than the model key.
* **No policy DSL.** A flat config of URL patterns and action types is enough at this scale, and
  a small readable file that a compliance reviewer can check beats an expressive language nobody
  fully understands. Richer needs -- per-tenant rules, time-of-day windows, amount thresholds,
  four-eyes approval on specific screens -- would justify a real policy engine, evaluated the
  same way but expressed properly.
* **No encryption at rest.** Evidence directories and artifacts are plain files with ordinary
  filesystem permissions. Redaction reduces what is written, but a deployment handling real
  member data needs encrypted volumes, retention limits, and deletion on request.
* **No image redaction.** Text redaction cannot touch pixels: the screenshots in `evidence/`
  show whatever was on screen, including balances and identifiers. That is acceptable only
  because the mock app serves fabricated data and says so on every page. A real deployment must
  either mask regions before writing, restrict screenshots to failures under access control, or
  not take them at all.

Two more worth naming because they are adjacent and easy to assume are covered: this layer does
not verify that the *target* application is genuinely the one intended (no certificate pinning
or host attestation -- an allowlist trusts DNS), and it does not defend against a compromised
artifact author, since anyone who can edit an artifact can direct the automation at anything the
policy permits. Artifact review is the control there, which is why the format is designed to be
read.
"""
