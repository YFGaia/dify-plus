# M07 13.5 CI and private image supply audit

## Inputs and method

- Upstream tag `1.17.1`, commit `8387590ace4a094de812b7847fc6a4c3a27cd52b`.
- Fork branch `codex/merge-upstream-1.17.1`, registered GitHub workflows and `.gitlab-ci.yml`.
- Read-only comparison: `git diff --exit-code 1.17.1 -- .github/workflows/build-push.yml .github/workflows/docker-build.yml` returned 0. No workflow ran and no credentials were read.

## GitHub build validation

- `.github/workflows/build-push.yml` is byte-identical to upstream. For a fork repository, the non-push validation job builds amd64 API, Web, Agent, and sandbox images with `push: false`. The official push/manifest jobs are guarded by `github.repository == 'langgenius/dify'`.
- The current branch `codex/merge-upstream-1.17.1` is outside the workflow's configured branch/tag filters, so this audit does not claim a CI run for this branch.
- `.github/workflows/docker-build.yml` is byte-identical to upstream and only selects Dockerfile/dependency-changing pull requests targeting `main`. Both its same-repository Depot path and cross-repository Buildx path use `push: false`; it is a build check, not fork image supply.

## Fork private image supply

- `.gitlab-ci.yml` separately defines tag-triggered multi-architecture API and Web pushes to `ccr.ccs.tencentyun.com/yfgaia/dify-plus-{api,web}`. It requires GitLab Docker runners and `DOCKER_USERNAME`/`DOCKER_PASSWORD`. The configured local remotes do not establish whether this pipeline is enabled, and no tag run or digest was verified.
- Static risk: the four architecture jobs write `manifest_api.txt` or `manifest_web.txt`, but do not declare artifacts; manifest jobs read those files in later jobs. The two architecture jobs for a service also use the same file name. Under isolated GitLab job workspaces, the manifest inputs may be absent or collide. This is a source-level inference; the pipeline was not run.
- Any tag can update `latest`. The pipeline does not supply the private plugin image or `sandbox-full` image used by the fork deployment.
- No CI edit was made: both registered GitHub workflow files exactly match upstream, while changing the separate fork release pipeline requires a new path registration and dedicated work item.

## Acceptance boundary

Green GitHub build validation cannot prove that private fork image tags exist, have expected digests, can be pulled on a deployment host, or include the matching Agent/plugin/sandbox images. Those checks remain for M07 13.6 or the later authorized environment gate. No token, tag, image pull, release, or push was performed in this audit.
