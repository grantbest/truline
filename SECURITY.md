# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Use GitHub's private vulnerability reporting on this repository: **Security → Report a
vulnerability**. It reaches the maintainer directly and nobody else.

You will get an acknowledgement within seven days. Reports that reproduce get a fix or a written
mitigation before any public disclosure, and credit in the release notes if you want it.

## What is in scope

- The substrate (`apps/substrate/`): authentication, the encryption of bead content, the API.
- The gateway (`apps/mcp-hub/`): the identity contract, scope checks, the substrate proxy.
- The factory dispatcher (`apps/factory-dispatcher/`): isolation of the worker, containment of
  what it can write, the release gate.
- The console (`apps/lifeops-console/`): anything that would expose an API key to a browser.

## What is not in scope

- A deployment's own infrastructure (Kubernetes, Cloudflare Access, the launchd worker host).
  Those manifests are not in this repository; the operator keeps them.
- The example personal-finance vertical's data model as a target of financial advice. It is a
  worked example of an extension, not a product.

## How secrets are handled here

The code refuses to start without the configuration it needs rather than guessing; no credential
is compiled in. Bead content is encrypted at the application layer with a key the operator
supplies. The browser never holds the substrate API key; it reaches the store through a
server-side proxy that injects it. `scripts/private_identifiers.py` and a gitleaks configuration
run on every pull request.
