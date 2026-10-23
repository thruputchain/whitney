"""Per-run metrics: fixed daemon metrics ports, Vector run labels, and the
optional Prometheus remote-write export.

Every host runs Vector (whitney-vector.service, set up by
infra/host-setup.sh.tftpl). It scrapes the /metrics endpoints of the daemons
on its host and the host's own metrics every second, and writes them to
/var/lib/whitney/metrics/<run>.ndjson. The run labels come from
/etc/whitney/vector/run.env, which Whitney writes before each run. When
/etc/whitney/vector/remote.env exists, Vector also sends the metrics to a
Prometheus remote-write endpoint. The credentials live only in that file
(0600, root) on the hosts: never in argv, tofu, or the results.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections.abc import Mapping
from urllib.parse import urlsplit

# Fixed metrics ports. infra/main.tf gives Vector the same role -> port map.
METRICS_PORTS = {"event": 9101, "db": 9102, "indexer": 9103}
EXEC_METRICS_PORT = 9104  # multi-host: each exec on its own host
SINGLE_EXEC_METRICS_BASE = 9110  # single host: execK on 9110 + K
# Multi-host with one hog-exec per L3 (exec_per_l3): the first hog-exec of
# exec host h keeps 9104; hog-exec i >= 1 is exec<h>-<i> on 9200 + 64h + i,
# away from the ports above. Vector does not scrape these.
EXTRA_EXEC_METRICS_BASE = 9200
EXTRA_EXEC_METRICS_STRIDE = 64

VECTOR_VERSION = "0.43.0"  # pinned with its sha256 in infra/main.tf
SCRAPE_INTERVAL_SECS = 1
VECTOR_ETC = "/etc/whitney/vector"
RUN_ENV = f"{VECTOR_ETC}/run.env"
REMOTE_ENV = f"{VECTOR_ETC}/remote.env"
METRICS_DIR = "/var/lib/whitney/metrics"
VECTOR_UNIT = "whitney-vector.service"

URL_ENV = "WHITNEY_REMOTE_WRITE_URL"
USER_ENV = "WHITNEY_REMOTE_WRITE_USERNAME"
PASS_ENV = "WHITNEY_REMOTE_WRITE_PASSWORD"
TOKEN_ENV = "WHITNEY_REMOTE_WRITE_BEARER_TOKEN"

# Values go into a systemd EnvironmentFile and, through Vector's ${VAR}
# interpolation, into TOML strings. Printable ASCII without quotes,
# backslashes, and "$" needs no escaping in either place.
_SAFE_VALUE = re.compile(r"[\x21-\x7e]+")
_UNSAFE_CHARS = set("\"'\\$")
_RUN_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def metrics_port(role: str, single_host: bool) -> int:
    """The --metrics-port of a role (event, db, indexer, exec<K>, or
    exec<h>-<i> for the extra hog-execs of a per-L3 exec host)."""
    if role in METRICS_PORTS:
        return METRICS_PORTS[role]
    m = re.fullmatch(r"exec(\d+)(?:-(\d+))?", role)
    if not m:
        raise ValueError(f"no metrics port for role {role!r}")
    if m.group(2) is not None:
        h, i = int(m.group(1)), int(m.group(2))
        if single_host or not 1 <= i < EXTRA_EXEC_METRICS_STRIDE:
            raise ValueError(f"no metrics port for role {role!r}")
        return EXTRA_EXEC_METRICS_BASE + EXTRA_EXEC_METRICS_STRIDE * h + i
    return SINGLE_EXEC_METRICS_BASE + int(m.group(1)) if single_host else EXEC_METRICS_PORT


def _safe(value: str) -> bool:
    return bool(_SAFE_VALUE.fullmatch(value)) and not (_UNSAFE_CHARS & set(value))


def run_env(run_name: str, scenario: str, n: int, rep: int) -> str:
    """/etc/whitney/vector/run.env: the run labels Vector puts on every metric."""
    for what, value in (("run name", run_name), ("scenario name", scenario)):
        if not _RUN_LABEL.fullmatch(value):
            raise ValueError(f"{what} must match {_RUN_LABEL.pattern}: {value!r}")
    return (f"WHITNEY_RUN={run_name}\nWHITNEY_SCENARIO={scenario}\n"
            f"WHITNEY_N={int(n)}\nWHITNEY_REP={int(rep)}\n")


@dataclass(frozen=True)
class RemoteWrite:
    """A Prometheus remote-write endpoint and one auth form. The credentials
    stay out of repr()."""

    url: str
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    bearer_token: str | None = field(default=None, repr=False)

    @property
    def host(self) -> str:
        """The URL host (and port), the only part of the endpoint in results."""
        return urlsplit(self.url).netloc

    def env_file(self) -> str:
        """remote.env for the hosts. Holds the credentials."""
        lines = [f"{URL_ENV}={self.url}"]
        if self.bearer_token:
            lines.append(f"{TOKEN_ENV}={self.bearer_token}")
        else:
            lines += [f"{USER_ENV}={self.username}", f"{PASS_ENV}={self.password}"]
        return "\n".join(lines) + "\n"


def redact_url(url: str) -> str:
    """https://<host>/REDACTED: what command.txt keeps of the endpoint."""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme or 'https'}://{host}/REDACTED"


def remote_write_from_env(url: str, env: Mapping[str, str]) -> RemoteWrite:
    """Check --metrics-remote-write and the credential variables. Exactly one
    auth form: WHITNEY_REMOTE_WRITE_USERNAME + _PASSWORD, or
    WHITNEY_REMOTE_WRITE_BEARER_TOKEN. Raises ValueError, which never holds
    a credential."""
    flag = "--metrics-remote-write"
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"{flag} must be an https URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{flag} must not hold credentials; set {USER_ENV} and "
                         f"{PASS_ENV}, or {TOKEN_ENV}")
    if not _safe(url):
        raise ValueError(f"{flag} has characters that are not allowed "
                         "(whitespace, quotes, backslash, $)")
    user, password, token = (env.get(k) or None for k in (USER_ENV, PASS_ENV, TOKEN_ENV))
    if token and (user or password):
        raise ValueError(f"{flag}: set {USER_ENV} and {PASS_ENV}, or {TOKEN_ENV}, not both")
    if bool(user) != bool(password):
        raise ValueError(f"{flag}: {USER_ENV} and {PASS_ENV} go together")
    if not token and not user:
        raise ValueError(f"{flag} needs credentials: set {USER_ENV} and {PASS_ENV}, "
                         f"or {TOKEN_ENV}")
    for name, value in ((USER_ENV, user), (PASS_ENV, password), (TOKEN_ENV, token)):
        if value is not None and not _safe(value):
            raise ValueError(f"{name} has characters that are not allowed "
                             "(whitespace, quotes, backslash, $)")
    return RemoteWrite(url=url, username=user, password=password, bearer_token=token)
