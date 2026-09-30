"""Repository-scoped credentials used only by external-plugin Git fetches."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.models import ExternalGitRepository

CREDENTIALS_ENV = "PIG_EXTERNAL_PLUGIN_CREDENTIALS"
_FETCH_CREDENTIAL_ENV = "_PIG_EXTERNAL_PLUGIN_FETCH_CREDENTIAL"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _credentials(raw: str) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        result: dict[str, dict[str, str]] = {}
        for url, credential in data.items():
            ExternalGitRepository(type="git", url=url)
            parts = urlsplit(url)
            # Git decodes credential contexts. Reject ambiguous paths rather than
            # allowing percent-encoded separators or dot segments to widen scope.
            if (
                parts.port == 0
                or unquote(parts.path) != parts.path
                or any(part in {"", ".", ".."} for part in parts.path.lstrip("/").split("/"))
                or parts.path.startswith("//")
            ):
                raise ValueError("expected a canonical repository URL")
            if not isinstance(credential, dict) or set(credential) != {"username", "password"}:
                raise ValueError("expected username and password")
            if any(
                not isinstance(value, str) or not value or any(ord(character) < 32 for character in value)
                for value in credential.values()
            ):
                raise ValueError("invalid credential value")
            if ":" in credential["username"]:
                raise ValueError("invalid credential username")
            result[url] = credential
        return result
    except (TypeError, ValueError):
        # Validation exceptions (including Pydantic's) can contain secret inputs.
        raise InstructionHubError(
            f"{CREDENTIALS_ENV} must be a JSON object mapping canonical HTTPS repository URLs "
            "to nonempty username/password strings, without duplicate keys or control characters"
        ) from None


def git_environment(url: str | None = None) -> dict[str, str]:
    """Remove the full secret map and expose only credentials for this exact fetch."""

    env = dict(os.environ)
    raw = env.pop(CREDENTIALS_ENV, "")
    env.pop(_FETCH_CREDENTIAL_ENV, None)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    if url is None or not raw:
        return env
    credential = _credentials(raw).get(url)
    if credential is None:
        return env

    parts = urlsplit(url)
    env[_FETCH_CREDENTIAL_ENV] = json.dumps(
        {"protocol": parts.scheme, "host": parts.netloc, "path": parts.path.lstrip("/"), **credential}
    )
    # Even explicitly enabled Git tracing must not record HTTP credentials.
    for name in tuple(env):
        if name.startswith("GIT_TRACE") or name == "GIT_CURL_VERBOSE":
            env.pop(name)
    env["GIT_TRACE_REDACT"] = "1"
    env.update(GIT_TRACE2="0", GIT_TRACE2_EVENT="0", GIT_TRACE2_PERF="0")
    return env


def git_config(environment: dict[str, str]) -> list[str]:
    """Use non-secret command options so inherited Git parameters cannot override scope."""

    if _FETCH_CREDENTIAL_ENV not in environment:
        return []
    credential = json.loads(environment[_FETCH_CREDENTIAL_ENV])
    url = f"{credential['protocol']}://{credential['host']}/{credential['path']}"
    helper = f"!{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
    settings = [
        ("credential.helper", ""),
        ("credential.helper", helper),
        ("credential.useHttpPath", "true"),
        (f"credential.{url}.useHttpPath", "true"),
        ("http.followRedirects", "false"),
        (f"http.{url}.followRedirects", "false"),
    ]
    return [argument for key, value in settings for argument in ("-c", f"{key}={value}")]


def _credential_helper() -> None:
    # Git also calls helpers with store/erase; neither may persist the CI secret.
    if sys.argv[1:] != ["get"]:
        return
    try:
        credential = json.loads(os.environ[_FETCH_CREDENTIAL_ENV])
        context = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if line.strip())
        if all(context.get(key) == credential[key] for key in ("protocol", "host", "path")):
            print(f"username={credential['username']}\npassword={credential['password']}\n")
            return
    except (KeyError, ValueError):
        pass
    # An insteadOf rewrite must not fall through to another helper or askpass.
    print("quit=true\n")


if __name__ == "__main__":
    _credential_helper()
