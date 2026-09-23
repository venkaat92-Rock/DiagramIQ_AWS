"""Who is calling, and may they.

Verification is delegated to Cognito itself. GetUser takes an access token and
either returns the account or refuses it — signature, expiry, revocation and
whether the account is still enabled are all checked by the service that issued
the token. That needs no crypto library inside this zip, which matters: the
deployment vendors pure-Python wheels only, and every RS256 library worth using
pulls a compiled one.

The cost is one API call per request, so answers are cached per container for
CACHE_TTL seconds. A disabled account therefore keeps working for at most that
long. That is the trade, stated here rather than discovered later.

REQUIRE_AUTH gates enforcement. It ships false so that deploying the login
layer cannot lock anyone out before a single person has proved they can sign
in; it is turned on deliberately, as its own change.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

CACHE_TTL = 300
_cache: dict[str, tuple[float, "Identity"]] = {}


def enforcing() -> bool:
    return os.environ.get("REQUIRE_AUTH", "false").strip().lower() in ("1", "true", "yes")


class NotAuthorised(Exception):
    """Raised with a message meant for the person, not the log."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass
class Identity:
    sub: str
    username: str
    email: str

    @property
    def label(self) -> str:
        return self.email or self.username

    @staticmethod
    def anonymous() -> "Identity":
        return Identity(sub="anonymous", username="anonymous", email="")


def _bearer(event) -> str:
    headers = event.get("headers") or {}
    raw = ""
    for key in ("authorization", "Authorization"):
        if headers.get(key):
            raw = headers[key]
            break
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _lookup(token: str) -> "Identity":
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("cognito-idp")
    try:
        user = client.get_user(AccessToken=token)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code in ("NotAuthorizedException", "UserNotFoundException",
                    "PasswordResetRequiredException", "UserNotConfirmedException"):
            raise NotAuthorised("Your session has expired. Sign in again.")
        raise NotAuthorised(f"Could not verify your session ({code or 'unknown error'}).", 502)

    attrs = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}
    return Identity(
        sub=attrs.get("sub", ""),
        username=user.get("Username", ""),
        email=attrs.get("email", ""),
    )


def identify(event) -> Identity:
    """The caller, or anonymous when enforcement is off and no token was sent.

    Raises NotAuthorised when a token is required and missing or bad — and also
    when a bad token is presented while enforcement is off, because a token
    that does not verify is a fact worth reporting either way.
    """
    token = _bearer(event)
    if not token:
        if enforcing():
            raise NotAuthorised("Sign in to use DiagramIQ.")
        return Identity.anonymous()

    now = time.time()
    hit = _cache.get(token)
    if hit and hit[0] > now:
        return hit[1]

    identity = _lookup(token)
    _cache[token] = (now + CACHE_TTL, identity)
    if len(_cache) > 200:                      # a container serves few people
        for stale in [k for k, (exp, _) in _cache.items() if exp <= now]:
            _cache.pop(stale, None)
    return identity
