# CyberEditor Agent maintainer and AI guide

CyberEditor is local-first. RAW media, extracted evidence, Ollama/CUDA,
FFmpeg, and DaVinci Resolve stay on the Windows editing workstation. The public
host runs only the standard-library control plane in `src/control_plane.py`.

## Non-negotiable boundaries

- Workers make outbound HTTPS requests; never expose a worker, Ollama, Resolve,
  SMB share, or local picker port to the Internet.
- Admin and worker tokens are distinct and never enter Git. Admin tokens stay
  in browser session storage; worker tokens never reach the browser.
- The `?demo=1` path uses the real Web Studio with a deterministic browser
  adapter. It must not call authenticated APIs or pretend to process real media.
- Keep preview uploads bounded per file, at 512 MiB total, and seven days by
  default. Do not remove managed-path validation or artifact pruning.
- Preserve strict serial execution and explicit GPU-process exit boundaries.

## Required checks and deployment

Run `python -m compileall -q main.py src tests`,
`python -m unittest discover -s tests -v`, and `node --check web/app.js`.
Platform-specific GUI tests should also run on Windows, which is the required CI
environment.

Branch from protected `main`, open a PR, and merge after CI passes. A merge
packages only `control_plane.py`, `src`, and `web`; the repository-scoped runner
calls `deploy-cybereditor-control`. Caddy serves
`https://tonytan.me/cybereditor/` and strips that prefix before proxying to
`127.0.0.1:4020`. Releases are immutable under
`/srv/cybereditor/releases`; persistent bounded state lives under
`/var/lib/cybereditor`.

Host service, Caddy, runner, sudo, and recovery definitions live in
`Personal-Website/ops`. Never commit media, previews, databases, tokens, `.env`,
PEM files, model weights, or Resolve credentials.
