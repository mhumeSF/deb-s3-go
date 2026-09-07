# CI integration and dependency updates

`integration.py` runs the compiled CLI against the private S3 bucket
`meadow-checks-4827` in `us-east-1`. Each run uses `ci/<run-id>/<attempt>/`.
It tests signed and by-hash publication, identical and conflicting uploads,
query commands, lock acquisition/contention, upgrades, copying, safe cleanup,
and missing-object repair. With `--apt`, an isolated source list and local
HTTP mirror of S3 exercise signature validation, by-hash fetches, installation,
and upgrade using real APT. Installation changes the disposable runner's dpkg
state; only use `--apt` on a disposable Ubuntu amd64 machine.

Run the S3 tests locally with AWS credentials and GPG installed:

```sh
go build -o /tmp/deb-s3 ./cmd/deb-s3
python3 ci/integration.py --binary /tmp/deb-s3
```

The script deletes its S3 prefix on exit; CI has a second always-run cleanup.
The bucket expires objects after seven days and incomplete uploads after one.

GitHub uses the `meadow-checks-ci` IAM role via OIDC, without stored AWS keys.
`aws/trust.json` and `aws/permissions.json` record the role policies. The role
can list only `ci/` prefixes and read/write/delete only objects under `ci/`.
It cannot change bucket configuration or access other buckets. Its trust is
limited to this repository's PR events and main branch. Fork PRs fail the
integration gate; a maintainer can review and copy changes to a branch in this
repository to run the test. This bucket must never contain production data.

Branch protection requires `go`, `package`, `s3-integration`, and `docker-test`
and requires branches to be up to date. Dependabot patch and minor updates
are marked for native GitHub auto-merge; major updates remain manual. Grouped
updates use Dependabot's highest update type. Auto-merge runs in a separate
metadata-only workflow with write permission; PR code runs in ordinary CI.
