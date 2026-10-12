# Institutional decisions

These decisions belong to the institution, not to the software. Each row says what the
system does today if nobody decides, and what the options are. Record the decision, who
made it and the date before go-live. The decisions marked **before pilot** block the
pilot.

## Identity and access

| # | Decision | What the code does today | Options | When |
|---|---|---|---|---|
| I1 | How users sign in | Local accounts only: username and password, Django's default validators (minimum 8 characters, not common, not all digits, not similar to the username). No SSO, no MFA, no password expiry. Repeated failures are throttled (10 per minute per address and name, 100 per hour per account), and nothing locks an account | (a) local accounts for the pilot; (b) put the site behind the institution's VPN or an identity-aware proxy; (c) require SSO/MFA first, which needs development work | **before pilot** |
| I2 | Who may reach `/django-admin/` | Reachable from the internet with superuser password only; 1 request/s per address at the edge; failures audited | (a) leave as is; (b) allow it only from the admin network or VPN (an nginx `allow`/`deny` rule) | **before pilot** |
| I3 | Who the first administrators are, and which departments exist | Nothing is created automatically except the first superuser | name a system administrator, and a department administrator per pilot department | **before pilot** |
| I4 | Whether students upload | Any signed-in user may upload into their own department and delete what they own | keep it, or limit the pilot to staff accounts | before pilot |
| I5 | Who approves production deploys | The CI `production` environment is meant as an approval gate, but no reviewers are configured, because no GitHub repository exists yet | name the reviewers in the repository's environment settings | before production |

## Data

| # | Decision | What the code does today | Options | When |
|---|---|---|---|---|
| D1 | Which documents enter the pilot | Anything a user uploads | a named set of departments and document classes; personal data excluded unless the data-protection officer approves | **before pilot** |
| D2 | Whether any hosted AI provider may receive document text | None does: models run locally and the answerer is extractive | (a) none; (b) a named provider, region and contract, with free tiers excluded and a spend cap set at the provider ([ai-release.md](ai-release.md#hosted-answerer-activation)) | before enabling one |
| D3 | How long audit rows are kept | Forever: nothing prunes them. A deleted document's title stays on its delete row | a retention period and a pruning job (not built) | before production |
| D4 | How long chat history is kept | Forever. A conversation keeps the citation text the user was shown, even after their access changes | a retention period (not built), or accept it as a record | before production |
| D5 | Languages | Both local models are English models | confirm the pilot corpus is English, or plan a multilingual evaluation | **before pilot** |
| D6 | Who writes and checks the institutional evaluation questions | No institutional evaluation set exists | staff from the pilot departments ([ai-release.md](ai-release.md#real-institutional-data-plan)) | **before pilot** |
| D7 | AI acceptance thresholds | Proposed numbers only | adopt or change them ([ai-release.md](ai-release.md#acceptance-criteria-proposed-the-institution-sets-the-numbers)) | **before pilot** |

## Security

| # | Decision | What the code does today | Options | When |
|---|---|---|---|---|
| S1 | Malware scanning | Off unless `DOCUMENT_SCANNER` is set. The bundled heuristic scanner quarantines executables, `#!` scripts and EICAR, and only flags macros and PDF active content. It is not an antivirus | (a) heuristic scanner (the recommended pilot setting); (b) ClamAV or a commercial scanner behind the same interface (needs integration work); (c) off | **before pilot** |
| S2 | Downloads before the scan finishes | A new upload can be downloaded by its readers until its scan completes | accept for the pilot, or hold downloads and previews until scanned (needs development work) | before pilot |
| S3 | HSTS | Off (`SECURE_HSTS_SECONDS=0`) | turn it on once the real certificate has renewed at least once; it is close to irreversible for the domain | after TLS is stable |
| S4 | Certificate source | Let's Encrypt over HTTP-01 (`init-letsencrypt.sh`) | Let's Encrypt, or the institution's certificate, or TLS terminated at an institutional load balancer | **before staging** |
| S5 | Redis and container hardening | Redis has no password and is reachable only from the compose network. Application containers run as uid 10001; there is no capability dropping, read-only root filesystem or network segmentation | accept for a single host, or ask the security team for a hardening pass | before production |
| S6 | Encryption at rest | None in the application; it relies on the disk or volume | encrypted disks on the host (the provider's or LUKS) | before production |

## Operations

| # | Decision | What the code does today | Options | When |
|---|---|---|---|---|
| O1 | Where backups are copied, how they are encrypted, who holds the key | Daily dumps on the same host, 7 kept. Nothing leaves the host | an off-host location; `age` or `gpg` or the backup system's own encryption; named key custodians ([staging runbook step 16](staging-runbook.md#16-backups)) | **before pilot** |
| O2 | Recovery targets (RPO, RTO) | RPO is the backup interval (24 h). RTO has not been measured beyond a 157 KB dump | targets agreed with the departments, then a timed restore on a production-sized copy | before production |
| O3 | Where alerts go and who responds | Prometheus collects metrics. No alert rules ship and nothing delivers alerts | an alert channel (email, chat) and an on-call rota | **before pilot** |
| O4 | Server and hostname | No server exists yet | sizing in [staging runbook step 1](staging-runbook.md#1-server); a staging and a production hostname | **before staging** |
| O5 | Mail relay | Nothing in the application sends mail yet. Passwords are reset by an administrator in the app; there is no self-service reset. Without a relay, Django's `check --deploy` flags the console backend | the institution's SMTP relay, so mail added later does not silently vanish | before production |
| O6 | Hardware for chat latency | CPU only; chat measured at 1.57 s p50 with the recommended MiniLM reranker, 13.8 s with bge-reranker-base | CPU with the MiniLM reranker (recommended), or a GPU host | before pilot |
