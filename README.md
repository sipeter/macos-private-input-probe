# Private-input iOS build probe

This small tool builds an unsigned iOS sample from a private test fixture.
Its own source is public. Its integration fixture and results use a separate private repository.
It tests private Git transfer, the Apple compiler, Metal compilation, IPA packaging, and diagnostic containment.
It contains no third-party app source, game data, signing certificate, or installable signed app.

## Run locally

Run `python -m unittest discover -s tests -v` for the portable containment checks.
The integration build needs an ARM64 Mac with Xcode 27.2 and Python 3.

Set these environment variables for a trusted integration run:

- `FIXTURE_REPOSITORY`: SSH URL of the dedicated private test repository.
- `FIXTURE_REVISION`: full commit SHA of the reviewed synthetic fixture.
- `FIXTURE_DEPLOY_KEY`: a deploy key for that test repository only.

The fixture has `main.m`, `sample.metal`, and a `marker.txt` canary.
The tool fetches the exact fixture commit. It compiles the sample and a Metal library for iOS.
It also checks that a deliberate compiler error containing the canary stays in private diagnostics.
It pushes `Probe.ipa`, `report.json`, and diagnostic files to a new result branch in the private repository.
It emits only fixed status text to the public job log. It uploads no Actions artifacts or caches.
The IPA is unsigned. No device installation is part of the test.

## Access boundary

The workflow accepts manual runs from the repository owner only.
Review all code before providing private credentials. Do not run untrusted changes with those credentials.
The supplied credential must access only the synthetic fixture repository.
Revoke the test deploy key and remove the workflow secrets when the integration test ends.
Public logs still expose workflow code, run metadata, and runner information.
GitHub processes the private input during the build. This test does not hide it from the service provider.

This probe tests one small fixture and selected failure paths.
It does not prove that arbitrary build scripts or production inputs cannot leak information.
Standard public hosted runners have GitHub's documented free-compute treatment.
A successful test does not establish eligibility of a different project's workload or verify an account invoice.

## Recorded integration result

[Run 41978302673](https://github.com/sipeter/macos-private-input-probe/actions/runs/41978302673) passed on 2026-11-05.
Tested source: `7557686c768bd884e72088503ceec9b5cec43098`.
The private fixture compiled into an iOS ARM64 executable and a Metal library.
The unsigned sample IPA and the deliberate compiler-error diagnostic were stored in the private result repository.
Six portable checks and the downloaded IPA file checks passed.
The private canary was absent from all four public test logs and the code history checked after the final run.
There were no public Actions artifacts or cache entries.
The run timing endpoint returned an empty `billable` object. An account invoice was not independently checked.

The earlier transfer failure came from CRLF in the deploy key secret sent from Windows.
The tool now normalizes that text before macOS SSH reads the key.
The test credential was revoked and the three workflow secrets were removed.
The manual workflow is disabled after the test. Configure a new scoped credential before another authorized run.
Large result storage, arbitrary private build scripts, and device installation remain outside this test.
