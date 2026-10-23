"""AWS credentials for tofu, and a check before any spend.

process mode (the default): tofu runs with a private AWS config file whose
one profile gets its credentials from `aws configure export-credentials`.
The AWS SDK in tofu runs that command again when the credentials expire, so
short-lived credentials (`aws login`, SSO) last as long as the login session.

env mode: tofu gets the caller's environment as is (static keys).

whitney's own aws CLI calls (s3 cp, presign) always use the caller's env.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from whitney.proc import Exec, redact, tail

MODES = ("process", "env")
PROFILE = "whitney"
ENV_BIN = "/usr/bin/env"
# Credentials in the caller's env. tofu must not see them in process mode:
# the SDK reads env credentials before the profile, and they never refresh.
STATIC_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
               "AWS_CREDENTIAL_EXPIRATION")
# Variables that select the config files and profile. tofu gets whitney's
# (an empty credentials file, so a [whitney] profile in ~/.aws/credentials
# cannot win). The credential_process command puts the caller's back (or
# removes them), so it never reads whitney's files again.
SELECT_VARS = ("AWS_CONFIG_FILE", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
               "AWS_SHARED_CREDENTIALS_FILE")
PATH_VARS = ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE")
EXPIRY_MARGIN_MINUTES = 30
REQUIRED_KEYS = ("AccessKeyId", "SecretAccessKey")
STS_CALLER = ["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"]


class AwsCredsError(RuntimeError):
    pass


@dataclass(frozen=True)
class AwsCreds:
    mode: str
    tofu_env: dict[str, str | None] = field(default_factory=dict)  # {} = caller's env
    caller: str = ""  # caller ARN without account id or session name

    def record(self) -> dict:
        """For aws.json in each run dir: no account id, no secrets."""
        return {"mode": self.mode, "caller": self.caller}


def find_aws() -> str | None:
    return shutil.which("aws")


def credential_process_command(aws: str, environ: Mapping[str, str]) -> str:
    """The credential_process line: the caller's normal aws CLI chain.

    It runs inside tofu's env (whitney's config file and profile), so it sets
    each SELECT_VARS variable back to the caller's value, or removes it. The
    line works both through `sh -c` (Go SDK) and split like shlex (aws CLI).
    """
    unset: list[str] = []
    assign: list[str] = []
    for var in SELECT_VARS:
        value = environ.get(var)
        if not value:
            unset += ["-u", var]
            continue
        if var in PATH_VARS:
            value = os.path.abspath(value)  # tofu runs in another dir
        assign.append(f"{var}={value}")
    cmd = shlex.join([ENV_BIN, *unset, *assign, aws,
                      "configure", "export-credentials", "--format", "process"])
    if "\n" in cmd or "\r" in cmd:
        raise AwsCredsError(f"{', '.join(SELECT_VARS)} must not contain a newline")
    return cmd


def write_aws_config(cache: Path, region: str, command: str) -> Path:
    """Write <cache>/aws/config-<hash> (mode 0600) and return its absolute path.

    The name comes from the content, so runs with other settings never share a
    file. The file stays: the manual destroy command needs it.
    """
    text = f"[profile {PROFILE}]\nregion = {region}\ncredential_process = {command}\n"
    path = aws_dir(cache) / f"config-{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    _write_private(path, text)
    return path


def aws_dir(cache: Path) -> Path:
    d = cache / "aws"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d.resolve()


def _write_private(path: Path, text: str) -> None:
    """Write path (mode 0600) through a temp file and os.replace."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def tofu_env(config: Path, credentials: Path) -> dict[str, str | None]:
    env: dict[str, str | None] = {v: None for v in STATIC_VARS}
    env.update(AWS_CONFIG_FILE=str(config), AWS_SHARED_CREDENTIALS_FILE=str(credentials),
               AWS_PROFILE=PROFILE, AWS_DEFAULT_PROFILE=None, AWS_SDK_LOAD_CONFIG="1")
    return env


def caller_name(arn: str) -> str:
    """arn:aws:sts::<account>:assumed-role/<role>/<session> -> assumed-role/<role>.

    The account id and the session name (often an email) are dropped.
    """
    parts = arn.strip().split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        return "unknown"
    resource = parts[5]
    if resource.startswith("assumed-role/"):
        return "/".join(resource.split("/")[:2])
    return resource


def _parse_time(raw: str, what: str) -> datetime:
    try:
        t = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise AwsCredsError(f"{what} is not an ISO 8601 time") from None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def check_env_expiry(environ: Mapping[str, str], max_lifetime_minutes: int,
                     now: datetime | None = None) -> None:
    raw = environ.get("AWS_CREDENTIAL_EXPIRATION")
    if not raw:
        return
    expires = _parse_time(raw, "AWS_CREDENTIAL_EXPIRATION")
    now = now or datetime.now(timezone.utc)
    left = max(0, math.floor((expires - now).total_seconds() / 60))
    if left < max_lifetime_minutes + EXPIRY_MARGIN_MINUTES:
        raise AwsCredsError(
            f"AWS credentials expire in {left} min, before this run's max lifetime "
            f"({max_lifetime_minutes} min); use --aws-creds process (default) or "
            f"longer-lived credentials")


def _check_process(ex: Exec, command: str, env: dict[str, str | None]) -> None:
    """Run the credential_process line once, the way the Go SDK runs it."""
    cp = ex(["/bin/sh", "-c", command], env=env, check=False, secret=True)
    if cp.returncode != 0:
        raise AwsCredsError(
            f"AWS credentials: the credential_process command failed (exit {cp.returncode}): "
            f"{redact(tail(cp.stderr or '', 5))}\nSign in again (for example `aws login`), "
            f"or use --aws-creds env with credentials in your environment")
    try:
        doc = json.loads(cp.stdout)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise AwsCredsError("AWS credentials: `aws configure export-credentials --format "
                            "process` did not print JSON")
    missing = [k for k in REQUIRED_KEYS if not isinstance(doc.get(k), str) or not doc.get(k)]
    if doc.get("Version") != 1:
        missing.insert(0, "Version 1")
    if missing:
        raise AwsCredsError(f"AWS credentials: the credential_process output lacks "
                            f"{', '.join(missing)}")
    if doc.get("Expiration"):
        _parse_time(str(doc["Expiration"]), "the credential_process Expiration")


def _caller(ex: Exec, env: dict[str, str | None]) -> str:
    cp = ex(STS_CALLER, env=env or None, check=False, secret=True)
    if cp.returncode != 0:
        raise AwsCredsError(f"AWS credentials do not work: aws sts get-caller-identity exited "
                            f"{cp.returncode}: {redact(tail(cp.stderr or '', 5))}")
    return caller_name(cp.stdout)


def prepare_aws(mode: str, region: str, cache: Path, ex: Exec, *, max_lifetime_minutes: int,
                environ: Mapping[str, str] | None = None, now: datetime | None = None,
                aws: str | None = None) -> AwsCreds:
    """Check the credentials and return the env changes for every tofu call.

    Runs before any build, upload, or apply. Never logs credential values.
    """
    environ = os.environ if environ is None else environ
    if mode == "env":
        check_env_expiry(environ, max_lifetime_minutes, now)
        return AwsCreds("env", {}, _caller(ex, {}))
    if mode != "process":
        raise ValueError(f"aws_creds must be one of {', '.join(MODES)}: {mode!r}")
    aws = aws or find_aws()
    if not aws:
        raise AwsCredsError("aws CLI not found on PATH: whitney needs it for credentials "
                            "and uploads")
    own = aws_dir(cache)
    for var in PATH_VARS:
        value = environ.get(var)
        if value and Path(os.path.abspath(value)).resolve().is_relative_to(own):
            raise AwsCredsError(f"{var} points into whitney's own AWS files ({own}); set it "
                                f"to your normal file, or unset it")
    command = credential_process_command(os.path.abspath(aws), environ)
    empty = own / "credentials-empty"
    _write_private(empty, "")
    env = tofu_env(write_aws_config(cache, region, command), empty)
    _check_process(ex, command, env)
    return AwsCreds("process", env, _caller(ex, env))


def env_prefix(env: Mapping[str, str | None]) -> str:
    """`env -u X ... K=V ...` for a manual command that needs env (or "")."""
    if not env:
        return ""
    unset = [a for k, v in env.items() if v is None for a in ("-u", k)]
    assign = [f"{k}={v}" for k, v in env.items() if v is not None]
    return shlex.join(["env", *unset, *assign]) + " "
