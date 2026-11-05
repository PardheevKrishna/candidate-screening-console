"""Uploaded tuning profiles must never change where the LLM connects."""
import json

from screening.config import AppConfig


def test_profile_cannot_redirect_llm_endpoint_or_backend():
    hostile = AppConfig().to_dict()
    hostile["llm"].update(base_url="https://attacker.example.net/v1", provider="tachyon",
                          backend_factory="os:system", api_key_env="HOME", cache_dir="C:/Windows",
                          request_extras={"x": 1}, fallback_models=["evil/model"])
    hostile["weights"]["hackerrank"] = 55.0          # tuning values still apply
    hostile["llm"]["blend_weight"] = 0.3
    cfg = AppConfig.from_json(json.dumps(hostile))
    default = AppConfig().llm
    assert (cfg.llm.base_url, cfg.llm.provider, cfg.llm.backend_factory, cfg.llm.api_key_env) == \
        (default.base_url, default.provider, default.backend_factory, default.api_key_env)
    assert cfg.llm.cache_dir == default.cache_dir and cfg.llm.request_extras == default.request_extras
    assert cfg.llm.fallback_models == default.fallback_models
    assert cfg.weights.hackerrank == 55.0 and cfg.llm.blend_weight == 0.3


def test_default_profile_round_trips():
    assert AppConfig.from_json(AppConfig().to_json()) == AppConfig()
