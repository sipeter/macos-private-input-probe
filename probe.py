"""Compile a reviewed private iOS fixture without publishing its diagnostics."""

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import struct
import subprocess
import tempfile
import zipfile

PHASE = "initialization"


class ProbeFailure(Exception):
    """Only fixed public failure messages are allowed."""


def run_private(command, *, cwd, log, env=None, expected=0, timeout=180):
    """Keep both process streams and exception details in a private file."""
    with Path(log).open("ab") as output:
        try:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            output.write((type(error).__name__ + "\n").encode())
            raise ProbeFailure("Private process failed.") from None
    if result.returncode != expected:
        diagnostic = Path(log).read_bytes()[-16384:]
        for fragment, category in [
            (b"invalid format", "ssh-key-format"),
            (b"error in libcrypto", "ssh-key-format"),
            (b"Host key verification failed", "ssh-host-verification"),
            (b"Permission denied (publickey)", "ssh-authentication"),
            (b"Repository not found", "repository-access"),
            (b"Could not resolve hostname", "ssh-dns"),
            (b"Connection refused", "ssh-connection-refused"),
            (b"Connection timed out", "ssh-connection-timeout"),
            (b"couldn't find remote ref", "fixture-revision"),
            (b"cannot run ssh", "ssh-unavailable"),
            (b"Bad owner or permissions", "ssh-permissions"),
            (b"bad permissions", "ssh-permissions"),
            (b"Too many authentication failures", "ssh-authentication-count"),
        ]:
            if fragment in diagnostic:
                raise ProbeFailure("Private process failed: " + category + ".")
        raise ProbeFailure("Private process failed.")


def validate_revision(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ProbeFailure("Invalid fixture revision.")
    return value


def validate_remote(value):
    if not re.fullmatch(r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", value):
        raise ProbeFailure("Invalid fixture repository.")
    return value


def normalize_deploy_key(value):
    # Windows CLI stdin can turn an LF key into CRLF secret text.
    value = value.replace("\r", "").strip() + "\n"
    if (not value.startswith("-----BEGIN OPENSSH PRIVATE KEY-----\n")
            or not value.endswith("\n-----END OPENSSH PRIVATE KEY-----\n")):
        raise ProbeFailure("Missing test credential.")
    return value


def pack_app(app, destination):
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(app.rglob("*")):
            if path.is_symlink():
                raise ProbeFailure("Unsupported app member.")
            if path.is_file():
                archive.write(path, "Payload/Probe.app/" + path.relative_to(app).as_posix())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ProbeFailure("Invalid archive.")


def execute(work):
    global PHASE
    PHASE = "fixture-configuration"
    remote = validate_remote(os.environ.pop("FIXTURE_REPOSITORY", ""))
    revision = validate_revision(os.environ.pop("FIXTURE_REVISION", ""))
    key_text = normalize_deploy_key(os.environ.pop("FIXTURE_DEPLOY_KEY", ""))
    key = work / "deploy-key"
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(key_text)
    del key_text
    env = os.environ.copy()
    known_hosts = Path(__file__).resolve().with_name("github_known_hosts")
    env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(part) for part in [
        "ssh", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known_hosts),
    ])
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    fixture = work / "fixture"
    result = work / "result"
    fixture.mkdir()
    result.mkdir()
    log = result / "private.log"

    def command(args, cwd=fixture, **kwargs):
        return run_private(args, cwd=cwd, log=log, env=env, **kwargs)

    PHASE = "fixture-repository-init"
    command(["git", "init", "-b", "main"])
    command(["git", "remote", "add", "origin", remote])
    PHASE = "fixture-fetch"
    command(["git", "fetch", "--depth=1", "origin", revision])
    PHASE = "fixture-checkout"
    command(["git", "checkout", "--detach", revision])
    PHASE = "fixture-validation"
    for filename in ["main.m", "sample.metal", "marker.txt"]:
        path = fixture / filename
        if not path.is_file() or path.is_symlink():
            raise ProbeFailure("Invalid private fixture.")
    marker = (fixture / "marker.txt").read_bytes().strip()
    if not re.fullmatch(rb"private-canary-[0-9a-f]{32}", marker):
        raise ProbeFailure("Invalid private fixture.")
    status = "failed"
    try:
        PHASE = "toolchain"
        command(["xcodebuild", "-version"])
        command(["xcodebuild", "-downloadComponent", "MetalToolchain"], timeout=420)
        sdk_file = result / "sdk.txt"
        run_private(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"],
                    cwd=fixture, log=sdk_file, env=env)
        sdk = sdk_file.read_text().strip()
        if not Path(sdk).is_dir():
            raise ProbeFailure("Missing iOS SDK.")
        app = work / "Probe.app"
        app.mkdir()
        PHASE = "ios-compilation"
        command(["xcrun", "--sdk", "iphoneos", "clang", "-target", "arm64-apple-ios26.0",
                 "-isysroot", sdk, "-fobjc-arc", "-framework", "UIKit", "-framework", "Foundation",
                 "main.m", "-o", str(app / "Probe")])
        header = (app / "Probe").read_bytes()[:8]
        if struct.unpack("<II", header) != (0xfeedfacf, 0x0100000c):
            raise ProbeFailure("Unexpected executable format.")
        PHASE = "metal-compilation"
        command(["xcrun", "--sdk", "iphoneos", "metal", "-std=metal3.0", "-c", "sample.metal",
                 "-o", str(work / "sample.air")])
        command(["xcrun", "--sdk", "iphoneos", "metallib", str(work / "sample.air"),
                 "-o", str(app / "sample.metallib")])
        if (app / "sample.metallib").read_bytes()[:4] != b"MTLB":
            raise ProbeFailure("Unexpected Metal library format.")
        PHASE = "ipa-packaging"
        info = {"CFBundleIdentifier": "org.example.PrivateInputProbe", "CFBundleExecutable": "Probe",
                "CFBundleName": "Probe", "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
                "CFBundleShortVersionString": "0.1", "MinimumOSVersion": "26.0",
                "UIDeviceFamily": [1, 2], "LSRequiresIPhoneOS": True}
        (app / "Info.plist").write_bytes(plistlib.dumps(info))
        (app / "fixture.txt").write_bytes(marker + b"\n")
        pack_app(app, result / "Probe.ipa")

        # A compiler error must carry the canary only to a private diagnostic file.
        PHASE = "negative-compiler-control"
        (fixture / "negative.c").write_bytes(b"#error " + marker + b"\n")
        negative_log = result / "negative-compiler.log"
        run_private(["xcrun", "clang", "-fsyntax-only", "negative.c"], cwd=fixture,
                    log=negative_log, env=env, expected=1)
        if marker not in negative_log.read_bytes():
            raise ProbeFailure("Negative control was not observed.")
        status = "passed"
    except Exception:
        # Full diagnostic text stays in the private result repository.
        status = "failed"

    ipa = result / "Probe.ipa"
    report = {"status": status, "last_build_phase": PHASE, "fixture_revision": revision,
              "marker_sha256": hashlib.sha256(marker).hexdigest(),
              "source_sha256": hashlib.sha256((fixture / "main.m").read_bytes()).hexdigest(),
              "ipa_sha256": hashlib.sha256(ipa.read_bytes()).hexdigest() if ipa.exists() else None,
              "ipa_bytes": ipa.stat().st_size if ipa.exists() else 0,
              "negative_diagnostic_contained": status == "passed",
              "platform": "iOS ARM64", "signed": False, "device_tested": False,
              "run_id": os.environ.get("GITHUB_RUN_ID"),
              "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
              "public_source_revision": os.environ.get("GITHUB_SHA")}
    (result / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    branch = "result-" + run_id + "-" + attempt
    if not re.fullmatch(r"result-(?:[0-9]+|local)-[0-9]+", branch):
        raise ProbeFailure("Invalid result identity.")
    upload_log = work / "upload.log"

    def publish(args):
        run_private(args, cwd=result, log=upload_log, env=env)

    PHASE = "private-result-upload"
    publish(["git", "init", "-b", branch])
    publish(["git", "config", "user.name", "Build probe"])
    publish(["git", "config", "user.email", "build-probe@users.noreply.github.com"])
    publish(["git", "add", "--", "."])
    publish(["git", "commit", "-m", "Record private fixture result"])
    publish(["git", "remote", "add", "origin", remote])
    publish(["git", "push", "origin", "HEAD:refs/heads/" + branch])
    if status != "passed":
        raise ProbeFailure("Private build failed.")


def main():
    try:
        with tempfile.TemporaryDirectory(prefix="private-input-probe-") as directory:
            execute(Path(directory))
    except ProbeFailure as error:
        print(f"Probe failed at {PHASE}: {error}")
        return 1
    except Exception:
        print(f"Probe failed at {PHASE}. Inspect the private result store.")
        return 1
    print("Probe passed. The unsigned sample and diagnostics are in the private result store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
