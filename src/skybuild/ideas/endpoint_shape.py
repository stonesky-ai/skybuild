"""The OpenAI-shaped half of the endpoint scripts (BRODSON-LLAMACPP-SCRIPTS).

llm.brodson.net has been llama.cpp since 2026-10-03: every `/api/*` path
answers 404. The vault and the lanes moved to the OpenAI API; the endpoint
scripts did not, and were dead against that endpoint. This module is what they
share so that none of them spells a route, a body field or a credential of its
own:

  * the SHAPE comes from the setting the vault reads (SKYKEEP_MODEL_PROVIDER)
    through `provider_speaks_openai`; an unknown name refuses, it is never read
    as "not OpenAI" and sent to Ollama's routes;
  * every route comes from the product's own resolvers (`resolve_chat_path`,
    `resolve_models_path`, `resolve_embeddings_url`), never a literal path;
  * the generation cap is sent under the field `resolve_max_tokens_field`
    names, the thinking switch under the one `resolve_think_field` names, and
    NOTHING ELSE: no `num_ctx`, no `options`, no `keep_alive` — the OpenAI
    contract has none of them, and the server fixes its window;
  * model names are the vault's own (SKYKEEP_EMBED_MODEL, SKYKEEP_THINK_MODEL).
    SKYKEEP_GATE_EMBED_MODEL / SKYKEEP_GATE_THINK_MODEL are not read on this
    shape: a lane refuses them now;
  * the credential is the vault's (`resolve_model_auth`: SKYKEEP_MODEL_AUTH and
    SKYKEEP_MODEL_SECRET / SKYKEEP_MODEL_SECRET_FILE), presented as a header
    only and never printed. When that resolves to no credential and the lane
    configuration's SKYKEEP_GATE_MODEL_SECRET_FILE is set, that file is the
    bearer, as it was before.

An Ollama-shaped endpoint never reaches this module's request builders: each
script keeps its own Ollama code path exactly as it was.
"""
from __future__ import annotations

import math
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: `src/` beside this script, so a checkout runs without an install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

#: The setting the vault reads its runtime shape from.
PROVIDER_ENV_VAR = "SKYKEEP_MODEL_PROVIDER"
EMBED_MODEL_ENV_VAR = "SKYKEEP_EMBED_MODEL"
THINK_MODEL_ENV_VAR = "SKYKEEP_THINK_MODEL"
GATE_SECRET_FILE_ENV_VAR = "SKYKEEP_GATE_MODEL_SECRET_FILE"


def provider_name(environ: Mapping[str, str] | None = None) -> str:
    """`SKYKEEP_MODEL_PROVIDER` as written; unset is the Ollama shape these
    scripts always spoke."""
    from skykeep.models.provider import OLLAMA_PROVIDER_NAME

    environ = os.environ if environ is None else environ
    return (environ.get(PROVIDER_ENV_VAR) or "").strip() or OLLAMA_PROVIDER_NAME


def speaks_openai(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the configured runtime speaks the OpenAI shape. Raises
    `ModelProviderError` for a name the vault would refuse."""
    from skykeep.models.provider import provider_speaks_openai

    return provider_speaks_openai(provider_name(environ))


def model_names(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(embed model, think model) the vault itself is configured with."""
    environ = os.environ if environ is None else environ
    return ((environ.get(EMBED_MODEL_ENV_VAR) or "").strip(),
            (environ.get(THINK_MODEL_ENV_VAR) or "").strip())


def chat_body(model: str, prompt: str, max_tokens: int, system: str | None = None,
              environ: Mapping[str, str] | None = None) -> dict:
    """A chat completion and nothing else: the model, the messages, the cap
    under its configured field name, and the thinking switch only when the
    vault names a spelling for it."""
    from skykeep.models.openaicompat import (
        USER_ROLE,
        resolve_max_tokens_field,
        resolve_think_field,
        think_switch_fields,
    )

    messages = [{"role": USER_ROLE, "content": prompt}]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    return {
        "model": model,
        "messages": messages,
        "stream": False,
        resolve_max_tokens_field(environ=environ): max_tokens,
        **think_switch_fields(resolve_think_field(environ=environ)),
    }


def chat_path(environ: Mapping[str, str] | None = None) -> str:
    """The configured chat path under a base URL (`resolve_chat_path`)."""
    from skykeep.models.openaicompat import resolve_chat_path

    return resolve_chat_path(environ=environ)


@dataclass(frozen=True)
class Reply:
    """One answer, or the reason there was none. Never raises."""

    code: int            # the HTTP status, 0 when none arrived
    seconds: float
    body: object         # the parsed JSON, None when there was none
    why: str             # "" on a 2xx with a JSON body


def _bearer_from_gate_file(environ: Mapping[str, str]):
    from skykeep.models.remoteauth import BearerAuth

    raw = (environ.get(GATE_SECRET_FILE_ENV_VAR) or "").strip()
    if not raw:
        return None
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.is_file():
        raise ValueError(f"{GATE_SECRET_FILE_ENV_VAR} ({path}) is not a file")
    token = path.read_text().strip()
    return BearerAuth(token) if token else None


class OpenAIEndpoint:
    """One OpenAI-compatible endpoint: its routes, its bodies, its credential.

    `client` is injectable so a test can hand it an `httpx.MockTransport`.
    """

    def __init__(self, base_url: str, environ: Mapping[str, str] | None = None,
                 client=None, user_agent: str = "skykeep-endpoint-script") -> None:
        import httpx

        from skykeep.models.remoteauth import NoAuth, client_options, resolve_model_auth

        self.environ = os.environ if environ is None else environ
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        auth = resolve_model_auth(self.base_url, self.environ)
        if isinstance(auth, NoAuth):
            auth = _bearer_from_gate_file(self.environ) or auth
        self.auth = auth
        self.client = client if client is not None else httpx.Client(**client_options(auth))

    # ------------------------------------------------------------ routes
    @property
    def chat_url(self) -> str:
        from skykeep.models.openaicompat import resolve_chat_path

        return self.base_url + resolve_chat_path(environ=self.environ)

    @property
    def models_url(self) -> str:
        from skykeep.models.openaicompat import resolve_models_path

        return self.base_url + resolve_models_path(environ=self.environ)

    @property
    def embeddings_url(self) -> str:
        from skykeep.models.openaicompat import resolve_embeddings_url

        return resolve_embeddings_url(self.base_url, environ=self.environ)

    # ------------------------------------------------------------ bodies
    def chat_body(self, model: str, prompt: str, max_tokens: int,
                  system: str | None = None) -> dict:
        return chat_body(model, prompt, max_tokens, system, self.environ)

    @staticmethod
    def embed_body(model: str, texts: list[str]) -> dict:
        return {"model": model, "input": texts}

    # ------------------------------------------------------------ calls
    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, **self.auth.auth_headers(self.client)}

    def send(self, method: str, url: str, payload: dict | None, timeout: float) -> Reply:
        import httpx

        started = time.monotonic()
        try:
            response = self.client.request(method, url, json=payload,
                                           headers=self._headers(), timeout=timeout)
        except Exception as exc:                # noqa: BLE001 - transport, TLS, timeout
            return Reply(0, time.monotonic() - started, None, f"{exc.__class__.__name__}")
        seconds = time.monotonic() - started
        if response.status_code >= 300:
            return Reply(response.status_code, seconds, None,
                         f"HTTP {response.status_code} {response.text[:120]}")
        try:
            body = response.json()
        except (httpx.DecodingError, ValueError):
            return Reply(response.status_code, seconds, None, "the answer was not JSON")
        return Reply(response.status_code, seconds, body, "")

    def post(self, url: str, payload: dict, timeout: float) -> Reply:
        return self.send("POST", url, payload, timeout)

    def get(self, url: str, timeout: float) -> Reply:
        return self.send("GET", url, None, timeout)


# ---------------------------------------------------------------- readers
@dataclass(frozen=True)
class Completion:
    content: str
    reasoning: str
    finish_reason: str | None
    completion_tokens: int
    #: llama.cpp's own generation speed (`timings.predicted_per_second`), when
    #: the server reports one; the OpenAI contract has no such field.
    tokens_per_second: float | None


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def read_completion(body: object) -> Completion | None:
    """`choices[0].message` of a chat answer, or None when the answer is not
    one. `content` and `reasoning_content` are kept apart: the vault stores the
    first and never the second."""
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content, reasoning = message.get("content"), message.get("reasoning_content")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    tokens = usage.get("completion_tokens")
    timings = body.get("timings") if isinstance(body.get("timings"), dict) else {}
    reason = choices[0].get("finish_reason")
    rate = _positive_number(timings.get("predicted_per_second"))
    return Completion(
        content if isinstance(content, str) else "",
        reasoning if isinstance(reasoning, str) else "",
        reason if isinstance(reason, str) else None,
        tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0 else 0,
        rate,
    )


def read_embeddings(body: object) -> list | None:
    """The vectors of a `/v1/embeddings` answer in input order, or None when
    the answer is not that shape. The NESTED shape of the unversioned
    `/embeddings` route has no `data` list and is None here on purpose."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    rows = body["data"]
    if not all(isinstance(r, dict) and "embedding" in r for r in rows):
        return None
    ordered = sorted(rows, key=lambda r: r.get("index") if isinstance(r.get("index"), int) else 0)
    return [r["embedding"] for r in ordered]


def listed_models(body: object) -> dict[str, str]:
    """Model id to its `status.value` ("" when the listing gives none), from a
    models listing; each alias is listed under the same status."""
    rows = body.get("data") if isinstance(body, dict) else body
    out: dict[str, str] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        status = row.get("status")
        value = status.get("value") if isinstance(status, dict) else ""
        value = value if isinstance(value, str) else ""
        out[row["id"]] = value
        for alias in row.get("aliases") or []:
            if isinstance(alias, str):
                out.setdefault(alias, value)
    return out


#: The `status.value` a router-mode llama.cpp gives a model it holds in memory.
LOADED_STATUS = "loaded"
