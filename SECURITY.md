# Security policy

pyreaction runs your components on the server and talks to every open page over
a WebSocket, so a flaw in it can reach your server and your users. Reports are
welcome and are handled first.

## Supported versions

pyreaction is before its first release. Fixes go into the `main` branch; once
versions are published, the latest one will be supported.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub: on the repository's **Security** tab, choose
**Report a vulnerability**
([direct link](https://github.com/Jeomon/pyreaction/security/advisories/new)).
If you cannot use GitHub, email jeogeoalukka@gmail.com with "pyreaction security"
in the subject.

Include what you can of:

- what an attacker can do, and what they need (a visitor's page, a crafted
  WebSocket message, a request to the server);
- the pyreaction version or commit, Python version, and how the app is served;
- steps or a small app that shows it.

You will get an answer within a week. Once the problem is confirmed, a fix is
prepared privately and released with an advisory that credits you, unless you
prefer otherwise.

## What counts

In scope: anything in this repository that lets an attacker run code on the
server, read or change another visitor's session, bypass the WebSocket origin
check, get unescaped input into a page, or exhaust the server past the limits
pyreaction sets (`max_upload_size`, `max_pending_events`, `max_pending_ops`).

Out of scope: problems that need the app itself to pass untrusted input to
`dangerously_set_inner_html`, `js()`, `js_component_from_string` or similar
escape hatches, which the documentation says send their input as written.

[Deployment and security](docs/deployment.md#security) explains how to run
pyreaction safely.
