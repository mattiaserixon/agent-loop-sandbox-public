Feature freeze is on: the loop hardens what exists and builds nothing new.
- Allowed: bug fixes, security hardening, tests, docs, performance fixes that add no new surface, removing code.
- Not allowed: new routes or endpoints, new settings or config keys, new outbound network calls (egress), new
  dependencies, new UI features, new stored data fields.
- Triage: a feature request is kept for after the freeze: verdict `deferred` with `--frozen`.
- Review: a PR that adds anything on the not-allowed list gets a blocking finding (severity medium, introduced,
  summary "feature under the freeze"), unless the owner has approved it; the approval reaches the loop as a command,
  not as text in the PR.
- Fix: if the task can only be done by adding something on the not-allowed list, stop and finish with outcome
  `blocked` and a note saying what it would add.
